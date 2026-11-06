"""采集监控存储：``monitor_*`` 表 + 既有真实历史记录的导入与统一查询。

设计要点
--------
* **只写真实发生的事**：运行、阶段、来源结果、逐条处理结果、结构化日志都由真实流水线
  在执行时写入（见 ``core.monitor.instrument``）。这里不做任何模拟或补零。
* **历史记录不全就诚实地空着**：计数列允许 NULL，界面显示「未知」，而不是 0。
* **旧库升级不用重跑采集**：``import_legacy`` 把已有的真实记录（``radar_runs``、
  ``paper_source_runs``、``digests``、``push_log``、``radar_push_log``、来源错误）
  一次性投影进 ``monitor_*``，带 ``origin='legacy'`` 与 ``legacy_ref``，幂等可重复执行。
* 与业务表完全隔离：只读业务表，不改任何一行业务数据。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .. import db
from . import redact

# ---------------------------------------------------------------- 字典

TASKS: "Dict[str, str]" = {
    "news_collect": "新闻 / 全球热点采集",
    "event_report": "事件聚类与日报",
    "github_daily": "GitHub 日报",
    "papers_collect": "论文采集",
    "geo_collect": "地理数据采集",
    "summary": "摘要与分析",
    "wechat_push": "微信推送",
}

# 每个任务的规范阶段顺序：详情里既显示真实执行过的阶段，也显示计划内但没执行的阶段
TASK_STAGES: "Dict[str, List[str]]" = {
    "news_collect": ["fetch", "parse", "normalize", "deduplicate", "persist"],
    "event_report": ["fetch", "parse", "normalize", "deduplicate", "persist",
                     "cluster", "score", "geo_tag", "report", "push"],
    "github_daily": ["fetch", "parse", "normalize", "deduplicate", "persist",
                     "score", "summary", "push"],
    "papers_collect": ["fetch", "parse", "normalize", "deduplicate", "persist"],
    "geo_collect": ["fetch", "parse", "normalize", "persist"],
    "summary": ["fetch", "summary"],
    "wechat_push": ["push"],
}

STAGES = ["fetch", "parse", "normalize", "deduplicate", "persist",
          "cluster", "score", "geo_tag", "report", "summary", "push"]

STAGE_LABELS = {
    "fetch": "抓取", "parse": "解析", "normalize": "规范化", "deduplicate": "去重",
    "persist": "入库", "cluster": "聚类", "score": "评分", "geo_tag": "地理标注",
    "report": "日报", "summary": "摘要", "push": "推送",
}

RUN_STATUSES = ["running", "ok", "partial", "failed", "interrupted", "unknown"]

RUN_STATUS_LABELS = {
    "running": "运行中", "ok": "成功", "partial": "部分成功", "failed": "失败",
    "interrupted": "被中断", "unknown": "未知",
}

STAGE_STATUSES = ["running", "pending", "ok", "partial", "failed",
                  "skipped", "not_applicable", "interrupted", "unknown"]

STAGE_STATUS_LABELS = {
    "running": "运行中", "pending": "待执行", "ok": "成功", "partial": "部分成功",
    "failed": "失败", "skipped": "已跳过", "not_applicable": "不适用",
    "interrupted": "被中断", "unknown": "未知",
}

SOURCE_STATUSES = ["ok", "error", "paused", "unknown", "disabled", "unavailable"]
SOURCE_STATUS_LABELS = {
    "ok": "正常", "error": "失败", "paused": "暂停", "unknown": "未知",
    "disabled": "已停用", "unavailable": "不可用",
}
# sources_view 真正支持的排序键（api 层用它做白名单，避免不支持的键悄悄退化）
SOURCE_SORTS = ["failures", "stale", "latency", "fetched", "name"]

ITEM_ACTIONS = ["new", "updated", "duplicate", "filtered", "persisted", "unknown"]
ITEM_ACTION_LABELS = {
    "new": "新增入库", "updated": "更新", "duplicate": "重复（未新增）",
    "filtered": "被过滤", "persisted": "已入库", "unknown": "未知",
}

TRIGGERS = ["manual", "schedule", "api", "cli", "startup", "queue", "import", "hourly", "unknown"]
TRIGGER_LABELS = {
    "manual": "手动", "schedule": "定时", "api": "接口触发", "cli": "命令行",
    "startup": "启动时", "queue": "队列", "import": "历史导入", "hourly": "每小时",
    "unknown": "未知",
}

LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]

ERROR_CODES = ["http_401", "http_403", "http_404", "http_429", "http_5xx", "timeout",
               "unreachable", "tls", "parse_error", "invalid_data", "auth", "not_implemented",
               "db_busy", "internal", "interrupted", "unknown"]

ERROR_CODE_LABELS = {
    "http_401": "HTTP 401 未授权", "http_403": "HTTP 403 拒绝访问", "http_404": "HTTP 404 不存在",
    "http_429": "HTTP 429 限流", "http_5xx": "HTTP 5xx 服务端错误", "timeout": "超时",
    "unreachable": "网络不可达", "tls": "TLS/证书错误", "parse_error": "解析失败",
    "invalid_data": "数据不合法", "auth": "认证/密钥问题", "not_implemented": "未接入（无合规入口）",
    "db_busy": "数据库繁忙", "internal": "内部错误", "unknown": "未知错误",
    "interrupted": "被中断",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS monitor_runs (
    run_id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    trigger TEXT NOT NULL DEFAULT 'unknown',
    started_at TEXT,
    finished_at TEXT,
    duration_ms INTEGER,
    status TEXT NOT NULL DEFAULT 'running',
    sources_attempted INTEGER,
    items_fetched INTEGER,
    items_new INTEGER,
    items_updated INTEGER,
    items_duplicate INTEGER,
    items_filtered INTEGER,
    error_count INTEGER NOT NULL DEFAULT 0,
    errors TEXT NOT NULL DEFAULT '[]',
    stats TEXT NOT NULL DEFAULT '{}',
    origin TEXT NOT NULL DEFAULT 'live',
    legacy_ref TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_monitor_runs_started ON monitor_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_monitor_runs_task ON monitor_runs(task, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_monitor_runs_status ON monitor_runs(status, started_at DESC);

CREATE TABLE IF NOT EXISTS monitor_stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    duration_ms INTEGER,
    result TEXT NOT NULL DEFAULT '{}',
    error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_monitor_stages_run_name ON monitor_stages(run_id, name);

CREATE TABLE IF NOT EXISTS monitor_source_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    name TEXT,
    platform TEXT,
    kind TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    latency_ms INTEGER,
    http_status INTEGER,
    fetched INTEGER,
    parsed INTEGER,
    items_new INTEGER,
    items_duplicate INTEGER,
    items_filtered INTEGER,
    error TEXT,
    error_code TEXT,
    started_at TEXT,
    finished_at TEXT,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_monitor_src_run ON monitor_source_results(run_id, source_id);
CREATE INDEX IF NOT EXISTS idx_monitor_src_source ON monitor_source_results(source_id, finished_at DESC);

CREATE TABLE IF NOT EXISTS monitor_item_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    source_id TEXT,
    kind TEXT NOT NULL DEFAULT 'news',
    title TEXT,
    url TEXT,
    url_hash TEXT,
    content_hash TEXT,
    published_at TEXT,
    collected_at TEXT,
    action TEXT NOT NULL,
    reason TEXT,
    item_id TEXT,
    event_id TEXT,
    story_id TEXT,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_monitor_items_run ON monitor_item_actions(run_id, id);
CREATE INDEX IF NOT EXISTS idx_monitor_items_hash ON monitor_item_actions(url_hash);

CREATE TABLE IF NOT EXISTS monitor_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    task TEXT,
    run_id TEXT,
    source_id TEXT,
    stage TEXT,
    error_code TEXT,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_monitor_logs_ts ON monitor_logs(ts DESC);
CREATE INDEX IF NOT EXISTS idx_monitor_logs_level ON monitor_logs(level, ts DESC);
CREATE INDEX IF NOT EXISTS idx_monitor_logs_run ON monitor_logs(run_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_monitor_logs_source ON monitor_logs(source_id, ts DESC);
"""

LEGACY_META_KEY = "monitor_legacy_import_v1"
CREATED_META_KEY = "monitor_created_at"
PRUNED_META_KEY = "monitor_pruned_at"

_RETENTION_DAYS = 45
_MAX_ITEMS_PER_RUN = 2000
MAX_ITEMS_PER_RUN = _MAX_ITEMS_PER_RUN
_READY = False


def ensure() -> None:
    """公开入口：确保监控表存在（同时完成历史导入与过期清理）。"""
    _ensure_ready()


# ---------------------------------------------------------------- 基础工具

