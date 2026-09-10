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

| Service    | Tech            | Role                                                        |
| ---------- | --------------- | ---------------------------------------------------------- |
| `web`      | FastAPI         | HTTP + (later) SSE edge; `/metrics`, health probes         |
| `worker`   | Python          | Row-claimable loops (`SKIP LOCKED`) + single-leader radio  |
| `frontend` | Next.js         | Listener/creator UI                                        |
| `postgres` | Postgres 16     | Source of truth                                            |
| `redis`    | Redis 7         | Derived/ephemeral: semaphore, counters, pub/sub, caches    |
| `minio`    | MinIO (S3 API)  | Audio object storage (Cloudflare R2 in prod)               |

The backend `web` and `worker` share one Python package, `songforge` (`backend/src`), so
config, logging, metrics, storage, and DB access are identical across both.

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
matches prod and "goes unlimited" only deliberately). See `.env.example` for the full set.

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
