# Deploy guide: staging & prod (issue #19, PRD #6 AC#4)

Prod target is **Railway**. This doc covers the topology, the env vars each service needs,
scaling web/worker, R2 object storage, the boot/migrate release step, and how failover
behaves with N workers. The local-equivalent of everything here is `docker-compose.yml` at
the repo root (see README's "Multi-instance & deploy" section) — bring that up first if you
want to see the exact same topology without touching Railway.

> **This doc does not perform a live deploy.** Actually running `railway up` / connecting a
> Railway project requires the operator's own Railway account and credentials — that's an
> outward action no agent can take on your behalf. Everything below up to
> ["Operator: doing the actual deploy"](#operator-doing-the-actual-deploy) is preparation
> (config artifacts + local verification); that final section is the exact command sequence
> a human operator runs.

## Topology

```
                                   ┌─────────────────────────┐
                                   │   Railway-managed LB     │   (Railway's own edge/LB
                                   │  (in front of `web`)     │    terminates TLS; no nginx
                                   └────────────┬─────────────┘    needed in Railway itself —
                                                │                  that's a LOCAL-only stand-in,
                                    ┌───────────┴───────────┐      see the note below)
                                    │                       │
                              ┌─────▼─────┐           ┌─────▼─────┐
                              │  web (N)  │   . . .   │  web (N)  │   stateless FastAPI
                              └─────┬─────┘           └─────┬─────┘   (no sticky sessions)
                                    │                       │
                                    └───────────┬───────────┘
                                                │
                                       ┌────────▼────────┐
                                       │    pgbouncer     │   TRANSACTION pooling
                                       │ (its own Railway  │   (fans in web's connections)
                                       │     service)      │
                                       └────────┬──────────┘
                                                │
                              ┌─────────────────▼─────────────────┐
                              │      Postgres (Railway plugin)     │◄──────────┐
                              └─────────────────┬───────────────────┘          │ DIRECT
                                                │                              │ (bypasses
                              ┌─────────────────▼─────────────────┐          │  pgbouncer)
                              │       Redis (Railway plugin)       │          │
                              └─────────────────▲─────────────────┘          │
                                                │                              │
                                    ┌───────────┴───────────┐                 │
                              ┌─────┴─────┐           ┌─────┴─────┐           │
                              │ worker(N) │   . . .   │ worker(N) │───────────┘
                              └─────┬─────┘           └─────┬─────┘  advisory lock (single
                                    │                       │        leader) + LISTEN/NOTIFY,
                                    └───────────┬───────────┘        DIRECT to Postgres only
                                                │
                                      ┌─────────▼─────────┐
                                      │  Cloudflare R2      │  (S3 API; external to Railway)
                                      └─────────────────────┘

                                      ┌─────────────────────┐
                                      │  frontend (Next.js)  │  its own Railway service
                                      └─────────────────────┘
```

**Note on the LB:** locally, `docker-compose.yml`'s `lb` service (nginx) exists because
plain Docker Compose has no built-in load balancer of its own. On Railway, N `web` replicas
are already reachable behind Railway's own platform-managed edge/load balancer — no nginx
(or equivalent) service is needed there. The `resolver 127.0.0.11` / dynamic-upstream trick
in `loadbalancer/nginx.conf` is a Compose-specific workaround for Docker's embedded DNS; it
has no Railway analog because Railway's edge already re-discovers replicas as they scale.

**Note on retry-on-restart:** `loadbalancer/nginx.conf` sets
`proxy_next_upstream error timeout http_502 http_503 http_504;` (retrying up to
`proxy_next_upstream_tries 2` a different `web` replica) so a request that lands on a
container mid-restart gets a second try instead of a hard error — this is a compose-local
concern only, covering the window between `docker stop`/a crash and the resolver's next
10s DNS refresh. Railway's own edge LB already handles unhealthy-replica retry/failover in
prod; nothing equivalent needs configuring there.

**Why pgbouncer is its own Railway service:** Railway's managed Postgres plugin does **not**
ship a built-in connection pooler. Running `edoburu/pgbouncer` (same image as the compose
stack, see `docker-compose.yml`) as its own Railway service — sourced from the Docker image
directly (Railway dashboard: "Deploy" → "Docker Image" → `edoburu/pgbouncer:v1.25.2-p0`,
not from this repo) — mirrors the local topology exactly and is required infrastructure,
not optional, for AC#2.

## Services & how they're configured

| Service      | Source                                             | Config-as-code file                | Start command    |
| ------------ | --------------------------------------------------- | ----------------------------------- | ----------------- |
| `web`        | this repo, root dir `backend/`                       | `deploy/railway/web.json`           | `songforge-web`    |
| `worker`     | this repo, root dir `backend/`                       | `deploy/railway/worker.json`        | `songforge-worker` |
| `frontend`   | this repo, root dir `frontend/`                      | `deploy/railway/frontend.json`      | (Dockerfile's own CMD) |
| `pgbouncer`  | Docker image `edoburu/pgbouncer:v1.25.2-p0`           | none (image-sourced; set env vars via dashboard/CLI) | image default |
| `postgres`   | Railway Postgres plugin                              | n/a                                  | n/a                |
| `redis`      | Railway Redis plugin                                 | n/a                                  | n/a                |

`web` and `worker` are **two separate Railway services built from the same `backend/`
directory** (same Dockerfile/image, different `startCommand` — Railway's config-as-code
`deploy.startCommand` overrides the Dockerfile's own `CMD` per-service), so each gets its
own independent env vars and replica count, exactly like the compose `web`/`worker`
services already do.

`deploy/railway/web.json` sets `deploy.releaseCommand: "songforge-boot"` — Railway runs
this **once per deploy, before the new `startCommand` takes traffic**, and a non-zero exit
blocks the rollout (the new version never goes live) — this is the "release step" (AC#4).
It's attached to `web` only (not `worker`) so migrations run from exactly one place. See
["Migrations must bypass PgBouncer"](#migrations-must-bypass-pgbouncer) for why this is
now *actually* equivalent to compose's one-shot `migrate` service (an earlier version of
this doc claimed parity here that wasn't true — see that section), and
["Ordering & first deploy"](#ordering--first-deploy-gotcha) below for the one gotcha this
implies.

## Migrations must bypass PgBouncer

**This is load-bearing, not a style preference.** `docker-compose.yml`'s `migrate` service
deliberately gives itself a *direct* `DATABASE_URL` (bypassing `pgbouncer`) because
Alembic's DDL and its own migration-locking are unsafe/undefined under PgBouncer
**transaction pooling** — a pooled logical connection can be handed a different backend
session between statements, and things like a future `CREATE INDEX CONCURRENTLY`
migration cannot run inside a pooler-imposed transaction at all.

On Railway, `songforge-boot` (the `releaseCommand` above) runs **inside the `web`
service** and inherits *its* environment — which, per the env var matrix below, points
`DATABASE_URL` at PgBouncer with `DB_PGBOUNCER_TRANSACTION_MODE=true`. Left as-is, the
release step would run Alembic DDL through the exact class of connection the local
`migrate` service was built to avoid — not equivalent to compose, despite an earlier
version of this doc claiming otherwise.

**Fix:** set `WORKER_DATABASE_URL` on the `web` Railway service to a **direct** Postgres
plugin connection string (bypassing pgbouncer) — the same variable that lets the worker's
advisory-lock/LISTEN-NOTIFY connections bypass the pooler (AC#2). `songforge-boot` (via
`migrations/env.py` → `Settings.sync_worker_database_url`) now migrates over
`effective_worker_database_url` — `WORKER_DATABASE_URL` when set, else `database_url` — so
setting it on `web` routes *only* the release step's migrations around PgBouncer; `web`'s
own request-serving connections (`db.get_engine()`) are untouched and still pool through
`database_url`. This mirrors compose's topology exactly: one shared env surface, one
override that steers migrations to a direct connection while request traffic stays
pooled. See `deploy/railway/web.json`'s `_notes.requiredEnvVars.WORKER_DATABASE_URL` for
the on-file reminder.

## Env var matrix (delta from `.env.example`)

All the datastore/S3/rate-limit/identity vars in `.env.example` still apply — this table
only calls out what **differs per service** or is **prod-required**.

| Var                                | `web`                                   | `worker`                          | notes |
| ----------------------------------- | ---------------------------------------- | ----------------------------------- | ----- |
| `DATABASE_URL`                      | `postgresql+asyncpg://…@<pgbouncer-host>:6432/<db>` | `postgresql+asyncpg://…@<postgres-host>:5432/<db>` (direct, from the Postgres plugin's own connection string) | AC#2: web pools, worker/release bypass the pooler entirely |
| `DB_PGBOUNCER_TRANSACTION_MODE`     | `true`                                    | unset (`false` default)             | disables asyncpg's server-side prepared-statement cache — only needed where a pooler sits in front |
| `WORKER_DATABASE_URL`               | **required**: direct (non-pooled) Postgres plugin URL | not needed (worker's own `DATABASE_URL` already IS the direct URL) | Phase-5 fix: `web`'s `releaseCommand` (`songforge-boot`) inherits `web`'s own env, so without this override its Alembic migrations would run through pgbouncer — see [Migrations must bypass PgBouncer](#migrations-must-bypass-pgbouncer). `web`'s request-serving connections are unaffected and still use pooled `DATABASE_URL` |
| `PORT`                              | set automatically by Railway             | n/a (worker binds no port)          | `Settings.web_port` reads this (see `config.py`); `songforge-web`'s `__main__.py` binds it, replacing the old Procfile's hard-coded `8000` |
| `SESSION_SECRET`                    | **required, real value**                 | not read by worker                  | prod fail-closed: `Settings` raises if `ENVIRONMENT=prod` and this is still the dev-insecure default — see `config.py`. **The validator does NOT currently enforce this in staging** (an existing test, `test_staging_with_default_session_secret_is_fine`, pins that the default is accepted there) — treat this as a manual, non-negotiable operator requirement instead: set a real `SESSION_SECRET` in staging too, since staging is prod-tier and may be internet-reachable (an unset one leaves the identity cookie's HMAC key a public constant, forgeable by anyone) |
| `ENVIRONMENT`                       | `staging` or `prod`                      | `staging` or `prod`                 | gates the `SESSION_SECRET` fail-closed check above (prod only, see note) and any other environment-specific behavior |
| `CLIENT_IP_HEADER`                  | set to `X-Forwarded-For` **only after verifying** Railway's edge (see below) | not read (worker never resolves a request's client IP) | see [Client IP / rate-limit trust](#client-ip--rate-limit-trust) |
| `S3_ENDPOINT_URL`                   | R2 endpoint: `https://<account-id>.r2.cloudflarestorage.com` | same | see [R2 setup](#r2-object-storage-setup) |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | R2 API token pair                 | same                                 | scoped to the one bucket (least privilege) |
| `S3_BUCKET` / `S3_REGION`           | your R2 bucket name / `auto`             | same                                 | |
| `S3_PUBLIC_BASE_URL`                | your R2 public bucket URL or CDN domain  | same (used for `MUSICGPT_WEBHOOK_URL` construction, not audio serving) | audio served straight from R2/CDN to the browser — nothing proxies through `web` |
| `MUSICGPT_BASE_URL`                 | real MusicGPT API base                   | not read by worker                  | the fault-injectable simulator (`SIM_*`) is dev/test-only — never set in staging/prod |
| `WORKER_LOCK_TCP_USER_TIMEOUT_SECONDS` | not read by web                       | `13` (default; tune per AC#4)       | Postgres `tcp_user_timeout` GUC on the worker's dedicated advisory-lock connection — see [Failover](#failover--n-workers) |

## R2 object storage setup

1. Create an R2 bucket in the Cloudflare dashboard (or `wrangler r2 bucket create <name>`).
2. Create an R2 API token scoped to that bucket only (Cloudflare dashboard → R2 → Manage
   API tokens) — this gives you `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY`.
3. Set `S3_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com`, `S3_REGION=auto`,
   `S3_BUCKET=<name>`.
4. Either make the bucket public (R2 "Public access" toggle, or a custom domain) and set
   `S3_PUBLIC_BASE_URL` to that public/CDN URL, or front it with a Cloudflare Worker/CDN and
   point `S3_PUBLIC_BASE_URL` there. `S3_PUBLIC_BUCKET=true` (the boot step's best-effort
   anonymous-read grant) is safe to leave on for R2 too, matching the local MinIO behavior.
5. Audio objects are kept **permanently** (storage is cheap relative to generation cost —
   see the redesign-object-storage decision log) — no lifecycle/expiry rule needed.

## Scaling web & worker

- **`web`**: set `numReplicas` in `deploy/railway/web.json` (or the dashboard's replica
  count), or trigger a redeploy after editing it. Stateless — any replica serves any
  request, Railway's edge load-balances automatically, no config coordination needed
  between replicas.
- **`worker`**: same mechanism, `deploy/railway/worker.json`. **N=2-3 is the target for
  HA** (AC#3). This is safe to scale purely as an infra change because the coordination
  logic already exists in code:
  - `dispatch`/`ingest`/`watchdog` claim rows with `FOR UPDATE SKIP LOCKED` (issues
    #12/#13/#16) — N workers pull from the same queues without double-processing.
  - The radio coordinator elects exactly **one leader** via `pg_advisory_lock` (issue
    #17) — scaling worker to N does not produce N leaders; N-1 instances sit idle on
    that specific responsibility (still doing row-claim work) until a failover.
- Locally, the equivalent is `docker compose up --scale web=2 --scale worker=2` — verified
  during this issue's implementation (see the compose file's `deploy.replicas` comments for
  the CLI-vs-Swarm caveat).

## Failover & N workers

The mechanism (issue #17, PRD #74) doesn't change with N — only one worker ever holds
`pg_advisory_lock(radio_advisory_lock_key)`; the rest block waiting for it. When the leader's
process dies or its connection drops:
- A **clean process exit/restart** (Railway redeploy, `SIGTERM`/`SIGKILL`) closes the
  backend session immediately — Postgres releases the advisory lock as soon as it notices
  the socket is gone, and a waiting worker acquires it right away (verified locally: a
  killed leader was replaced inside the same second).
- A **silent network partition** (the process is still "alive" from Postgres's point of
  view but unreachable) is what `WORKER_LOCK_TCP_USER_TIMEOUT_SECONDS` (default 13s) is
  actually for: it's threaded into the lock connection's asyncpg `server_settings` as the
  Postgres `tcp_user_timeout` GUC, so Postgres tears down that specific backend (and
  releases its lock) within roughly that window even without a clean disconnect. The exact
  value (default 13s, PRD target ~10-15s) should be re-validated against staging's real
  network characteristics during a load test (issue #18's k6 harness), not assumed
  correct from local testing alone.

## Ordering & first deploy gotcha

`songforge-boot`'s `releaseCommand` is attached to the `web` service only (see above). On a
**brand new environment**, deploy/redeploy `web` first (or at least once) before scaling
`worker` up, so the schema exists before worker tries to query it. On every subsequent
deploy this is a non-issue: Alembic's migrations are versioned (it only applies revisions
the schema doesn't already have), so redeploying `web` again is idempotent, and `worker`
redeploying independently never needs to run `songforge-boot` itself.

## Client IP / rate-limit trust

The per-IP daily-quota cap (`anon_daily_songs_per_ip`, issue #15) trusts a forwarded
header for the client's IP **only** when `CLIENT_IP_HEADER` is configured — and that is
only safe when the proxy in front **overwrites** the header with the real TCP peer,
never appends to a client-supplied value (`web/identity.py::client_ip` reads the
**leftmost** token, trusting whatever a client puts there if the header is merely
appended to).

- **Local (compose):** SAFE. `loadbalancer/nginx.conf` sets
  `proxy_set_header X-Forwarded-For $remote_addr;` — an overwrite, not
  `$proxy_add_x_forwarded_for` (which appends) — and `web` has no host port of its own,
  so `lb` is genuinely the only hop in front of every request. `docker-compose.yml` sets
  `CLIENT_IP_HEADER=X-Forwarded-For` on the `web` service to match. Verify it yourself:
  ```sh
  curl -s -H 'X-Forwarded-For: 1.2.3.4' http://localhost:${LB_HOST_PORT:-8000}/health/ready
  # then check the rate-limit path actually resolves the REAL peer, not 1.2.3.4, e.g. by
  # exhausting ANON_DAILY_SONGS_PER_IP from one real client and confirming a spoofed
  # X-Forwarded-For on a fresh request does NOT get a fresh quota bucket.
  ```
- **Railway:** **NOT safe to assume.** Before setting `CLIENT_IP_HEADER=X-Forwarded-For`
  on the `web` service, the operator must confirm Railway's edge overwrites/normalizes
  `X-Forwarded-For` (or sets its own trustworthy header, e.g. an `X-Real-IP` /
  platform-specific header) rather than appending to a client-supplied value — consult
  Railway's current networking docs, since platform behavior here is not something this
  repo can verify or pin. **If unverified, leave `CLIENT_IP_HEADER` unset.** The
  degraded-but-safe fallback (direct socket peer) means every request looks like it
  comes from Railway's own edge IP — the per-IP cap collapses to one shared bucket
  (unfair rate-limiting, but NOT spoofable) — strictly better than trusting an
  unverified, appendable header, which would let a client bypass the cap entirely by
  rotating a spoofed value per request.

## Observability: `/metrics` exposure

`GET /metrics` (issue #18, "always on, every environment") stays **always-on** on `web`
itself — this section restricts *where it's reachable from*, not whether it exists.

- **Local (compose):** `loadbalancer/nginx.conf` blocks it at the public edge
  (`location = /metrics { deny all; return 403; }`) — a request to
  `http://localhost:${LB_HOST_PORT}/metrics` gets a 403. This does NOT break scraping:
  `monitoring/prometheus/prometheus.yml` targets `web:8000` directly over the compose
  network (`job_name: songforge_web`, `targets: ["web:8000"]`) — it never goes through
  `lb` in the first place.
- **Railway/prod:** there is no self-hosted Prometheus and no nginx-equivalent public
  edge rule to add — instead, per `monitoring/prometheus/prometheus.prod.example.yml`, a
  lightweight scrape-and-forward agent (Prometheus in `remote_write`-only mode, or
  Grafana Agent) runs **alongside** the prod `web` instance(s) and forwards to Grafana
  Cloud's free tier. Scrape it over Railway's **private network** (internal DNS /
  colocated agent), never by pointing a scraper at `web`'s public Railway domain. Do not
  add a public route or expose `/metrics` on the internet-facing domain.

## Rollback / release-command failure behavior

If `songforge-boot` exits non-zero during a `web` deploy, Railway keeps the **previous**
`web` deployment serving traffic and marks the new one failed — mirroring compose's
`service_completed_successfully` gate (which blocks `web`/`worker` from even starting on a
failed `migrate`). No manual rollback action is needed in that case; fix the migration and
redeploy. If you need to roll back a deploy that DID succeed but is behaviorally bad,
use Railway's dashboard "Redeploy" on a prior successful deployment for the `web`/`worker`
services independently — they're separate services, so you can roll one back without
touching the other.

## Operator: doing the actual deploy

Everything above is preparation. The actual deploy requires **your own** Railway account,
project, and credentials — an agent cannot create or authenticate a Railway project on
your behalf. Once you have a Railway project:

```bash
# One-time: install & log in (interactive; needs your browser/credentials)
npm i -g @railway/cli
railway login

# From the repo root, link this checkout to your Railway project
railway link

# Provision the plugins (or do this once via the dashboard instead)
railway add --plugin postgresql
railway add --plugin redis

# Create the pgbouncer service from the public image (dashboard: New → Docker Image
# → edoburu/pgbouncer:v1.25.2-p0), then set its env vars (mirror docker-compose.yml's
# `pgbouncer` service: DATABASE_URL -> the Postgres plugin's connection string,
# POOL_MODE=transaction, LISTEN_PORT=6432, AUTH_TYPE=scram-sha-256, plus
# MAX_CLIENT_CONN/DEFAULT_POOL_SIZE per the env var matrix above).

# Create the web/worker/frontend services, each pointed at this repo with the
# matching root directory and config-as-code path:
railway service create web       # root: backend/, config: deploy/railway/web.json
railway service create worker    # root: backend/, config: deploy/railway/worker.json
railway service create frontend  # root: frontend/, config: deploy/railway/frontend.json

# Set each service's env vars (dashboard, or `railway variables set KEY=VALUE
# --service <name>`) per the env var matrix above, then deploy:
railway up --service web
railway up --service worker
railway up --service frontend
```

The exact dashboard steps (linking a GitHub repo vs. `railway up` from a local checkout,
setting each service's root directory / config-as-code path) are Railway UI details that
may shift between Railway releases — treat the commands above as the shape, and the
Railway dashboard's own service settings as the source of truth for field names at the
time you actually deploy.
