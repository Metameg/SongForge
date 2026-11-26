#!/usr/bin/env bash
# Delete every generated song: removes the Postgres, Redis and MinIO volumes together,
# then restarts the stack, which reseeds the static library.
#
# All three go at once on purpose: deleting only MinIO objects would leave database rows
# pointing at missing audio, and the radio would keep scheduling them.
set -euo pipefail
# shellcheck source=scripts/server-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/server-lib.sh"

require_env

printf 'This deletes ALL songs, jobs and users on the server stack. Type wipe to continue: ' >&2
answer=""
IFS= read -r answer || true
[ "$answer" = "wipe" ] || die "aborted; nothing was removed"

require_compose
dc down --volumes --remove-orphans
dc up -d --build --remove-orphans
dc ps
