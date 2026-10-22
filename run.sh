#!/usr/bin/env bash
# Start Filter Bubble locally. Data and taste memory stay in ./data/filter-bubble.db.
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-$PWD/.pycache}"
exec python3 app.py serve
