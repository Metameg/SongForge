// THROWAWAY: ORDINARY steady-state traffic -- listeners trickle in gradually and the
// station runs at a stable population, which is how a real day looks. Contrast with
// `viral-connections.js` (a flash crowd slamming new connections as fast as possible).
//
// Why gradual matters: the connect-storm stalls we diagnosed (webhook/queue-fill
// freezing for tens of seconds, elevated latency) are driven by the NEW-connection
// RATE, not the listener COUNT. A stable population of the same size produced zero
// stalls. So this script holds the connection rate low and spends most of its time at
// steady state, so the measured latency reflects real capacity instead of a herd artifact.
//
// Same two-scenario shape + custom metrics + pass/fail rules as viral-connections.js,
// so results are directly comparable:
//   listeners -- ramping-vus: each VU holds one /events SSE connection (a held socket
//                == one concurrent listener). Ramps GENTLY to TARGET_USERS at
//                ~ARRIVALS_PER_SEC/sec, then HOLDS for STEADY_MINUTES.
//   actions   -- ramping-arrival-rate at users * ACTIONS_PER_USER req/s: create /
//                quota / queue from the bounded sf_uid cookie pool.
//
// Pass/fail (video's three rules, on ACTION requests): p95 < 500ms, p99 < 1000ms,
// error rate < 1%; PLUS new users must CONNECT (connect errors < 1%).
//
// Tunables (env), with ordinary-traffic defaults:
//   TARGET_USERS=2000        steady concurrent listeners (keep < the ~7141 nginx cap)
//   ARRIVALS_PER_SEC=5       NEW connections/sec during the ramp (viral is ~22)
//   STEADY_MINUTES=6         how long to hold the steady population (the real measurement)
//   ACTIONS_PER_USER=0.1     per-user request rate (create/quota/queue blend)
// Crank TARGET_USERS / ARRIVALS_PER_SEC up to approach viral behaviour on one dial.
import http from "k6/http";
import { check, sleep } from "k6";
import { SharedArray } from "k6/data";
import { Trend, Rate } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const COOKIE_FILE = __ENV.COOKIE_FILE || "./cookies.json";
const APU = Number(__ENV.ACTIONS_PER_USER || "0.1");
const TARGET_USERS = Number(__ENV.TARGET_USERS || "2000");
const ARRIVALS_PER_SEC = Number(__ENV.ARRIVALS_PER_SEC || "5");
const STEADY_MINUTES = Number(__ENV.STEADY_MINUTES || "6");

// Gentle ramp: reach TARGET_USERS at ~ARRIVALS_PER_SEC new connections/sec.
const RAMP_SECONDS = Math.max(1, Math.ceil(TARGET_USERS / ARRIVALS_PER_SEC));
const STEADY_SECONDS = Math.round(STEADY_MINUTES * 60);
// Each listener holds its SSE connection for the whole run (ramp + steady) so VU count
// == concurrent listeners with no reconnect churn; HOLD must exceed the total runtime.
const HOLD = __ENV.HOLD || `${RAMP_SECONDS + STEADY_SECONDS + 120}s`;
const PEAK_ACTION_RATE = Math.max(1, Math.round(TARGET_USERS * APU));

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

export const options = {
  discardResponseBodies: true,
  scenarios: {
    listeners: {
      executor: "ramping-vus",
      exec: "listen",
      startVUs: 0,
      gracefulRampDown: "0s",
      stages: [
        { duration: `${RAMP_SECONDS}s`, target: TARGET_USERS },
        { duration: `${STEADY_SECONDS}s`, target: TARGET_USERS }, // hold steady
      ],
    },
    actions: {
      executor: "ramping-arrival-rate",
      exec: "act",
      startRate: 1,
      timeUnit: "1s",
      preAllocatedVUs: Math.min(500, PEAK_ACTION_RATE * 2 + 50),
      maxVUs: Math.min(2000, PEAK_ACTION_RATE * 5 + 200),
      stages: [
        { duration: `${RAMP_SECONDS}s`, target: PEAK_ACTION_RATE },
        { duration: `${STEADY_SECONDS}s`, target: PEAK_ACTION_RATE },
      ],
    },
  },
  thresholds: {
    action_latency: ["p(95)<500", "p(99)<1000"],
    action_errors: ["rate<0.01"],
    connect_errors: ["rate<0.01"],
  },
};

// One held SSE connection == one concurrent listener.
export function listen() {
  const res = http.get(`${BASE_URL}/events`, {
    timeout: HOLD,
    tags: { name: "events" },
  });
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
