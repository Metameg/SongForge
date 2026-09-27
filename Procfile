# Heroku/Railway-style process types. Issue #19 (PRD #6 AC#4): the greenfield
# `songforge` package (backend/pyproject.toml [project.scripts]) replaces the OLD
# Flask/eventlet app this file used to launch (`gunicorn ... run:app`).
#
# `release` runs once, before `web`/`worker` start, and must succeed (block startup on
# failure) -- see docs/deploy.md for how Railway's own "Deploy > Release Command"
# setting is the actual mechanism used there; this line is kept for Heroku-style
# tooling / documentation parity.
web: songforge-web
worker: songforge-worker
release: songforge-boot
