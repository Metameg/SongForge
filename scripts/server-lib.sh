# shellcheck shell=bash
# Shared helpers for the home dev server scripts (scripts/server-*.sh). Source it; do
# not run it. See docs/server-deploy.md.
#
# Overrides:
#   SERVER_ENV_FILE   env file to use (default: deploy/server/.env)
#   SERVER_PROFILES   space-separated Compose profiles (default: "tunnel monitoring")
#   SERVER_COOKIE_FILE  k6 identities (default: loadtest/cookies.json)

SERVER_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
SERVER_ENV_FILE="${SERVER_ENV_FILE:-$SERVER_REPO/deploy/server/.env}"
SERVER_COOKIE_FILE="${SERVER_COOKIE_FILE:-$SERVER_REPO/loadtest/cookies.json}"
SERVER_PROFILES="${SERVER_PROFILES-tunnel monitoring}"
SERVER_MIN_COMPOSE="2.24.4"

# Every variable deploy/server/compose.yml marks as required.
SERVER_REQUIRED_VARS=(
  PUBLIC_HOSTNAME
  TUNNEL_TOKEN
  POSTGRES_PASSWORD
  S3_ACCESS_KEY_ID
  S3_SECRET_ACCESS_KEY
  SESSION_SECRET
  WEBHOOK_SECRET
  GRAFANA_ADMIN_PASSWORD
)

die() {
  echo "error: $*" >&2
  exit 1
}

# env_get FILE KEY -- the value of the last KEY= line in FILE ("" if absent). The file is
# parsed, never sourced, so values are taken literally.
env_get() {
  local line
  line="$(grep -E "^$2=" "$1" 2>/dev/null | tail -n 1 || true)"
  printf '%s' "${line#*=}"
}

# dc ARGS... -- docker compose with the base file, the server overlay, the env file and
# the selected profiles.
dc() {
  local args=(-f "$SERVER_REPO/docker-compose.yml" -f "$SERVER_REPO/deploy/server/compose.yml"
    --env-file "$SERVER_ENV_FILE")
  local profile
  for profile in $SERVER_PROFILES; do
    args+=(--profile "$profile")
  done
  docker compose "${args[@]}" "$@"
}

# require_env -- exit 1 (naming the variable, never its value) unless the env file exists
# and every required variable is non-blank.
require_env() {
  [ -f "$SERVER_ENV_FILE" ] \
    || die "$SERVER_ENV_FILE not found -- run scripts/server-init.sh <hostname> first"
  local var
  for var in "${SERVER_REQUIRED_VARS[@]}"; do
    [ -n "$(env_get "$SERVER_ENV_FILE" "$var")" ] \
      || die "$var is blank or missing in $SERVER_ENV_FILE -- run scripts/server-init.sh or set it"
  done
}

# require_compose -- exit 1 unless Docker Compose is at least $SERVER_MIN_COMPOSE (the
# overlay's !reset / !override tags need it).
require_compose() {
  local version
  version="$(docker compose version --short 2>/dev/null || true)"
  version="${version#v}"
  version="${version%%-*}"
  [ -n "$version" ] || die "docker compose not found (need >= $SERVER_MIN_COMPOSE)"
  local lowest
  lowest="$(printf '%s\n%s\n' "$SERVER_MIN_COMPOSE" "$version" | sort -V | head -n 1)"
  [ "$lowest" = "$SERVER_MIN_COMPOSE" ] \
    || die "docker compose $version is too old; need >= $SERVER_MIN_COMPOSE"
}
