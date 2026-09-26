# SongForge observability: env split (issue #18, AC5)

Three environments, three different topologies for the same metrics
(`/metrics` on the `web` app, per `backend/src/songforge/metrics.py` +
`pipeline_collector.py`). None of them require a code change to switch
between -- only config.

## Local

**Self-hosted Prometheus + Grafana**, brought up via a docker-compose
`monitoring` profile so the default `docker compose up` workflow is
unaffected:

```sh
docker compose --profile monitoring up -d
```

- Prometheus: `monitoring/prometheus/prometheus.yml` -- scrapes `web:8000/metrics`,
  `postgres_exporter:9187`, `redis_exporter:9121` every **5s** (aggressive on
  purpose: this is a load-test tool, not a production scrape config -- a k6
  run's ramp shape needs to be visible on the dashboards, not smoothed away
  by a 30-60s interval). Exposed on the host at `${PROMETHEUS_HOST_PORT:-59090}`.
- Grafana: auto-provisioned via `monitoring/grafana/provisioning/` (datasource
  pointing at the compose-local Prometheus, dashboards provider loading
  everything under `monitoring/grafana/dashboards/`). Exposed on the host at
  `${GRAFANA_HOST_PORT:-59091}`, anonymous Viewer access enabled locally for
  convenience (`admin`/`admin` by default for the admin account -- **local
  only**, never carry these credentials or this anonymous-access setting to
  staging/prod).
- Ephemeral: no persistent volume for `prometheus`/`grafana` -- dashboards
  are re-provisioned fresh on every `up`, metrics history doesn't need to
  survive a restart for a load-test session.
- Config knobs (all in `.env`, see `.env.example`): `PROMETHEUS_HOST_PORT`,
  `GRAFANA_HOST_PORT`, `GRAFANA_ADMIN_USER`, `GRAFANA_ADMIN_PASSWORD`.

## Staging (on-demand, prod-tier sizing)

**Same `monitoring` profile, deployed on-demand** to a prod-tier-sized
instance for the duration of a load-test session, then torn down. Not
automated by this repo (no staging CI/CD config here) -- documented
procedure:

1. Deploy the full `docker-compose.yml` stack (including the `monitoring`
   profile) to a Railway (or equivalent) instance sized like prod, not the
   lighter local-dev sizing.
2. Point `MUSICGPT_BASE_URL` at the simulator (same as local) so load tests
   never hit the real MusicGPT API.
3. Change the Prometheus scrape interval to match prod's (see below) rather
   than local's aggressive 5s -- copy `monitoring/prometheus/prometheus.yml`
   and raise `global.scrape_interval`/`evaluation_interval` to `30s` for a
   staging run, unless the specific test needs finer resolution.
4. Run the `loadtest/` k6 scripts against the staging `BASE_URL`.
5. Tear the instance down after the session -- staging is not a
   long-running environment for this project.

## Prod

**No self-hosted Prometheus or Grafana in prod.** A lightweight
scrape-and-forward agent runs alongside the prod `web` instance(s), scrapes
their local `/metrics`, and forwards samples to **Grafana Cloud's free
tier** via `remote_write`. This keeps prod's footprint to "one more small
process," not a second stateful service to operate.

- Template config: `monitoring/prometheus/prometheus.prod.example.yml`
  (Prometheus in `remote_write`-only mode; a Grafana Agent config would be
  the equivalent minimal setup for the same target). **Never commit filled-in
  credentials** -- copy the template and fill in the two knobs below from
  environment variables or your deploy platform's secret store.
- Config knobs needed (from Grafana Cloud's stack "Connection details" >
  Prometheus page):
  - `GRAFANA_CLOUD_PROMETHEUS_REMOTE_WRITE_URL` -- the `remote_write.url`.
  - `GRAFANA_CLOUD_PROMETHEUS_USERNAME` / `GRAFANA_CLOUD_API_KEY` -- the
    `remote_write.basic_auth` credentials.
- Scrape interval: conservative (`30s` in the template) -- prod is not a
  load-test target, it's continuous low-overhead visibility.
- Dashboards: the same JSON files under `monitoring/grafana/dashboards/`
  (`pipeline.json`, `capacity-thesis.json`) can be imported directly into
  the Grafana Cloud instance -- they're plain Grafana dashboard JSON with no
  local-only assumptions (the Prometheus datasource name/UID they reference
  is the default `Prometheus`, which Grafana Cloud also uses for its own
  built-in datasource).

## Summary

| Env | Prometheus/Grafana | Scrape interval | Lifetime |
|---|---|---|---|
| Local | self-hosted (compose `monitoring` profile) | 5s | ephemeral, per dev session |
| Staging | self-hosted (same profile, prod-tier instance) | 30s | on-demand, per load-test session |
| Prod | none -- agent `remote_write`s to Grafana Cloud free tier | 30s | continuous |
