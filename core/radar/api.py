"""Radar HTTP API. app.py delegates here; nothing in this module imports app.py."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional, Tuple

from .. import db
from . import config as radar_config
from . import investigate, llm, push, report, secrets, service, sources, store

_TTL = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _ok(payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    return 200, payload


def _err(status: int, message: str) -> Tuple[int, Dict[str, Any]]:
    return status, {"error": message}


def handle_get(path: str, query: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    cfg = radar_config.cached()
    if path.startswith("/api/radar/papers"):
        try:
            from ..papers import api as papers_api
        except Exception as exc:  # noqa: BLE001 - 论文模块坏了不能影响雷达
            return _err(503, "papers module unavailable: %s" % exc)
        try:
            return papers_api.handle_get(path, query, cfg)
        except Exception as exc:  # noqa: BLE001
            print("[papers] GET error:", exc)
            return _err(500, str(exc))
    conn = db.connect()
    try:
        store.init()
        if path in ("/api/radar/status", "/api/radar/health"):
            return _ok(service.status(conn, cfg))
        if path == "/api/radar/report":
            date = query.get("date")
            if date:
                row = conn.execute("SELECT * FROM radar_reports WHERE date=? AND kind='daily'", (date,)).fetchone()
                if not row:
                    return _err(404, "no report for %s" % date)
                return _ok({"date": row["date"], "generated_at": row["generated_at"],
                            "payload": json.loads(row["payload"])})
            latest = store.latest_report(conn, "daily")
            if not latest:
                return 202, {"pending": True, "status": service.status(conn, cfg)}
            payload = latest["payload"]
            # NEW 标签按当前已读状态重算：用户点过「深入调查」后刷新就消失
            try:
                from .. import seen as seen_store
                seen_store.annotate("radar", payload.get("highlights") or [], "story_id")
            except Exception:  # noqa: BLE001
                pass
            payload["last_collect"] = store.last_collect_at(conn)
            return _ok({"date": latest["date"], "generated_at": latest["generated_at"],
                        "payload": payload})
        if path == "/api/radar/events":
            date = query.get("date") or service.today()
            events = store.events_for_date(conn, date, limit=200)
            for event in events:
                event["items"] = store.items_for_event(conn, event["id"])[:6]
            return _ok({"date": date, "events": events})
        if path == "/api/radar/sources":
            rows = store.sources(conn)
            return _ok({"sources": rows,
                        "summary": {
                            "total": len(rows),
                            "ok": len([r for r in rows if r["status"] == "ok"]),
                            "error": len([r for r in rows if r["status"] == "error"]),
                            "paused": len([r for r in rows if r["status"] == "paused"]),
                            "disabled": len([r for r in rows if not r["enabled"]]),
                            "unavailable": len([r for r in rows if r["kind"] == "unavailable"]),
                        }})
        if path == "/api/radar/history":
            return _ok({"reports": store.report_history(conn, "daily", limit=30),
                         "investigations": store.report_history(conn, "investigation", limit=30),
                         "runs": store.last_runs(conn, limit=15)})
        if path == "/api/radar/settings":
            return _ok({"settings": _public_settings(cfg), "path": str(radar_config.CONFIG_PATH)})
        if path == "/api/radar/investigation":
            event_id = query.get("event_id")
            text = query.get("q")
            if event_id:
                return _ok(_investigation_view(conn, event_id))
            if text:
                return _ok(investigate.run_query(conn, text, cfg))
            return _err(400, "event_id or q required")
        if path == "/api/radar/event":
            event_id = query.get("id") or ""
            event = store.event_by_id(conn, event_id)
            if not event:
                return _err(404, "event not found")
            event["items"] = store.items_for_event(conn, event_id)
            event["claims"] = store.claims_for_event(conn, event_id)
            return _ok(event)
        return _err(404, "not found")
    except Exception as exc:  # noqa: BLE001 - radar errors must not break the app
        print("[radar] api GET error:", exc)
        return _err(500, str(exc))
    finally:
        conn.close()


def _investigation_view(conn, event_id: str) -> Dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM radar_reports WHERE kind='investigation' ORDER BY generated_at DESC LIMIT 200"
    ).fetchall()
    for row in rows:
        if str(row["date"]).endswith("|" + event_id):
            return json.loads(row["payload"])
    return investigate.run(conn, event_id, radar_config.cached())


def handle_post(path: str, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    cfg = radar_config.cached()
    if path.startswith("/api/radar/papers"):
        try:
            from ..papers import api as papers_api
        except Exception as exc:  # noqa: BLE001
            return _err(503, "papers module unavailable: %s" % exc)
        try:
            return papers_api.handle_post(path, body, cfg)
        except Exception as exc:  # noqa: BLE001
            print("[papers] POST error:", exc)
            return _err(500, str(exc))
    conn = db.connect()
    try:
        store.init()
        if path == "/api/radar/collect":
            started = service.refresh_async(cfg, reason="api")
            return 202 if started else 200, {"started": started, "status": service.status(conn, cfg)}
        if path == "/api/radar/report":
            result = service.generate_report_only(cfg)
            return _ok({"result": result, "status": service.status(conn, cfg)})
        if path == "/api/radar/investigate":
            event_id = str(body.get("event_id") or "").strip()
            text = str(body.get("query") or "").strip()
            try:
                if event_id:
                    return _ok(investigate.run(conn, event_id, cfg))
                if text:
                    return _ok(investigate.run_query(conn, text, cfg))
                return _err(400, "event_id or query required")
            except ValueError as exc:
                return _err(404, str(exc))
        if path == "/api/radar/sources/reset":
            source_id = str(body.get("source_id") or "").strip() or None
            reset = store.reset_source_failures(conn, source_id)
            conn.commit()
            return _ok({"reset": reset, "sources": store.sources(conn)})
        if path == "/api/radar/push":
            latest = store.latest_report(conn, "daily")
            if not latest:
                return _err(409, "还没有可推送的日报")
            result = push.send_daily(conn, latest["payload"], cfg, force=bool(body.get("force")),
                                     manual=True)
            return _ok({"result": result, "push": push.status(conn, cfg, latest["date"])})
        if path == "/api/radar/settings":
            saved = _save_settings(body)
            fresh = radar_config.cached()
            return _ok({"saved": saved, "settings": _public_settings(fresh),
                        "path": str(radar_config.CONFIG_PATH),
                        "llm_status": llm.status(fresh)})
        if path == "/api/radar/llm/test":
            # 大模型已改为全局配置；这里保留是为了兼容旧前端调用
            fresh = radar_config.cached()
            result = llm.test_connection(fresh)
            return _ok({"result": result, "llm_status": llm.status(fresh)})
        return _err(404, "not found")
    except Exception as exc:  # noqa: BLE001
        print("[radar] api POST error:", exc)
        return _err(500, str(exc))
    finally:
        conn.close()


def _public_settings(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Surface only what the UI may edit, plus read-only source metadata."""
    limits = cfg.get("limits") or {}
    llm_cfg = cfg.get("llm") or {}
    env_name = str(llm_cfg.get("api_key_env") or secrets.ENV_NAME)
    llm_ok, llm_reason, _ = llm.resolve(cfg)
    return {
        "schedule": cfg.get("schedule") or {},
        "report": cfg.get("report") or {},
        "push": {k: v for k, v in (cfg.get("push") or {}).items() if k != "sendkey_env"},
        "scoring": cfg.get("scoring") or {},
        "interests": cfg.get("interests") or {},
        "limits": limits,
        "investigation": {k: v for k, v in (cfg.get("investigation") or {}).items() if k != "tech_keywords"},
        "enabled": cfg.get("enabled", True),
        "timezone": cfg.get("timezone"),
        "source_toggles": {s["source_id"]: bool(s["enabled"]) for s in sources.all_specs(cfg)},
        "llm": {
            "enabled": bool(llm_cfg.get("enabled")),
            "provider": llm_cfg.get("provider") or "openai_compatible",
            "base_url": llm_cfg.get("base_url") or "",
            "model": llm_cfg.get("model") or "",
            "api_key_env": env_name,
            "providers": list(llm.PROVIDERS),
            "available": llm_ok,
            "unavailable_reason": llm_reason,
            "key": secrets.public_state(env_name),
            "stats": {k: llm.STATS.get(k) for k in ("calls", "failures", "prompt_tokens",
                                                    "completion_tokens", "last_error")},
        },
    }


