#!/usr/bin/env python3
"""Filter Bubble — a local daily GitHub intelligence digest with a taste model.

    python3 app.py serve      # start the UI at http://127.0.0.1:8791
    python3 app.py refresh    # build today's digest once (for cron/launchd)
    python3 app.py rerank     # re-sort today's digest using current feedback
    python3 app.py reset      # forget all feedback and the taste model
    python3 app.py push       # send today's Server酱 WeChat brief
"""
from __future__ import annotations

import datetime as dt
import json
import mimetypes
import os
import sqlite3
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from core import config, db, digest, follows, notify, prefs

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def _json(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _read_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        return {}
    if length <= 0 or length > 1_000_000:
        return {}
    try:
        return json.loads(handler.rfile.read(length).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


def _today_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM digests WHERE date=?", (digest.local_today().isoformat(),)
    ).fetchone()
    return bool(row)


def _status_payload() -> Dict[str, Any]:
    conn = db.connect()
    try:
        exists = _today_exists(conn)
        last = db.get_meta(conn, "last_generated_date")
        last_failed = db.get_meta(conn, "last_refresh_failed_at") or None
        push = notify.status(conn)
        row = conn.execute("SELECT date, generated_at FROM digests ORDER BY date DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    state = digest.get_state()
    now = digest.local_now()
    try:
        hh, mm = [int(x) for x in config.REFRESH_TIME.split(":")]
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        next_run = target.isoformat(timespec="minutes")
    except ValueError:
        next_run = None
    return {
        "state": state,
        "today_ready": exists,
        "last_generated_date": last,
        "last_refresh_failed_at": last_failed,
        "push": push,
        "latest_date": row["date"] if row else None,
        "latest_generated_at": row["generated_at"] if row else None,
        "schedule": {
            "time": config.REFRESH_TIME,
            "timezone": config.TIMEZONE,
            "next_run": next_run,
        },
        "github_api_remaining": None,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "FilterBubble/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter server logs
        if "/api/" in (self.path or ""):
            sys.stderr.write("[web] %s\n" % (fmt % args))

    # ---------- static ----------
    def _static(self, path: str) -> None:
        if path in ("/", ""):
            path = "/index.html"
        rel = urllib.parse.unquote(path).lstrip("/")
        if ".." in rel.split("/"):
            self.send_error(403)
            return
        full = (config.WEB_DIR / rel).resolve()
        try:
            full.relative_to(config.WEB_DIR.resolve())
        except ValueError:
            self.send_error(403)
            return
        if not full.is_file():
            full = config.WEB_DIR / "index.html"
        data = full.read_bytes()
        ext = os.path.splitext(str(full))[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # ---------- radar (independent module; failures never touch the GitHub flow) ----------
    def _radar(self, path: str, parsed, body: Optional[Dict[str, Any]]):
        try:
            from core.radar import api as radar_api
        except Exception as exc:  # noqa: BLE001
            print("[radar] unavailable:", exc)
            return _json(self, 503, {"error": "radar module unavailable: %s" % exc})
        try:
            if body is None:
                query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                status, payload = radar_api.handle_get(path, query)
            else:
                status, payload = radar_api.handle_post(path, body)
        except Exception as exc:  # noqa: BLE001
            print("[radar] handler error:", exc)
            status, payload = 500, {"error": str(exc)}
        return _json(self, status, payload)

    # ---------- API ----------
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/radar" or path.startswith("/api/radar/"):
            return self._radar(path, parsed, None)
        if path == "/api/health":
            return _json(self, 200, {"ok": True, "time": db.now_iso()})
        if path == "/api/status":
            return _json(self, 200, _status_payload())
        if path == "/api/digest":
            conn = db.connect()
            try:
                payload = digest.latest(conn)
            finally:
                conn.close()
            if not payload:
                return _json(self, 202, {"pending": True, "status": _status_payload()})
            return _json(self, 200, payload)
        if path == "/api/prefs":
            conn = db.connect()
            try:
                return _json(self, 200, prefs.profile(conn))
            finally:
                conn.close()
        if path == "/api/push":
            conn = db.connect()
            try:
                return _json(self, 200, notify.status(conn))
            finally:
                conn.close()
        if path == "/api/follows":
            conn = db.connect()
            try:
                query = urllib.parse.parse_qs(parsed.query)
                force = query.get("refresh", ["0"])[0] in ("1", "true", "yes")
                if force:
                    follows.refresh(conn, force=True, max_seconds=25)
                return _json(self, 200, follows.summary(conn))
            except Exception as exc:  # noqa: BLE001
                print("[api] follows error:", exc)
                return _json(self, 500, {"error": str(exc)})
            finally:
                conn.close()
        if path.startswith("/api/"):
            return _json(self, 404, {"error": "not found"})
        return self._static(path)

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = _read_body(self)
        if path.startswith("/api/radar/"):
            return self._radar(path, parsed, body)
        conn = db.connect()
        try:
            if path == "/api/refresh":
                started = digest.refresh_async()
                return _json(self, 202 if started else 200, {"started": started, "status": _status_payload()})
            if path == "/api/feedback":
                return self._feedback(conn, body)
            if path == "/api/bookmark":
                return self._bookmark(conn, body)
            if path == "/api/follow":
                owner = str(body.get("owner") or "").strip()
                try:
                    result = follows.toggle(conn, owner)
                except ValueError as exc:
                    return _json(self, 400, {"error": str(exc)})
                return _json(self, 200, result)
            if path == "/api/follows/refresh":
                follows.refresh(conn, force=True, max_seconds=30)
                return _json(self, 200, follows.summary(conn))
            if path == "/api/push":
                payload = digest.latest(conn)
                if not payload:
                    return _json(self, 409, {"error": "digest not ready"})
                result = notify.send_daily(payload, conn=conn, force=bool(body.get("force")))
                return _json(self, 200, {"result": result, "push": notify.status(conn)})
            if path == "/api/rerank":
                payload = digest.rerank(conn)
                return _json(self, 200, payload)
            if path == "/api/reset":
                conn.execute("DELETE FROM feedback")
                conn.execute("DELETE FROM weights")
                db.set_meta(conn, "bias", 0)
                db.set_meta(conn, "feedback_active", 0)
                conn.commit()
                payload = digest.rerank(conn)
                return _json(self, 200, payload)
            return _json(self, 404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            print("[api] error:", exc)
            return _json(self, 500, {"error": str(exc)})
        finally:
            conn.close()

    def _feedback(self, conn, body: Dict[str, Any]):
        repo = str(body.get("repo") or "").strip()
        try:
            signal = int(body.get("signal"))
        except (TypeError, ValueError):
            return _json(self, 400, {"error": "signal must be -1, 0 or 1"})
        if signal not in (-1, 0, 1):
            return _json(self, 400, {"error": "signal must be -1, 0 or 1"})
        payload = digest.latest(conn)
        if not payload:
            return _json(self, 409, {"error": "digest not ready"})
        item = next((i for i in payload["items"] if i["full_name"] == repo), None)
        if not item:
            return _json(self, 404, {"error": "repo not in today's digest"})
        conn.execute(
            "INSERT INTO feedback(full_name, signal, features, created_at) VALUES(?,?,?,?)",
            (repo, signal, json.dumps(item.get("features") or [], ensure_ascii=False), db.now_iso()),
        )
        conn.commit()
        prefs.rebuild(conn)
        db.set_meta(conn, "profile_updated", db.now_iso())
        conn.commit()
        payload = digest.rerank(conn)
        return _json(self, 200, payload)

    def _bookmark(self, conn, body: Dict[str, Any]):
        repo = str(body.get("repo") or "").strip()
        payload = digest.latest(conn)
        if not payload:
            return _json(self, 409, {"error": "digest not ready"})
        existing = conn.execute("SELECT 1 FROM bookmarks WHERE full_name=?", (repo,)).fetchone()
        if existing:
            conn.execute("DELETE FROM bookmarks WHERE full_name=?", (repo,))
            marked = False
        else:
            conn.execute("INSERT INTO bookmarks(full_name, created_at) VALUES(?,?)", (repo, db.now_iso()))
            marked = True
        conn.commit()
        for item in payload.get("items", []):
            if item["full_name"] == repo:
                item["bookmarked"] = marked
        conn.execute(
            "INSERT OR REPLACE INTO digests(date, payload, generated_at) VALUES(?,?,?)",
            (payload["date"], json.dumps(payload, ensure_ascii=False), payload["generated_at"]),
        )
        conn.commit()
        return _json(self, 200, {"bookmarked": marked, "repo": repo})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _scheduler(stop: threading.Event) -> None:
    while not stop.wait(25):
        try:
            conn = db.connect()
            try:
                done_today = db.get_meta(conn, "last_generated_date") == digest.local_today().isoformat()
                last_failed = db.get_meta(conn, "last_refresh_failed_at", "") or ""
            finally:
                conn.close()
            now = digest.local_now()
            if done_today or digest.get_state().get("running"):
                continue
            if last_failed:
                try:
                    failed_at = dt.datetime.fromisoformat(last_failed)
                    if (now - failed_at).total_seconds() < 600:
                        continue
                except (ValueError, TypeError):
                    pass
            if now.strftime("%H:%M") >= config.REFRESH_TIME:
                print("[schedule] daily refresh starting at", now.isoformat(timespec="seconds"))
                digest.refresh_async()
        except Exception as exc:  # noqa: BLE001
            print("[schedule] error:", exc)


def serve() -> None:
    db.init()
    conn = db.connect()
    try:
        ready = _today_exists(conn)
    finally:
        conn.close()
    if not ready:
        print("[startup] no digest for today — generating in the background")
        digest.refresh_async()
    stop = threading.Event()
    threading.Thread(target=_scheduler, args=(stop,), daemon=True, name="filter-bubble-scheduler").start()
    # 全球热点雷达：独立线程、独立表、独立时间表。这里的任何异常都不能影响 GitHub 服务启动。
    try:
        from core.radar import service as radar_service
        from core.radar import store as radar_store
        radar_store.init()
        radar_service.start_scheduler(stop)
    except Exception as exc:  # noqa: BLE001
        print("[radar] scheduler not started:", exc)
    httpd = Server((config.HOST, config.PORT), Handler)
    url = "http://%s:%d" % (config.HOST, config.PORT)
    print("\n  Filter Bubble  %s\n  daily refresh %s (%s)\n" % (url, config.REFRESH_TIME, config.TIMEZONE))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        stop.set()
        httpd.server_close()


def main() -> None:
    db.init()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        serve()
    elif cmd == "refresh":
        try:
            payload = digest.generate(progress=lambda m: print("»", m, flush=True))
        except Exception as exc:  # noqa: BLE001
            print("✗ 生成失败：%s" % exc, file=sys.stderr)
            sys.exit(1)
        print("✓ %s: %d repos, generated %s" % (payload["date"], len(payload["items"]), payload["generated_at"]))
    elif cmd == "rerank":
        conn = db.connect()
        try:
            payload = digest.rerank(conn)
            print("✓ reranked %d items" % len(payload["items"]))
        finally:
            conn.close()
    elif cmd == "push":
        conn = db.connect()
        try:
            payload = digest.latest(conn)
            if not payload:
                print("✗ 还没有日报")
                sys.exit(1)
            if "--dry-run" in sys.argv:
                rendered = notify.render(payload, conn)
                print("TITLE:", rendered["title"])
                print()
                print(rendered["body"])
                print()
                print("(dry-run: 未发送，也未消耗 Server酱额度)")
            else:
                result = notify.send_daily(payload, conn=conn, force="--force" in sys.argv)
                print(result)
        finally:
            conn.close()
    elif cmd == "reset":
        conn = db.connect()
        try:
            conn.execute("DELETE FROM feedback")
            conn.execute("DELETE FROM weights")
            conn.commit()
            digest.rerank(conn)
            print("✓ feedback cleared")
        finally:
            conn.close()
    elif cmd == "radar":
        # 独立入口：python app.py radar [collect|report|investigate <event_id>|query <text>]
        from core.radar import config as radar_config, investigate, service, store

        store.init()
        radar_cfg = radar_config.cached()
        sub = sys.argv[2] if len(sys.argv) > 2 else "collect"
        if sub == "collect":
            result = service.collect_and_report(radar_cfg, reason="cli")
            print(json.dumps(result.get("stats") or {}, ensure_ascii=False, default=str))
            sys.exit(0 if result.get("ok") else 1)
        if sub == "report":
            print(service.generate_report_only(radar_cfg))
            sys.exit(0)
        if sub == "investigate" and len(sys.argv) > 3:
            conn = db.connect()
            try:
                print(json.dumps(investigate.run(conn, sys.argv[3], radar_cfg), ensure_ascii=False)[:4000])
            finally:
                conn.close()
            sys.exit(0)
        if sub == "query" and len(sys.argv) > 3:
            conn = db.connect()
            try:
                print(json.dumps(investigate.run_query(conn, " ".join(sys.argv[3:]), radar_cfg),
                                 ensure_ascii=False)[:4000])
            finally:
                conn.close()
            sys.exit(0)
        print(__doc__)
        sys.exit(1)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