def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def parse_iso(value: Any) -> Optional[dt.datetime]:
    """宽松解析：带时区/不带时区/只有日期都能解析；解析不了返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 10:
        text = text + "T00:00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now().tzinfo)
    return parsed


def age_minutes(iso: Any, reference: Optional[dt.datetime] = None) -> Optional[float]:
    parsed = parse_iso(iso)
    if parsed is None:
        return None
    return round(((reference or now()) - parsed).total_seconds() / 60.0, 1)


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except ValueError:
        return default


def _dumps(value: Any) -> str:
    try:
        return json.dumps(value if value is not None else {}, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def classify_error(text: Any, http_status: Optional[int] = None) -> str:
    """把错误文本归类成稳定的错误码，供筛选与统计使用（纯函数，不改写原文）。"""
    if http_status:
        if http_status == 401:
            return "http_401"
        if http_status == 403:
            return "http_403"
        if http_status == 404:
            return "http_404"
        if http_status == 429:
            return "http_429"
        if http_status >= 500:
            return "http_5xx"
    message = str(text or "").lower()
    if not message:
        return "unknown"
    if "http 401" in message or "unauthorized" in message or "401" in message and "http" in message:
        return "http_401"
    if "http 403" in message or "forbidden" in message or "blocked" in message:
        return "http_403"
    if "http 404" in message:
        return "http_404"
    if "http 429" in message or "ratelimit" in message or "rate limit" in message or "限流" in message:
        return "http_429"
    if "http 5" in message:
        return "http_5xx"
    if any(word in message for word in ("未接入", "not implemented", "尚未接入")):
        return "not_implemented"
    if any(word in message for word in ("timeout", "timed out", "超时")):
        return "timeout"
    if any(word in message for word in ("certificate", "ssl", "tls", "证书")):
        return "tls"
    if any(word in message for word in ("urlerror", "unreachable", "connection refused",
                                        "name or service not known", "network", "getaddrinfo",
                                        "不可达", "连接被重置", "connection reset")):
        return "unreachable"
    if any(word in message for word in ("api key", "api_key", "sendkey", "unauthorized",
                                        "认证", "密钥", "forbidden")):
        return "auth"
    if any(word in message for word in ("json", "parse", "解析", "invalid", "malformed",
                                        "expecting value", "not well-formed")):
        return "parse_error"
    if any(word in message for word in ("database is locked", "unable to open database",
                                        "sqlite")):
        return "db_busy"
    return "internal"


def _ensure_ready() -> None:
    global _READY
    if _READY:
        return
    init()


def _connect() -> sqlite3.Connection:
    """监控自己的连接：写锁等待有上限，绝不为写监控把业务拖慢。"""
    conn = db.connect()
    conn.execute("PRAGMA busy_timeout=4000")
    return conn


def init() -> None:
    """建表 + 一次性导入既有真实历史 + 过期清理（每个进程只做一次）。"""
    global _READY
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        if not db.get_meta(conn, CREATED_META_KEY):
            db.set_meta(conn, CREATED_META_KEY, now_iso())
        conn.commit()
        _READY = True
        try:
            import_legacy(conn)
        except Exception as exc:  # noqa: BLE001 - 导入失败不影响监控本身
            print("[monitor] legacy import failed:", exc)
        try:
            maybe_prune(conn)
        except Exception as exc:  # noqa: BLE001
            print("[monitor] prune failed:", exc)
    finally:
        conn.close()


def maybe_prune(conn: sqlite3.Connection, days: Optional[int] = None) -> Dict[str, int]:
    """清理过期监控记录（每天最多一次）。监控是诊断数据，保留有上限才不会无限膨胀。"""
    last = parse_iso(db.get_meta(conn, PRUNED_META_KEY))
    if last is not None and (now() - last).total_seconds() < 24 * 3600:
        return {}
    counts = prune(conn, days=days)
    db.set_meta(conn, PRUNED_META_KEY, now_iso())
    conn.commit()
    return counts


def prune(conn: sqlite3.Connection, days: Optional[int] = None) -> Dict[str, int]:
    keep = int(days or _RETENTION_DAYS)
    cutoff = (now() - dt.timedelta(days=max(1, keep))).isoformat(timespec="seconds")
    out: Dict[str, int] = {}
    stale = "SELECT run_id FROM monitor_runs WHERE COALESCE(finished_at, started_at) < ?"
    for table in ("monitor_stages", "monitor_source_results", "monitor_item_actions"):
        cur = conn.execute(
            "DELETE FROM %s WHERE run_id IN (%s)" % (table, stale), (cutoff,))
        out[table] = cur.rowcount or 0
    cur = conn.execute(
        "DELETE FROM monitor_runs WHERE COALESCE(finished_at, started_at) < ?", (cutoff,))
    out["monitor_runs"] = cur.rowcount or 0
    # 运行被保留但日志过期时，日志也要清掉（日志是最占空间的一张表）
    cur = conn.execute("DELETE FROM monitor_logs WHERE ts < ?", (cutoff,))
    out["monitor_logs"] = cur.rowcount or 0
    conn.commit()
    return out


# ---------------------------------------------------------------- 写入（供 instrument 使用）

def start_run(conn: sqlite3.Connection, run: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO monitor_runs(run_id, task, trigger, started_at, finished_at, duration_ms,
               status, sources_attempted, items_fetched, items_new, items_updated,
               items_duplicate, items_filtered, error_count, errors, stats, origin,
               legacy_ref, created_at)
           VALUES(:run_id, :task, :trigger, :started_at, :finished_at, :duration_ms, :status,
               :sources_attempted, :items_fetched, :items_new, :items_updated,
               :items_duplicate, :items_filtered, :error_count, :errors, :stats, :origin,
               :legacy_ref, :created_at)
           ON CONFLICT(run_id) DO UPDATE SET
             task=excluded.task, trigger=excluded.trigger, started_at=excluded.started_at,
             finished_at=excluded.finished_at, duration_ms=excluded.duration_ms,
             status=excluded.status, sources_attempted=excluded.sources_attempted,
             items_fetched=excluded.items_fetched, items_new=excluded.items_new,
             items_updated=excluded.items_updated, items_duplicate=excluded.items_duplicate,
             items_filtered=excluded.items_filtered, error_count=excluded.error_count,
             errors=excluded.errors, stats=excluded.stats""",
        {
            "run_id": run["run_id"], "task": run["task"],
            "trigger": run.get("trigger") or "unknown",
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "duration_ms": run.get("duration_ms"),
            "status": run.get("status") or "running",
            "sources_attempted": run.get("sources_attempted"),
            "items_fetched": run.get("items_fetched"),
            "items_new": run.get("items_new"),
            "items_updated": run.get("items_updated"),
            "items_duplicate": run.get("items_duplicate"),
            "items_filtered": run.get("items_filtered"),
            "error_count": int(run.get("error_count") or 0),
            "errors": _dumps(redact.redact_obj(run.get("errors") or [])),
            "stats": _dumps(redact.redact_obj(run.get("stats") or {})),
            "origin": run.get("origin") or "live",
            "legacy_ref": run.get("legacy_ref"),
            "created_at": run.get("created_at") or now_iso(),
        },
    )


def update_run(conn: sqlite3.Connection, run_id: str, **fields: Any) -> None:
    """按白名单更新运行记录的若干字段（None = 不改）。"""
    allowed = ("status", "started_at", "finished_at", "duration_ms", "trigger",
               "sources_attempted", "items_fetched", "items_new", "items_updated",
               "items_duplicate", "items_filtered", "error_count")
    sets, params = [], []
    for key in allowed:
        if key not in fields or fields[key] is None:
            continue
        sets.append("%s=?" % key)
        params.append(fields[key])
    if "errors" in fields and fields["errors"] is not None:
        sets.append("errors=?")
        params.append(_dumps(redact.redact_obj(fields["errors"])))
    if "stats" in fields and fields["stats"] is not None:
        sets.append("stats=?")
        params.append(_dumps(redact.redact_obj(fields["stats"])))
    if not sets:
        return
    params.append(run_id)
    conn.execute("UPDATE monitor_runs SET %s WHERE run_id=?" % ", ".join(sets), params)


def upsert_stage(conn: sqlite3.Connection, stage: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO monitor_stages(run_id, name, seq, status, started_at, finished_at,
               duration_ms, result, error)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(run_id, name) DO UPDATE SET
             seq=excluded.seq, status=excluded.status, started_at=excluded.started_at,
             finished_at=excluded.finished_at, duration_ms=excluded.duration_ms,
             result=excluded.result, error=excluded.error""",
        (stage["run_id"], stage["name"], int(stage.get("seq") or 0), stage["status"],
         stage.get("started_at"), stage.get("finished_at"), stage.get("duration_ms"),
         _dumps(redact.redact_obj(stage.get("result") or {})),
         redact.redact_text(stage.get("error") or "")[:2000] or None),
    )


def upsert_source_result(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO monitor_source_results(run_id, source_id, name, platform, kind, status,
               reason, latency_ms, http_status, fetched, parsed, items_new, items_duplicate,
               items_filtered, error, error_code, started_at, finished_at, detail)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(run_id, source_id) DO UPDATE SET
             name=excluded.name, platform=excluded.platform, kind=excluded.kind,
             status=excluded.status, reason=excluded.reason, latency_ms=excluded.latency_ms,
             http_status=excluded.http_status, fetched=excluded.fetched, parsed=excluded.parsed,
             items_new=excluded.items_new, items_duplicate=excluded.items_duplicate,
             items_filtered=excluded.items_filtered, error=excluded.error,
             error_code=excluded.error_code, started_at=excluded.started_at,
             finished_at=excluded.finished_at, detail=excluded.detail""",
        (
            row["run_id"], row["source_id"], (row.get("name") or "")[:200],
            (row.get("platform") or "")[:120], (row.get("kind") or "")[:40],
            row.get("status") or "unknown", redact.redact_text(row.get("reason") or "")[:500] or None,
            row.get("latency_ms"), row.get("http_status"), row.get("fetched"),
            row.get("parsed"), row.get("items_new"), row.get("items_duplicate"),
            row.get("items_filtered"),
            redact.redact_text(row.get("error") or "")[:2000] or None,
            row.get("error_code"), row.get("started_at"), row.get("finished_at"),
            _dumps(redact.redact_obj(row.get("detail") or {})),
        ),
    )


def insert_item_actions(conn: sqlite3.Connection, rows: Iterable[Dict[str, Any]]) -> int:
    count = 0
    for row in rows:
        conn.execute(
            """INSERT INTO monitor_item_actions(run_id, source_id, kind, title, url, url_hash,
                   content_hash, published_at, collected_at, action, reason, item_id, event_id,
                   story_id, detail)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["run_id"], row.get("source_id"), row.get("kind") or "news",
                redact.redact_text(row.get("title") or "")[:500] or None,
                redact.strip_url_secrets(row.get("url") or "")[:1000] or None,
                row.get("url_hash"), row.get("content_hash"), row.get("published_at"),
                row.get("collected_at"), row.get("action") or "unknown",
                redact.redact_text(row.get("reason") or "")[:500] or None,
                str(row["item_id"]) if row.get("item_id") not in (None, "") else None,
                row.get("event_id"), row.get("story_id"),
                _dumps(redact.redact_obj(row.get("detail") or {})),
            ),
        )
        count += 1
    return count


def insert_logs(conn: sqlite3.Connection, rows: Iterable[Dict[str, Any]]) -> int:
    count = 0
    for row in rows:
        conn.execute(
            """INSERT INTO monitor_logs(ts, level, task, run_id, source_id, stage, error_code,
                   message, details_json)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                row.get("ts") or now_iso(), (row.get("level") or "INFO").upper()[:10],
                row.get("task"), row.get("run_id"), row.get("source_id"), row.get("stage"),
                row.get("error_code"),
                redact.redact_text(row.get("message") or "")[:2000],
                _dumps(redact.redact_obj(row.get("details") or {})),
            ),
        )
        count += 1
    return count


# ---------------------------------------------------------------- 历史导入

def import_legacy(conn: sqlite3.Connection, force: bool = False) -> Dict[str, Any]:
    """把既有真实记录投影进 monitor_* （幂等）。

    每个导入器单独提交：任何一个失败都只影响它自己，而且不会写 LEGACY_META_KEY，
    下次启动会重试（已经导入过的行用 legacy_ref 判重，不会重复）。
    """
    if not force and db.get_meta(conn, LEGACY_META_KEY):
        return {"skipped": True}
    created = db.get_meta(conn, CREATED_META_KEY) or now_iso()
    db.set_meta(conn, CREATED_META_KEY, created)
    created_at = parse_iso(created) or now()
    result: Dict[str, Any] = {"radar_runs": 0, "paper_runs": 0, "digests": 0, "pushes": 0,
                              "logs": 0, "errors": []}
    importers = (
        ("radar_runs", "radar_runs", _import_radar_runs),
        ("paper_runs", "paper_source_runs", _import_paper_runs),
        ("digests", "digests", _import_digests),
        ("pushes", "push_log", _import_pushes),
        ("logs", None, _import_source_logs),
    )
    for name, table, importer in importers:
        if table and not _has_table(conn, table):
            continue   # 这部分业务还没建表：没有可导入的真实历史
        try:
            result[name] = importer(conn, created_at)
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - 单个导入器失败不影响其它
            conn.rollback()
            result["errors"].append({"importer": name,
                                     "error": "%s: %s" % (type(exc).__name__, exc)})
    if not result["errors"]:
        db.set_meta(conn, LEGACY_META_KEY, now_iso())
        conn.commit()
    return result


