"""``/api/monitor/*`` 只读接口。app.py 只负责把请求转进来，本模块不反向依赖 app.py。

约定
----
* 全部是 GET（只读）：页面的刷新、筛选、分页都靠重新查询，不存在任何写操作。
* 所有列表接口都分页并有上限；日志永不一次性返回全量。
* 任何异常都返回可诊断的 JSON（``{error, error_type, path}``），不抛给调用方。
* 所有出参再过一遍脱敏：写库时已经脱敏，这里是第二道闸门。
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

from .. import db
from ..radar import config as radar_config
from . import instrument, redact, store

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 25


def _ok(payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    return 200, payload


def _err(status: int, message: str, **extra: Any) -> Tuple[int, Dict[str, Any]]:
    payload = {"error": redact.redact_text(message)}
    payload.update(extra)
    return status, payload


def _int(query: Dict[str, str], name: str, default: int, low: int, high: int) -> int:
    try:
        number = int(str(query.get(name) or "").strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _text(query: Dict[str, str], name: str, limit: int = 80) -> str:
    return str(query.get(name) or "").strip()[:limit]


def _list(query: Dict[str, str], name: str, allowed: Optional[List[str]] = None) -> Optional[List[str]]:
    raw = str(query.get(name) or "").strip()
    if not raw:
        return None
    parts = [part.strip() for part in raw.split(",") if part.strip()][:12]
    if allowed is not None:
        parts = [part for part in parts if part in allowed]
    return parts or None


def _iso(value: Any) -> Optional[str]:
    parsed = store.parse_iso(value)
    return parsed.isoformat(timespec="seconds") if parsed else None


def _window(query: Dict[str, str], default_hours: int = 24) -> Dict[str, Any]:
    """时间范围：显式 from/to 优先，其次 window=24h|7d|30d|all。"""
    reference = store.now()
    label = _text(query, "window", 12) or ("%dh" % default_hours)
    hours = default_hours
    everything = label.lower() in ("all", "*", "0")
    if label.endswith("h"):
        try:
            hours = max(1, min(24 * 90, int(label[:-1])))
        except ValueError:
            hours = default_hours
    elif label.endswith("d"):
        try:
            hours = max(1, min(24 * 90, int(label[:-1]) * 24))
        except ValueError:
            hours = default_hours * 24
    from_iso = _iso(query.get("from"))
    to_iso = _iso(query.get("to"))
    if not from_iso and not everything:
        from_iso = (reference - dt.timedelta(hours=hours)).isoformat(timespec="seconds")
    if not to_iso and not (query.get("to") or "").strip():
        to_iso = reference.isoformat(timespec="seconds")
    elif to_iso:
        parsed = store.parse_iso(to_iso)
        if parsed is not None and len(str(query.get("to") or "").strip()) == 10:
            to_iso = (parsed + dt.timedelta(days=1)).isoformat(timespec="seconds")
    return {"from": from_iso, "to": to_iso, "hours": hours, "label": label,
            "all": everything}


def _specs() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """当前配置里的全部来源（含自定义 RSS / AnySearch 查询）；雷达模块坏了也不影响监控。"""
    degraded: List[Dict[str, Any]] = []
    try:
        from ..radar import sources as radar_sources
        cfg = radar_config.cached()
        return list(radar_sources.all_specs(cfg)), degraded
    except Exception as exc:  # noqa: BLE001
        degraded.append({"module": "radar.sources",
                         "reason": "%s: %s" % (type(exc).__name__, exc)})
        return [], degraded


# ---------------------------------------------------------------- 入口

def handle_get(path: str, query: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    try:
        conn = db.connect()
    except Exception as exc:  # noqa: BLE001 - 数据库打不开时也要给出可读的错误
        return _err(503, "监控数据库不可用：%s: %s" % (type(exc).__name__, exc),
                    error_type=type(exc).__name__)
    try:
        try:
            store.ensure()
        except Exception as exc:  # noqa: BLE001 - 建表/导入失败不能挡住已有数据
            print("[monitor] ensure failed:", exc)
        return _route(path, query, conn)
    except Exception as exc:  # noqa: BLE001
        print("[monitor] GET error:", exc)
        return _err(500, "%s: %s" % (type(exc).__name__, exc), error_type=type(exc).__name__,
                    path=path,
                    hint="监控接口失败；采集流水线本身不受影响（监控是旁路）。")
    finally:
        conn.close()


def handle_post(path: str, body: Optional[Dict[str, Any]] = None
                ) -> Tuple[int, Dict[str, Any]]:
    return _err(405, "监控接口是只读的：只支持 GET（页面不会触发任何采集或推送）",
                path=path)


def _route(path: str, query: Dict[str, str], conn) -> Tuple[int, Dict[str, Any]]:
    if path in ("/api/monitor", "/api/monitor/", "/api/monitor/health"):
        counts = store.counts(conn)
        return _ok({"ok": True, "time": store.now_iso(), "counts": counts,
                    "readonly": True,
                    "note": "只读接口：页面不会触发采集、推送或写库"})
    if path == "/api/monitor/meta":
        return _ok(store.meta(conn))
    if path == "/api/monitor/overview":
        hours = _int(query, "hours", store.OVERVIEW_HOURS, 1, 24 * 90)
        stale = _int(query, "stale_hours", store.STALE_HOURS, 1, 24 * 90)
        specs, degraded = _specs()
        payload = store.overview(conn, specs, hours=hours, stale_hours=stale)
        payload["degraded"] = degraded
        return _ok(payload)
    if path == "/api/monitor/runs":
        return _runs(conn, query)
    if path == "/api/monitor/timeline":
        return _timeline(conn, query)
    if path == "/api/monitor/run":
        return _run_detail(conn, query)
    if path == "/api/monitor/run/items":
        run_id = _text(query, "id", 80)
        if not run_id:
            return _err(400, "缺少运行 id")
        if not store.get_run(conn, run_id):
            return _err(404, "没有这条运行记录：%s" % run_id)
        page = store.run_item_page(
            conn, run_id, page=_int(query, "page", 1, 1, 10 ** 6),
            page_size=_int(query, "page_size", 50, 1, MAX_PAGE_SIZE),
            action=_text(query, "action", 24) or None, q=_text(query, "q") or None,
            source_id=_text(query, "source_id", 80) or None)
        page["run_id"] = run_id
        return _ok(page)
    if path == "/api/monitor/run/logs":
        run_id = _text(query, "id", 80)
        if not run_id:
            return _err(400, "缺少运行 id")
        if not store.get_run(conn, run_id):
            return _err(404, "没有这条运行记录：%s" % run_id)
        payload = store.logs_for_run(
            conn, run_id, page=_int(query, "page", 1, 1, 10 ** 6),
            page_size=_int(query, "page_size", 50, 1, MAX_PAGE_SIZE),
            level=_text(query, "level", 12) or None,
            stage=_text(query, "stage", 24) or None,
            source_id=_text(query, "source_id", 80) or None)
        payload["run_id"] = run_id
        return _ok(payload)
    if path == "/api/monitor/sources":
        return _sources(conn, query)
    if path == "/api/monitor/source":
        source_id = _text(query, "id", 120)
        if not source_id:
            return _err(400, "缺少来源 id")
        payload = store.source_detail(conn, source_id,
                                      limit=_int(query, "limit", 20, 1, 200))
        specs, degraded = _specs()
        view = store.sources_view(conn, specs)
        match = next((row for row in view["sources"] if row["source_id"] == source_id), None)
        payload["source"] = match
        payload["degraded"] = degraded
        if match is None and not payload["results"] and not payload["state"]:
            return _err(404, "没有这个来源：%s" % source_id)
        return _ok(payload)
    if path == "/api/monitor/logs":
        return _logs(conn, query)
    if path == "/api/monitor/log":
        log_id = _int(query, "id", 0, 0, 10 ** 12)
        if not log_id:
            return _err(400, "缺少日志 id")
        payload = store.get_log(conn, log_id)
        if not payload:
            return _err(404, "没有这条日志：%s" % log_id)
        return _ok({"log": payload})
    return _err(404, "not found: %s" % path)


def _runs(conn, query: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    window = _window(query, store.OVERVIEW_HOURS)
    page = _int(query, "page", 1, 1, 10 ** 6)
    page_size = _int(query, "page_size", DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)
    sort = _text(query, "sort", 24) or "started_at"
    if sort not in ("started_at", "duration", "error_count"):
        sort = "started_at"
    order = "asc" if _text(query, "order", 6).lower() == "asc" else "desc"
    rows, total = store.list_runs(
        conn, from_iso=window["from"], to_iso=window["to"],
        tasks=_list(query, "task", list(store.TASKS)),
        statuses=_list(query, "status", store.RUN_STATUSES),
        triggers=_list(query, "trigger", store.TRIGGERS),
        origin=_text(query, "origin", 12) or None,
        q=_text(query, "q") or None, sort=sort, order=order,
        page=page, page_size=page_size)
    return _ok({
        "runs": rows, "total": total, "page": page, "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "has_more": page * page_size < total,
        "sort": sort, "order": order, "window": window,
        "unfiltered_total": store.counts(conn)["runs"],
    })


def _timeline(conn, query: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    window = _window(query, store.OVERVIEW_HOURS)
    tasks = _list(query, "task", list(store.TASKS))
    runs = store.run_ids_in_window(conn, window["from"], window["to"], tasks)
    groups: List[Dict[str, Any]] = []
    for task, label in store.TASKS.items():
        member = [run for run in runs if run["task"] == task]
        if member or not tasks:
            groups.append({"task": task, "label": label, "runs": member, "count": len(member)})
    by_status: Dict[str, int] = {}
    for run in runs:
        by_status[run["status"]] = by_status.get(run["status"], 0) + 1
    unknown_start = len([run for run in runs if not run.get("started_at")])
    return _ok({
        "window": window, "groups": groups, "runs": runs, "total": len(runs),
        "by_status": by_status,
        "statuses": [{"value": key, "label": store.RUN_STATUS_LABELS[key]}
                     for key in store.RUN_STATUSES],
        "unknown_start": unknown_start,
        "unknown_start_note": ("有 %d 条历史运行没有记录开始时间（旧记录只保存了结束时间），"
                               "它们不会画在时间轴上，但会出现在运行记录列表里。" % unknown_start)
        if unknown_start else None,
        "running": len([run for run in runs if run["status"] == "running"]),
    })


def _run_detail(conn, query: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    run_id = _text(query, "id", 80)
    if not run_id:
        return _err(400, "缺少运行 id")
    run = store.get_run(conn, run_id)
    if not run:
        return _err(404, "没有这条运行记录：%s" % run_id)
    stages = store.compose_stages(conn, run)
    sources = store.run_sources(conn, run_id)
    items = store.run_item_page(
        conn, run_id, page=_int(query, "items_page", 1, 1, 10 ** 6),
        page_size=_int(query, "items_size", 50, 1, MAX_PAGE_SIZE),
        action=_text(query, "items_action", 24) or None,
        q=_text(query, "items_q") or None,
        source_id=_text(query, "items_source", 80) or None)
    logs = store.logs_for_run(
        conn, run_id, page=_int(query, "logs_page", 1, 1, 10 ** 6),
        page_size=_int(query, "logs_size", 25, 1, MAX_PAGE_SIZE),
        level=_text(query, "logs_level", 12) or None,
        stage=_text(query, "logs_stage", 24) or None,
        source_id=_text(query, "logs_source", 80) or None)
    return _ok({
        "run": run,
        "stages": stages,
        "stage_plan": store.TASK_STAGES.get(run["task"], []),
        "sources": sources,
        "items": items,
        "logs": logs,
        "task_label": run["task_label"],
        "status_label": run["status_label"],
        "trigger_label": run["trigger_label"],
        "stage_labels": store.STAGE_LABELS,
        "stage_status_labels": store.STAGE_STATUS_LABELS,
        "item_action_labels": store.ITEM_ACTION_LABELS,
        "source_status_labels": {"ok": "成功", "empty": "成功但 0 条", "filtered_all": "全部被过滤",
                                 "failed": "失败", "skipped": "已跳过", "paused": "熔断暂停",
                                 "not_run": "未运行", "unknown": "未知"},
        "caliber": store.CALIBER,
    })


def _sources(conn, query: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    specs, degraded = _specs()
    only_enabled = _text(query, "enabled", 6) in ("1", "true", "yes")
    kind = _text(query, "kind", 24)
    # 与 /runs 一样先做白名单：不支持的排序键不要悄悄退化成「按连续失败排」
    sort = _text(query, "sort", 24) or "failures"
    if sort not in store.SOURCE_SORTS:
        sort = "failures"
    order = "asc" if _text(query, "order", 6).lower() == "asc" else "desc"
    payload = store.sources_view(
        conn, specs, q=_text(query, "q") or None,
        kinds=[kind] if kind else None,
        statuses=_list(query, "status", store.SOURCE_STATUSES),
        only_enabled=only_enabled,
        sort=sort, order=order,
        stale_hours=_int(query, "stale_hours", store.STALE_HOURS, 1, 24 * 90))
    payload["summary"]["kinds"] = sorted({str(row["kind"]) for row in payload["sources"]
                                          if row.get("kind")})
    payload["summary"]["coverage"] = {
        "note": "「配置存在 / 当前启用 / 最近成功 / 具有新数据」是四个独立概念："
                "配置里写了不代表启用；启用不代表最近成功过；成功也不代表这次有新增条目。"
                "返回 0 条算成功（列为“成功但 0 条”）。",
        "configured": payload["summary"]["configured"],
        "enabled": payload["summary"]["enabled"],
        "recently_succeeded": payload["summary"]["configured"]
        - payload["summary"]["never_succeeded"],
        "with_new_data": payload["summary"]["with_new_data"],
    }
    payload["degraded"] = degraded
    payload["sort"] = sort
    payload["order"] = order
    return _ok(payload)


def _logs(conn, query: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    window = _window(query, 24 * 7)
    page_size = _int(query, "page_size", 50, 1, MAX_PAGE_SIZE)
    page = _int(query, "page", 1, 1, 10 ** 6)
    rows, total = store.list_logs(
        conn, from_iso=window["from"], to_iso=window["to"],
        levels=_list(query, "level", store.LEVELS),
        tasks=_list(query, "task", list(store.TASKS)),
        run_id=_text(query, "run_id", 80) or None,
        source_id=_text(query, "source_id", 80) or None,
        stages=_list(query, "stage", store.STAGES),
        error_code=_text(query, "error_code", 24) or None,
        q=_text(query, "q") or None,
        page=page, page_size=page_size,
        before_id=_int(query, "before_id", 0, 0, 10 ** 12) or None,
        since_id=_int(query, "since_id", 0, 0, 10 ** 12) or None)
    # 等级计数必须和列表用同一个窗口：window=all 时 from/to 是 None，
    # 直接拿 None 比较会让计数变成空字典，页面就会显示「ERROR 0」这种假数字。
    counts_by_level = {row["level"]: int(row["c"]) for row in conn.execute(
        "SELECT level, COUNT(1) c FROM monitor_logs "
        "WHERE (? IS NULL OR ts >= ?) AND (? IS NULL OR ts <= ?) GROUP BY level",
        (window["from"], window["from"], window["to"], window["to"]))}
    return _ok({
        "logs": rows, "total": total, "page": page, "page_size": page_size,
        "has_more": page * page_size < total, "window": window,
        "counts_by_level": counts_by_level,
        "error_codes": store.log_error_codes(conn),
        "levels": store.LEVELS,
        "note": "日志按 id 倒序分页返回，永远不会一次性返回全量；"
                "详情里的 details_json 已在写入和出参两处脱敏。",
    })


def meta_counts(conn) -> Dict[str, int]:
    return store.counts(conn)


def notes() -> Dict[str, Any]:
    """给文档/自检用的接口说明。"""
    return {
        "endpoints": [
            "GET /api/monitor/health", "GET /api/monitor/meta",
            "GET /api/monitor/overview", "GET /api/monitor/runs",
            "GET /api/monitor/timeline", "GET /api/monitor/run",
            "GET /api/monitor/run/items", "GET /api/monitor/run/logs",
            "GET /api/monitor/sources", "GET /api/monitor/source",
            "GET /api/monitor/logs", "GET /api/monitor/log",
        ],
        "readonly": True,
        "max_page_size": MAX_PAGE_SIZE,
        "instrument": "运行 / 阶段 / 来源结果 / 逐条处理结果 / 结构化日志，"
                      "全部由真实流水线在执行时写入；未执行的阶段显示为 "
                      "skipped / not_applicable / pending。",
        "redaction": "写入与出参两处都会对 message / details_json / 错误文本做脱敏；"
                     "外链只允许 http(s)。",
    }