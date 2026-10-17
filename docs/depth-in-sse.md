# Depth-in-SSE — change + load-test results

## What this branch changes

New SSE connections no longer query Postgres for the queue. The radio
coordinator now computes the waiting-queue **depth** on each song change and
publishes it inside the `song-change` SSE event (via the pointer cache record).

- A fresh listener gets the global queue depth from the connect-time SSE frame —
  **no `/queue` round-trip, no Postgres hit**.
- Only clients that actually hold a queue position still call `/queue` (for their
  per-user position), and only when a song-change event tells them to.

Pointer reads at connect time are served from the **local in-process cache →
Redis → Postgres** chain, so connect-time datastore load is effectively zero.

## Load-test results: web-instance count vs the p99 ceiling

`realistic-session.js` (gentle ~5 conn/s ramp, 0.1 req/s/user mixed `/create`,
`/quota`, `/queue` actions) run twice against the same 14-core dev box, varying
only the number of `web` replicas, to isolate web-tier CPU as the scaling lever.
Figures captured from Prometheus / cAdvisor at the exact moment combined p99
(`/create` and `/events`) first crossed the video's 1s threshold.

### 8 web instances — p99 crosses 1s at 12:46:45 (local)

| Metric | Value |
|---|---|
| Combined request rate (`/create`+`/quota`+`/queue`) | **~500 req/s** |
| Connected listeners | **~5.58k** |
| `/create` p99 | crosses at 12:46:22: **0.64s → 2.11s** |
| `/events` p99 | crosses at 12:46:37: **0.95s → 2.10s** |
| Busiest web instance CPU | **0.80 → 0.92 / 1.0 core** — climbing, never fully pins |
| Web CPU summed (8 instances) | **~6.0 → 6.8 cores** (avg ~0.85/instance — headroom elsewhere) |
| Postgres CPU | **~1.6 → 1.7 cores** (climbs to ~2.0 shortly after) |

No single instance is pinned at its ceiling here — load is spread thin enough
across 8 replicas that the crossing comes from aggregate queueing delay as the
busiest loop *approaches* 1.0 core, not from any one loop being fully saturated.

### 3 web instances — p99 crosses 1s at 13:02:45 (local)

| Metric | Value |
|---|---|
| Combined request rate (`/create`+`/quota`+`/queue`) | **~336 req/s** |
| Connected listeners | **~4.05k** |
| `/create` p99 | crosses at 13:02:42: **0.97s → 2.21s** |
| `/events` p99 | crosses at 13:02:42: **0.89s → 1.74s** |
| Web CPU, every instance | **web-1 1.014 · web-2 1.016 · web-3 1.004** — **all three pinned at the ceiling** |
| Web CPU summed (3 instances) | **~3.03 cores** (avg ~1.01/instance — zero slack) |
| Postgres CPU | **~0.73 cores** |

A much cleaner signature: with only 3 replicas, the SAME kind of load pins
**every** instance at its ~1.0-core ceiling simultaneously, and the crossing
happens at roughly **2/3 the request rate and listener count** of the 8-instance
run. Postgres is barely touched (0.73 vs 14 cores available) in both cases —
confirms the bottleneck is web-tier CPU, not the database, and that adding web
replicas directly raises the achievable p99-bounded throughput.

## Notes / caveats

- Each web replica is one uvicorn process = one event loop ≈ 1 core; scale with
  `--scale web=N`. These two runs are the direct before/after of that lever.
- Generation is gated by a Redis semaphore (capacity **1**, ~7.08s/song), so the
  QUEUED backlog grows during these runs by design — a growing backlog also makes
  the web tier's periodic `jobs_in_state` scrape query (run by every replica, every
  5s) progressively more expensive over a long run, a secondary, time-correlated
  contributor on top of the request-driven CPU load above.
- CPU figures are cAdvisor per-container rates (20s window), not host loadavg.

## Reproduce

```
# realistic steady-state session (needs loadtest/cookies.json from mint-cookies.py)
docker run --rm --network host -v "$PWD/loadtest":/ls \
  -e BASE_URL=http://localhost:8000 -e COOKIE_FILE=/ls/cookies.json \
  grafana/k6 run /ls/realistic-session.js

# vary web replica count:
docker compose --profile monitoring up -d --scale web=3   # or --scale web=8
```
