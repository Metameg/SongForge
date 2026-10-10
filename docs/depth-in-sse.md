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

## Load-test results

All figures measured on a single 14-core dev box (app + load generator +
monitoring co-resident), captured from Prometheus / cAdvisor. Three throwaway k6
runs: create-rate ramp, SSE connection ceiling, connect-rate (thundering-herd).

### Create throughput (`POST /create`)

| Metric | Value |
|---|---|
| **Max create rate @ server p99 < 1s** | **~595 req/s** (p99 ≈ 0.97s at the peak) |
| Requests in run / errors | 99,719 / **0** (0.00% failed) |
| Behaviour past the ceiling | saturates, not faster: achieved falls to ~540/s, server p99 → ~1.8s |
| Client-observed p95 (k6, whole run) | 4.86s — request **queueing** during the 800/s overload hold, not failures |
| App CPU at peak | **~9.3 cores** (web 7.09 · postgres 2.32 · workers 0.63 · pgbouncer 0.29 · nginx 0.17 · redis 0.04) |
| Busiest web event loop at peak | **0.91 / 1.0 core** → web loops are the bottleneck |
| pgbouncer pool at peak | 40 / 300 in use, **0 acquire-timeouts** → DB/pool not the limit |

p99 stays flat (~25ms) to ~180 req/s, then climbs: 0.21s @ 410 · 0.61s @ 484 ·
0.96s @ 567 · 0.97s @ 592. It crosses 1s only once offered load exceeds the
~595/s ceiling and a backlog forms.

### Concurrent listeners (SSE connection ceiling)

| Metric | Value |
|---|---|
| **Max concurrent SSE listeners** | **7,141** |
| Limiting factor | **nginx `worker_connections`** (14 workers × 1024 ÷ 2 ≈ 7,168), not the app |
| Web CPU / memory at ceiling | ~0.9 core · ~1.56 GB (flat) |
| Postgres / Redis at ceiling | ~27 txn/s · ~385 cmd/s — **flat as listeners scale** |

The datastores stay flat because the local+Redis pointer cache decouples listener
count from Postgres. Lifting the nginx cap is the next lever for more listeners.

### Connect rate (thundering-herd)

| Metric | Value |
|---|---|
| **Max new connects/s** | **~570/s** (30s-avg), ~650/s burst |
| Limiting factor | **single-box TCP socket exhaustion** (ephemeral-port / TIME_WAIT), **not the app** |
| App CPU during run | idle — web 3.1/8 cores, busiest loop 0.41/1.0 |
| Cache source split at peak | local **561/s (98.4%)** · redis 9/s (1.6%) · **postgres 0** |

Postgres served **0** connect-time pointer reads — the depth-in-SSE + cache path
holds. The ~570/s wall is the load rig funnelling every connection through one
source IP's ~28k ephemeral ports (rapid connect/close → TIME_WAIT); the app's true
connect ceiling is higher and needs off-box load generation to find.

## Notes / caveats

- Generation is gated by a Redis semaphore (capacity **1**, ~7.08s/song), so the
  QUEUED backlog grows during create runs by design — these tests measure request
  **intake**, not dispatch.
- Single-box artifacts: connect-rate is a TCP/socket limit, not application
  behaviour; CPU "cores" are cAdvisor container rates, not host loadavg.
- Each web replica is one uvicorn process = one event loop ≈ 1 core; scale with
  `--scale web=N`.

## Reproduce

```
# create-rate ramp (needs loadtest/cookies.json from mint-cookies.py)
docker run --rm --network host -v "$PWD/loadtest":/ls \
  -e BASE_URL=http://localhost:8000 -e COOKIE_FILE=/ls/cookies.json \
  grafana/k6 run /ls/create-rate-ramp.js

# connection ceiling / connect rate: see loadtest/connection-ceiling.js, create-rate.js
```