def _coerce_time(value: Any) -> Optional[str]:
    text = str(value or "")
    if not _TTL.match(text):
        return None
    return text + ":00"


def _save_settings(body: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a whitelisted subset into social_radar.json; unknown keys are ignored."""
    file_data: Dict[str, Any] = {}
    if radar_config.CONFIG_PATH.exists():
        try:
            file_data = json.loads(radar_config.CONFIG_PATH.read_text(encoding="utf-8") or "{}")
        except ValueError:
            file_data = {}
    if not isinstance(file_data, dict):
        file_data = {}

    saved: Dict[str, Any] = {}

    if "daily_time" in body:
        value = _coerce_time(body["daily_time"])
        if value:
            file_data.setdefault("schedule", {})["daily_time"] = value
            saved["daily_time"] = value

    if "top_events" in body:
        try:
            n = max(5, min(30, int(body["top_events"])))
            file_data.setdefault("report", {})["top_events"] = n
            saved["top_events"] = n
        except (TypeError, ValueError):
            pass

    if "push_enabled" in body:
        flag = bool(body["push_enabled"])
        file_data.setdefault("push", {})["enabled"] = flag
        saved["push_enabled"] = flag

    if "push_items" in body:
        try:
            n = max(1, min(10, int(body["push_items"])))
            file_data.setdefault("push", {})["items"] = n
            saved["push_items"] = n
        except (TypeError, ValueError):
            pass

    if "alert_threshold" in body:
        try:
            n = max(0, min(100, float(body["alert_threshold"])))
            file_data.setdefault("push", {})["alert_threshold"] = n
            saved["alert_threshold"] = n
        except (TypeError, ValueError):
            pass

    if "topics" in body and isinstance(body["topics"], list):
        topics = [str(t)[:40] for t in body["topics"][:20] if str(t).strip()]
        file_data.setdefault("interests", {})["topics"] = topics
        saved["topics"] = topics

    for key in ("keywords", "exclude"):
        if key in body and isinstance(body[key], list):
            words = [str(w)[:40] for w in body[key][:50] if str(w).strip()]
            file_data.setdefault("interests", {})[key] = words
            saved[key] = words

    if "min_heat" in body:
        try:
            n = max(0, min(100, float(body["min_heat"])))
            file_data.setdefault("investigation", {})["min_heat"] = n
            saved["min_heat"] = n
        except (TypeError, ValueError):
            pass

    if "weights" in body and isinstance(body["weights"], dict):
        weights = file_data.setdefault("scoring", {})
        for key in ("velocity", "cross_platform", "engagement", "verification", "interest"):
            if key in body["weights"]:
                try:
                    weights[key] = max(0.0, min(1.0, float(body["weights"][key])))
                    saved.setdefault("weights", {})[key] = weights[key]
                except (TypeError, ValueError):
                    pass

    if "sources" in body and isinstance(body["sources"], dict):
        block = file_data.setdefault("sources", {})
        for source_id, enabled in body["sources"].items():
            if source_id not in {s["source_id"] for s in sources.SOURCES}:
                continue
            block.setdefault(source_id, {})["enabled"] = bool(enabled)
            saved.setdefault("sources", {})[source_id] = bool(enabled)

    if saved:
        radar_config.CONFIG_PATH.write_text(
            json.dumps(file_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return saved


def save_llm_settings(body: Dict[str, Any]) -> Dict[str, Any]:
    """保存大模型开关与端点配置；密钥单独落到本地 secrets 文件，绝不回传。

    前端提交 ``{"llm": {enabled, provider, base_url, model}, "api_key": "..."}``；
    ``api_key`` 为空表示「保持原样」，``clear_api_key: true`` 才清除。
    """
    incoming = body.get("llm")
    if not isinstance(incoming, dict):
        incoming = {}

    file_data: Dict[str, Any] = {}
    if radar_config.CONFIG_PATH.exists():
        try:
            file_data = json.loads(radar_config.CONFIG_PATH.read_text(encoding="utf-8-sig") or "{}")
        except (ValueError, OSError):
            file_data = {}
    if not isinstance(file_data, dict):
        file_data = {}

    block = file_data.setdefault("llm", {})
    saved: Dict[str, Any] = {}

    if "enabled" in incoming:
        block["enabled"] = bool(incoming["enabled"])
        saved["enabled"] = block["enabled"]

    if "provider" in incoming:
        provider = str(incoming["provider"] or "").strip()
        if provider in llm.PROVIDERS:
            block["provider"] = provider
            saved["provider"] = provider

    if "base_url" in incoming:
        base_url = str(incoming["base_url"] or "").strip()[:300]
        if not base_url or re.match(r"^https?://", base_url):
            block["base_url"] = base_url
            saved["base_url"] = base_url

    if "model" in incoming:
        model = str(incoming["model"] or "").strip()[:120]
        block["model"] = model
        saved["model"] = model

    if saved:
        radar_config.CONFIG_PATH.write_text(
            json.dumps(file_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if body.get("clear_api_key"):
        if secrets.set_api_key(None):
            saved["api_key"] = "cleared"
    elif str(body.get("api_key") or "").strip():
        if secrets.set_api_key(str(body["api_key"])):
            saved["api_key"] = "saved"
    return saved


def build_report_payload(date: Optional[str] = None) -> Dict[str, Any]:
    conn = db.connect()
    try:
        payload = report.build(conn, date or service.today(), radar_config.cached())
    finally:
        conn.close()
    return payload
