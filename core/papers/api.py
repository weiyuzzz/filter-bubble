"""学术论文模块 API，挂在 /api/radar/papers 下，由雷达 API 分发。"""
from __future__ import annotations

import json
from typing import Any, Dict, Tuple

from .. import db
from . import classify, probe, service, store


def _ok(payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    return 200, payload


def _err(status: int, message: str) -> Tuple[int, Dict[str, Any]]:
    return status, {"error": message}


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def handle_get(path: str, query: Dict[str, str], cfg: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    try:
        if path in ("/api/radar/papers", "/api/radar/papers/"):
            discipline = classify.normalize_discipline(query.get("discipline", "all"))
            limit = _int(query.get("limit"), 10, 1, 50)
            offset = _int(query.get("offset"), 0, 0, 5000)
            data = service.list_papers(discipline, limit, offset)
            data["updated_at"] = service.status(cfg)["stats"].get("last_fetched_at") or ""
            return _ok(data)
        if path == "/api/radar/papers/status":
            return _ok(service.status(cfg))
        return _err(404, "not found")
    except Exception as exc:  # noqa: BLE001
        print("[papers] api GET error:", exc)
        return _err(500, str(exc))


def handle_post(path: str, body: Dict[str, Any], cfg: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    try:
        if path == "/api/radar/papers/collect":
            started = service.collect_async(cfg)
            return _ok({"started": started, "state": service.get_state()})
        if path == "/api/radar/papers/retry":
            paper_id = str(body.get("id") or "").strip()
            if not paper_id:
                return _err(400, "id required")
            return _ok({"queued": service.retry_one(paper_id)})
        if path == "/api/radar/papers/retry-failed":
            conn = db.connect()
            try:
                rows = conn.execute(
                    "SELECT paper_id FROM paper_ai_overviews WHERE status='failed' LIMIT 30").fetchall()
            finally:
                conn.close()
            queued = sum(1 for row in rows if service.retry_one(row["paper_id"]))
            return _ok({"queued": queued})
        if path == "/api/radar/papers/analyze":
            return _ok({"queued": service.analyze_pending(cfg)})
        if path == "/api/radar/papers/test-sources":
            result = probe.run(cfg)
            return _ok(result)
        return _err(404, "not found")
    except Exception as exc:  # noqa: BLE001
        print("[papers] api POST error:", exc)
        return _err(500, str(exc))