def _legacy_exists(conn: sqlite3.Connection, ref: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM monitor_runs WHERE legacy_ref=?", (ref,)).fetchone())


def _iso_or_none(value: Any) -> Optional[str]:
    return str(value) if value else None


def _run_row(run_id: str, task: str, trigger: str, started_at: Optional[str],
             finished_at: Optional[str], status: str, legacy_ref: str = "",
             **counts: Any) -> Dict[str, Any]:
    duration = None
    start, end = parse_iso(started_at), parse_iso(finished_at)
    if start is not None and end is not None:
        duration = max(0, int((end - start).total_seconds() * 1000))
    row = {"run_id": run_id, "task": task, "trigger": trigger, "started_at": started_at,
           "finished_at": finished_at, "duration_ms": duration, "status": status,
           "origin": "legacy", "legacy_ref": legacy_ref or run_id,
           "created_at": started_at or now_iso()}
    row.update({k: v for k, v in counts.items() if v is not None})
    return row


def _import_radar_runs(conn: sqlite3.Connection, created_at: dt.datetime) -> int:
    count = 0
    rows = conn.execute(
        "SELECT * FROM radar_runs ORDER BY started_at ASC").fetchall()
    for row in rows:
        source_id = str(row["id"])
        ref = "radar_runs:" + source_id
        if _legacy_exists(conn, ref):
            continue
        started = parse_iso(row["started_at"])
        if started is not None and started > created_at:
            continue  # 监控上线之后新产生的运行由实时埋点负责，不要再导入一遍
        stats = _loads(row["stats"], {})
        if not isinstance(stats, dict):
            stats = {}
        collect = stats.get("collect") or {}
        failures = collect.get("failures") or []
        per_source = collect.get("per_source") or []
        geo_layers = collect.get("geo_layers") or []
        error_text = redact.redact_text(row["error"] or "")
        status = {"ok": "ok", "partial": "partial", "failed": "failed",
                  "running": "interrupted"}.get(str(row["status"]), "unknown")
        trigger = {"schedule": "schedule", "api": "api", "cli": "cli", "manual": "manual",
                   "hourly": "hourly"}.get(str(stats.get("reason") or ""), "import")
        run_id = "legacy-radar-" + source_id.replace("run-", "")
        errors = []
        if error_text:
            errors.append({"message": error_text, "at": _iso_or_none(row["finished_at"]),
                           "error_code": classify_error(error_text)})
        for failure in failures:
            if not isinstance(failure, dict):
                continue
            message = redact.redact_text("%s: %s" % (failure.get("name") or failure.get("source_id"),
                                                    failure.get("error") or ""))
            errors.append({"message": message, "at": _iso_or_none(row["finished_at"]),
                           "source_id": failure.get("source_id"),
                           "error_code": classify_error(failure.get("error"))})
        start_iso, end_iso = _iso_or_none(row["started_at"]), _iso_or_none(row["finished_at"])
        start_run(conn, _run_row(
            run_id, "event_report", trigger, start_iso, end_iso, status, legacy_ref=ref,
            sources_attempted=collect.get("sources_enabled"),
            items_fetched=collect.get("items_raw"),
            items_new=collect.get("items_new"),
            items_duplicate=collect.get("items_seen"),
            items_filtered=collect.get("items_filtered"),
            error_count=len(errors),
            errors=errors,
            stats={
                "imported_from": "radar_runs",
                "reason": stats.get("reason"),
                "events": stats.get("events"),
                "stories": stats.get("stories"),
                "items_considered": stats.get("items_considered"),
                "geo_tag": stats.get("geo_tag"),
                "threat_snapshot": stats.get("threat_snapshot"),
                "report_date": stats.get("report_date"),
                "pruned": stats.get("pruned"),
                "push": stats.get("push"),
                "skipped_sources": collect.get("sources_skipped"),
                "geo_layers": geo_layers,
            },
        ))
        seq = 0
        sources_ok = int(collect.get("sources_ok") or 0)
        sources_failed = int(collect.get("sources_failed") or 0)
        if collect:
            seq += 1
            upsert_stage(conn, {
                "run_id": run_id, "name": "fetch", "seq": seq,
                "status": ("ok" if sources_failed == 0 and sources_ok > 0
                           else "partial" if sources_ok > 0 else "failed" if sources_failed else "unknown"),
                "started_at": start_iso, "finished_at": end_iso, "duration_ms": None,
                "result": {"sources_ok": sources_ok, "sources_failed": sources_failed,
                           "sources_skipped": collect.get("sources_skipped"),
                           "items_fetched": collect.get("items_raw"),
                           "note": "历史导入：只有本轮合计值，没有逐来源分阶段计时"},
            })
            seq += 1
            upsert_stage(conn, {
                "run_id": run_id, "name": "persist", "seq": seq, "status": "ok",
                "started_at": None, "finished_at": end_iso, "duration_ms": None,
                "result": {"items_new": collect.get("items_new"),
                           "items_duplicate": collect.get("items_seen"),
                           "note": "历史导入：入库计数来自本轮合计值"},
            })
        if stats.get("events") is not None:
            seq += 1
            upsert_stage(conn, {
                "run_id": run_id, "name": "cluster", "seq": seq, "status": "ok",
                "started_at": None, "finished_at": end_iso, "duration_ms": None,
                "result": {"events": stats.get("events"), "stories": stats.get("stories"),
                           "items_considered": stats.get("items_considered"),
                           "note": "历史导入：聚类/评分在同一阶段内完成，未分阶段计时"},
            })
        if stats.get("report_date"):
            seq += 1
            upsert_stage(conn, {
                "run_id": run_id, "name": "report", "seq": seq, "status": "ok",
                "started_at": None, "finished_at": end_iso, "duration_ms": None,
                "result": {"report_date": stats.get("report_date"),
                           "threat_snapshot": stats.get("threat_snapshot")},
            })
        push_info = stats.get("push")
        if isinstance(push_info, dict):
            seq += 1
            push_status = ("ok" if push_info.get("ok") else
                           "skipped" if push_info.get("skipped") else "failed")
            upsert_stage(conn, {
                "run_id": run_id, "name": "push", "seq": seq, "status": push_status,
                "started_at": None, "finished_at": end_iso, "duration_ms": None,
                "result": {"ok": bool(push_info.get("ok")),
                           "skipped": bool(push_info.get("skipped")),
                           "reason": redact.redact_text(push_info.get("reason") or ""),
                           "detail": redact.redact_text(push_info.get("detail") or "")},
                "error": None if push_info.get("ok") else
                         redact.redact_text(push_info.get("reason") or push_info.get("detail") or ""),
            })

        # 逐来源结果：真实数字来自本轮 per_source / failures
        by_source = {str(entry.get("source_id")): entry for entry in per_source
                     if isinstance(entry, dict)}
        for failure in failures:
            if isinstance(failure, dict):
                by_source.setdefault(str(failure.get("source_id")), {})
        for entry in geo_layers:
            if isinstance(entry, dict):
                by_source.setdefault(str(entry.get("source_id")), {})
        for source_id, entry in by_source.items():
            failure = next((f for f in failures
                            if isinstance(f, dict) and str(f.get("source_id")) == source_id), None)
            layer = next((g for g in geo_layers
                          if isinstance(g, dict) and str(g.get("source_id")) == source_id), None)
            if failure:
                error = redact.redact_text(failure.get("error") or "")
                upsert_source_result(conn, {
                    "run_id": run_id, "source_id": source_id, "name": failure.get("name"),
                    "status": "failed", "error": error,
                    "error_code": classify_error(failure.get("error")),
                    "fetched": 0, "started_at": start_iso, "finished_at": end_iso,
                    "detail": {"imported_from": "radar_runs.collect.failures"},
                })
                continue
            if layer:
                upsert_source_result(conn, {
                    "run_id": run_id, "source_id": source_id, "kind": "geo",
                    "status": "ok", "fetched": layer.get("points"), "parsed": layer.get("points"),
                    "items_new": layer.get("inserted"), "items_updated": None,
                    "started_at": start_iso, "finished_at": end_iso,
                    "detail": {"imported_from": "radar_runs.collect.geo_layers",
                               "updated": layer.get("updated"), "layer": layer.get("layer")},
                })
                continue
            fetched = entry.get("raw")
            kept = entry.get("kept")
            upsert_source_result(conn, {
                "run_id": run_id, "source_id": source_id, "status":
                    "empty" if not kept else "ok",
                "reason": None if kept else "本次返回 0 条可用条目（不等于失败）",
                "fetched": fetched, "parsed": fetched,
                "items_new": entry.get("new"), "items_filtered":
                    (int(entry.get("dropped_noise") or 0) + int(entry.get("dropped_low_engagement") or 0)
                     + int(entry.get("truncated") or 0)) if entry else None,
                "started_at": start_iso, "finished_at": end_iso,
                "detail": {"imported_from": "radar_runs.collect.per_source",
                           "dropped_noise": entry.get("dropped_noise"),
                           "dropped_low_engagement": entry.get("dropped_low_engagement"),
                           "truncated": entry.get("truncated"), "kept": kept},
            })

        # 逐条处理结果：历史运行没有逐条动作，只能定位「采集窗口内的条目」，动作标为未知
        if by_source and start_iso and end_iso:
            _import_window_items(conn, run_id, start_iso, end_iso, list(by_source.keys()),
                                 kind="news")
        count += 1
    return count


def _import_window_items(conn: sqlite3.Connection, run_id: str, start_iso: str, end_iso: str,
                         source_ids: List[str], kind: str, limit: int = 400) -> int:
    if not (source_ids and start_iso and end_iso):
        return 0
    table = "radar_items" if kind == "news" else "paper_items"
    if not _has_table(conn, table):
        return 0
    placeholders = ",".join("?" for _ in source_ids)
    if kind == "news":
        sql = ("SELECT id, source_id, title, url, url_hash, content_hash, published_at, "
               "collected_at, event_id FROM radar_items WHERE collected_at >= ? AND "
               "collected_at <= ? AND source_id IN (%s) ORDER BY collected_at ASC LIMIT ?"
               % placeholders)
    else:
        sql = ("SELECT id, source AS source_id, title, url, '' AS url_hash, '' AS content_hash, "
               "published_at, fetched_at AS collected_at, NULL AS event_id FROM paper_items "
               "WHERE fetched_at >= ? AND fetched_at <= ? AND source IN (%s) "
               "ORDER BY fetched_at ASC LIMIT ?" % placeholders)
    rows = conn.execute(sql, [start_iso, end_iso] + source_ids + [limit]).fetchall()
    payload = []
    for row in rows:
        data = dict(row)
        payload.append({
            "run_id": run_id, "source_id": data["source_id"], "kind": kind,
            "title": data["title"], "url": data["url"], "url_hash": data["url_hash"],
            "content_hash": data["content_hash"], "published_at": data["published_at"],
            "collected_at": data["collected_at"],
            "action": "persisted" if kind == "paper" else "unknown",
            "reason": ("历史导入：论文记录时间落在本次运行窗口内（已入库，未记录新增/合并明细）"
                       if kind == "paper" else
                       "历史导入：不在雷达库里的条目没有被记录过处理结果"),
            "item_id": data["id"], "event_id": data["event_id"],
            "detail": {"imported_from": "radar_items" if kind == "news" else "paper_items"},
        })
    return insert_item_actions(conn, payload)


def _import_paper_runs(conn: sqlite3.Connection, created_at: dt.datetime) -> int:
    rows = conn.execute("SELECT * FROM paper_source_runs ORDER BY id ASC").fetchall()
    groups: "Dict[str, List[Dict[str, Any]]]" = {}
    for row in rows:
        start = parse_iso(row["started_at"])
        if start is not None and start > created_at:
            continue
        groups.setdefault(str(row["started_at"]), []).append(dict(row))
    count = 0
    for started_at, group in groups.items():
        ref = "paper_source_runs:" + started_at
        if _legacy_exists(conn, ref):
            continue
        digest = hashlib.sha1(started_at.encode("utf-8")).hexdigest()[:12]
        run_id = "legacy-paper-" + digest
        finished = max([str(g.get("finished_at") or "") for g in group] or [""])
        finished_iso = finished or None
        statuses = {str(g.get("status")) for g in group}
        status = ("ok" if statuses <= {"ok"} else "failed" if statuses <= {"failed"}
                  else "partial" if "ok" in statuses else "unknown")
        errors = []
        for row in group:
            if row.get("error"):
                errors.append({"message": redact.redact_text(row["error"]), "at": row.get("finished_at"),
                               "source_id": row.get("source"),
                               "error_code": classify_error(row.get("error"))})
        start_run(conn, _run_row(
            run_id, "papers_collect", "import", _iso_or_none(started_at), finished_iso, status,
            legacy_ref=ref,
            sources_attempted=len(group),
            items_fetched=sum(int(g.get("fetched") or 0) for g in group),
            error_count=len(errors), errors=errors,
            stats={"imported_from": "paper_source_runs",
                   "with_abstract": sum(int(g.get("with_abstract") or 0) for g in group),
                   "note": "历史记录只有逐来源的抓取数，没有新增/合并明细"},
        ))
        seq = 0
        fetched = sum(int(g.get("fetched") or 0) for g in group)
        failed = [g for g in group if str(g.get("status")) != "ok"]
        seq += 1
        upsert_stage(conn, {
            "run_id": run_id, "name": "fetch", "seq": seq,
            "status": "ok" if not failed else ("partial" if len(failed) < len(group) else "failed"),
            "started_at": _iso_or_none(started_at), "finished_at": finished_iso,
            "duration_ms": max(int(g.get("duration_ms") or 0) for g in group) or None,
            "result": {"sources": len(group), "sources_failed": len(failed),
                       "items_fetched": fetched,
                       "note": "历史导入：耗时取本轮最慢来源"},
        })
        seq += 1
        upsert_stage(conn, {
            "run_id": run_id, "name": "persist", "seq": seq,
            "status": "ok" if fetched else "unknown",
            "started_at": None, "finished_at": finished_iso, "duration_ms": None,
            "result": {"note": "历史记录未保存本次入库/合并条数，新增与重复显示为未知"},
        })
        for row in group:
            upsert_source_result(conn, {
                "run_id": run_id, "source_id": str(row.get("source") or ""), "kind": "paper",
                "name": str(row.get("source") or ""),
                "status": "failed" if str(row.get("status")) != "ok" else
                          ("empty" if not row.get("fetched") else "ok"),
                "reason": None if row.get("fetched") else "本次返回 0 篇（不等于失败）",
                "latency_ms": row.get("duration_ms"), "fetched": row.get("fetched"),
                "parsed": row.get("fetched"),
                "error": redact.redact_text(row.get("error") or "") or None,
                "error_code": classify_error(row.get("error")) if row.get("error") else None,
                "started_at": _iso_or_none(row.get("started_at")),
                "finished_at": _iso_or_none(row.get("finished_at")),
                "detail": {"imported_from": "paper_source_runs",
                           "with_abstract": row.get("with_abstract")},
            })
        if finished_iso and started_at:
            _import_window_items(conn, run_id, _iso_or_none(started_at), finished_iso,
                                 [str(g.get("source")) for g in group], kind="paper")
        count += 1
    return count


def _import_digests(conn: sqlite3.Connection, created_at: dt.datetime) -> int:
    rows = conn.execute("SELECT * FROM digests ORDER BY date DESC LIMIT 60").fetchall()
    count = 0
    for row in rows:
        ref = "digests:" + str(row["date"])
        if _legacy_exists(conn, ref):
            continue
        generated = _iso_or_none(row["generated_at"])
        stamp = parse_iso(generated)
        if stamp is not None and stamp > created_at:
            continue
        payload = _loads(row["payload"], {})
        if not isinstance(payload, dict):
            payload = {}
        items = payload.get("items") or []
        push_row = conn.execute(
            "SELECT * FROM push_log WHERE date=?", (row["date"],)).fetchone() \
            if _has_table(conn, "push_log") else None
        push_data = dict(push_row) if push_row else None
        run_id = "legacy-digest-" + str(row["date"])
        start_run(conn, _run_row(
            run_id, "github_daily", "import", None, generated, "ok", legacy_ref=ref,
            items_fetched=len(items), items_new=payload.get("new_count"),
            error_count=0, errors=[],
            stats={"imported_from": "digests",
                   "note": "历史日报没有记录开始时间，开始时间显示为未知",
                   "source_counts": payload.get("source_counts"),
                   "profile_ready": (payload.get("profile") or {}).get("ready"),
                   "new_count": payload.get("new_count"),
                   "push": {"ok": bool(push_data and push_data["status"] == "sent"),
                            "skipped": push_data is None,
                            "detail": redact.redact_text(push_data.get("detail") or "")
                            if push_data else "当天没有推送记录"}},
        ))
        source_counts = payload.get("source_counts") or {}
        upsert_stage(conn, {
            "run_id": run_id, "name": "fetch", "seq": 1,
            "status": "ok" if source_counts else "unknown",
            "started_at": None, "finished_at": generated, "duration_ms": None,
            "result": {"daily": source_counts.get("daily"), "weekly": source_counts.get("weekly"),
                       "fresh": source_counts.get("fresh"),
                       "total_pool": source_counts.get("total_pool"),
                       "fallback": source_counts.get("fallback"),
                       "note": "历史导入：来自当日日报 payload 的 source_counts"},
        })
        upsert_stage(conn, {
            "run_id": run_id, "name": "persist", "seq": 2, "status": "ok",
            "started_at": None, "finished_at": generated, "duration_ms": None,
            "result": {"items": len(items), "note": "写入 digests 表"},
        })
        if any(item.get("brief") for item in items if isinstance(item, dict)):
            upsert_stage(conn, {
                "run_id": run_id, "name": "summary", "seq": 3, "status": "ok",
                "started_at": None, "finished_at": generated, "duration_ms": None,
                "result": {"with_brief": len([i for i in items
                                              if isinstance(i, dict) and i.get("brief")])},
            })
        if push_data is not None:
            upsert_stage(conn, {
                "run_id": run_id, "name": "push", "seq": 4,
                "status": "ok" if push_data["status"] == "sent" else "failed",
                "started_at": _iso_or_none(push_data["created_at"]),
                "finished_at": _iso_or_none(push_data["updated_at"]), "duration_ms": None,
                "result": {"channel": push_data["channel"], "status": push_data["status"],
                           "title": push_data["title"],
                           "detail": redact.redact_text(push_data["detail"] or "")},
            })
        actions = []
        for item in items[:60]:
            if not isinstance(item, dict):
                continue
            actions.append({
                "run_id": run_id, "source_id": "github", "kind": "github",
                "title": item.get("full_name"),
                "url": item.get("url"), "url_hash": "",
                "content_hash": item.get("full_name"),
                "published_at": item.get("created_at"), "collected_at": generated,
                "action": "persisted", "reason": "历史导入：来自当日日报 payload（是否新增未逐条记录）",
                "item_id": item.get("full_name"),
                "detail": {"rank": item.get("rank"), "stars": item.get("stars"),
                           "language": item.get("language"), "score": item.get("score"),
                           "sources": item.get("sources")},
            })
        insert_item_actions(conn, actions)
        count += 1
    return count


def _import_pushes(conn: sqlite3.Connection, created_at: dt.datetime) -> int:
    count = 0
    rows = conn.execute("SELECT * FROM radar_push_log ORDER BY updated_at DESC LIMIT 90").fetchall() \
        if _has_table(conn, "radar_push_log") else []
    for row in rows:
        ref = "radar_push_log:%s:%s" % (row["date"], row["channel"])
        if _legacy_exists(conn, ref):
            continue
        stamp = parse_iso(row["updated_at"])
        if stamp is not None and stamp > created_at:
            continue
        run_id = "legacy-push-radar-%s-%s" % (row["date"], str(row["channel"]).replace(" ", "-"))
        detail = redact.redact_text(row["detail"] or "")
        status = "ok" if row["status"] == "sent" else "failed"
        start_run(conn, _run_row(
            run_id, "wechat_push", "import", _iso_or_none(row["created_at"]),
            _iso_or_none(row["updated_at"]), status, legacy_ref=ref,
            error_count=0 if status == "ok" else 1,
            errors=[] if status == "ok" else [{"message": detail or "推送失败",
                                               "at": row["updated_at"],
                                               "error_code": classify_error(detail)}],
            stats={"imported_from": "radar_push_log", "channel": row["channel"],
                   "title": row["title"], "attempts": row["attempts"],
                   "content_hash": row["content_hash"], "detail": detail},
        ))
        upsert_stage(conn, {
            "run_id": run_id, "name": "push", "seq": 1, "status": status,
            "started_at": _iso_or_none(row["created_at"]),
            "finished_at": _iso_or_none(row["updated_at"]), "duration_ms": None,
            "result": {"channel": row["channel"], "attempts": row["attempts"], "detail": detail},
            "error": None if status == "ok" else (detail or "推送失败"),
        })
        count += 1
    rows = conn.execute("SELECT * FROM push_log ORDER BY updated_at DESC LIMIT 90").fetchall() \
        if _has_table(conn, "push_log") else []
    for row in rows:
        ref = "push_log:%s:%s" % (row["date"], row["channel"])
        if _legacy_exists(conn, ref):
            continue
        stamp = parse_iso(row["updated_at"])
        if stamp is not None and stamp > created_at:
            continue
        run_id = "legacy-push-github-%s" % row["date"]
        detail = redact.redact_text(row["detail"] or "")
        status = "ok" if row["status"] == "sent" else "failed"
        start_run(conn, _run_row(
            run_id, "wechat_push", "import", _iso_or_none(row["created_at"]),
            _iso_or_none(row["updated_at"]), status, legacy_ref=ref,
            error_count=0 if status == "ok" else 1,
            errors=[] if status == "ok" else [{"message": detail or "推送失败",
                                               "at": row["updated_at"],
                                               "error_code": classify_error(detail)}],
            stats={"imported_from": "push_log", "channel": row["channel"],
                   "title": row["title"], "detail": detail},
        ))
        upsert_stage(conn, {
            "run_id": run_id, "name": "push", "seq": 1, "status": status,
            "started_at": _iso_or_none(row["created_at"]),
            "finished_at": _iso_or_none(row["updated_at"]), "duration_ms": None,
            "result": {"channel": row["channel"], "title": row["title"], "detail": detail},
            "error": None if status == "ok" else (detail or "推送失败"),
        })
        count += 1
    return count


def _log_exists(conn: sqlite3.Connection, key: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM monitor_logs WHERE details_json LIKE ? LIMIT 1",
        ('%"legacy_key": "' + key + '"%',)).fetchone())


