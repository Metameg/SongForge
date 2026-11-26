#!/usr/bin/env bash
# Create the home dev server's env file (deploy/server/.env, mode 600).
#
#   scripts/server-init.sh dev.example.com      # then paste the tunnel token
#
# Re-running is safe: values already in the file are kept (the Postgres and MinIO
# volumes were initialised with them); only blank or missing ones are filled. To change
# the hostname or token later, edit the file. Secret values are never printed.
set -euo pipefail
# shellcheck source=scripts/server-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/server-lib.sh"

# normalise_hostname RAW -- "https://Dev.Example.com/" -> "dev.example.com"; "" if invalid.
normalise_hostname() {
  local host="${1,,}"
  host="${host#*://}"
  host="${host%%/*}"
  local label='[a-z0-9]([a-z0-9-]*[a-z0-9])?'
  if [[ "$host" =~ ^$label(\.$label)+$ ]]; then
    printf '%s' "$host"
  fi
}

command -v openssl >/dev/null || die "openssl is required to generate secrets"

raw_host="${1-}"
host=""
if [ -n "$raw_host" ]; then
  host="$(normalise_hostname "$raw_host")"
  [ -n "$host" ] || die "'$raw_host' is not a hostname like dev.example.com"
fi

existing() { [ -f "$SERVER_ENV_FILE" ] && env_get "$SERVER_ENV_FILE" "$1" || true; }

# Values to write: keep what is there, generate the rest.
declare -A fill=()
for var in "${SERVER_REQUIRED_VARS[@]}" GLOBAL_GENERATION_CONCURRENCY; do
  [ -n "$(existing "$var")" ] && continue
  case "$var" in
    PUBLIC_HOSTNAME)
      [ -n "$host" ] || die "usage: $0 <hostname>   (e.g. dev.example.com)"
      fill[$var]="$host"
      ;;
    TUNNEL_TOKEN)
      [ -t 0 ] && printf 'Tunnel token (input hidden): ' >&2
      token=""
      IFS= read -rs token || true
      [ -t 0 ] && echo >&2
      [ -n "$token" ] || die "no tunnel token given (copy it from the tunnel's Docker command)"
      fill[$var]="$token"
      ;;
    S3_ACCESS_KEY_ID) fill[$var]="$(openssl rand -hex 8)" ;;
    GLOBAL_GENERATION_CONCURRENCY) fill[$var]="50" ;;
    *) fill[$var]="$(openssl rand -hex 20)" ;;
  esac
done

if [ "${#fill[@]}" -eq 0 ]; then
  chmod 600 "$SERVER_ENV_FILE"
  echo "$SERVER_ENV_FILE is complete; nothing changed."
  exit 0
fi

umask 077
tmp="$(mktemp "$(dirname "$SERVER_ENV_FILE")/.env.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

if [ -f "$SERVER_ENV_FILE" ]; then
  # Fill blank KEY= lines in place, keep every other line as it is.
  while IFS= read -r line || [ -n "$line" ]; do
    key="${line%%=*}"
    if [ "$line" = "$key=" ] && [ -n "${fill[$key]+set}" ]; then
      printf '%s=%s\n' "$key" "${fill[$key]}"
      unset "fill[$key]"
    else
      printf '%s\n' "$line"
    fi
  done <"$SERVER_ENV_FILE" >"$tmp"
else
  printf '# Home dev server env, made by scripts/server-init.sh. Never commit it.\n' >"$tmp"
fi
# Append whatever was missing entirely, in a stable order.
for var in "${SERVER_REQUIRED_VARS[@]}" GLOBAL_GENERATION_CONCURRENCY; do
  if [ -n "${fill[$var]+set}" ]; then
    printf '%s=%s\n' "$var" "${fill[$var]}" >>"$tmp"
  fi
done

chmod 600 "$tmp"
mv "$tmp" "$SERVER_ENV_FILE"
trap - EXIT
echo "Wrote $SERVER_ENV_FILE (mode 600). Next: scripts/server-up.sh"
