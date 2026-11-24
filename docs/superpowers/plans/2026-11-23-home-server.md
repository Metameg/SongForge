# Home Dev Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run SongForge from `development` on a home Ubuntu server, public through a Cloudflare Tunnel, with k6 run locally on it.

**Architecture:** A Compose overlay on the unchanged `docker-compose.yml` adds `cloudflared`, closes ports and adds restarts. Small bash scripts wrap init, up, load test and wipe. The tunnel's routes and Access policy are dashboard settings, covered by a guide.

**Tech Stack:** Docker Compose, bash, cloudflared, k6, pytest.

**Spec:** `docs/superpowers/specs/2026-11-23-home-server-design.md`

## Global Constraints

- `docker-compose.yml` and the root `.env.example` are not modified.
- Docker Compose >= 2.24.4 (`!override`, `!reset`).
- Images: `cloudflare/cloudflared:2026.10.7`, `grafana/k6:1.8.3`.
- `ENFORCE_RATE_LIMITS=false`, `MUSICGPT_BASE_URL=http://simulator:8080`, `ENVIRONMENT=staging`.
- Project name `songforge-server`; env file `deploy/server/.env` (already ignored by the `.env` rule).
- Nothing published on a non-loopback address.
- No secret is committed or printed.
- Backend tests run from `backend/` with `.venv/bin/python -m pytest`. This machine's hook requires test runs to be prefixed with `safe-test`.
- Work on `development`; push only after the tests pass.

## Review Focus

- Re-running init: existing secrets are kept, since the Postgres and MinIO volumes already use them (Task 2).
- Hostname pasted as `https://dev.example.com/`: normalised to `dev.example.com` (Task 2).
- Missing or incomplete env file: a clear error before any container starts (Task 2).
- Nearly full disk: the load test refuses to start (Task 3).
- Wipe run by accident: nothing is removed without typing `wipe` (Task 3).

---

### Task 1: Compose overlay

**Files:**
- Create: `deploy/server/compose.yml`, `deploy/server/.env.example`
- Test: `backend/tests/test_server_overlay.py`

**Interfaces:**
- Produces: variables `PUBLIC_HOSTNAME`, `TUNNEL_TOKEN`, `POSTGRES_PASSWORD`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `SESSION_SECRET`, `WEBHOOK_SECRET`, `GRAFANA_ADMIN_PASSWORD` (all required), `GLOBAL_GENERATION_CONCURRENCY` (default 50); Compose profile `tunnel`.

- [ ] **Step 1: Write the failing tests.** A module fixture runs `docker compose -f docker-compose.yml -f deploy/server/compose.yml --env-file <tmp> --profile tunnel --profile monitoring config --format json` from the repo root, with dummy values and `PUBLIC_HOSTNAME=dev.example.com`. Skip if `docker compose` is missing.

```python
def test_only_loopback_ports(cfg):
    pub = {n: s["ports"] for n, s in cfg["services"].items() if s.get("ports")}
    assert set(pub) == {"lb", "frontend", "prometheus", "grafana"}
    assert all(p["host_ip"] == "127.0.0.1" for ports in pub.values() for p in ports)

def test_long_running_services_restart(cfg):
    for name, svc in cfg["services"].items():
        assert svc["restart"] == ("no" if name == "migrate" else "unless-stopped"), name

def test_backend_env(cfg):
    for name in ("web", "worker", "migrate"):
        env = cfg["services"][name]["environment"]
        assert env["ENVIRONMENT"] == "staging"
        assert env["ENFORCE_RATE_LIMITS"] == "false"
        assert env["MUSICGPT_BASE_URL"] == "http://simulator:8080"
        assert env["S3_PUBLIC_BASE_URL"] == "https://dev.example.com/songforge-audio"
        assert env["S3_PRESIGN_ENDPOINT_URL"] == "https://dev.example.com"
        assert env["GLOBAL_GENERATION_CONCURRENCY"] == "50"
        assert env["WEBHOOK_SECRET"] == "dummy-webhook"

def test_base_settings_survive_merge(cfg):
    s = cfg["services"]
    assert s["web"]["environment"]["DB_PGBOUNCER_TRANSACTION_MODE"] == "true"
    assert "@postgres:5432" in s["worker"]["environment"]["DATABASE_URL"]

def test_tunnel(cfg):
    cf = cfg["services"]["cloudflared"]
    assert cf["image"] == "cloudflare/cloudflared:2026.10.7"
    assert cf["environment"]["TUNNEL_TOKEN"] == "dummy-token"

@pytest.mark.parametrize("var", REQUIRED)  # the eight required variables
def test_missing_variable_fails(var): ...  # config exits non-zero and stderr names var
```

