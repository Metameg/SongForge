# Home dev server

Runs SongForge from `development` on a home Ubuntu Server LTS box, as a free stand-in for
Railway staging. It uses the MusicGPT simulator, rate limits are off, and the only way in
from outside is a Cloudflare named tunnel behind Cloudflare Access. No router ports are
opened. The k6 load tests run on the server itself, against localhost.

Design: [`docs/superpowers/specs/2026-11-23-home-server-design.md`](superpowers/specs/2026-11-23-home-server-design.md).

## What runs

| Piece | Where |
|---|---|
| Base stack | `docker-compose.yml`, unchanged |
| Server overlay | `deploy/server/compose.yml`: `lb` as the front door, 2 frontend replicas, no datastore ports, restarts, simulator, `cloudflared` |
| Front door | `deploy/server/nginx.conf` (mounted over `loadbalancer/nginx.conf` on the server only) |
| Secrets | `deploy/server/.env` on the server only, mode 600, git-ignored |
| Scripts | `scripts/server-init.sh`, `server-up.sh`, `server-loadtest.sh`, `server-wipe.sh` |

Request path, mirroring Railway (whose edge balances the public frontend service, which
calls `web` over the private network):

```
Cloudflare → cloudflared → lb:80 (nginx)
                             ├─ /songforge-audio/*  → minio:9000   (playback, seeking, downloads)
                             └─ everything else     → frontend:3000 × 2 replicas
                                                        → web:8000 × 2 (frontend picks a replica)
```

Ports on the server, all bound to `127.0.0.1`: `8080` (lb, the front door), `59090`
(Prometheus), `59091` (Grafana). Nothing else publishes a port.

## Prerequisites

- Ubuntu Server LTS with Docker Engine and the Compose plugin **≥ 2.24.4**
  (`docker compose version`). Use Docker's own apt repository; Ubuntu's `docker.io`
  package may be older.
