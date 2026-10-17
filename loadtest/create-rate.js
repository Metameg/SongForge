// k6 load test -- issue #18, AC4 (write-path half).
//
// Sustains a target `POST /create` rate to prove the write path scales
// independently of the SSE listener count (the capacity thesis's other half --
// see connection-ceiling.js for the read/listener side). Run this against the
// LOCAL stack, where `MUSICGPT_BASE_URL` already points at the fault-injectable
// simulator (docker-compose.yml's default) -- the simulator IS the seam that
// makes a sustained create-rate possible without hitting the real MusicGPT API.
//
// IMPORTANT: the default daily-quota caps (ANON_DAILY_SONGS_PER_COOKIE=2,
// ANON_DAILY_SONGS_PER_IP=6, etc., see docker-compose.yml x-backend-env) will
// 429 almost immediately at any real load-test rate. Either set
// ENFORCE_RATE_LIMITS=false or raise the caps generously in the target
// stack's env before running this script at a meaningful TARGET_RATE_PER_SEC.
// This is a load-test *profile* concern, not a code change.
//
// Run:
//   BASE_URL=http://localhost:8000 TARGET_RATE_PER_SEC=20 \
//     k6 run loadtest/create-rate.js
//
// See loadtest/README.md for env vars and how to read results against the
// Grafana capacity-thesis dashboard (songforge_jobs_created_total /
// songforge_jobs_dispatched_total throughput panel).

import http from "k6/http";
import { check } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const TARGET_RATE_PER_SEC = __ENV.TARGET_RATE_PER_SEC
  ? parseInt(__ENV.TARGET_RATE_PER_SEC, 10)
  : 10;
const DURATION = __ENV.DURATION || "3m";

// Pre-allocate generously above the target rate so k6 itself is never the
// bottleneck -- VUs here are just request-issuing workers, unrelated to the
// SSE connection count in connection-ceiling.js.
const PRE_ALLOCATED_VUS = __ENV.PRE_ALLOCATED_VUS
  ? parseInt(__ENV.PRE_ALLOCATED_VUS, 10)
  : Math.max(20, TARGET_RATE_PER_SEC * 2);
const MAX_VUS = __ENV.MAX_VUS
  ? parseInt(__ENV.MAX_VUS, 10)
  : PRE_ALLOCATED_VUS * 3;

export const options = {
  scenarios: {
    create_rate: {
      executor: "constant-arrival-rate",
      rate: TARGET_RATE_PER_SEC,
      timeUnit: "1s",
      duration: DURATION,
      preAllocatedVUs: PRE_ALLOCATED_VUS,
      maxVUs: MAX_VUS,
    },
  },
  thresholds: {
    // The write path sustaining the target create-rate means: low latency
    // AND a high success rate at that rate, not just "some requests succeeded".
    http_req_duration: ["p(95)<1000"],
    http_req_failed: ["rate<0.02"],
  },
};

const PROMPTS = [
  "a lo-fi beat for studying",
  "upbeat synthwave for a road trip",
  "acoustic guitar campfire song",
  "energetic drum and bass",
  "melancholy piano instrumental",
];

export default function () {
  const prompt = PROMPTS[Math.floor(Math.random() * PROMPTS.length)];
  const res = http.post(
    `${BASE_URL}/create`,
    JSON.stringify({ prompt }),
    {
      headers: { "Content-Type": "application/json" },
      tags: { name: "create" },
    },
  );

  check(res, {
    // Accept any 2xx as "accepted for generation" -- the exact response
    // shape is the app's contract, not this load test's concern. A 429
    // (quota) is treated as a failure here on purpose: it means the target
    // stack wasn't configured per the README note above, and the run isn't
    // measuring what it claims to measure.
    "create accepted (2xx)": (r) => r.status >= 200 && r.status < 300,
  });
}
