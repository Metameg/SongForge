# Railway staging walkthrough: simulated generation and load tests

Prepared from the local `development` branch on 2026-10-12. This is an operator guide;
it has not been tested against a live Railway project. The Dockerfiles and configuration
below are instructions to create files, not files already installed by this guide.

This staging setup matches **prod's actual services** (`docs/deploy.md`'s topology) with
one deliberate exception: it keeps the fault-injectable **simulator** in place of the real
MusicGPT API, so generation can be exercised without a real API key or real spend.
Concretely: object storage is **Cloudflare R2** (not MinIO), and observability is a
lightweight private agent forwarding to **Grafana Cloud** (not self-hosted
Prometheus/Grafana plus database exporters). Everything else — Postgres, Redis, PgBouncer
as its own service, no self-hosted load balancer — is identical to prod.

## 1. Understand what you will deploy

A Dockerfile packages one program and its dependencies into an image. Railway builds
that image and runs a container from it. Each long-running program gets its own Railway
service. You do not need to run Docker Compose inside Railway.

Use a fresh Railway project/environment for this exercise. Start with **one replica of
each service**, all in the same region. Keep serverless/sleeping disabled for this test.

| Local Compose service | Railway setup for this walkthrough |
|---|---|
| `postgres` | Railway PostgreSQL database, named `Postgres` |
| `redis` | Railway Redis database, named `Redis` |
| `pgbouncer` | Docker-image service named `pgbouncer` (Railway does not bundle this with Postgres by default — see step 5) |
| audio storage | **Cloudflare R2** (external S3-API bucket, not a Railway service — see step 6) |
| `migrate` | `songforge-boot` pre-deploy command on `web` |
| `web` | GitHub service running `songforge-web`; private |
| `worker` | GitHub service running `songforge-worker`; private |
| `simulator` | GitHub service running `songforge-simulator`; private, one replica — **kept deliberately**, see the note above |
| `frontend` | GitHub service running the Next.js image; public HTTPS domain |
| `lb` | Railway edge handles public traffic to `frontend`; no separate nginx needed here (matches prod) |
| monitoring | one private `grafana-agent` service forwarding to **Grafana Cloud** (free tier); no self-hosted Prometheus/Grafana/exporters — see step 9 |
| `cadvisor` | Not used; Railway's per-service CPU/memory graphs plus Grafana Cloud's dashboards replace it |

The public app path is browser/test runner → Railway → Next.js → FastAPI. The worker
calls the simulator privately; the simulator calls FastAPI's webhook privately; the
worker stores audio in R2. Browsers fetch audio from R2's public domain/CDN.

This preserves the existing frontend's `/create`, `/events`, `/quota`, `/queue`, and
`/now-playing` proxy handlers. Load tests against the frontend measure this complete
request path, including Next.js and Railway's edge, rather than an isolated FastAPI
instance. `/metrics` is kept on private services.

