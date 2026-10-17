# SongForge

A synchronized, prompt-fed internet radio: everyone listening hears the same song at
the same position, and anyone can submit a prompt that generates a song which then
plays on the air. 

## Architecture
Backend is FastAPI + Postgres + Redis + MinIO (S3-compatible audio
storage) behind nginx; the frontend is Next.js.
<img width="1176" height="736" alt="SongForge Architecture drawio" src="https://github.com/user-attachments/assets/26657efa-ea42-47aa-a98c-650869095004" />


## Capacity vs replica count

Load-tested on the Railway staging environment, varying the number of `web` and
`frontend` replicas. Each row is the peak reached before the `web` replicas' CPUs
maxed out and request latency crossed the test's thresholds.

| `web` replicas | `frontend` replicas | Peak SSE listeners | Peak request rate |
|:---:|:---:|---:|---:|
| 1 | 1 | ~1.35k | ~145 req/s |
| 2 | 2 | ~2.5k | ~201 req/s |
| 3 | 2 | ~4.6k | ~500 req/s |

Capacity grows with each added `web` replica: the web tier scales horizontally,
while Postgres and Redis stay well below their limits.

## Run locally

```bash
cp .env.example .env
docker compose up --build
```

- Web API: <http://localhost:8000>
- Frontend: <http://localhost:3000>
- Prometheus metrics: <http://localhost:8000/metrics>

Scale replicas: `docker compose up --build --scale web=3 --scale worker=2`
