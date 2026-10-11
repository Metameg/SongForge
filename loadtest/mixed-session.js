// THROWAWAY: realistic SongForge "user" load, modeled to match the video's method.
//
// A real listener is TWO concurrent load sources a single k6 VU can't do at once
// (an SSE GET blocks the VU for its whole hold), so we run two lockstep-ramped
// scenarios keyed to the SAME "users" number:
//
//   listeners  -- ramping-vus: each VU holds one /events SSE connection open for
//                 the whole run. VU count == concurrent users (the radio's real
//                 per-user cost is a held socket, not a request).
//   actions    -- ramping-arrival-rate at users * ACTIONS_PER_USER req/s: the
//                 mixed thing connected users DO -- create / quota / queue, using
//                 the bounded sf_uid cookie pool. His 0.1 req/s/user model.
//
// Pass/fail = the video's three rules, applied to ACTION requests:
//   p95 < 500ms, p99 < 1000ms, error rate < 1%.
// PLUS a radio-specific rule: new users must be able to CONNECT (SSE connect
// errors < 1%) -- "can't get on the app" is the real user-facing failure.
//
// The built-in http_req_* is polluted by the /events holds (every stream is cut
// at the timeout = an expected "failure"), so action latency/errors use CUSTOM
// metrics. Per-LEVEL numbers come from the server-side Prometheus histogram
// aligned to this ramp timeline (k6's summary is whole-run only).
import http from "k6/http";
import { check, sleep } from "k6";
import { SharedArray } from "k6/data";
import { Trend, Rate } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const COOKIE_FILE = __ENV.COOKIE_FILE || "./cookies.json";
const HOLD = __ENV.HOLD || "600s"; // > whole run: listeners never churn
const APU = Number(__ENV.ACTIONS_PER_USER || "0.1"); // video's per-user req/s

const cookies = new SharedArray("cookies", () => JSON.parse(open(COOKIE_FILE)));

const actionLatency = new Trend("action_latency", true);
const actionErrors = new Rate("action_errors");
const connectErrors = new Rate("connect_errors");

const PROMPTS = [
  "a lo-fi beat for studying",
  "upbeat synthwave for a road trip",
  "acoustic guitar campfire song",
  "energetic drum and bass",
  "melancholy piano instrumental",
];

// users per stage -> actions/s = users * APU, ramped in lockstep.
const USER_STAGES = [
  { duration: "45s", users: 1000 },
  { duration: "45s", users: 2000 },
  { duration: "45s", users: 3000 },
  { duration: "45s", users: 4000 },
  { duration: "45s", users: 5000 },
  { duration: "45s", users: 6000 },
  { duration: "45s", users: 7000 },
  { duration: "60s", users: 8000 },
  { duration: "60s", users: 8000 }, // hold at peak
];

const vuStages = USER_STAGES.map((s) => ({ duration: s.duration, target: s.users }));
const rateStages = USER_STAGES.map((s) => ({
  duration: s.duration,
  target: Math.round(s.users * APU),
}));

export const options = {
  discardResponseBodies: true,
  scenarios: {
    listeners: {
      executor: "ramping-vus",
      exec: "listen",
      startVUs: 0,
      gracefulRampDown: "0s",
      stages: vuStages,
    },
    actions: {
      executor: "ramping-arrival-rate",
      exec: "act",
      startRate: Math.round(1000 * APU),
      timeUnit: "1s",
      preAllocatedVUs: 300,
      maxVUs: 2500,
      stages: rateStages,
    },
  },
  thresholds: {
    action_latency: ["p(95)<500", "p(99)<1000"],
    action_errors: ["rate<0.01"],
    connect_errors: ["rate<0.01"],
  },
};

// One held SSE connection = one concurrent user.
export function listen() {
  const res = http.get(`${BASE_URL}/events`, {
    timeout: HOLD,
    tags: { name: "events" },
  });
  // Timeout = expected hold expiry (stream cut). Any OTHER error = real connect
  // failure (e.g. nginx worker_connections cap, socket refused): the user is
  // locked out of the app.
  const err = res.error || "";
  const isTimeout = err.toLowerCase().includes("timeout") || res.error_code === 1050;
  connectErrors.add(err !== "" && !isTimeout);
  if (err !== "" && !isTimeout) sleep(2); // don't spin-retry a refused connect
}

// What a connected user DOES: mostly create, some quota/queue reads.
export function act() {
  const cookie = cookies[Math.floor(Math.random() * cookies.length)];
  const hdr = { Cookie: `sf_uid=${cookie}` };
  const roll = Math.random();
  let res;
  if (roll < 0.4) {
    const prompt = PROMPTS[Math.floor(Math.random() * PROMPTS.length)];
    res = http.post(`${BASE_URL}/create`, JSON.stringify({ prompt }), {
      headers: { "Content-Type": "application/json", ...hdr },
      tags: { name: "create" },
    });
  } else if (roll < 0.75) {
    res = http.get(`${BASE_URL}/quota`, { headers: hdr, tags: { name: "quota" } });
  } else {
    res = http.get(`${BASE_URL}/queue`, { headers: hdr, tags: { name: "queue" } });
  }
  actionLatency.add(res.timings.duration);
  const ok = res.status >= 200 && res.status < 300;
  actionErrors.add(!ok);
  check(res, { "action 2xx": () => ok });
}