def _import_source_logs(conn: sqlite3.Connection, created_at: dt.datetime) -> int:
    """既有真实错误（来源最近错误、暂停原因、运行错误）作为结构化日志导入一次。"""
    payload = []
    if _has_table(conn, "radar_sources"):
        for row in conn.execute(
                """SELECT source_id, name, platform, kind, last_error, last_error_at,
                          paused_reason, consecutive_failures
                   FROM radar_sources
                   WHERE (last_error IS NOT NULL AND last_error <> '')
                      OR (paused_reason IS NOT NULL AND paused_reason <> '')"""):
            error_at = _iso_or_none(row["last_error_at"])
            stamp = parse_iso(error_at)
            if stamp is not None and stamp > created_at:
                continue
            if row["last_error"]:
                key = "radar_sources:%s:%s" % (row["source_id"], error_at)
                if _log_exists(conn, key):
                    continue
                payload.append({
                    "ts": error_at or now_iso(), "level": "ERROR", "task": "news_collect",
                    "source_id": row["source_id"], "stage": "fetch",
                    "error_code": classify_error(row["last_error"]),
                    "message": "最近一次错误（累计连续失败 %s 次）：%s" % (
                        row["consecutive_failures"], row["last_error"]),
                    "details": {"imported_from": "radar_sources", "legacy_key": key,
                                "name": row["name"], "platform": row["platform"],
                                "kind": row["kind"],
                                "consecutive_failures": row["consecutive_failures"],
                                "last_error_at": error_at},
                })
            if row["paused_reason"]:
                key = "radar_sources-paused:%s:%s" % (row["source_id"], row["paused_reason"])
                if _log_exists(conn, key):
                    continue
                payload.append({
                    "ts": error_at or now_iso(), "level": "WARNING", "task": "news_collect",
                    "source_id": row["source_id"], "stage": "fetch",
                    "error_code": None,
                    "message": "来源已被熔断暂停：%s" % row["paused_reason"],
                    "details": {"imported_from": "radar_sources", "legacy_key": key,
                                "name": row["name"], "platform": row["platform"],
                                "kind": row["kind"]},
                })
    if _has_table(conn, "radar_runs"):
        for row in conn.execute(
                "SELECT id, started_at, error FROM radar_runs "
                "WHERE error IS NOT NULL AND error <> '' LIMIT 200"):
            key = "radar_runs:" + str(row["id"])
            stamp = parse_iso(row["started_at"])
            if stamp is not None and stamp > created_at:
                continue
            if _log_exists(conn, key):
                continue
            payload.append({
                "ts": _iso_or_none(row["started_at"]) or now_iso(), "level": "ERROR",
                "task": "event_report",
                "run_id": "legacy-radar-" + str(row["id"]).replace("run-", ""),
                "stage": "report",
                "error_code": classify_error(row["error"]),
                "message": "运行失败：%s" % row["error"],
                "details": {"imported_from": "radar_runs", "legacy_key": key},
            })
    if _has_table(conn, "paper_source_runs"):
        for row in conn.execute(
                "SELECT id, source, started_at, error FROM paper_source_runs "
                "WHERE error IS NOT NULL AND error <> '' LIMIT 200"):
            key = "paper_source_runs:" + str(row["id"])
            stamp = parse_iso(row["started_at"])
            if stamp is not None and stamp > created_at:
                continue
            if _log_exists(conn, key):
                continue
            payload.append({
                "ts": _iso_or_none(row["started_at"]) or now_iso(), "level": "ERROR",
                "task": "papers_collect", "source_id": row["source"], "stage": "fetch",
                "error_code": classify_error(row["error"]),
                "message": "论文来源失败：%s" % row["error"],
                "details": {"imported_from": "paper_source_runs", "legacy_key": key},
            })
    return insert_logs(conn, payload)


