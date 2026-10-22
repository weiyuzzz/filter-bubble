#!/usr/bin/env bash
# One deterministic daily update, suitable for a Harness task or cron.
# Exit codes: 0 = today's digest is ready, 75 = offline (retry later), 1 = error.
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-$DIR/.pycache}"

PORT="${GITPULSE_PORT:-8791}"
TODAY="$(date +%F)"
BASE="http://127.0.0.1:$PORT"

DONE="$(sqlite3 data/gitpulse.db "SELECT value FROM meta WHERE key='last_generated_date'" 2>/dev/null || true)"
if [ "$DONE" = "$TODAY" ]; then
  echo "UP-TO-DATE: $TODAY"
  exit 0
fi

if ! curl -sf --max-time 10 https://api.github.com/rate_limit >/dev/null 2>&1; then
  echo "OFFLINE: GitHub API is unreachable; the local scheduler will retry after the network returns."
  exit 75
fi

if curl -sf --max-time 3 "$BASE/api/health" >/dev/null 2>&1; then
  curl -s --max-time 10 -X POST "$BASE/api/refresh" >/dev/null 2>&1 || {
    echo "ERROR: could not trigger refresh"
    exit 1
  }
  for _ in $(seq 1 72); do
    DONE="$(sqlite3 data/gitpulse.db "SELECT value FROM meta WHERE key='last_generated_date'" 2>/dev/null || true)"
    RUNNING="$(curl -s --max-time 3 "$BASE/api/status" 2>/dev/null | grep -c '"running": true' || true)"
    if [ "$DONE" = "$TODAY" ] && [ "$RUNNING" = "0" ]; then
      echo "UPDATED: $TODAY"
      exit 0
    fi
    sleep 5
  done
  echo "ERROR: refresh did not finish within 6 minutes; see data/launchd.err.log"
  exit 1
fi

# Service is not running: build directly. The next LaunchAgent run will serve it.
if python3 app.py refresh; then
  echo "UPDATED: $TODAY"
  exit 0
fi
echo "ERROR: direct refresh failed"
exit 1
