#!/usr/bin/env python3
"""Portable daily update (Windows equivalent of scripts/daily-update.sh).

Exit codes: 0 = today's digest is ready, 75 = offline (retry later), 1 = error.
Runs with the project's own runtime: runtime\\python\\python.exe scripts\\daily_update.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import config, db, digest, notify  # noqa: E402

BASE = "http://%s:%d" % (config.HOST, config.PORT)
TIMEOUT = 6 * 60


def _today() -> str:
    return digest.local_today().isoformat()


def _last_generated() -> str:
    conn = db.connect()
    try:
        return db.get_meta(conn, "last_generated_date") or ""
    finally:
        conn.close()


def _get(path: str, timeout: float = 3.0):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.status, r.read()


def _post(path: str, timeout: float = 10.0) -> int:
    req = urllib.request.Request(BASE + path, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def _server_up() -> bool:
    try:
        return _get("/api/health")[0] == 200
    except Exception:
        return False


def _online() -> bool:
    try:
        req = urllib.request.Request("https://api.github.com/rate_limit",
                                     headers={"User-Agent": config.USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def _push(quiet: bool = True) -> None:
    """Best effort: a failed WeChat brief must never fail the digest run."""
    try:
        conn = db.connect()
        try:
            payload = digest.latest(conn)
            if not payload:
                return
            result = notify.send_daily(payload, conn=conn)
            if not quiet:
                print("[push]", json.dumps(result, ensure_ascii=False))
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        print("[push] skipped:", exc)


def main() -> int:
    db.init()
    today = _today()

    if _last_generated() == today:
        _push()
        print("UP-TO-DATE:", today)
        return 0

    if not _online():
        print("OFFLINE: GitHub API is unreachable; the local scheduler will retry after the network returns.")
        return 75

    if _server_up():
        try:
            _post("/api/refresh")
        except Exception as exc:  # noqa: BLE001
            print("ERROR: could not trigger refresh:", exc)
            return 1
        deadline = time.time() + TIMEOUT
        while time.time() < deadline:
            try:
                _, body = _get("/api/status", timeout=5)
                running = bool(json.loads(body.decode("utf-8")).get("state", {}).get("running"))
            except Exception:
                running = True
            if _last_generated() == today and not running:
                _push()
                print("UPDATED:", today)
                return 0
            time.sleep(5)
        print("ERROR: refresh did not finish within %d minutes" % (TIMEOUT // 60))
        return 1

    print("[info] local service is not running — building the digest directly")
    payload = digest.generate(progress=lambda m: print("»", m, flush=True))
    _push()
    print("UPDATED:", payload["date"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