# ---------------------------------------------------------------- 查询：运行

_RUN_FILTER_COLUMNS = {
    "started_at": "COALESCE(started_at, finished_at)",
    "duration": "duration_ms",
    "error_count": "error_count",
}

_SAFE_TIME_EXPR = "COALESCE(started_at, finished_at)"


def _window_clause(from_iso: Optional[str], to_iso: Optional[str]) -> Tuple[str, List[Any]]:
    where, params = [], []
    if from_iso:
        where.append("%s >= ?" % _SAFE_TIME_EXPR)
        params.append(from_iso)
    if to_iso:
        where.append("%s <= ?" % _SAFE_TIME_EXPR)
        params.append(to_iso)
    return (" AND ".join(where), params)


def list_runs(conn: sqlite3.Connection, *, from_iso: Optional[str] = None,
              to_iso: Optional[str] = None, tasks: Optional[List[str]] = None,
              statuses: Optional[List[str]] = None, triggers: Optional[List[str]] = None,
              origin: Optional[str] = None, q: Optional[str] = None,
              sort: str = "started_at", order: str = "desc",
              page: int = 1, page_size: int = 25) -> Tuple[List[Dict[str, Any]], int]:
    where, params = [], []
    window, window_params = _window_clause(from_iso, to_iso)
    if window:
        where.append(window)
        params.extend(window_params)
    if tasks:
        where.append("task IN (%s)" % ",".join("?" for _ in tasks))
        params.extend(tasks)
    if statuses:
        where.append("status IN (%s)" % ",".join("?" for _ in statuses))
        params.extend(statuses)
    if triggers:
        where.append("trigger IN (%s)" % ",".join("?" for _ in triggers))
        params.extend(triggers)
    if origin in ("live", "legacy"):
        where.append("origin = ?")
        params.append(origin)
    text = (q or "").strip()[:80]
    if text:
        like = "%" + text + "%"
        where.append("(run_id LIKE ? OR task LIKE ? OR trigger LIKE ? OR errors LIKE ? "
                     "OR stats LIKE ? OR status LIKE ?)")
        params.extend([like] * 6)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = int(conn.execute("SELECT COUNT(1) c FROM monitor_runs" + clause, params).fetchone()["c"])
    column = _RUN_FILTER_COLUMNS.get(sort, "COALESCE(started_at, finished_at)")
    direction = "ASC" if str(order).lower() == "asc" else "DESC"
    # NULL 永远排在最后，避免「未知」的时间/耗时把有数据的运行挤下去
    order_sql = "%s IS NULL, %s %s" % (column, column, direction)
    offset = max(0, (page - 1) * page_size)
    rows = conn.execute(
        "SELECT * FROM monitor_runs%s ORDER BY %s, run_id LIMIT ? OFFSET ?" % (
            clause, order_sql), params + [page_size, offset]).fetchall()
    return [_run_view(dict(row)) for row in rows], total


def _duration_ms(row: Dict[str, Any]) -> Optional[int]:
    if row.get("duration_ms") is not None:
        return int(row["duration_ms"])
    start, end = parse_iso(row.get("started_at")), parse_iso(row.get("finished_at"))
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def _run_view(row: Dict[str, Any]) -> Dict[str, Any]:
    errors = _loads(row.get("errors"), [])
    stats = _loads(row.get("stats"), {})
    out = dict(row)
    out["errors"] = redact.redact_obj(errors)
    out["stats"] = redact.redact_obj(stats)
    out["duration_ms"] = _duration_ms(row)
    out["task_label"] = TASKS.get(row["task"], row["task"])
    out["status_label"] = RUN_STATUS_LABELS.get(row["status"], row["status"])
    out["trigger_label"] = TRIGGER_LABELS.get(row["trigger"], row["trigger"])
    out["live"] = row.get("origin") != "legacy"
    out["started_known"] = bool(row.get("started_at"))
    out["finished_known"] = bool(row.get("finished_at"))
    return out


