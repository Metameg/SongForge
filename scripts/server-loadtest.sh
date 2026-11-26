#!/usr/bin/env bash
# Run one of the k6 scripts in loadtest/ on the server itself, against localhost.
#
#   scripts/server-loadtest.sh create-rate
#   TARGET=frontend TARGET_USERS=50 scripts/server-loadtest.sh realistic-session
#
# Environment:
#   TARGET        lb (default, http://localhost:8000) or frontend (http://localhost:3000)
#   BASE_URL      overrides the URL TARGET picks
#   MIN_FREE_GB   refuse to start below this much free space for Docker (default 30;
#                 simulated songs are full-size MP3s). FORCE=1 skips the check.
#   SERVER_COOKIE_FILE   identities for k6 (default loadtest/cookies.json); minted from
#                 $BASE_URL/quota when missing or ["none"].
#   Script tunables are forwarded when set: TARGET_RATE_PER_SEC DURATION
#   PRE_ALLOCATED_VUS MAX_VUS HOLD_OPEN_SECONDS TARGET_USERS ARRIVALS_PER_SEC
#   STEADY_MINUTES ACTIONS_PER_USER HOLD
set -euo pipefail
# shellcheck source=scripts/server-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/server-lib.sh"

K6_IMAGE="grafana/k6:1.8.3"
COOKIE_COUNT=100
TUNABLES=(TARGET_RATE_PER_SEC DURATION PRE_ALLOCATED_VUS MAX_VUS HOLD_OPEN_SECONDS
  TARGET_USERS ARRIVALS_PER_SEC STEADY_MINUTES ACTIONS_PER_USER HOLD)

available_scripts() {
  local f names=()
  for f in "$SERVER_REPO"/loadtest/*.js; do
    names+=("$(basename "$f" .js)")
  done
  echo "${names[*]}"
}

name="${1-}"
if ! [[ "$name" =~ ^[A-Za-z0-9_-]+$ ]] || [ ! -f "$SERVER_REPO/loadtest/$name.js" ]; then
  die "usage: $0 <script>   (one of: $(available_scripts))"
fi

case "${TARGET:-lb}" in
  lb) default_url="http://localhost:8000" ;;
  frontend) default_url="http://localhost:3000" ;;
  *) die "TARGET must be lb or frontend, not '${TARGET}'" ;;
esac
base_url="${BASE_URL:-$default_url}"

require_env

if [ "${FORCE:-0}" != "1" ]; then
  min_gb="${MIN_FREE_GB:-30}"
  root="$(docker info -f '{{.DockerRootDir}}')" \
    || die "cannot reach the Docker daemon (is it running, and are you in the docker group?)"
  avail="$(df --output=avail -BG "$root" | tail -n 1)"
  avail="${avail//[!0-9]/}"
  if [ "${avail:-0}" -lt "$min_gb" ]; then
    die "only ${avail:-0} GB free under $root (need $min_gb GB; simulated songs are full-size). Free space, run scripts/server-wipe.sh, or set FORCE=1"
  fi
fi

cookie_dir="$(cd "$(dirname "$SERVER_COOKIE_FILE")" && pwd)" \
  || die "directory for SERVER_COOKIE_FILE ($SERVER_COOKIE_FILE) does not exist"
cookie_file="$cookie_dir/$(basename "$SERVER_COOKIE_FILE")"
if [ ! -s "$cookie_file" ] || [ "$(tr -d '[:space:]' <"$cookie_file")" = '["none"]' ]; then
  echo "Minting $COOKIE_COUNT identities from $base_url/quota ..." >&2
  tmp="$(mktemp "$cookie_file.XXXXXX")"
  trap 'rm -f "$tmp"' EXIT
  python3 -I - "$base_url" "$COOKIE_COUNT" "$tmp" <<'PY'
import json
import sys
import urllib.request

base, count, out = sys.argv[1].rstrip("/"), int(sys.argv[2]), sys.argv[3]
values = []
for _ in range(count):
    with urllib.request.urlopen(f"{base}/quota", timeout=10) as resp:
        for header in resp.headers.get_all("Set-Cookie") or []:
            name, _, value = header.split(";", 1)[0].strip().partition("=")
            if name == "sf_uid" and value:
                values.append(value)
if len(values) != count:
    sys.exit(f"expected {count} sf_uid cookies from {base}/quota, got {len(values)}")
with open(out, "w") as f:
    json.dump(values, f)
PY
  chmod 644 "$tmp"
  mv "$tmp" "$cookie_file"
  trap - EXIT
fi

args=(run --rm --network host --ulimit nofile=65536:65536
  -v "$SERVER_REPO/loadtest:/scripts:ro"
  -v "$cookie_file:/cookies.json:ro"
  -e "BASE_URL=$base_url"
  -e COOKIE_FILE=/cookies.json)
for var in "${TUNABLES[@]}"; do
  if [ -n "${!var+set}" ]; then
    args+=(-e "$var=${!var}")
  fi
done
docker "${args[@]}" "$K6_IMAGE" run "/scripts/$name.js"