- Docker starts on boot: `sudo systemctl enable --now docker`.
- Your user is in the `docker` group (`sudo usermod -aG docker $USER`, then log in again).
- `git`, `openssl`, `python3` (all present on a stock Ubuntu Server).
- A domain on Cloudflare, with Zero Trust enabled (the free plan is enough).
- About 30 GB free for Docker before load testing (see [Load tests](#load-tests)).

## 1. Get the code

```bash
git clone https://github.com/Metameg/SongForge.git
cd SongForge
git checkout development
```

## 2. Create the tunnel and copy its token

In the Cloudflare dashboard: **Zero Trust → Networks → Tunnels → Create a tunnel →
Cloudflared**. Name it (e.g. `songforge-home`). On the install step choose **Docker** and
copy **only the token**, the long string after `--token`. Do not run the command it
shows; the stack runs `cloudflared` itself.

Do not add any routes yet.

## 3. Write the env file

```bash
scripts/server-init.sh dev.<domain>
# paste the tunnel token at the prompt (input is hidden)
```

This creates `deploy/server/.env` (mode 600) with the hostname, the token and freshly
generated secrets. It never prints a secret. Re-running it keeps every value already in
the file, because the Postgres and MinIO volumes were initialised with them. To change
the hostname or token later, edit the file.

Do not copy the laptop's root `.env` here. It holds the real MusicGPT key.

## 4. Protect the hostname with Access (before any route)

**Zero Trust → Access → Applications → Add an application → Self-hosted.**

- Application domain: `dev.<domain>`, with no path, so it covers everything, including
  `/songforge-audio/`.
- Policy: action **Allow**, include **Emails** = your email address. Nothing else.

Do this first. Once a route exists, the hostname is public, so Access needs to be there
already.

## 5. Add the route

On the tunnel: **Public hostnames (Published application routes) → Add**.

- Subdomain `dev`, domain `<domain>`, **no path**, service **HTTP** `lb:80`.

That is the only route: nginx splits audio from the app itself. Leave **HTTP Host
Header** blank. Presigned download URLs are signed for `dev.<domain>`, so MinIO has to
see that host, and nginx passes it through.

**Upgrading from the two-route setup** (`^/songforge-audio/` → `minio:9000` and
`frontend:3000`): after `git pull && scripts/server-up.sh`, delete both old routes and
add the one above. The old routes would still answer (Docker still resolves `frontend`
and `minio`), but they bypass nginx, so visitors would skip the front door the load
tests measure.

## 6. Start it

```bash
scripts/server-up.sh
```

This builds and starts everything (tunnel and monitoring included), then prints
`docker compose ps`. The first build takes a few minutes. Once `cloudflared` is up, the
tunnel shows **Healthy** in the dashboard. Every service restarts on its own after a
reboot.

## Updating

```bash
git pull
scripts/server-up.sh
```

There is no auto-deploy.

## Running Compose by hand

The scripts wrap Compose with the right files, env file and profiles. To use it directly:

```bash
bash -c 'source scripts/server-lib.sh && dc logs -f cloudflared'
bash -c 'source scripts/server-lib.sh && dc ps'
```

## Grafana and Prometheus

Both listen on the server's loopback only. From your laptop:

```bash
ssh -L 59091:localhost:59091 -L 59090:localhost:59090 <user>@<server>
```

Then open <http://localhost:59091>. User `admin`, password `GRAFANA_ADMIN_PASSWORD` from
`deploy/server/.env`.

## Load tests

```bash
scripts/server-loadtest.sh create-rate
TARGET_RATE_PER_SEC=5 DURATION=2m scripts/server-loadtest.sh create-rate
TARGET_USERS=200 scripts/server-loadtest.sh realistic-session
MAX_VUS=4000 scripts/server-loadtest.sh connection-ceiling
```

- The argument is a file in `loadtest/` without `.js`. Run the script with no argument
  to list them. See `loadtest/README.md` for what each one measures.
- k6 hits `http://localhost:8080` (`TARGET=edge-origin`, the only target): the same
  front door cloudflared uses, so every request takes a visitor's path, lb → frontend
  replicas → web, minus Cloudflare itself.
- nginx is sized for held-open streams in `deploy/server/nginx.conf`
  (`worker_processes auto`, `worker_connections`, `worker_rlimit_nofile`). Each live
  listener holds two nginx connections.
- When set, these are passed through to k6: `TARGET_RATE_PER_SEC DURATION
  PRE_ALLOCATED_VUS MAX_VUS HOLD_OPEN_SECONDS TARGET_USERS ARRIVALS_PER_SEC
  STEADY_MINUTES ACTIONS_PER_USER HOLD`.
- Identities: if `loadtest/cookies.json` is missing, or holds `["none"]`, 100 are minted
  from `/quota` first. The wipe deletes the file, because the users it names are gone.
- **Disk:** each simulated song is a full-size MP3. At the 50-job cap and 5 s delay that
  is up to about 24 GB per 10 minutes, so the script refuses to start with less than
  `MIN_FREE_GB` (default 30) free under Docker's root. `FORCE=1` skips the check. Watch
  the space and wipe often.

Load tests run against localhost, not through the tunnel: Cloudflare's free plan isn't
meant for that traffic, and the tunnel would cap the numbers.

## Wiping simulated songs

```bash
scripts/server-wipe.sh      # type: wipe
```

This removes the Postgres, Redis and MinIO volumes together, plus
`loadtest/cookies.json`, then restarts the stack, which reseeds the static library. Anything other than the exact word `wipe` aborts
without removing anything. Secrets in `deploy/server/.env` are kept and reused.

## Troubleshooting

| Symptom | Check |
|---|---|
| `server-up.sh`: "... is blank or missing" | Run `scripts/server-init.sh dev.<domain>` or fill the named variable in `deploy/server/.env`. |
| `server-up.sh`: "docker compose ... is too old" | Install Compose ≥ 2.24.4 from Docker's apt repository. |
| Tunnel not Healthy | `dc logs cloudflared`. A bad token logs an auth error; fix `TUNNEL_TOKEN` and run `server-up.sh` again. `cloudflared` only starts once `lb` is healthy, and `lb` waits for `frontend` and `minio` (`dc ps`). |
| 502 / 1033 at `dev.<domain>` | The route must be exactly `lb:80`, type HTTP. Then `dc logs lb`. |
| Page loads, audio doesn't (404 or HTML) | A leftover `^/songforge-audio/` route still points at MinIO directly, or nginx can't reach it: `dc logs lb minio`. |
| Download fails with `SignatureDoesNotMatch` | The route sets **HTTP Host Header**; clear it. |
| Load test plateaus or fails fast | `dc logs lb \| grep -E 'worker_connections\|Too many open files'` names an nginx limit; otherwise check `docker stats` for a pinned `frontend` or `web`. |
| Radio stops updating | `dc logs lb web frontend`. SSE sends a heartbeat every 30 s, well under Cloudflare's 125 s timeout. A browser extension that buffers responses can also do this. |
| Login loop / no Access prompt | The Access application's domain must be exactly `dev.<domain>` with no path. |
| Postgres auth errors after editing secrets | The volumes keep the old passwords. Restore the old values, or wipe. |
| Disk filling up | `scripts/server-wipe.sh`; `docker system prune` for old images. |

## Acceptance list

After the first deploy, check each item. Start with SSE.

- [ ] In a private browser window, `https://dev.<domain>` shows the Cloudflare Access
      login, not the app.
- [ ] `https://dev.<domain>/songforge-audio/` in a private window also shows the Access
      login.
- [ ] After logging in, the radio updates for over 5 minutes without a reload.
- [ ] Seeking in the player works. In devtools, the audio request returns **206**.
- [ ] A song download saves a playable file.
- [ ] After `sudo reboot`, the stack comes back on its own and the site works again.
- [ ] From the laptop, connections to the server on 5432, 6379, 9000, 8000, 3000 and
      8080 are all refused, for example
      `for p in 5432 6379 9000 8000 3000 8080; do nc -zv -w 3 <server> $p; done`.