- [ ] **Step 2: Run** `cd backend && .venv/bin/python -m pytest tests/test_server_overlay.py -v`. Expected: FAIL, overlay not found.
- [ ] **Step 3: Write `deploy/server/compose.yml`.** `name: songforge-server`; `ports: !reset []` for postgres, redis, minio, simulator; `ports: !override` with `127.0.0.1:` for lb and frontend; `restart: unless-stopped` everywhere except migrate. One YAML anchor holds the environment asserted above for web, worker and migrate. Required variables use `${VAR:?set in deploy/server/.env}`. `cloudflared`: profile `tunnel`, command `tunnel --no-autoupdate run`, `depends_on` frontend and minio healthy. `WEBHOOK_SECRET` and `GLOBAL_GENERATION_CONCURRENCY` must be set here because the base file does not forward them.
- [ ] **Step 4: Write `deploy/server/.env.example`** with every variable, blank secrets, and a comment saying not to copy the laptop `.env` (it holds the real MusicGPT key).
- [ ] **Step 5: Run** the Step 2 command. Expected: PASS. Also run `docker compose --env-file /dev/null config -q`. Expected: exit 0, so the laptop stack still parses.
- [ ] **Step 6: Commit** `feat(server): compose overlay for the home dev server`.

### Task 2: Init and up scripts

**Files:**
- Create: `scripts/server-lib.sh`, `scripts/server-init.sh`, `scripts/server-up.sh`
- Test: `backend/tests/test_server_scripts.py`

**Interfaces:**
- Consumes: the variables and `tunnel` profile from Task 1.
- Produces, in `server-lib.sh`: `dc <args...>` (Compose with both files, the env file and the profiles), `require_env` (exits 1 naming the first blank required variable), `require_compose` (exits 1 below 2.24.4). Overrides: `SERVER_ENV_FILE` (env path), `SERVER_PROFILES` (default `tunnel monitoring`).

- [ ] **Step 1: Write the failing tests**, with a fake `docker` first on `PATH` that records its arguments and reports Compose version `2.40.0` (overridable).

```python
def test_init_writes_secrets(run, env_file):
    run("server-init.sh", "https://dev.example.com/", stdin="tok\n")
    env = parse(env_file)
    assert env["PUBLIC_HOSTNAME"] == "dev.example.com"
    assert env["TUNNEL_TOKEN"] == "tok"
    assert all(len(env[k]) == 40 for k in SECRETS)  # the six secrets minus S3_ACCESS_KEY_ID
    assert len(env["S3_ACCESS_KEY_ID"]) == 16
    assert env_file.stat().st_mode & 0o777 == 0o600

def test_init_keeps_existing_secrets(run, env_file): ...   # second run: file bytes unchanged, exit 0
def test_init_does_not_print_secrets(run, env_file): ...   # no secret value in stdout or stderr
def test_up_without_env_fails_before_docker(run): ...      # exit 1, mentions server-init.sh, no `up` call
def test_up_with_blank_token_fails(run, env_file): ...     # exit 1, names TUNNEL_TOKEN, no `up` call
def test_up_rejects_old_compose(run, env_file): ...        # version 2.20.0: exit 1, mentions 2.24.4
def test_up_invokes_compose(run, env_file): ...            # args contain both profiles and "up -d --build --remove-orphans"
```

