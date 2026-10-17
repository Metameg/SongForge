// THROWAWAY load-test instrumentation (spike): find the POST /create throughput ceiling.
//
// Ramps the create arrival rate in stages until POST /create latency/errors spike.
// Uses a BOUNDED POOL of pre-signed sf_uid cookies (see mint-cookies.py) so every
// request reuses one of N real identities -- this measures the actual web+Postgres
// write path (pgbouncer pool, inserts) WITHOUT the cookie-less unbounded
// sem:gen:user:* key explosion that would otherwise crash Redis/worker first.
//
// Prereq: ENFORCE_RATE_LIMITS=false on the stack (already set this session), else 429s.
// Create accept-rate is NOT gated by the generation semaphore, so the QUEUED backlog
// growing during this run is EXPECTED -- we're measuring request intake, not dispatch.
//
// Run:  docker run --rm --network host -v <tmpdir>:/ls \
//         -e BASE_URL=http://localhost:8000 -e COOKIE_FILE=/ls/cookies.json \
//         grafana/k6 run /ls/create-rate-ramp.js
//
// Ceiling = the stage where http_req_failed climbs or p95 blows past the threshold.
import http from "k6/http";
import { SharedArray } from "k6/data";
import { check } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
// k6 open() resolves a relative path against THIS script's directory, so the
// default works when cookies.json sits next to the script (e.g. loadtest/).
// Override with -e COOKIE_FILE=/ls/cookies.json for the Docker-mounted layout.
const COOKIE_FILE = __ENV.COOKIE_FILE || "./cookies.json";
const cookies = new SharedArray("cookies", () => JSON.parse(open(COOKIE_FILE)));

const PROMPTS = [
  "a lo-fi beat for studying",
  "upbeat synthwave for a road trip",
  "acoustic guitar campfire song",
  "energetic drum and bass",
  "melancholy piano instrumental",
];

export const options = {
  scenarios: {
    create: {
      executor: "ramping-arrival-rate",
      startRate: 10,
      timeUnit: "1s",
      preAllocatedVUs: 200,
      maxVUs: 2000,
      stages: [
        { duration: "1m", target: 25 },
        { duration: "1m", target: 50 },
        { duration: "1m", target: 100 },
        { duration: "1m", target: 200 },
        { duration: "1m", target: 400 },
        { duration: "1m", target: 800 },
        { duration: "1m", target: 800 }, // hold at peak
      ],
    },
  },
  // Reporting only -- do NOT abort, so we see the full curve past the break point.
  thresholds: {
    http_req_duration: ["p(95)<1000"],
    http_req_failed: ["rate<0.02"],
  },
};

export default function () {
  const cookie = cookies[Math.floor(Math.random() * cookies.length)];
  const prompt = PROMPTS[Math.floor(Math.random() * PROMPTS.length)];
  const res = http.post(`${BASE_URL}/create`, JSON.stringify({ prompt }), {
    headers: { "Content-Type": "application/json", Cookie: `sf_uid=${cookie}` },
    tags: { name: "create" },
  });
  check(res, { "create accepted (2xx)": (r) => r.status >= 200 && r.status < 300 });
}
