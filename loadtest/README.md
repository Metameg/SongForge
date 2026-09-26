# SongForge load tests (issue #18)

k6 scripts that prove the capacity thesis (PRD #6, decision #8) **by shape,
not scale**: row-claimable work (job dispatch, ingest) scales horizontally
via Postgres `SKIP LOCKED`; only the time-triggered radio advance needs
leader election. The observable proof is that `sse_connected_listeners`
grows with the number of connected clients while `radio_advances_total` and
datastore-op counters stay flat, because SSE fan-out happens in-process off
ONE Redis pub/sub subscription per app instance.

Neither script asserts that shape by itself -- they generate the load side
(rising connection count, sustained write-rate); the *proof* is reading the
resulting metrics on the Grafana `capacity-thesis` dashboard
(`monitoring/grafana/dashboards/capacity-thesis.json`) while a script runs.

## Prerequisites

- [k6](https://k6.io/docs/get-started/installation/) installed locally (no
  custom build / extensions required -- see the "k6 + SSE" decision below).
- The local stack up, including the monitoring profile so the metrics these
  scripts exercise are actually scraped and visible:

  ```sh
  docker compose --profile monitoring up -d
  ```

- For `create-rate.js` specifically: raise or disable the anonymous daily
  quota caps on the target stack, or every request past the first couple
  will 429 (see that script's header comment and `docker-compose.yml`'s
  `x-backend-env` block, e.g. `ENFORCE_RATE_LIMITS=false` or a generously
  raised `ANON_DAILY_SONGS_PER_COOKIE` / `ANON_DAILY_SONGS_PER_IP`).

## `connection-ceiling.js` (AC3)

Ramps concurrent SSE `/events` connections to find the per-instance
connection ceiling (event-loop / file-descriptor / thread-pool exhaustion).

```sh
BASE_URL=http://localhost:8000 k6 run loadtest/connection-ceiling.js

# quick smoke check (1 VU, 1s) before a real run:
BASE_URL=http://localhost:8000 k6 run --vus 1 --duration 1s loadtest/connection-ceiling.js
```

Env vars:

| Var | Default | Meaning |
|---|---|---|
| `BASE_URL` | `http://localhost:8000` | Target app instance |
| `MAX_VUS` | `1000` | Peak concurrent held-open connections |
| `HOLD_OPEN_SECONDS` | `600` | How long each VU holds its connection before k6 tears it down |

**What it proves:** compare k6's own `vus` metric against the Grafana
`capacity-thesis` dashboard's `songforge_sse_connected_listeners` panel
during the run. If they track 1:1 up to some VU count and then diverge (the
gauge plateaus or drops while `vus` keeps climbing), that divergence point
is the per-instance connection ceiling.

**k6 + SSE decision:** k6's open-source `http` module has no built-in SSE
frame parser, and this repo doesn't vendor an `xk6-sse` custom binary. This
script uses plain `http.get('/events', { timeout: ... })` per VU -- a
connection held open until its timeout fires counts as one open SSE
connection for the purpose of finding the ceiling; it does not parse
`event:`/`data:` frames. The ground truth for "how many connections the
server thinks are open" is the server-side `songforge_sse_connected_listeners`
gauge, not anything k6 itself reports. If the team wants true SSE
frame-level assertions inside k6, that requires adopting `xk6-sse` (or an
equivalent extension) and a custom k6 binary build -- noted here as a
deliberate decision, not a silent gap.

## `create-rate.js` (AC4)

Sustains a target `POST /create` rate against the write path, backed by the
fault-injectable MusicGPT simulator seam (`MUSICGPT_BASE_URL=http://simulator:8080`,
already the local compose default) so no real MusicGPT API traffic is ever sent.

```sh
BASE_URL=http://localhost:8000 TARGET_RATE_PER_SEC=20 DURATION=3m \
  k6 run loadtest/create-rate.js
```

Env vars:

| Var | Default | Meaning |
|---|---|---|
| `BASE_URL` | `http://localhost:8000` | Target app instance |
| `TARGET_RATE_PER_SEC` | `10` | Constant arrival rate of `POST /create` |
| `DURATION` | `3m` | How long to sustain the rate |
| `PRE_ALLOCATED_VUS` | `max(20, rate*2)` | k6 VU pool sized above the target rate so k6 itself isn't the bottleneck |
| `MAX_VUS` | `PRE_ALLOCATED_VUS*3` | Upper bound k6 may grow the VU pool to if requests run long |

**What it proves:** the write path (create -> dispatch -> simulator ->
webhook -> ingest) sustains the target create-rate with a low failure rate
and bounded p95 latency (thresholds in the script), while the
`capacity-thesis` dashboard's create-rate throughput panel
(`rate(songforge_jobs_created_total[1m])` vs.
`rate(songforge_jobs_dispatched_total[1m])`) shows the two tracking each
other -- i.e. the queue isn't unboundedly backing up at the target rate.

## Reading results together

Run `connection-ceiling.js` and `create-rate.js` in the same monitoring
session (sequentially, or concurrently on separate terminals against the
same stack) with the Grafana `capacity-thesis` dashboard open. The thesis is
confirmed when: `sse_connected_listeners` rises through the ramp while
`rate(songforge_radio_advances_total[1m])` and the datastore-op counters
stay flat, AND the create-rate throughput panel shows the write path
keeping pace independent of how many listeners are connected.
