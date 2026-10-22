"""Radar orchestration: collect → cluster → score → report → (optional) push.

The scheduler here is completely separate from the GitHub digest scheduler in ``app.py``:
it has its own thread, its own time, its own state and never touches ``last_generated_date``.
"""
from __future__ import annotations

import datetime as dt
import threading
import traceback
import uuid
from typing import Any, Dict, Optional

from .. import db
from . import cluster, config as radar_config, investigate, push, report, sources, store

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Windows without tzdata
    ZoneInfo = None

_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {
    "running": False,
    "stage": "",
    "started_at": None,
    "finished_at": None,
    "last_error": None,
    "last_run_id": None,
    "last_result": None,
}
_thread: Optional[threading.Thread] = None


def tz():
    cfg = radar_config.cached()
    name = cfg.get("timezone") or "Asia/Shanghai"
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001
            pass
    return dt.datetime.now().astimezone().tzinfo


def now_iso() -> str:
    return dt.datetime.now(tz()).isoformat(timespec="seconds")


def today() -> str:
    return dt.datetime.now(tz()).date().isoformat()


def get_state() -> Dict[str, Any]:
    with _LOCK:
        return dict(_STATE)


def _set(stage: str, **kwargs) -> None:
    with _LOCK:
        _STATE["stage"] = stage
        _STATE.update(kwargs)


def _begin() -> bool:
    with _LOCK:
        if _STATE["running"]:
            return False
        _STATE.update({"running": True, "stage": "starting", "started_at": now_iso(),
                       "finished_at": None, "last_error": None})
        return True


def _end(error: Optional[str] = None, result: Optional[Dict[str, Any]] = None) -> None:
    with _LOCK:
        _STATE.update({"running": False, "finished_at": now_iso(),
                       "last_error": error, "last_result": result})


def recent_report_date(conn) -> Optional[str]:
    latest = store.latest_report(conn, "daily")
    return latest["date"] if latest else None


def collect_and_report(cfg: Optional[Dict[str, Any]] = None, reason: str = "manual") -> Dict[str, Any]:
    """Full pipeline. Safe to call directly (blocking) or from the async wrapper."""
    cfg = cfg or radar_config.cached()
    run_id = "run-" + uuid.uuid4().hex[:12]
    started = now_iso()
    conn = db.connect()
    stats: Dict[str, Any] = {"reason": reason}
    error: Optional[str] = None
    try:
        store.init()
        store.start_run(conn, run_id, "collect", started)

        _set("collecting", last_run_id=run_id)
        try:
            collect_summary = sources.collect(conn, cfg)
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - never let collection kill the report
            conn.rollback()
            collect_summary = {"error": "%s: %s" % (type(exc).__name__, exc),
                               "sources_enabled": 0, "sources_ok": 0, "sources_failed": 0,
                               "items_new": 0, "failures": []}
            error = collect_summary["error"]
        stats["collect"] = collect_summary

        _set("clustering")
        date = today()
        since = (dt.datetime.now(tz()) - dt.timedelta(hours=36)).isoformat(timespec="seconds")
        items = store.recent_items(conn, since, limit=int((cfg.get("limits") or {}).get("max_candidates") or 150))
        events = cluster.build_events(conn, date, items, cfg, now_iso())
        max_events = int((cfg.get("limits") or {}).get("max_events") or 60)
        events = sorted(events, key=lambda e: (e.get("importance", 0) + e.get("heat", 0)), reverse=True)[:max_events]

        for event in events:
            store.assign_event(conn, event.get("_item_ids") or [], event["id"])
            store.upsert_event(conn, event)
            store.save_event_snapshot(conn, event["id"], date, event.get("item_count", 0),
                                      event.get("platform_count", 0), event.get("heat", 0))
        conn.commit()
        stats["events"] = len(events)
        stats["items_considered"] = len(items)

        _set("reporting")
        payload = report.build(conn, date, cfg, collect_summary)
        store.save_report(conn, date, "daily", payload, now_iso(),
                          version=int(payload.get("version") or 1))
        conn.commit()
        stats["report_date"] = date

        _set("pushing")
        try:
            push_result = push.send_daily(conn, payload, cfg)
        except Exception as exc:  # noqa: BLE001
            push_result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        stats["push"] = push_result

        try:
            pruned = store.prune(conn, int((cfg.get("retention") or {}).get("days") or 90))
            conn.commit()
            stats["pruned"] = pruned
        except Exception:  # noqa: BLE001
            conn.rollback()

        store.finish_run(conn, run_id, now_iso(), "ok" if not error else "partial", stats, error)
        conn.commit()
        _end(error, stats)
        return {"ok": True, "run_id": run_id, "stats": stats, "error": error}
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc(limit=3)
        print("[radar] run failed:", exc)
        try:
            store.finish_run(conn, run_id, now_iso(), "failed", stats, "%s\n%s" % (exc, tb))
            conn.commit()
        except Exception:  # noqa: BLE001
            conn.rollback()
        _end("%s: %s" % (type(exc).__name__, exc), stats)
        return {"ok": False, "run_id": run_id, "error": str(exc), "stats": stats}
    finally:
        conn.close()