Railway maps Compose services to individual services and has no `depends_on` ordering
equivalent. Follow the deployment order below. [Railway's Compose guide](https://docs.railway.com/guides/docker-compose)

## 2. Prepare the branch and a backend image containing the audio pool

The existing `backend/Dockerfile` builds from `backend/`, but the MP3s live in
`app/static/audios/`. Compose mounts them; Railway cannot mount that directory from your
laptop. The simulator and boot seed need those files in their image.

From the repository root:

```sh
mkdir -p deploy/railway/staging
```

Create `deploy/railway/staging/Dockerfile.backend` with:

```dockerfile
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY backend/pyproject.toml ./
COPY backend/src ./src
COPY backend/migrations ./migrations
COPY backend/alembic.ini ./
RUN pip install -e .
COPY app/static/audios/ /app/static-library/
EXPOSE 8000 8080 9100
CMD ["songforge-web"]
```

Create a repository-root `.dockerignore` with:

```text
.git
.env
.env.*
.aws
.agents
.codex
.orchestrator
**/.venv
venv
**/node_modules
**/.next
**/__pycache__
**/*.pyc
**/.pytest_cache
**/.mypy_cache
cookies.txt
jar.txt
loadtest/cookies.json
```

The root build context lets Docker see both `backend/` and `app/static/audios/`. The
explicit `COPY` instructions put only the required backend files and audio in the image.

Optional local image check, if Docker is already installed:

```sh
docker build -f deploy/railway/staging/Dockerfile.backend -t songforge-staging-backend .
docker run --rm songforge-staging-backend python -c 'from pathlib import Path; files=list(Path("/app/static-library").glob("*.mp3")); print(len(files), "MP3 files"); assert files'
```

At inspection time the directory had 16 tracked MP3s, about 73 MB.

Create the monitoring files in step 9 before committing everything. Stage only the
intended files, review the diff, commit, and push `development` to GitHub. Railway
deploys the GitHub commit, so unpushed local commits will not be deployed.

## 3. Create the Railway staging environment

1. Open Railway and choose **New Project → Empty Project**. Name it `SongForge Staging`.
2. Open the environment dropdown and create an **Empty Environment** named `staging`.
3. Select `staging` before creating any services.
4. Put every service in the same Railway region.

An environment separates services, variables, and the private network. [Railway environments](https://docs.railway.com/environments)

For this first setup, configure services in the dashboard. **Do not select the existing
`deploy/railway/*.json` files as their configuration files.** They contain defaults of
two replicas, and `web.json` uses `releaseCommand` rather than the documented
`preDeployCommand`. Railway also currently marks the older Config as Code format as
deprecated. [Railway configuration reference](https://docs.railway.com/config-as-code/reference)

## 4. Add PostgreSQL and Redis

**Use Railway's `Database` add-path for both, NOT `Docker Image`.** Typing `postgres`
or `redis` into the Docker Image field will actually pull and deploy — they're real
official Docker Hub images — but you'll get a plain unmanaged container, not Railway's
managed database plugin. That plugin is what generates the `PGUSER`/`PGPASSWORD`/
`PGDATABASE`/`RAILWAY_PRIVATE_DOMAIN` variables every `${{Postgres.*}}`/`${{Redis.*}}`
reference in this guide depends on; a Docker-Image-sourced container exposes none of
those, and every step below would break.

1. Choose **New → Database → PostgreSQL**. Name the service exactly `Postgres`.
2. Choose **New → Database → Redis**. Name the service exactly `Redis`.
3. Wait until both deploy successfully. Check their attached persistent storage.
4. Open each service's Variables tab. PostgreSQL should expose `PGUSER`, `PGPASSWORD`,
   `PGDATABASE`, and `RAILWAY_PRIVATE_DOMAIN`. Redis should expose `REDIS_URL`.

The examples below use Railway references such as `${{Postgres.PGUSER}}`. Paste these
literally into Railway's Variables Raw Editor: Railway resolves them. Do not paste them
into a local shell. If you use different service names, update the references.

For Redis, inspect the resolved `REDIS_URL`: it should use Redis's private hostname.
If the template instead gives a public proxy URL, select/create a private connection
URL using its private domain, port 6379, and the template's password. Use that URL on
the app and exporter services.

All services must belong to the same project **and environment** to communicate
privately. New environments support IPv4 and IPv6, which matters because the current
Python services listen on `0.0.0.0`. [Railway private networking](https://docs.railway.com/networking/private-networking/how-it-works)

## 5. Add PgBouncer

Railway's managed PostgreSQL does **not** bundle a connection pooler by default. (As of a
2026 Railway changelog, PgBouncer can be added from the Postgres service's own
Database → Config → Connection Pooling screen, but that is gated behind a Priority
Boarding feature flag and is not guaranteed available on every account.) The portable
approach — and prod's actual setup, per `docs/deploy.md`'s topology — is PgBouncer as its
own Railway service, which is what this step does.

Unlike Postgres/Redis above, this one genuinely is a Docker Image service — but the
field needs a full `namespace/name:tag` reference, never a bare word: there is no
official `docker.io/library/pgbouncer`, so typing just `pgbouncer` fails to pull.

Choose **New → Docker Image** and enter:

```text
edoburu/pgbouncer:v1.25.2-p0
```

Verify this tag still resolves at [hub.docker.com/r/edoburu/pgbouncer/tags](https://hub.docker.com/r/edoburu/pgbouncer/tags)
before using it — image tags get superseded. If it's gone, substitute
`edoburu/pgbouncer:latest` (fine for a one-off staging session; pin an exact tag again
for anything longer-lived).

Name it `pgbouncer`. Leave its start command at the image default. Paste into Variables:

```dotenv
DB_HOST=${{Postgres.RAILWAY_PRIVATE_DOMAIN}}
DB_PORT=5432
DB_USER=${{Postgres.PGUSER}}
DB_PASSWORD=${{Postgres.PGPASSWORD}}
DB_NAME=${{Postgres.PGDATABASE}}
POOL_MODE=transaction
LISTEN_PORT=6432
AUTH_TYPE=scram-sha-256
MAX_CLIENT_CONN=200
DEFAULT_POOL_SIZE=20
STATS_USERS=${{Postgres.PGUSER}}
IGNORE_STARTUP_PARAMETERS=extra_float_digits
```

Deploy it after PostgreSQL is ready. It needs no public domain or persistent volume.
This image accepts separate `DB_*` variables. [PgBouncer image instructions](https://github.com/edoburu/docker-pgbouncer)

Only the web request-serving database connections go through PgBouncer. Worker
connections and migrations must go directly to PostgreSQL for advisory locks and
LISTEN/NOTIFY to work.

## 6. Configure Cloudflare R2 for staging audio storage

Prod uses Cloudflare R2, not MinIO — this staging setup matches that exactly rather than
standing up a separate MinIO-vs-R2 migration later. R2 is external to Railway: there is
no Railway service to create here, only a bucket and an API token, plus env vars on
`web`/`worker` (step 7). Mirrors `docs/deploy.md`'s "R2 object storage setup".

1. Create an R2 bucket in the Cloudflare dashboard (or `wrangler r2 bucket create <name>`),
   e.g. `songforge-staging-audio`.
2. Create an R2 API token scoped to that one bucket (Cloudflare dashboard → R2 → Manage
   API tokens). This gives you the values for `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY`.
3. Either make the bucket public (R2's "Public access" toggle, or a custom domain) and
   note that public URL, or front it with a Cloudflare Worker/CDN and note that URL
   instead. Either way, write down the resulting HTTPS base URL — you'll set it as
   `S3_PUBLIC_BASE_URL` in step 7.
4. Note your R2 account's S3 API endpoint, `https://<account-id>.r2.cloudflarestorage.com`
   — this is both `S3_ENDPOINT_URL` and the host the browser needs (R2's S3 endpoint is
   already browser-reachable, unlike local MinIO's internal Docker hostname, so — unlike
   the old MinIO-based version of this walkthrough — **no separate presign-endpoint
   variable is needed here**; leave `S3_PRESIGN_ENDPOINT_URL` unset).
5. Audio objects are kept permanently (storage is cheap relative to generation cost) —
   no lifecycle/expiry rule needed.

## 7. Create the three backend services and set their variables

For each of `web`, `worker`, and `simulator`, choose **New → GitHub Repo**, select this
repository, and rename the service. Railway may try an initial deployment before you
finish configuration; configure it and redeploy afterward.

For all three set:

| Setting | Value |
|---|---|
| Source branch | `development` |
| Root Directory | `/` (repository root) |
| Builder | Dockerfile |
| Dockerfile Path | `deploy/railway/staging/Dockerfile.backend` |
| Replicas | `1` |
| Serverless/sleeping | Disabled |

Use separate services even though they share an image: they run different commands.

On **both `web` and `worker`**, paste this common block, replacing the session secret
and the R2 bucket name/public URL from step 6. Use **the same session secret on both
services**.

```dotenv
ENVIRONMENT=staging
LOG_LEVEL=INFO
REDIS_URL=${{Redis.REDIS_URL}}
SESSION_SECRET=REPLACE_WITH_A_RANDOM_SECRET
STATIC_LIBRARY_DIR=/app/static-library
S3_ENDPOINT_URL=https://REPLACE_WITH_YOUR_R2_ACCOUNT_ID.r2.cloudflarestorage.com
S3_ACCESS_KEY_ID=REPLACE_WITH_R2_ACCESS_KEY_ID
S3_SECRET_ACCESS_KEY=REPLACE_WITH_R2_SECRET_ACCESS_KEY
S3_BUCKET=songforge-staging-audio
S3_REGION=auto
S3_PUBLIC_BUCKET=true
S3_PUBLIC_BASE_URL=REPLACE_WITH_YOUR_R2_PUBLIC_OR_CDN_URL
MUSICGPT_BASE_URL=http://${{simulator.RAILWAY_PRIVATE_DOMAIN}}:8080
MUSICGPT_API_KEY=
MUSICGPT_WEBHOOK_URL=http://${{web.RAILWAY_PRIVATE_DOMAIN}}:8000/api/generation/webhook
ENFORCE_RATE_LIMITS=false
GLOBAL_GENERATION_CONCURRENCY=50
PER_USER_CONCURRENT_JOBS=2
METRICS_ENABLED=true
```

Generate a session secret on your laptop with `openssl rand -hex 32` and paste its
output into Railway. Keep secrets out of Git.

`ENFORCE_RATE_LIMITS=false` is the staging load-test setting: default anonymous quotas
otherwise return 429 after a few creates. Generation concurrency limits still apply
even when daily quotas are disabled. A global cap of 50 provides initial headroom for
small tests with a 5-second simulator delay; it is not a throughput guarantee.

`S3_REGION=auto` and leaving `S3_PRESIGN_ENDPOINT_URL` unset both match R2's own
defaults and `docs/deploy.md`'s guidance — do not reintroduce the MinIO-only presign
variable here; it would be a no-op at best and a misconfiguration at worst.

Leave `CLIENT_IP_HEADER` unset for this walkthrough. The existing Next.js proxies do
not forward the browser's original IP to FastAPI, so this setup is not a validation of
per-IP abuse limits.

On **`web`**, add:

```dotenv
PORT=8000
DATABASE_URL=postgresql+asyncpg://${{Postgres.PGUSER}}:${{Postgres.PGPASSWORD}}@${{pgbouncer.RAILWAY_PRIVATE_DOMAIN}}:6432/${{Postgres.PGDATABASE}}
WORKER_DATABASE_URL=postgresql+asyncpg://${{Postgres.PGUSER}}:${{Postgres.PGPASSWORD}}@${{Postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/${{Postgres.PGDATABASE}}
DB_PGBOUNCER_TRANSACTION_MODE=true
DB_POOL_SIZE=5
DB_MAX_OVERFLOW=10
```

Set its **Start Command** to `songforge-web`, **Pre-deploy Command** to `songforge-boot`,
and **Healthcheck Path** to `/health/ready`. Use a healthcheck timeout of 120 seconds for
the initial setup, and allow adequate time for the first seed in the pre-deploy timeout
(for example 600 seconds). Keep this service private.

`WORKER_DATABASE_URL` makes Alembic migrations direct. The seed's ordinary database
sessions still use `DATABASE_URL`, so PgBouncer also needs to be ready for boot to
complete. The MP3s are baked into the image because pre-deploy containers cannot use
runtime volumes. [Railway pre-deploy commands](https://docs.railway.com/deployments/pre-deploy-command)

On **`worker`**, add:

```dotenv
DATABASE_URL=postgresql+asyncpg://${{Postgres.PGUSER}}:${{Postgres.PGPASSWORD}}@${{Postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/${{Postgres.PGDATABASE}}
WORKER_DATABASE_URL=postgresql+asyncpg://${{Postgres.PGUSER}}:${{Postgres.PGPASSWORD}}@${{Postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/${{Postgres.PGDATABASE}}
DB_PGBOUNCER_TRANSACTION_MODE=false
DB_POOL_SIZE=5
DB_MAX_OVERFLOW=2
WORKER_METRICS_PORT=9100
```

Set its Start Command to `songforge-worker`. Leave HTTP healthcheck and pre-deploy
commands empty: it is a background worker, not the FastAPI server. Check its logs and
heartbeat using step 11. Keep it private.

Use `DB_POOL_SIZE` and `DB_MAX_OVERFLOW` on Railway. The `WEB_DB_*`/`WORKER_DB_*` names
in `.env.example` are Compose inputs translated by `docker-compose.yml`, not the
runtime variable names read by the application.

On **`simulator`**, set just:

```dotenv
PORT=8080
SIM_PUBLIC_BASE_URL=http://${{simulator.RAILWAY_PRIVATE_DOMAIN}}:8080
SIM_AUDIO_POOL_DIR=/app/static-library
SIM_WEBHOOK_DELAY_SECONDS=5
SIM_LOG_LEVEL=INFO
```

Set its Start Command to `songforge-simulator`. Its port is fixed at 8080 in the code.
You can use `/openapi.json` as its HTTP startup healthcheck; it has no `/health` route.
Keep it private. **Keep one replica**: task/audio URL metadata lives in process memory
and is not shared between replicas or persisted over restarts.

This is the one place staging deliberately stays non-prod: prod sets `MUSICGPT_BASE_URL`
to the real MusicGPT API and has no `simulator` service at all. Keep the simulator here
for now so generation can be exercised without a real API key or real spend; swapping to
the real API later is a `MUSICGPT_BASE_URL`/`MUSICGPT_API_KEY`/`MUSICGPT_WEBHOOK_URL`
config change on `web`/`worker`, not a code change — see `docs/deploy.md`.

The `postgresql+asyncpg://` prefix is required for the Python app. A plain PostgreSQL
template URL is not interchangeable with it. If you supply custom passwords containing
URL-reserved characters, URL-encode them when constructing database URLs.

## 8. Deploy in order, then add the frontend

Apply/deploy changes in this order rather than launching everything simultaneously:

1. PostgreSQL and Redis.
2. PgBouncer. (R2 needs no Railway deploy step — just the env vars from step 6, already
   in place before web boots.)
3. Simulator.
4. Web, with its pre-deploy boot step. Look for `migrations_complete`,
   `static_audio_uploaded`, `static_library_seeded`, and `boot_complete` in logs.
   Confirm the first seed inserts tracks; a successful boot with zero tracks is not
   enough to establish that the audio pool is correctly packaged.
5. Worker, only after web's boot step succeeds.
6. Frontend.

For frontend, create another GitHub service named `frontend`:

| Setting | Value |
|---|---|
| Branch | `development` |
| Root Directory | `/frontend` |
| Builder | Dockerfile |
| Dockerfile Path | `Dockerfile` |
| Start Command | Leave empty; use Dockerfile's `node server.js` |
| Healthcheck Path | `/api/health` |
| Replicas | `1` |

Set:

```dotenv
NODE_ENV=production
HOSTNAME=0.0.0.0
PORT=3000
BACKEND_URL=http://${{web.RAILWAY_PRIVATE_DOMAIN}}:8000
```

Deploy and generate its public HTTPS domain with target port **3000**. Write down this
URL: it is your browser URL and the initial load-test `BASE_URL`. `BACKEND_URL` is read
at runtime by the existing Route Handlers; you do not need a `NEXT_PUBLIC_*` API URL.
Railway issues HTTPS domains through the Networking settings. [Railway public networking](https://docs.railway.com/networking/public-networking)

For later releases, web and worker remain separate deployments. A GitHub push does
not give worker the equivalent of Compose's migration ordering. Initially disable
automatic deployments while configuring; for schema-changing releases, deploy web's
migrations successfully before updating the worker, and use compatible migrations.

## 9. Forward metrics to Grafana Cloud

Prod has **no self-hosted Prometheus or Grafana** — per `docs/deploy.md`, a lightweight
agent runs alongside the app, scrapes `web`'s and `worker`'s `/metrics` over Railway's
private network, and forwards samples to Grafana Cloud's free tier via `remote_write`.
This staging setup uses the exact same approach, so dashboards/alerting built against
staging behave identically once pointed at prod. There is no self-hosted Grafana
instance and no SSH tunnel for one (step 11).

First, sign up for Grafana Cloud's free tier if you haven't, then from your stack's
"Connection details" → Prometheus page, note the `remote_write` URL, your Prometheus
username, and an API key/token with `metrics:write` permission.

Create `deploy/railway/staging/prometheus.yml` under the repository root (adapted from
`monitoring/prometheus/prometheus.prod.example.yml`):

```yaml
global:
  # Conservative interval -- this agent only scrapes and forwards; it isn't a
  # load-test-grade local TSDB like the local-dev `prometheus.yml`.
  scrape_interval: 30s
  evaluation_interval: 30s

scrape_configs:
  - job_name: songforge_web
    dns_sd_configs:
      - names: [web.railway.internal]
        type: A
        port: 8000
        refresh_interval: 10s
  - job_name: songforge_worker
    dns_sd_configs:
      - names: [worker.railway.internal]
        type: A
        port: 9100
        refresh_interval: 10s

remote_write:
  - url: "${GRAFANA_CLOUD_PROMETHEUS_REMOTE_WRITE_URL}"
    basic_auth:
      username: "${GRAFANA_CLOUD_PROMETHEUS_USERNAME}"
      password: "${GRAFANA_CLOUD_API_KEY}"
```

Names must match your service names. A-record discovery assumes the fresh IPv4-capable
environment from step 3. [Prometheus configuration](https://prometheus.io/docs/prometheus/latest/configuration/)

Create `deploy/railway/staging/Dockerfile.grafana-agent`:

```dockerfile
FROM prom/prometheus:v2.55.1
COPY deploy/railway/staging/prometheus.yml /etc/prometheus/prometheus.yml
CMD ["--config.file=/etc/prometheus/prometheus.yml", "--storage.tsdb.path=/prometheus", "--storage.tsdb.retention.time=1h"]
```

A short local retention is intentional: this instance's own TSDB is just a scrape buffer
in front of `remote_write`, not a dashboard source — Grafana Cloud is where you look.

Create a GitHub service named `grafana-agent`, branch `development`, Root Directory `/`,
Dockerfile Path `deploy/railway/staging/Dockerfile.grafana-agent`. Leave its Start
Command empty. One replica, private, no public domain, no persistent volume. Set:

```dotenv
PORT=9090
GRAFANA_CLOUD_PROMETHEUS_REMOTE_WRITE_URL=REPLACE_WITH_YOUR_STACK_REMOTE_WRITE_URL
GRAFANA_CLOUD_PROMETHEUS_USERNAME=REPLACE_WITH_YOUR_STACK_USERNAME
GRAFANA_CLOUD_API_KEY=REPLACE_WITH_A_METRICS_WRITE_API_KEY
```

Optional HTTP healthcheck `/-/ready`. **Never** add a public domain to this service or
expose `/metrics` on an internet-facing domain — scrape it only over the private network,
matching `docs/deploy.md`'s explicit warning on this point.

This recipe deliberately does not add database-level exporters (Postgres/Redis/PgBouncer)
or a cAdvisor equivalent — prod's own documented recipe (`prometheus.prod.example.yml`)
scrapes only the app-level `/metrics` endpoints. Railway's own per-service CPU/memory/
restart graphs cover basic infra health; if you want DB-level dashboards later, add
exporter services and extra `scrape_configs` jobs the same way the old self-hosted setup
did, and forward those too.

## 10. Open Grafana Cloud and check the running services

Install the Railway CLI on your laptop if needed; this example assumes Node/npm:

```sh
npm install -g @railway/cli
railway login
railway link
```

Select your SongForge project and `staging` when prompted. Use the dashboard for builds,
variables, and deployment; the CLI is for log/SSH inspection below.

Check the worker:

```sh
railway ssh --service worker --environment staging
python -m songforge.worker.health
exit
```

The health command should exit with status zero. It checks the heartbeat; also check
worker logs and successful job completion, since a heartbeat alone does not prove
every dependency is working.

Open your Grafana Cloud org directly in your browser — it's hosted externally, so no
Railway SSH tunnel is needed (unlike a self-hosted Grafana instance). In Grafana Cloud's
Prometheus "Explore" view, confirm `up{job="songforge_web"}` and
`up{job="songforge_worker"}` both report `1`; this proves the agent is scraping and
forwarding successfully. Import or recreate the dashboards under
`monitoring/grafana/dashboards/` in your Grafana Cloud org (Dashboards → Import → paste
JSON) — they are datasource-agnostic, so they work unmodified against the Grafana Cloud
Prometheus datasource. The old `capacity-thesis` dashboard is now under
`monitoring/grafana/dashboards-archive/`; the current dashboard is the overview.

## 11. Prove one simulated generation works before testing capacity

On your laptop, from the repository root, substitute the frontend's public URL:

```sh
export BASE_URL=https://REPLACE_WITH_FRONTEND_DOMAIN
curl -f "$BASE_URL/api/health"
curl -f "$BASE_URL/now-playing"
curl -i -c /tmp/songforge-staging-smoke.cookies \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a lo-fi beat for studying"}' \
  "$BASE_URL/create"
curl -N --max-time 20 -b /tmp/songforge-staging-smoke.cookies "$BASE_URL/events"
```

The frontend health endpoint confirms Next.js is up; `/now-playing` exercises its
backend connection. Create should return HTTP **202** with `job_id` and `state=QUEUED`.
The SSE command should display event frames; it intentionally exits after 20 seconds
with curl's timeout exit code.

In Railway logs, confirm the job progresses through `dispatch_submitted`, simulator
`sim_webhook_delivered`, and worker `ingest_completed`. In Grafana Cloud, confirm
dispatch and ingest counters increase. Open the frontend in your browser, press play,
submit a prompt there, and confirm the resulting simulated audio plays and the download
works. The browser has its own identity, separate from the curl cookie file.

The simulator uses existing MP3s as fake generated music. No MusicGPT API key is needed.
Check the worker's resolved `MUSICGPT_BASE_URL` is the simulator's private URL before
load testing. `SIM_PUBLIC_BASE_URL` is private despite its name: only the worker needs
to fetch those simulator URLs. Final playback URLs must use R2's public domain/CDN.

## 12. Run the first load tests locally using Docker

The load generator runs on your laptop and sends requests to staging; it does not need
to be a permanent Railway service. Run these commands from the repository root, with
`BASE_URL` still set to the frontend's public HTTPS URL. Docker avoids a separate k6
installation. [Install k6 with Docker](https://grafana.com/docs/learning-paths/run-first-k6-test/install-k6/)

First, a 30-second test at **one create per second**:

```sh
docker run --rm -i \
  -e BASE_URL="$BASE_URL" -e TARGET_RATE_PER_SEC=1 -e DURATION=30s \
  grafana/k6 run - < loadtest/create-rate.js
```

Then a small connection ramp to **50 concurrent listeners** (approximately six minutes):

```sh
docker run --rm -i \
  -e BASE_URL="$BASE_URL" -e MAX_VUS=50 -e HOLD_OPEN_SECONDS=600 \
  grafana/k6 run --discard-response-bodies - < loadtest/connection-ceiling.js
```

Then **five creates per second for two minutes**:

```sh
docker run --rm -i \
  -e BASE_URL="$BASE_URL" -e TARGET_RATE_PER_SEC=5 -e DURATION=2m \
  grafana/k6 run - < loadtest/create-rate.js
```

Run these sequentially at first. Later run the listener test in one terminal and a
small create test in another to observe how listener count affects the write path.
Increase one input at a time and record replica counts, resource limits, pool sizes,
simulator delay, and generation cap with each result.

For repeatable results, record `docker run --rm grafana/k6 version`, then pin the tested
k6 image tag/digest on later runs.

Interpret the results carefully:

- `create-rate.js` checks HTTP acceptance latency (p95 < 1000 ms) and errors (< 2%).
  An accepted create is not proof the job finished. Watch dispatch, ingest, and pending
  job states in Grafana Cloud as well.
- Watch `sum(songforge_sse_connected_listeners)` for real server-side listener count.
  The script holds sockets open and does not parse SSE frames. Its timeout/error
  handling and `http_req_failed` threshold are imperfect, so a red summary alone
  does not establish a server connection ceiling. Correlate it with listener metrics
  and logs; retain the manual stream check from step 11.
- Do not use the loadtest README's `--vus 1 --duration 1s` shortcut on
  `connection-ceiling.js`: it defines an explicit scenario. `MAX_VUS=50` is the
  straightforward small run for the existing script.
- A growing **QUEUED generation-job** backlog means dispatch/ingest is falling behind,
  or the configured generation cap is limiting it. Disabling quotas does not disable
  that cap. More worker replicas do not raise the global cap.
- A growing **READY-song playback queue** is expected when users create songs faster
  than a single station can play them. Separate it from generation backlog.
- These scripts exercise APIs and SSE, not thousands of browsers downloading MP3s.
  Audio delivery capacity needs a separate test if that is a later objective.
- Public SSE requests are subject to Railway's edge limits, including its 15-minute
  maximum request duration. Keep test holds/runtime within that bound or explicitly
  model reconnections. [Railway SSE limits](https://docs.railway.com/guides/sse-vs-websockets)

## 13. Run the additional session/stress scripts after the small tests

`realistic-session.js`, `viral-connections.js`, and `create-rate-ramp.js` read a local
`cookies.json`. The checked-out file is ignored by Git and may contain identities signed
with a different secret. Generate fresh staging identities rather than copying it.

Save this as `/tmp/songforge-staging-cookies.py` and run it with Python 3 on your laptop:

```python
import json
import os
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.request import urlopen

base = os.environ["BASE_URL"].rstrip("/")
cookies = []
for _ in range(100):
    with urlopen(base + "/quota", timeout=30) as response:
        parsed = SimpleCookie()
        for header in response.headers.get_all("Set-Cookie", []):
            parsed.load(header)
        cookies.append(parsed["sf_uid"].value)
Path("loadtest/cookies.json").write_text(json.dumps(cookies))
print("Wrote", len(cookies), "staging identities")
```

```sh
python3 /tmp/songforge-staging-cookies.py
docker run --rm \
  -v "$PWD/loadtest:/scripts:ro" \
  -e BASE_URL="$BASE_URL" -e COOKIE_FILE=/scripts/cookies.json \
  -e TARGET_USERS=50 -e ARRIVALS_PER_SEC=2 -e STEADY_MINUTES=2 \
  grafana/k6 run /scripts/realistic-session.js
```

This combines listeners and mixed create/quota/queue actions. Use it before the larger
stress tests. Treat the cookie pool as test credentials and leave it uncommitted.

The remaining scripts run with the following forms, **only when you intend their
hard-coded stress levels**:

```sh
docker run --rm -i -e BASE_URL="$BASE_URL" \
  grafana/k6 run - < loadtest/connection-ceiling-ramp.js

docker run --rm -v "$PWD/loadtest:/scripts:ro" \
  -e BASE_URL="$BASE_URL" -e COOKIE_FILE=/scripts/cookies.json \
  grafana/k6 run /scripts/create-rate-ramp.js

docker run --rm -v "$PWD/loadtest:/scripts:ro" \
  -e BASE_URL="$BASE_URL" -e COOKIE_FILE=/scripts/cookies.json \
  grafana/k6 run /scripts/viral-connections.js
```

Their defaults reach 2,500 listeners, 800 creates/sec, and 8,000 listeners respectively.
Lower the stages in a copy for an intermediate run; setting `MAX_VUS` does not rewrite
their hard-coded stages. The create ramp measures intake, and can create a large pending
backlog and many stored audio objects. You can stop the local generator with Ctrl+C;
jobs already accepted by staging will continue processing.

## 14. Scale, troubleshoot, and finish the session

Once the baseline works, try two `web` replicas, then two `worker` replicas, then two
`frontend` replicas, changing one tier at a time. Keep the simulator at one replica.
Use the same session secret for all web replicas. Keep worker database connections
direct and their connection pools small. Start by checking PostgreSQL's actual
`SHOW max_connections` rather than assuming a Compose environment variable changed it.

After scaling, inspect Grafana Cloud's `up{job=...}` series and confirm every replica is
represented before interpreting summed process metrics. If Railway's DNS discovery in
your setup does not enumerate every replica, arrange per-replica discovery; a
service-level scrape must not be presented as a complete fleet total.

| Symptom | First check |
|---|---|
| Web boot succeeds but station is idle | MP3s packaged at `/app/static-library`, nonzero initial seed, worker running |
| Simulator audio returns 404 | `SIM_AUDIO_POOL_DIR`, packaged MP3s, one simulator replica, recent restart |
| Creates return 429 | `ENFORCE_RATE_LIMITS=false` actually deployed on web |
| Creates accepted but stuck QUEUED | Worker running, direct PostgreSQL connection, generation semaphore capacity |
| Waiting for webhook | Worker simulator URL and callback URL; simulator delivery logs |
| Ingest fails | Simulator advertised URL, R2 endpoint/credentials/bucket name, private networking |
| Browser cannot play audio | R2 bucket public access/custom domain, `S3_PUBLIC_BASE_URL`, bucket name matches `S3_BUCKET` |
| Download signature fails | `S3_ENDPOINT_URL` must be the exact R2 account S3 API host; `S3_PRESIGN_ENDPOINT_URL` should stay unset on R2 |
| Frontend returns 502/503 | `BACKEND_URL`, web port 8000, web readiness, both services in staging |
| `psycopg2` import/driver error | Python app URL must start `postgresql+asyncpg://` |
| Grafana Cloud shows no data | `grafana-agent` service logs, `GRAFANA_CLOUD_*` vars, private DNS names in `prometheus.yml` match your service names |
| CPU panels empty | The dashboards' cAdvisor `container_*` panels are local-dev-only; use Railway's per-service resource graphs instead |
| Failure only at large load | Check both local generator resources and Railway service limits, plus Next.js, database pools and edge behavior |

After tests, save results, restore `ENFORCE_RATE_LIMITS=true` and your normal concurrency
settings on the app services, and redeploy. If staging is only for this session, remove
or stop its services when finished. Persistent volumes remain billable storage unless
removed; removing them destroys the staging data. Keep staging isolated from production
R2 buckets, databases, and provider credentials.

For the initial setup, your completion criteria are: successful nonempty seed, running
worker, one simulated create reaching ingestion, browser playback/download, `up{...}`
reporting `1` for both app services in Grafana Cloud, and successful small create and
listener runs with interpretable metrics.
