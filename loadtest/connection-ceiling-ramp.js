// THROWAWAY load-test instrumentation (spike): find the concurrent-SSE-listener ceiling.
//
// Ramps held-open /events connections in stages well past the expected ~500/instance
// ceiling. Each VU holds one SSE connection open (plain http.get to timeout -- same
// "connection held open counts as one listener" approach as loadtest/connection-ceiling.js;
// it does NOT parse SSE frames). The ground truth for "how many the server thinks are
// open" is the per-instance songforge_sse_connected_listeners gauge -- watch it with
// watch-listeners.sh, NOT k6's own numbers.
//
// Run:  docker run --rm -i --network host -e BASE_URL=http://localhost:8000 \
//         grafana/k6 run - < connection-ceiling-ramp.js
//
// Ceiling = where the summed gauge plateaus / k6 http_req_failed climbs while VUs keep rising.
import http from "k6/http";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const HOLD = __ENV.HOLD_SECONDS || "120s";

export const options = {
  discardResponseBodies: true,
  scenarios: {
    ceiling: {
      executor: "ramping-vus",
      startVUs: 0,
      gracefulRampDown: "10s",
      stages: [
        { duration: "1m", target: 500 },
        { duration: "1m", target: 1000 },
        { duration: "1m", target: 1500 },
        { duration: "1m", target: 2000 },
        { duration: "1m", target: 2500 },
        { duration: "2m", target: 2500 }, // hold at peak
      ],
    },
  },
};

export default function () {
  // Hold a connection open until timeout; while blocked here the VU = 1 open connection.
  http.get(`${BASE_URL}/events`, { timeout: HOLD, tags: { name: "events" } });
}