def get_run(conn: sqlite3.Connection, run_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM monitor_runs WHERE run_id=?", (run_id,)).fetchone()
    return _run_view(dict(row)) if row else None


def compose_stages(conn: sqlite3.Connection, run: Dict[str, Any]) -> List[Dict[str, Any]]:
    """真实执行过的阶段 + 计划内但没执行的阶段（不能把没执行显示成成功）。"""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM monitor_stages WHERE run_id=? ORDER BY seq, id", (run["run_id"],))]
    seen = {row["name"]: row for row in rows}
    plan = TASK_STAGES.get(run["task"], [])
    for row in rows:
        if row["name"] not in plan:
            plan = plan + [row["name"]]
    out: List[Dict[str, Any]] = []
    status = str(run.get("status") or "unknown")
    legacy = str(run.get("origin") or "") == "legacy"
    last_executed = max([int(seen[name].get("seq") or 0) for name in seen] or [0])
    for index, name in enumerate(plan, 1):
        row = seen.get(name)
        if row is None:
            if legacy:
                # 旧记录根本没有分阶段数据：既不能说“执行了”，也不能断言“没执行”
                state, note = "unknown", ("历史导入：旧记录没有保留这个阶段的独立结果，"
                                          "无法区分“未执行”和“执行过但未记录”")
            elif status == "running":
                state, note = "pending", "任务仍在运行，尚未进入该阶段"
            else:
                state, note = "skipped", "本次运行没有执行该阶段（未记录）"
                if status in ("failed", "interrupted") and index > (last_executed or 0):
                    note = "运行在更早阶段%s，未进入该阶段" % (
                        "失败" if status == "failed" else "被中断")
            row = {"name": name, "seq": index, "status": state, "started_at": None,
                   "finished_at": None, "duration_ms": None, "result": {"note": note},
                   "error": None, "composed": True}
        else:
            row = dict(row)
            row["result"] = redact.redact_obj(_loads(row.get("result"), {}))
            row["error"] = redact.redact_text(row.get("error") or "") or None
            row["seq"] = int(row.get("seq") or index)
            row["composed"] = False
        row["label"] = STAGE_LABELS.get(name, name)
        row["status_label"] = STAGE_STATUS_LABELS.get(row["status"], row["status"])
        out.append(row)
    return out


def run_sources(conn: sqlite3.Connection, run_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM monitor_source_results WHERE run_id=? ORDER BY (status='failed') DESC, "
        "source_id", (run_id,)).fetchall()
    out = []
    for row in rows:
        data = dict(row)
        data["detail"] = redact.redact_obj(_loads(data.get("detail"), {}))
        data["error"] = redact.redact_text(data.get("error") or "") or None
        data["reason"] = redact.redact_text(data.get("reason") or "") or None
        data["status_label"] = _source_result_label(data["status"])
        out.append(data)
    return out


def _source_result_label(status: str) -> str:
    return {"ok": "成功", "empty": "成功但 0 条", "filtered_all": "全部被过滤",
            "failed": "失败", "skipped": "已跳过", "paused": "熔断暂停",
            "not_run": "未运行", "unknown": "未知"}.get(status, status)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    """监控要读业务表（radar_items / paper_items / radar_sources …）做关联展示。

    这些表由各自模块按需创建，在「那部分业务还没跑过」的库里可能还不存在。
    监控是只读旁路：缺表时必须退化成「没有这一列」，而不是让整个页面 500。
    """
    try:
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,)).fetchone())
    except sqlite3.Error:
        return False


def _item_select(conn: sqlite3.Connection) -> str:
    has_items = _has_table(conn, "radar_items")
    has_events = _has_table(conn, "radar_events")
    has_papers = _has_table(conn, "paper_items")
    joins = []
    if has_items:
        joins.append("LEFT JOIN radar_items i ON a.kind = 'news' AND a.url_hash IS NOT NULL "
                     "AND a.url_hash <> '' AND i.url_hash = a.url_hash")
    if has_events:
        ref = "COALESCE(i.event_id, a.event_id)" if has_items else "a.event_id"
        joins.append("LEFT JOIN radar_events e ON e.id = %s" % ref)
    if has_papers:
        joins.append("LEFT JOIN paper_items p ON a.kind = 'paper' AND p.id = a.item_id")
    return ("SELECT a.*, %s AS radar_id, %s AS radar_event_id, %s AS radar_story_id, "
            "%s AS paper_present FROM monitor_item_actions a %s" % (
                "i.id" if has_items else "NULL",
                "i.event_id" if has_items else "NULL",
                "e.story_id" if has_events else "NULL",
                "p.id" if has_papers else "NULL",
                " ".join(joins)))


def run_items(conn: sqlite3.Connection, run_id: str, *, page: int = 1, page_size: int = 50,
              action: Optional[str] = None, q: Optional[str] = None,
              source_id: Optional[str] = None) -> Tuple[List[Dict[str, Any]], int, Dict[str, Any]]:
    where = ["a.run_id = ?"]
    params: List[Any] = [run_id]
    if action:
        where.append("a.action = ?")
        params.append(action)
    if source_id:
        where.append("a.source_id = ?")
        params.append(source_id)
    text = (q or "").strip()[:80]
    if text:
        where.append("(a.title LIKE ? OR a.url LIKE ? OR a.reason LIKE ? OR a.item_id LIKE ?)")
        params.extend(["%" + text + "%"] * 4)
    clause = " WHERE " + " AND ".join(where)
    total = int(conn.execute(
        "SELECT COUNT(1) c FROM monitor_item_actions a" + clause, params).fetchone()["c"])
    offset = max(0, (page - 1) * page_size)
    rows = conn.execute(_item_select(conn) + clause + " ORDER BY a.id ASC LIMIT ? OFFSET ?",
                        params + [page_size, offset]).fetchall()
    items = [_item_view(dict(row)) for row in rows]
    counts = {row["action"]: int(row["c"]) for row in conn.execute(
        "SELECT action, COUNT(1) c FROM monitor_item_actions WHERE run_id=? GROUP BY action",
        (run_id,))}
    summary = {"total": total, "by_action": counts}
    return items, total, summary


def _item_view(row: Dict[str, Any]) -> Dict[str, Any]:
    """单条信息追踪。任何字段缺失都不能让整张表报错。"""
    detail = _loads(row.get("detail"), {})
    kind = str(row.get("kind") or "news")
    radar_id = row.get("radar_id")
    url_hash = row.get("url_hash") or ""
    in_radar: Optional[bool] = None
    if kind == "news" and url_hash:
        in_radar = radar_id is not None
    paper_present: Optional[bool] = None
    if kind == "paper":
        paper_present = row.get("paper_present") is not None
    event_id = row.get("radar_event_id") or row.get("event_id")
    story_id = row.get("radar_story_id")
    return {
        "kind": kind,
        "title": redact.redact_text(row.get("title") or "") or None,
        "url": redact.strip_url_secrets(row.get("url") or ""),
        "link": redact.safe_link(row.get("url") or ""),
        "source_id": row.get("source_id"),
        "published_at": row.get("published_at") or None,
        "collected_at": row.get("collected_at") or None,
        "action": row.get("action"),
        "action_label": ITEM_ACTION_LABELS.get(row.get("action"), row.get("action")),
        "reason": redact.redact_text(row.get("reason") or "") or None,
        "item_id": row.get("item_id"),
        "event_id": event_id,
        "story_id": story_id,
        "in_radar": in_radar,
        "paper_present": paper_present,
        "detail": redact.redact_obj(detail),
    }


def run_item_page(conn: sqlite3.Connection, run_id: str, **kwargs: Any) -> Dict[str, Any]:
    page = int(kwargs.pop("page", 1) or 1)
    page_size = int(kwargs.pop("page_size", 50) or 50)
    items, total, summary = run_items(conn, run_id, page=page, page_size=page_size, **kwargs)
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "has_more": page * page_size < total, "summary": summary}


# ---------------------------------------------------------------- 查询：日志

def list_logs(conn: sqlite3.Connection, *, from_iso: Optional[str] = None,
              to_iso: Optional[str] = None, levels: Optional[List[str]] = None,
              tasks: Optional[List[str]] = None, run_id: Optional[str] = None,
              source_id: Optional[str] = None, stages: Optional[List[str]] = None,
              error_code: Optional[str] = None, q: Optional[str] = None,
              page: int = 1, page_size: int = 50,
              before_id: Optional[int] = None, since_id: Optional[int] = None
              ) -> Tuple[List[Dict[str, Any]], int]:
    where, params = [], []
    if from_iso:
        where.append("ts >= ?")
        params.append(from_iso)
    if to_iso:
        where.append("ts <= ?")
        params.append(to_iso)
    if levels:
        where.append("level IN (%s)" % ",".join("?" for _ in levels))
        params.extend([level.upper() for level in levels])
    if tasks:
        where.append("task IN (%s)" % ",".join("?" for _ in tasks))
        params.extend(tasks)
    if run_id:
        where.append("run_id = ?")
        params.append(run_id)
    if source_id:
        where.append("source_id = ?")
        params.append(source_id)
    if stages:
        where.append("stage IN (%s)" % ",".join("?" for _ in stages))
        params.extend(stages)
    if error_code:
        where.append("error_code = ?")
        params.append(error_code)
    if before_id:
        where.append("id < ?")
        params.append(int(before_id))
    if since_id:
        where.append("id > ?")
        params.append(int(since_id))
    text = (q or "").strip()[:80]
    if text:
        where.append("(message LIKE ? OR details_json LIKE ? OR source_id LIKE ? OR run_id LIKE ?)")
        params.extend(["%" + text + "%"] * 4)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = int(conn.execute("SELECT COUNT(1) c FROM monitor_logs" + clause, params).fetchone()["c"])
    offset = max(0, (page - 1) * page_size)
    rows = conn.execute(
        "SELECT * FROM monitor_logs%s ORDER BY id DESC LIMIT ? OFFSET ?" % clause,
        params + [page_size, offset]).fetchall()
    return [_log_view(dict(row)) for row in rows], total


def _log_view(row: Dict[str, Any]) -> Dict[str, Any]:
    message = redact.redact_text(row.get("message") or "")
    return {
        "id": row.get("id"), "ts": row.get("ts"), "level": (row.get("level") or "INFO").upper(),
        "level_label": {"DEBUG": "调试", "INFO": "信息", "WARNING": "警告",
                        "ERROR": "错误"}.get((row.get("level") or "").upper(), row.get("level")),
        "task": row.get("task"), "task_label": TASKS.get(row.get("task") or "", row.get("task")),
        "run_id": row.get("run_id"), "source_id": row.get("source_id"), "stage": row.get("stage"),
        "stage_label": STAGE_LABELS.get(row.get("stage") or "", row.get("stage")),
        "error_code": row.get("error_code"),
        "error_code_label": ERROR_CODE_LABELS.get(row.get("error_code") or "", row.get("error_code")),
        "message": message,
        "message_short": (message[:180] + "…") if len(message) > 180 else message,
    }


