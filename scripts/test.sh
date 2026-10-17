#!/usr/bin/env bash
# Run the backend test suite from the repo root or backend/.
set -euo pipefail
cd "$(dirname "$0")/../backend"
exec .venv/bin/python -m pytest "$@"
