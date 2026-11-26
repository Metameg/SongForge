#!/usr/bin/env bash
# Build and (re)start the home dev server stack, then show its status.
#
#   git pull && scripts/server-up.sh
#
# SERVER_PROFILES (default "tunnel monitoring") picks the Compose profiles.
set -euo pipefail
# shellcheck source=scripts/server-lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/server-lib.sh"

require_env
require_compose
dc up -d --build --remove-orphans
dc ps