def generate_report_only(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = cfg or radar_config.cached()
    if not _begin():
        return {"ok": False, "reason": "already running"}
    conn = db.connect()
    try:
        store.init()
        _set("reporting")
        date = today()
        payload = report.build(conn, date, cfg)
        store.save_report(conn, date, "daily", payload, now_iso())
        conn.commit()
        _end(None, {"report_date": date})
        return {"ok": True, "date": date}
    except Exception as exc:  # noqa: BLE001
        _end(str(exc))
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def refresh_async(cfg: Optional[Dict[str, Any]] = None, reason: str = "manual") -> bool:
    global _thread
    with _LOCK:
        if _STATE["running"]:
            return False
        _STATE.update({"running": True, "stage": "queued", "started_at": now_iso(),
                       "finished_at": None, "last_error": None})
    cfg = cfg or radar_config.cached()

    def _worker() -> None:
        try:
            collect_and_report(cfg, reason=reason)
        except Exception as exc:  # noqa: BLE001
            _end(str(exc))

    _thread = threading.Thread(target=_worker, daemon=True, name="radar-run")
    _thread.start()
    return True


def status(conn, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = cfg or radar_config.cached()
    date = today()
    latest = store.latest_report(conn, "daily")
    runs = store.last_runs(conn, limit=5)
    all_sources = store.sources(conn)
    schedule = cfg.get("schedule") or {}
    push_cfg = cfg.get("push") or {}

    next_run = None
    daily_time = str(schedule.get("daily_time") or "09:00")
    try:
        hh, mm = [int(x) for x in daily_time.split(":")]
        target = dt.datetime.now(tz()).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if (latest or {}).get("date") == date:
            target += dt.timedelta(days=1)
        elif target <= dt.datetime.now(tz()):
            target += dt.timedelta(days=1)
        next_run = target.isoformat(timespec="minutes")
    except ValueError:
        next_run = None

    return {
        "state": get_state(),
        "today_ready": (latest or {}).get("date") == date,
        "latest_date": (latest or {}).get("date"),
        "latest_generated_at": (latest or {}).get("generated_at"),
        "schedule": {"time": daily_time, "timezone": cfg.get("timezone"), "next_run": next_run},
        "push": push.status(conn, cfg, date),
        "push_enabled": bool(push_cfg.get("enabled")),
        "sources": {
            "total": len(all_sources),
            "ok": len([s for s in all_sources if s["status"] == "ok"]),
            "error": len([s for s in all_sources if s["status"] == "error"]),
            "disabled": len([s for s in all_sources if not s["enabled"]]),
            "unavailable": len([s for s in all_sources if s["kind"] == "unavailable"]),
        },
        "runs": runs,
        "enabled": bool(cfg.get("enabled")),
    }


def _minutes_since(iso: str) -> float:
    if not iso:
        return 1e9
    try:
        then = dt.datetime.fromisoformat(iso)
        return (dt.datetime.now(tz()) - then).total_seconds() / 60.0
    except ValueError:
        return 1e9


def scheduler_loop(stop: threading.Event) -> None:
    """Independent daily scheduler. Never touches the GitHub digest scheduler."""
    print("[radar] scheduler started")
    while not stop.wait(30):
        try:
            cfg = radar_config.cached()
            if not cfg.get("enabled", True):
                continue
            schedule = cfg.get("schedule") or {}
            limits = cfg.get("limits") or {}
            conn = db.connect()
            try:
                latest = store.latest_report(conn, "daily")
                last_runs = store.last_runs(conn, limit=1)
            finally:
                conn.close()

            if (latest or {}).get("date") == today():
                continue

            now = dt.datetime.now(tz())
            daily_time = str(schedule.get("daily_time") or "09:00")
            if now.strftime("%H:%M") < daily_time:
                continue

            last = last_runs[0] if last_runs else None
            if last and last.get("status") == "running":
                continue
            if last and last.get("status") in ("failed", "partial"):
                retry_after = float(schedule.get("retry_after_minutes") or 10)
                if _minutes_since(last.get("started_at", "")) < retry_after:
                    continue
            min_gap = float(limits.get("min_minutes_between_reports") or 60)
            if latest and _minutes_since(latest.get("generated_at", "")) < min_gap:
                continue

            print("[radar] daily run starting at", now.isoformat(timespec="seconds"))
            collect_and_report(cfg, reason="schedule")
        except Exception as exc:  # noqa: BLE001 - the scheduler thread must never die
            print("[radar] scheduler error:", exc)


def start_scheduler(stop: threading.Event) -> Optional[threading.Thread]:
    thread = threading.Thread(target=scheduler_loop, args=(stop,), daemon=True, name="radar-scheduler")
    thread.start()
    return thread
