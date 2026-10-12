# SongForge

A synchronized, prompt-fed internet radio: everyone listening hears the same song at
the same position, and anyone can submit a prompt that generates a song which then
plays on the air. 

## Architecture
Backend is FastAPI + Postgres + Redis + MinIO (S3-compatible audio
storage) behind nginx; the frontend is Next.js.
<img width="1176" height="736" alt="SongForge Architecture drawio" src="https://github.com/user-attachments/assets/26657efa-ea42-47aa-a98c-650869095004" />


## Web-instance count vs p99 latency

`realistic-session.js` load test, varying only the number of `web` replicas, run
until combined p99 latency (`/create`) first crossed 1 second:

| Metric | 3 web instances | 8 web instances |

|---|---|---|
| Connected listeners | ~4.05k | ~5.58k |
| Combined request rate | ~336 req/s | ~500 req/s |
| `/create` p99 at crossing (POF) | 0.97s → 2.21s | 0.64s → 2.11s |
| Web CPU | **all 3 pinned at ~1.01 core** | busiest **0.80 → 0.92 core** (not pinned) |
| Postgres CPU | ~0.73 cores | ~1.6 → 1.7 cores |

More web replicas raise the p99-bounded ceiling; Postgres stays far under its
14-core budget in both runs, confirming web-tier CPU as the bottleneck, not the
database.

## Run locally

```bash
cp .env.example .env
docker compose up --build
```

- Web API: <http://localhost:8000>
- Frontend: <http://localhost:3000>
- Prometheus metrics: <http://localhost:8000/metrics>

Scale replicas: `docker compose up --build --scale web=3 --scale worker=2`
