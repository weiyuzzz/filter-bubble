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
from . import cluster, config as radar_config, investigate, push, report, sources, store, summarize

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


def collect_only(cfg: Optional[Dict[str, Any]] = None, reason: str = "hourly") -> Dict[str, Any]:
    """只采集，不生成日报也不推送。用于每小时的常规抓取。"""
    cfg = cfg or radar_config.cached()
    if not _begin():
        return {"started": False, "reason": "already running"}
    conn = db.connect()
    try:
        store.init()
        _set("collecting")
        summary = sources.collect(conn, cfg)
        conn.commit()
        _set("idle", last_collect_at=summary.get("collected_at"))
        return {"started": True, "reason": reason, "summary": summary}
    except Exception as exc:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        _end(error="%s: %s" % (type(exc).__name__, exc))
        return {"started": True, "reason": reason,
                "error": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        conn.close()
        if _STATE.get("running"):
            _end()


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
        stories = cluster.build_stories(conn, date, events, cfg, now_iso())

        for event in events:
            store.assign_event(conn, event.get("_item_ids") or [], event["id"])
            store.upsert_event(conn, event)
            store.save_event_snapshot(conn, event["id"], date, event.get("item_count", 0),
                                      event.get("platform_count", 0), event.get("heat", 0))
        store.replace_stories(conn, date, stories)
        conn.commit()
        stats["events"] = len(events)
        stats["stories"] = len(stories)
        stats["items_considered"] = len(items)

        # 地理标注 + 主题分类：只处理当天事件，纯本地计算（不联网），
        # 每 50 条一个短事务，避免长事务增加写锁竞争。
        try:
            from . import geo_tag
            geo_stats = geo_tag.tag_events(conn, events, force=False, commit_every=50)
            stats["geo_tag"] = {
                "processed": geo_stats.get("processed"),
                "located": geo_stats.get("located"),
                "by_confidence": geo_stats.get("by_confidence"),
                "by_theme": geo_stats.get("by_theme"),
            }
        except Exception as exc:  # noqa: BLE001 - 地理标注失败不影响日报
            print("[radar] geo tagging failed:", exc)
            stats["geo_tag"] = {"error": "%s: %s" % (type(exc).__name__, exc)}

        _set("reporting")
        payload = report.build(conn, date, cfg, collect_summary)
        store.save_report(conn, date, "daily", payload, now_iso(),
                          version=int(payload.get("version") or 1))
        # 全球态势：在同一个事务里物化当日的固定口径快照。
        # 纯本地计算，不联网、不等待外部请求；失败不影响日报。
        try:
            from . import intel
            record = intel.record_daily_snapshot(conn, date, cfg)
            stats["threat_snapshot"] = {
                "date": date,
                "caliber_version": record.get("caliber_version"),
                "activity": record.get("activity"),
                "credibility": record.get("credibility"),
            }
        except Exception as exc:  # noqa: BLE001
            print("[radar] threat snapshot failed:", exc)
            stats["threat_snapshot"] = {"error": "%s: %s" % (type(exc).__name__, exc)}
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
        # 兜底收尾：任何异常路径漏掉时也把运行记录置为 failed，绝不留 running
        try:
            row = conn.execute("SELECT status FROM radar_runs WHERE id=?", (run_id,)).fetchone()
            if row is not None and str(row["status"]) == "running":
                store.finish_run(conn, run_id, now_iso(), "failed", stats,
                                 error or "运行未正常结束（兜底收尾）")
                conn.commit()
        except Exception:  # noqa: BLE001 - 收尾失败也不能再抛出去
            pass
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
        # 与完整流水线一致：同一事务内物化当日态势快照
        try:
            from . import intel
            intel.record_daily_snapshot(conn, date, cfg)
        except Exception as exc:  # noqa: BLE001
            print("[radar] threat snapshot failed:", exc)
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
    interval = float(schedule.get("collect_interval_minutes") or 60)
    last_collect = None
    try:
        last_collect = store.last_collect_at(conn)
    except Exception:  # noqa: BLE001
        last_collect = None

    next_run = None
    daily_time = str(schedule.get("daily_time") or "08:00")
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
        "schedule": {"time": daily_time, "timezone": cfg.get("timezone"), "next_run": next_run,
                      "collect_interval_minutes": interval},
        "last_collect_at": last_collect,
        "push": push.status(conn, cfg, date),
        "push_enabled": bool(push_cfg.get("enabled")),
        "summary_backend": summarize.backend_status(cfg),
        "cluster": cfg.get("cluster") or {},
        "sources": {
            "total": len(all_sources),
            "ok": len([s for s in all_sources if s["status"] == "ok"]),
            "error": len([s for s in all_sources if s["status"] == "error"]),
            "paused": len([s for s in all_sources if s["status"] == "paused"]),
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
    """两条时间线：每小时采集一次；每天到点生成日报并推送一次。"""
    print("[radar] scheduler started")
    last_collect: Optional[dt.datetime] = None
    while not stop.wait(30):
        try:
            cfg = radar_config.cached()
            if not cfg.get("enabled", True):
                continue
            schedule = cfg.get("schedule") or {}
            limits = cfg.get("limits") or {}
            interval = float(schedule.get("collect_interval_minutes") or 60)
            daily_time = str(schedule.get("daily_time") or "08:00")

            conn = db.connect()
            try:
                latest = store.latest_report(conn, "daily")
                last_runs = store.last_runs(conn, limit=1)
            finally:
                conn.close()

            now = dt.datetime.now(tz())
            running = bool(_STATE.get("running"))
            if running:
                continue

            # 1) 每日日报 + 推送（一天一次）
            if (latest or {}).get("date") != today() and now.strftime("%H:%M") >= daily_time:
                last = last_runs[0] if last_runs else None
                if last and last.get("status") in ("failed", "partial"):
                    retry_after = float(schedule.get("retry_after_minutes") or 10)
                    if _minutes_since(last.get("started_at", "")) < retry_after:
                        continue
                min_gap = float(limits.get("min_minutes_between_reports") or 60)
                if latest and _minutes_since(latest.get("generated_at", "")) < min_gap:
                    continue
                print("[radar] daily report starting at", now.isoformat(timespec="seconds"))
                collect_and_report(cfg, reason="schedule")
                last_collect = now
                continue

            # 2) 每小时采集（不生成日报、不推送）
            if last_collect is None:
                last_collect = now  # 启动后先等一个间隔，避免重启即抓
                continue
            if (now - last_collect).total_seconds() < interval * 60:
                continue
            min_collect = float(limits.get("min_minutes_between_collects") or 20)
            if last_collect and (now - last_collect).total_seconds() < min_collect * 60:
                continue
            print("[radar] hourly collect at", now.isoformat(timespec="seconds"))
            collect_only(cfg, reason="hourly")
            last_collect = now
        except Exception as exc:  # noqa: BLE001 - the scheduler thread must never die
            print("[radar] scheduler error:", exc)


def start_scheduler(stop: threading.Event) -> Optional[threading.Thread]:
    # 启动时收尾上一次被中断、永远停在 running 的运行记录（不新增任何线程）
    try:
        conn = db.connect()
        try:
            stale = store.finish_stale_runs(conn)
        finally:
            conn.close()
        if stale:
            print("[radar] 已收尾 %d 条未正常结束的运行记录" % stale)
    except Exception as exc:  # noqa: BLE001
        print("[radar] stale run cleanup failed:", exc)
    thread = threading.Thread(target=scheduler_loop, args=(stop,), daemon=True, name="radar-scheduler")
    thread.start()
    return thread