- [ ] **Step 2: Run** `cd backend && .venv/bin/python -m pytest tests/test_server_scripts.py -v`. Expected: FAIL, scripts missing.
- [ ] **Step 3: Implement.** `server-init.sh <hostname>` reads the token silently from stdin, makes secrets with `openssl rand -hex 20` (URL-safe, since the Postgres password goes in a URL) and the access key with `-hex 8`, and creates the file under `umask 077`. `server-up.sh` runs `require_compose`, `require_env`, `dc up -d --build --remove-orphans`, then `dc ps`. All scripts use `set -euo pipefail`.
- [ ] **Step 4: Run** the Step 2 command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(server): init and up scripts`.

### Task 3: Load-test and wipe scripts

**Files:**
- Create: `scripts/server-loadtest.sh`, `scripts/server-wipe.sh`
- Modify: `backend/tests/test_server_scripts.py`

**Interfaces:**
- Consumes: `dc`, `require_env` from Task 2.
- Produces: `server-loadtest.sh <script-name>` (no `.js`), environment `TARGET=lb|frontend` (default `lb`), `FORCE=1`, `MIN_FREE_GB` (default 30); `server-wipe.sh`.

- [ ] **Step 1: Write the failing tests.** The fixture puts a `cookies.json` in place so nothing is minted.

```python
def test_loadtest_runs_k6(run, env_file): ...          # grafana/k6:1.8.3, --network host, --ulimit nofile=65536:65536, BASE_URL=http://localhost:8000, /scripts/create-rate.js
def test_loadtest_frontend_target(run, env_file): ...  # TARGET=frontend: BASE_URL=http://localhost:3000
def test_loadtest_forwards_tunables(run, env_file): ... # TARGET_USERS=50 becomes -e TARGET_USERS=50
def test_loadtest_unknown_script(run, env_file): ...   # exit 1, lists available names, no docker run
def test_loadtest_low_disk_refuses(run, env_file): ... # MIN_FREE_GB=999999: exit 1, no docker run
def test_loadtest_force_overrides_disk(run, env_file): ... # same with FORCE=1: docker run happens
def test_wipe_requires_confirmation(run, env_file): ... # stdin "y": exit 1, no docker call
def test_wipe_resets_volumes(run, env_file): ...       # stdin "wipe": "down --volumes", then "up -d --build --remove-orphans"
```

- [ ] **Step 2: Run** the Task 2 test command. Expected: the new tests FAIL.
- [ ] **Step 3: Implement.** Disk check: `df --output=avail -BG` on the path from `docker info -f '{{.DockerRootDir}}'` (the fake returns `/`). Forward these when set: `TARGET_RATE_PER_SEC DURATION PRE_ALLOCATED_VUS MAX_VUS HOLD_OPEN_SECONDS TARGET_USERS ARRIVALS_PER_SEC STEADY_MINUTES ACTIONS_PER_USER HOLD`. If the cookie file (`SERVER_COOKIE_FILE`, default `loadtest/cookies.json`) is missing or holds `["none"]`, use `python3` to request `$BASE_URL/quota` 100 times and write the `sf_uid` values from `Set-Cookie` as a JSON list (mode 644, so the k6 user can read it). Mount it at `/cookies.json` and pass `COOKIE_FILE=/cookies.json`. Wipe confirms by reading the exact word `wipe`.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(server): k6 and wipe scripts`.

### Task 4: Local end-to-end check

**Files:** none; fix earlier files if something fails.

- [ ] **Step 1:** With a throwaway `SERVER_ENV_FILE` (dummy token) and `SERVER_PROFILES=monitoring`, run `server-up.sh` on the laptop.
- [ ] **Step 2:** `curl -fsS localhost:8000/health/ready` and `localhost:3000/api/health`. Expected: 200.
- [ ] **Step 3:** `timeout 40 curl -sN --compressed localhost:3000/events`. Expected: data arrives within 35s, so compression is not buffering SSE.
- [ ] **Step 4:** `ss -ltn`. Expected: the stack's ports bind only `127.0.0.1`.
- [ ] **Step 5:** `TARGET_RATE_PER_SEC=1 DURATION=30s scripts/server-loadtest.sh create-rate`. Expected: k6 passes and `songforge_ingest_completed_total` rises.
- [ ] **Step 6:** Run the wipe. Expected: the stack returns healthy and `/now-playing` gives a seeded track. Then remove the test stack with its volumes.

### Task 5: Operator guide and push

**Files:**
- Create: `docs/server-deploy.md`

- [ ] **Step 1: Write the guide:** prerequisites; `git checkout development`; init; creating the tunnel (type Docker, copy the token only); an Access application for the hostname allowing only your email, created **before** the routes; the routes **in this order**: `dev.<domain>` with path `^/songforge-audio/` to `http://minio:9000`, then `dev.<domain>` with no path to `http://frontend:3000`; up; `git pull` updates; reaching Grafana with `ssh -L 59091:localhost:59091`; load tests; wipe; troubleshooting.
- [ ] **Step 2: Add the acceptance list:** Access login appears in a private window; `/songforge-audio/` is also behind it; the radio updates for over 5 minutes; seeking returns 206; a download saves; the stack returns after `sudo reboot`; from the laptop, connections to the server on 5432, 6379, 9000, 8000, 3000 and 8080 are all refused.
- [ ] **Step 3: Run** the full backend suite. Expected: PASS.
- [ ] **Step 4: Commit** `docs(server): operator guide` and push `development`.

### Task 6: On the server (you run this)

- [ ] **Step 1:** Check `docker compose version` is at least 2.24.4, then follow `docs/server-deploy.md`.
- [ ] **Step 2:** Work through the acceptance list, SSE first, and send back anything that fails.
