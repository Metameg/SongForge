// k6 load test -- issue #18, AC3.
//
// Ramps concurrent SSE `/events` connections to find the per-instance
// connection ceiling. This is the independent-variable half of the capacity
// thesis (PRD #6, decision #8): row-claimable work scales via `SKIP LOCKED`,
// but listener fan-out is in-process off ONE Redis pub/sub subscription per
// app instance, so the ceiling here is an event-loop/fd-limit/thread-pool
// property of a SINGLE `web` instance, not a datastore property.
//
// Decision (recorded, not made silently): k6's open-source `http` module has
// no built-in SSE frame parser, and this repo doesn't vendor an `xk6-sse`
// custom binary. Rather than requiring a custom k6 build for a tool that only
// needs to HOLD connections open (not parse `event:`/`data:` frames), this
// script uses a plain `http.get('/events', { timeout: ... })` per VU -- a
// connection held open until its timeout fires counts as "one open SSE
// connection" for the ceiling measurement. The GROUND TRUTH for "how many
// connections the server thinks are open" is NOT anything k6 reports -- it's
// the server-side `songforge_sse_connected_listeners` gauge, scraped by
// Prometheus (see monitoring/prometheus/prometheus.yml) and charted in
// monitoring/grafana/dashboards/capacity-thesis.json. Compare k6's `vus`
// metric against that gauge during a run: if they track 1:1 up to some VU
// count and then diverge, that divergence point IS the per-instance ceiling.
// If the team wants true SSE frame-level assertions inside k6 itself, that
// requires adopting `xk6-sse` (or an equivalent extension) -- flagged here,
// not silently worked around.
//
// Run:
//   BASE_URL=http://localhost:8000 k6 run loadtest/connection-ceiling.js
//
// See loadtest/README.md for env vars, stage tuning, and how to read the
// results against the Grafana capacity-thesis dashboard.

import http from "k6/http";
import { check } from "k6";
import { Rate } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";

// How long each VU holds its connection open before k6 tears it down. This
// should comfortably exceed the total test duration (sum of all stages) so
// every VU is still "connected" (in k6's eyes) when the ramp completes --
// the timeout firing mid-test is EXPECTED at teardown, not a failure signal.
// It is intentionally NOT the test's pass/fail signal; `songforge_sse_connected_listeners`
// on the Grafana dashboard is.
const HOLD_OPEN_SECONDS = __ENV.HOLD_OPEN_SECONDS
  ? parseInt(__ENV.HOLD_OPEN_SECONDS, 10)
  : 600;

const connectFailureRate = new Rate("sse_connect_failures");

// Ramping-VUs executor: each VU is one held-open `/events` connection.
// Tune MAX_VUS / stage durations per-environment via env vars so the same
// script serves both a quick local smoke run and a bigger staging run.
const MAX_VUS = __ENV.MAX_VUS ? parseInt(__ENV.MAX_VUS, 10) : 1000;

export const options = {
  scenarios: {
    connection_ceiling: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "30s", target: Math.floor(MAX_VUS * 0.1) },
        { duration: "1m", target: Math.floor(MAX_VUS * 0.5) },
        { duration: "2m", target: MAX_VUS },
        // Hold at the peak so the server's gauge has time to settle and the
        // ceiling (if any) shows up as a plateau rather than a transient spike.
        { duration: "2m", target: MAX_VUS },
        { duration: "30s", target: 0 },
      ],
      gracefulRampDown: "10s",
    },
  },
  thresholds: {
    // A rising failure rate as VUs ramp up is itself evidence of the
    // ceiling -- connections that can't even be established (vs. ones that
    // are established and then held, which is the success path here).
    sse_connect_failures: ["rate<0.05"],
    http_req_failed: ["rate<0.05"],
  },
};

export default function () {
  const res = http.get(`${BASE_URL}/events`, {
    timeout: `${HOLD_OPEN_SECONDS}s`,
    tags: { name: "sse_events" },
  });

  // A timed-out held-open request comes back as k6 status 0 with an ECONNRESET-
  // style error -- that's the SUCCESS path for this script (the connection
  // stayed open until we tore it down), not a failure. A failure is a
  // connection that never opened (refused, reset immediately, non-2xx).
  const openedSuccessfully =
    res.status === 200 || (res.status === 0 && res.error_code !== 1211);
  connectFailureRate.add(!openedSuccessfully);

  check(res, {
    "connection opened or held until timeout": () => openedSuccessfully,
  });
}