def get_log(conn: sqlite3.Connection, log_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM monitor_logs WHERE id=?", (int(log_id),)).fetchone()
    if not row:
        return None
    data = _log_view(dict(row))
    data["details"] = redact.redact_obj(_loads(row["details_json"], {}))
    data["details_json"] = json.dumps(data["details"], ensure_ascii=False, indent=2)
    return data


def logs_for_run(conn: sqlite3.Connection, run_id: str, page: int = 1, page_size: int = 50,
                 level: Optional[str] = None, stage: Optional[str] = None,
                 source_id: Optional[str] = None) -> Dict[str, Any]:
    levels = [level] if level else None
    stages = [stage] if stage else None
    rows, total = list_logs(conn, run_id=run_id, levels=levels, stages=stages,
                            source_id=source_id, page=page, page_size=page_size)
    return {"logs": rows, "total": total, "page": page, "page_size": page_size,
            "has_more": page * page_size < total}


def log_error_codes(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT error_code, COUNT(1) c FROM monitor_logs WHERE error_code IS NOT NULL "
        "GROUP BY error_code ORDER BY c DESC").fetchall()
    return [{"code": row["error_code"], "count": int(row["c"]),
             "label": ERROR_CODE_LABELS.get(row["error_code"], row["error_code"])}
            for row in rows]


# ---------------------------------------------------------------- 查询：来源管理

def source_stats(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """每个来源最近的几次真实结果 + 累计失败/空结果次数（来源管理视图用）。"""
    out: Dict[str, Dict[str, Any]] = {}
    latest = conn.execute(
        """SELECT * FROM (
               SELECT *, ROW_NUMBER() OVER (
                   PARTITION BY source_id
                   ORDER BY COALESCE(finished_at, started_at) DESC, id DESC) AS rn
                 FROM monitor_source_results)
           WHERE rn <= 40""").fetchall()
    for row in latest:
        data = dict(row)
        key = str(data.get("source_id") or "")
        detail = _loads(data.get("detail"), {})
        entry = out.setdefault(key, {"attempts": 0, "failures": 0, "empty": 0,
                                     "last_attempt_at": data.get("finished_at")
                                     or data.get("started_at")})
        if data.get("rn") == 1:
            entry.update({
                "last_status": data.get("status"),
                "last_fetched": data.get("fetched"),
                "last_new": data.get("items_new"),
                "last_updated": detail.get("updated") if isinstance(detail, dict) else None,
                "last_latency_ms": data.get("latency_ms"),
                "last_http_status": data.get("http_status"),
                "last_error": redact.redact_text(data.get("error") or "") or None,
                "last_error_code": data.get("error_code"),
                "last_run_id": data.get("run_id"),
            })
        if entry.get("last_ok_run_at") is None and data.get("status") in (
                "ok", "empty", "filtered_all"):
            entry["last_ok_run_at"] = data.get("finished_at") or data.get("started_at")
            entry["last_ok_fetched"] = data.get("fetched")
            entry["last_ok_new"] = data.get("items_new")
        if entry.get("last_failure_at") is None and data.get("status") == "failed":
            entry["last_failure_at"] = data.get("finished_at") or data.get("started_at")
        if entry.get("last_skipped_at") is None and data.get("status") in ("skipped", "paused"):
            entry["last_skipped_at"] = data.get("finished_at") or data.get("started_at")
    for row in conn.execute(
            """SELECT source_id, COUNT(1) attempts,
                      SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failures,
                      SUM(CASE WHEN status='empty' THEN 1 ELSE 0 END) empty,
                      MAX(CASE WHEN status IN ('ok','empty','filtered_all')
                               THEN COALESCE(finished_at, started_at) END) last_ok_run_at
               FROM monitor_source_results GROUP BY source_id"""):
        entry = out.setdefault(str(row["source_id"]), {})
        entry["attempts"] = int(row["attempts"] or 0)
        entry["failures"] = int(row["failures"] or 0)
        entry["empty"] = int(row["empty"] or 0)
    return out


def sources_view(conn: sqlite3.Connection, specs: List[Dict[str, Any]], *,
                 q: Optional[str] = None, kinds: Optional[List[str]] = None,
                 statuses: Optional[List[str]] = None, only_enabled: bool = False,
                 sort: str = "failures", order: str = "desc",
                 stale_hours: int = 48) -> Dict[str, Any]:
    states = {row["source_id"]: dict(row) for row in conn.execute("SELECT * FROM radar_sources")} \
        if _has_table(conn, "radar_sources") else {}
    stats = source_stats(conn)
    reference = now()
    rows: List[Dict[str, Any]] = []
    seen_ids = set()
    for spec in specs or []:
        source_id = str(spec.get("source_id") or "")
        if not source_id or source_id in seen_ids:
            continue
        seen_ids.add(source_id)
        rows.append(_source_row(spec, states.get(source_id), stats.get(source_id), reference,
                                stale_hours))
    for source_id, state in states.items():
        if source_id in seen_ids:
            continue
        rows.append(_source_row({"source_id": source_id, "name": state.get("name"),
                                 "platform": state.get("platform"), "kind": state.get("kind"),
                                 "enabled": bool(state.get("enabled"))}, state,
                                stats.get(source_id), reference, stale_hours))
    rows.sort(key=lambda item: _source_sort_key(item, sort), reverse=str(order).lower() != "asc")
    text = (q or "").strip().lower()
    if text:
        rows = [row for row in rows if text in " ".join(
            str(row.get(field) or "") for field in
            ("source_id", "name", "platform", "kind", "last_error", "paused_reason")).lower()]
    if kinds:
        rows = [row for row in rows if row["kind"] in kinds]
    if statuses:
        rows = [row for row in rows if row["status"] in statuses]
    if only_enabled:
        rows = [row for row in rows if row["enabled"]]
    summary = {
        "total": len(rows),
        "configured": len(rows),
        "enabled": len([r for r in rows if r["enabled"]]),
        "ok": len([r for r in rows if r["status"] == "ok"]),
        "error": len([r for r in rows if r["status"] == "error"]),
        "paused": len([r for r in rows if r["status"] == "paused"]),
        "disabled": len([r for r in rows if r["status"] == "disabled"]),
        "unavailable": len([r for r in rows if r["status"] == "unavailable"]),
        "unknown": len([r for r in rows if r["status"] == "unknown"]),
        "never_succeeded": len([r for r in rows if not r["last_ok_at"]]),
        "stale": len([r for r in rows if r["stale"]]),
        "with_new_data": len([r for r in rows if r["has_new_data"] is True]),
    }
    return {"sources": rows, "summary": summary}


def _source_sort_key(row: Dict[str, Any], sort: str) -> Tuple[Any, ...]:
    """升序排序键；排序方向由调用方 reverse 决定（替代降序即“最严重/最慢在前后”）。"""
    name = str(row.get("source_id") or "")
    if sort == "latency":
        latency = row.get("last_latency_ms")
        return (latency if latency is not None else -1, name)
    if sort == "stale":
        age = row.get("last_ok_age_minutes")
        return (age if age is not None else 10 ** 9, name)
    if sort == "name":
        return (name,)
    if sort == "fetched":
        fetched = row.get("last_fetched")
        return (fetched if fetched is not None else -1, name)
    return (int(row.get("consecutive_failures") or 0), name)


def _source_row(spec: Dict[str, Any], state: Optional[Dict[str, Any]],
                stat: Optional[Dict[str, Any]], reference: dt.datetime,
                stale_hours: int) -> Dict[str, Any]:
    state = state or {}
    stat = stat or {}
    enabled = bool(state.get("enabled", spec.get("enabled", True)))
    kind = str(state.get("kind") or spec.get("kind") or "")
    raw_status = str(state.get("status") or "unknown")
    if kind == "unavailable":
        status = "unavailable"
    elif not enabled and raw_status != "paused":
        status = "disabled"
    elif raw_status not in SOURCE_STATUSES:
        status = "unknown"
    else:
        status = raw_status
    last_ok_at = state.get("last_ok_at") or None
    age = age_minutes(last_ok_at, reference)
    stale = (age is None) or (age > stale_hours * 60)
    last_new = stat.get("last_new")
    # radar_sources.last_item_count 的默认值是 0；来源从没跑过的时候它不是「真的 0 条」，
    # 而是「没有记录」。只有跑过（有成功时间或错误时间或监控结果）才展示数字。
    ever_ran = bool(last_ok_at or state.get("last_error_at") or stat.get("last_attempt_at")
                    or state.get("last_latency_ms") is not None)
    last_fetched = (stat.get("last_fetched") if stat.get("last_fetched") is not None
                    else (state.get("last_item_count") if ever_ran else None))
    last_latency = (stat.get("last_latency_ms") if stat.get("last_latency_ms") is not None
                    else (state.get("last_latency_ms") if ever_ran else None))
    return {
        "source_id": str(spec.get("source_id") or state.get("source_id") or ""),
        "name": state.get("name") or spec.get("name") or "",
        "platform": state.get("platform") or spec.get("platform") or "",
        "kind": kind,
        "configured": True,
        "enabled": enabled,
        "status": status,
        "status_label": SOURCE_STATUS_LABELS.get(status, status),
        "raw_status": raw_status,
        "status_reason": state.get("paused_reason") or state.get("last_error") or None,
        "notes": spec.get("notes") or state.get("notes") or "",
        "last_attempt_at": stat.get("last_attempt_at") or state.get("last_error_at") or last_ok_at,
        "last_attempt_source": ("monitor_source_results" if stat.get("last_attempt_at")
                                else "radar_sources"),
        "last_ok_at": last_ok_at,
        "last_ok_age_minutes": age,
        "last_ok_run_at": stat.get("last_ok_run_at"),
        "last_failure_at": stat.get("last_failure_at"),
        "last_skipped_at": stat.get("last_skipped_at"),
        "last_latency_ms": last_latency,
        "last_fetched": last_fetched,
        "last_new": last_new,
        "last_updated": stat.get("last_updated"),
        "last_result_status": stat.get("last_status"),
        "last_result_label": _source_result_label(stat["last_status"]) if stat.get("last_status") else None,
        "last_http_status": stat.get("last_http_status"),
        "last_error": redact.redact_text(stat.get("last_error") or state.get("last_error") or "") or None,
        "last_error_code": stat.get("last_error_code") or (
            classify_error(state.get("last_error")) if state.get("last_error") else None),
        "last_error_at": state.get("last_error_at"),
        "consecutive_failures": int(state.get("consecutive_failures") or 0),
        "paused_reason": state.get("paused_reason"),
        "never_succeeded": last_ok_at is None,
        "stale": stale,
        "has_new_data": (None if last_new is None else bool(int(last_new or 0) > 0)),
        "disabled": not enabled,
        "attempts_recorded": int(stat.get("attempts") or 0),
        "failures_recorded": int(stat.get("failures") or 0),
        "empty_successes": int(stat.get("empty") or 0),
    }


def source_detail(conn: sqlite3.Connection, source_id: str, limit: int = 20) -> Dict[str, Any]:
    results = [dict(row) for row in conn.execute(
        "SELECT * FROM monitor_source_results WHERE source_id=? "
        "ORDER BY COALESCE(finished_at, started_at) DESC, id DESC LIMIT ?",
        (source_id, limit))]
    for row in results:
        row["detail"] = redact.redact_obj(_loads(row.get("detail"), {}))
        row["error"] = redact.redact_text(row.get("error") or "") or None
        row["reason"] = redact.redact_text(row.get("reason") or "") or None
        row["status_label"] = _source_result_label(row["status"])
    logs, total = list_logs(conn, source_id=source_id, page=1, page_size=20)
    runs = [dict(row) for row in conn.execute(
        "SELECT run_id, task, status, started_at, finished_at FROM monitor_runs WHERE run_id IN "
        "(SELECT DISTINCT run_id FROM monitor_source_results WHERE source_id=?) "
        "ORDER BY COALESCE(started_at, finished_at) DESC LIMIT 20", (source_id,))]
    for row in runs:
        row["task_label"] = TASKS.get(row["task"], row["task"])
        row["status_label"] = RUN_STATUS_LABELS.get(row["status"], row["status"])
    state = conn.execute("SELECT * FROM radar_sources WHERE source_id=?", (source_id,)).fetchone() \
        if _has_table(conn, "radar_sources") else None
    state_data = dict(state) if state else {}
    if state_data.get("last_error"):
        state_data["last_error"] = redact.redact_text(state_data["last_error"])
    return {"source_id": source_id, "state": state_data, "results": results,
            "logs": logs, "logs_total": total, "runs": runs}


# ---------------------------------------------------------------- 总览

_CALIBER = {
    "last_run": "取 COALESCE(结束时间, 开始时间) 最新的一条运行记录；没有任何记录时显示未知。",
    "success_rate": "分母是过去 24 小时内已结束（成功 / 部分成功 / 失败）的运行数，"
                    "不含运行中、被中断与状态未知的运行；成功只统计“成功”，部分成功单列。"
                    "分母为 0 时显示未知而不是 0%。",
    "item_flow": "过去 24 小时内各次运行记录的条目计数求和。只累加有记录的运行，"
                 "并给出“有记录运行数 / 运行总数”，缺记录的运行不按 0 计算。",
    "sources": "来源总数来自当前配置（含自定义 RSS 与 AnySearch 查询）；状态来自真实采集结果。"
               "「正常 / 失败 / 暂停」中的失败与暂停以最近一次采集结果为准，"
               "而“最近成功”是独立的字段——一个来源可以现在失败但历史上成功过。"
               "返回 0 条算成功（列为“成功但 0 条”），不等于失败。",
    "last_success": "最近一次状态为成功或部分成功的采集类运行（新闻采集 / 事件日报 / "
                    "论文采集 / 地理采集）距现在的时长；没有任何成功记录时显示未知。",
    "errors": "未解决的严重错误：按来源或任务分组后，最新一条日志仍是错误级别的分组。"
              "长期未更新来源：启用中但超过 48 小时没有成功、或从未成功过的来源。",
}

OVERVIEW_HOURS = 24
STALE_HOURS = 48
CALIBER = _CALIBER


def overview(conn: sqlite3.Connection, specs: Optional[List[Dict[str, Any]]] = None,
             hours: int = OVERVIEW_HOURS, stale_hours: int = STALE_HOURS) -> Dict[str, Any]:
    reference = now()
    since = (reference - dt.timedelta(hours=max(1, int(hours)))).isoformat(timespec="seconds")
    window_clause, window_params = _window_clause(since, None)

    last_row = conn.execute(
        "SELECT * FROM monitor_runs ORDER BY COALESCE(finished_at, started_at) DESC, "
        "created_at DESC LIMIT 1").fetchone()
    last_run = _run_view(dict(last_row)) if last_row else None

    runs = [_run_view(dict(row)) for row in conn.execute(
        "SELECT * FROM monitor_runs WHERE " + window_clause + " ORDER BY created_at",
        window_params)]

    terminal = [run for run in runs if run["status"] in ("ok", "partial", "failed")]
    ok = len([run for run in terminal if run["status"] == "ok"])
    partial = len([run for run in terminal if run["status"] == "partial"])
    failed = len([run for run in terminal if run["status"] == "failed"])
    success_rate = None if not terminal else round(ok / float(len(terminal)), 4)
    success_rate_with_partial = None if not terminal else round((ok + partial) / float(len(terminal)), 4)

    flow_fields = ("items_fetched", "items_new", "items_updated", "items_duplicate",
                   "items_filtered")
    totals: Dict[str, Optional[int]] = {}
    coverage: Dict[str, int] = {}
    for field in flow_fields:
        values = [run[field] for run in runs if run.get(field) is not None]
        totals[field] = (sum(int(v) for v in values) if values else None)
        coverage[field] = len(values)

    source_summary = sources_view(conn, specs or [], stale_hours=stale_hours)["summary"]

    success_row = conn.execute(
        "SELECT * FROM monitor_runs WHERE task IN ('news_collect','event_report','geo_collect',"
        "'papers_collect') AND status IN ('ok','partial') AND finished_at IS NOT NULL "
        "ORDER BY finished_at DESC LIMIT 1").fetchone()
    if success_row:
        data = _run_view(dict(success_row))
        last_success: Dict[str, Any] = {
            "unknown": False, "run_id": data["run_id"], "task": data["task"],
            "task_label": data["task_label"], "at": data["finished_at"],
            "status": data["status"], "status_label": data["status_label"],
            "age_minutes": age_minutes(data["finished_at"], reference),
        }
    else:
        newest = conn.execute("SELECT MAX(last_ok_at) m FROM radar_sources").fetchone() \
            if _has_table(conn, "radar_sources") else None
        source_last_ok = (newest["m"] if newest else None) or None
        last_success = {
            "unknown": True, "run_id": None, "task": None, "task_label": None, "at": None,
            "status": None, "status_label": None,
            "age_minutes": age_minutes(source_last_ok, reference),
            "source_last_ok_at": source_last_ok,
            "unknown_reason": "没有状态为成功/部分成功的采集类运行记录"
                              "（下面显示的是来源表里最近一次成功时间，可能来自历史记录）",
        }

    all_sources = sources_view(conn, specs or [], stale_hours=stale_hours)["sources"]
    stale_sources = [row for row in all_sources if row["stale"] and row["enabled"]
                     and not row["disabled"]]
    stale_sources.sort(key=lambda item: -(item["last_ok_age_minutes"]
                                          if item["last_ok_age_minutes"] is not None else 10 ** 9))

    return {
        "generated_at": now_iso(),
        "window": {"hours": max(1, int(hours)), "from": since},
        "caliber": dict(_CALIBER),
        "last_run": last_run,
        "success_rate": {
            "value": success_rate,
            "value_including_partial": success_rate_with_partial,
            "ok": ok, "partial": partial, "failed": failed, "total": len(terminal),
            "runs_in_window": len(runs),
            "unknown": not terminal,
            "unknown_reason": None if terminal else "过去 %d 小时内没有已结束的运行记录" % hours,
        },
        "item_flow": {
            "fetched": totals["items_fetched"], "new": totals["items_new"],
            "updated": totals["items_updated"], "duplicate": totals["items_duplicate"],
            "filtered": totals["items_filtered"], "coverage": coverage,
            "runs_in_window": len(runs),
            "unknown": not any(totals[field] is not None
                               for field in ("items_new", "items_duplicate", "items_filtered")),
        },
        "sources": source_summary,
        "last_success": last_success,
        "open_errors": _open_errors(conn),
        "stale_sources": {"count": len(stale_sources), "items": stale_sources[:10],
                          "threshold_hours": stale_hours},
    }


def _open_errors(conn: sqlite3.Connection, limit: int = 10) -> Dict[str, Any]:
    rows = conn.execute(
        """SELECT * FROM monitor_logs g WHERE g.level='ERROR' AND g.id = (
               SELECT MAX(x.id) FROM monitor_logs x
               WHERE COALESCE(NULLIF(x.source_id,''), NULLIF(x.run_id,''),
                              NULLIF(x.task,''), 'system') =
                     COALESCE(NULLIF(g.source_id,''), NULLIF(g.run_id,''),
                              NULLIF(g.task,''), 'system'))
           ORDER BY g.id DESC LIMIT ?""", (limit,)).fetchall()
    items = []
    for row in rows:
        data = _log_view(dict(row))
        age = age_minutes(data["ts"])
        items.append({
            "id": data["id"], "ts": data["ts"], "level": data["level"],
            "source_id": data["source_id"], "task": data["task"], "task_label": data["task_label"],
            "run_id": data["run_id"], "stage": data["stage"], "stage_label": data["stage_label"],
            "error_code": data["error_code"], "error_code_label": data["error_code_label"],
            "message": data["message"], "age_minutes": age,
        })
    return {"count": len(items), "items": items}


def counts(conn: sqlite3.Connection) -> Dict[str, int]:
    return {
        "runs": int(conn.execute("SELECT COUNT(1) c FROM monitor_runs").fetchone()["c"]),
        "runs_running": int(conn.execute(
            "SELECT COUNT(1) c FROM monitor_runs WHERE status='running'").fetchone()["c"]),
        "stages": int(conn.execute("SELECT COUNT(1) c FROM monitor_stages").fetchone()["c"]),
        "source_results": int(conn.execute(
            "SELECT COUNT(1) c FROM monitor_source_results").fetchone()["c"]),
        "items": int(conn.execute("SELECT COUNT(1) c FROM monitor_item_actions").fetchone()["c"]),
        "logs": int(conn.execute("SELECT COUNT(1) c FROM monitor_logs").fetchone()["c"]),
        "legacy_runs": int(conn.execute(
            "SELECT COUNT(1) c FROM monitor_runs WHERE origin='legacy'").fetchone()["c"]),
    }


def meta(conn: sqlite3.Connection) -> Dict[str, Any]:
    first = conn.execute(
        "SELECT MIN(COALESCE(started_at, finished_at)) m FROM monitor_runs").fetchone()
    return {
        "generated_at": now_iso(),
        "started_at_first": (first["m"] if first else None),
        "tasks": [{"value": key, "label": TASKS[key]} for key in TASKS],
        "statuses": [{"value": key, "label": RUN_STATUS_LABELS[key]} for key in RUN_STATUSES],
        "stage_statuses": [{"value": key, "label": STAGE_STATUS_LABELS[key]}
                           for key in STAGE_STATUSES],
        "source_statuses": [{"value": key, "label": SOURCE_STATUS_LABELS[key]}
                            for key in SOURCE_STATUSES],
        "item_actions": [{"value": key, "label": ITEM_ACTION_LABELS[key]} for key in ITEM_ACTIONS],
        "triggers": [{"value": key, "label": TRIGGER_LABELS[key]} for key in TRIGGERS],
        "levels": LEVELS,
        "stages": [{"value": key, "label": STAGE_LABELS[key]} for key in STAGES],
        "error_codes": ERROR_CODES,
        "windows": [{"value": "24h", "label": "过去 24 小时"},
                    {"value": "7d", "label": "过去 7 天"},
                    {"value": "30d", "label": "过去 30 天"}],
        "caliber": dict(_CALIBER),
        "overview_hours": OVERVIEW_HOURS,
        "stale_hours": STALE_HOURS,
        "retention_days": _RETENTION_DAYS,
        "counts": counts(conn),
        "live_log_error_codes": log_error_codes(conn),
    }


def run_ids_in_window(conn: sqlite3.Connection, from_iso: Optional[str], to_iso: Optional[str],
                      tasks: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """时间线用：窗口内的运行 + 时长（不含阶段/来源细节）。from_iso 为 None = 不设下界。"""
    clause, params = _window_clause(from_iso, to_iso)
    where = [clause] if clause else []
    if tasks:
        where.append("task IN (%s)" % ",".join("?" for _ in tasks))
        params.extend(tasks)
    sql = "SELECT * FROM monitor_runs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = conn.execute(sql + " ORDER BY " + _SAFE_TIME_EXPR + " ASC", params).fetchall()
    out = []
    for row in rows:
        data = _run_view(dict(row))
        data["stats"] = {}          # 时间线不需要整包 stats，减小响应
        out.append(data)
    return out