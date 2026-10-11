# SongForge

A synchronized, prompt-fed internet radio: everyone listening hears the same song at
the same position, and anyone can submit a prompt that generates a song which then
plays on the air. Backend is FastAPI + Postgres + Redis + MinIO (S3-compatible audio
storage) behind nginx; the frontend is Next.js.

## Architecture

![SongForge architecture diagram](docs/architecture.png)
*(placeholder — nginx → `web` (FastAPI, N replicas) → Postgres/PgBouncer, Redis,
MinIO; `worker`(s) drive generation + the radio clock; Next.js frontend)*

## Web-instance count vs p99 latency

`realistic-session.js` load test, varying only the number of `web` replicas, run
until combined p99 latency (`/create` + `/events`) first crossed 1 second:

| Metric | 3 web instances | 8 web instances |
|---|---|---|
| p99 crosses 1s at | 13:02:45 | 12:46:45 |
| Combined request rate | ~336 req/s | ~500 req/s |
| Connected listeners | ~4.05k | ~5.58k |
| `/create` p99 at crossing | 0.97s → 2.21s | 0.64s → 2.11s |
| `/events` p99 at crossing | 0.89s → 1.74s | 0.95s → 2.10s |
| Web CPU | **all 3 pinned at ~1.01 core** | busiest **0.80 → 0.92 core** (not pinned) |
| Postgres CPU | ~0.73 cores | ~1.6 → 1.7 cores |

More web replicas raise the p99-bounded ceiling; Postgres stays far under its
14-core budget in both runs, confirming web-tier CPU as the bottleneck, not the
database. Full methodology and caveats: [`docs/depth-in-sse.md`](docs/depth-in-sse.md).

## Run locally

```bash
cp .env.example .env
docker compose up --build
```

- Web API: <http://localhost:8000>
- Frontend: <http://localhost:3000>
- Prometheus metrics: <http://localhost:8000/metrics>

Scale replicas: `docker compose up --build --scale web=3 --scale worker=2`
