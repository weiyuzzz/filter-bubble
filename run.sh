#!/usr/bin/env bash
# Start GitPulse locally. Data and taste memory stay in ./data/gitpulse.db.
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-$PWD/.pycache}"
exec python3 app.py serve
