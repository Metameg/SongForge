# SongForge

A synchronized, prompt-fed global internet radio — greenfield rebuild. Everyone hears the
same song at the same position; anyone can submit a prompt that generates a song which then
plays on the air. See [`plans/songforge_spec.md`](plans/songforge_spec.md) for the full
system-redesign specification.

This repository currently contains the **scaffold & local infrastructure** (issue #7): the
foundation every other ticket builds on. Domain logic (radio coordination, the generation
pipeline, rate limiting, SSE playback) lands in subsequent tickets at the seams marked in
the code.

## Architecture

| Service     | Tech               | Role                                                        |
| ----------- | ------------------ | ---------------------------------------------------------- |
| `lb`        | nginx               | Load balancer fronting N `web` replicas (issue #19)         |
| `web`       | FastAPI             | HTTP + SSE edge; `/metrics`, health probes — stateless, scales to N |
| `pgbouncer` | PgBouncer (transaction pooling) | Fans in `web`'s Postgres connections (issue #19) |
| `worker`    | Python              | Row-claimable loops (`SKIP LOCKED`) + single-leader radio; scales to N=2-3 for HA |
| `frontend`  | Next.js             | Listener/creator UI                                        |
| `postgres`  | Postgres 16         | Source of truth                                            |
| `redis`     | Redis 7             | Derived/ephemeral: semaphore, counters, pub/sub, caches    |
| `minio`     | MinIO (S3 API)      | Audio object storage (Cloudflare R2 in prod)               |

The backend `web` and `worker` share one Python package, `songforge` (`backend/src`), so
config, logging, metrics, storage, and DB access are identical across both. `web`'s
connections to Postgres are pooled through `pgbouncer`; `worker`'s advisory-lock and
`LISTEN/NOTIFY` connections bypass it and talk to Postgres directly (see "Multi-instance &
deploy" below).

## One-command local environment

```bash
cp .env.example .env
docker compose up --build
```

Compose brings up Postgres, Redis, and MinIO, then runs a one-shot **`migrate`** service
that migrates the schema and seeds the static library **before** `web` and `worker` start
(they wait on it via `service_completed_successfully`). Once healthy:

- Web API + health: <http://localhost:8000/health/ready>
- Prometheus metrics: <http://localhost:8000/metrics>
- Frontend: <http://localhost:3000>
- MinIO console: <http://localhost:9001>

## Configuration

All runtime configuration is read from a **single** env-driven module,
[`backend/src/songforge/config.py`](backend/src/songforge/config.py) — no other module
touches the environment directly. Every rate limit is defined there and tunable via env;
enforcement is gated by the one `ENFORCE_RATE_LIMITS` boolean (default `true`, so dev
matches prod and "goes unlimited" only deliberately). See `.env.example` for the full set,
including the multi-instance/deploy vars below (`WORKER_DATABASE_URL`,
`DB_PGBOUNCER_TRANSACTION_MODE`, `PORT`, `PGBOUNCER_*`, `LB_HOST_PORT`).

## Multi-instance & deploy

The stack scales horizontally (issue #19, PRD #6 AC#1-#4): a load balancer in front of N
stateless `web` replicas, PgBouncer fanning in the web tier's Postgres connections while
`worker`'s advisory-lock + `LISTEN/NOTIFY` connections bypass it, and N=2-3 `worker`
replicas for HA (row-claim parallelism + single-leader radio coordination, already built —
this is a topology/verification slice, not new distributed-systems logic).

Bring the full topology up locally:

```bash
docker compose up --build --scale web=2 --scale worker=2
```

- Everything is reachable at `http://localhost:${LB_HOST_PORT:-8000}` — `web` no longer
  publishes its own host port, so traffic goes through `lb` (nginx,
  [`loadbalancer/nginx.conf`](loadbalancer/nginx.conf)), which is also what makes running
  multiple `web` replicas possible at all (they can't all bind one static host port).
- `docker compose up` alone (no `--scale`) still works and runs one `web` + one `worker` —
  the `deploy.replicas` values in `docker-compose.yml` are documentation for Swarm; plain
  Compose ignores them, so `--scale` is the actual mechanism locally.
- Verify exactly one worker holds the radio leader lock:
  `docker compose exec postgres psql -U songforge -d songforge -c "select pid, granted from pg_locks where locktype='advisory';"`
  — exactly one row should show `granted = t`.

Prod target is Railway — see [`docs/deploy.md`](docs/deploy.md) for the full topology
diagram, per-service env var matrix, Cloudflare R2 setup, scaling, the release/migrate
step, and failover behavior with N workers. That doc also states plainly what an agent
cannot do (the live `railway up`) and gives the operator the exact commands to run.

## Observability

Instrumentation is always on, in every environment: `/metrics` serves Prometheus text,
logs are structured JSON, and every request carries a correlation ID (inbound
`X-Correlation-ID` honoured, otherwise minted) so a single song's journey is traceable.

## Backend development (without Docker)

```bash
cd backend
uv venv --python 3.12
uv pip install -e ".[dev]"
scripts/test.sh          # or: .venv/bin/python -m pytest
```

Tests drive the system at its edges (HTTP for the app, in-memory SQLite for seed logic, an
in-process S3 fake for storage) and need no live datastore. Tests marked `integration` are
skipped when their dependency is unavailable.
