"""Radar persistence. Adds new tables to the existing SQLite file; never touches old ones."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, Iterable, List, Optional

from .. import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS radar_sources (
    source_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    url TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'unknown',
    last_ok_at TEXT,
    last_error TEXT,
    last_error_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_item_count INTEGER NOT NULL DEFAULT 0,
    last_latency_ms INTEGER,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS radar_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    url_hash TEXT NOT NULL UNIQUE,
    author_name TEXT,
    published_at TEXT,
    collected_at TEXT NOT NULL,
    language TEXT,
    engagement TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    raw_content TEXT,
    source_type TEXT NOT NULL DEFAULT 'news',
    fetch_status TEXT NOT NULL DEFAULT 'ok',
    last_seen_at TEXT NOT NULL,
    event_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_radar_items_collected ON radar_items(collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_radar_items_hash ON radar_items(content_hash);
CREATE INDEX IF NOT EXISTS idx_radar_items_event ON radar_items(event_id);

CREATE TABLE IF NOT EXISTS radar_events (
    id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    category TEXT,
    keywords TEXT NOT NULL DEFAULT '[]',
    heat REAL NOT NULL DEFAULT 0,
    importance REAL NOT NULL DEFAULT 0,
    confidence TEXT NOT NULL DEFAULT 'unverified',
    item_count INTEGER NOT NULL DEFAULT 0,
    platform_count INTEGER NOT NULL DEFAULT 0,
    source_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_updated_at TEXT NOT NULL,
    origin_item_id INTEGER,
    tech_flag INTEGER NOT NULL DEFAULT 0,
    investigation_state TEXT NOT NULL DEFAULT 'none',
    metrics TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_radar_events_date ON radar_events(date DESC);

CREATE TABLE IF NOT EXISTS radar_event_snapshots (
    event_id TEXT NOT NULL,
    date TEXT NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    platform_count INTEGER NOT NULL DEFAULT 0,
    heat REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (event_id, date)
);

CREATE TABLE IF NOT EXISTS radar_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    text TEXT NOT NULL,
    claimant TEXT,
    stated_at TEXT,
    status TEXT NOT NULL DEFAULT 'insufficient',
    confidence TEXT NOT NULL DEFAULT 'low',
    rationale TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_radar_claims_event ON radar_claims(event_id);

CREATE TABLE IF NOT EXISTS radar_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    claim_id INTEGER,
    url TEXT,
    source_id TEXT,
    platform TEXT,
    source_type TEXT NOT NULL DEFAULT 'news',
    quote TEXT,
    published_at TEXT,
    collected_at TEXT NOT NULL,
    language TEXT,
    first_hand INTEGER NOT NULL DEFAULT 0,
    independent_group TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_radar_evidence_event ON radar_evidence(event_id);

CREATE TABLE IF NOT EXISTS radar_claim_evidence (
    claim_id INTEGER NOT NULL,
    evidence_id INTEGER NOT NULL,
    relation TEXT NOT NULL DEFAULT 'context',
    independence TEXT NOT NULL DEFAULT 'unknown',
    limits TEXT,
    PRIMARY KEY (claim_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS radar_reports (
    date TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (date, kind)
);

CREATE TABLE IF NOT EXISTS radar_push_log (
    date TEXT NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT,
    content_hash TEXT,
    detail TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (date, channel)
);

CREATE TABLE IF NOT EXISTS radar_stories (
    id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    category TEXT,
    heat REAL NOT NULL DEFAULT 0,
    importance REAL NOT NULL DEFAULT 0,
    confidence TEXT NOT NULL DEFAULT 'unverified',
    event_count INTEGER NOT NULL DEFAULT 0,
    item_count INTEGER NOT NULL DEFAULT 0,
    platform_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_updated_at TEXT NOT NULL,
    metrics TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_radar_stories_date ON radar_stories(date DESC);

CREATE TABLE IF NOT EXISTS radar_summaries (
    cache_key TEXT PRIMARY KEY,
    event_id TEXT,
    provider TEXT,
    model TEXT,
    content_hash TEXT,
    headline TEXT,
    summary TEXT NOT NULL,
    points TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS radar_runs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    stats TEXT NOT NULL DEFAULT '{}',
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_radar_runs_started ON radar_runs(started_at DESC);

-- 排名轨迹：每次采集记录每条材料的位次，用于判断"持续发酵 / 快速升温 / 退烧"
CREATE TABLE IF NOT EXISTS radar_rank_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url_hash TEXT NOT NULL,
    rank INTEGER NOT NULL,
    captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_radar_rank_hash ON radar_rank_history(url_hash, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_radar_rank_date ON radar_rank_history(date, captured_at DESC);

-- 每日态势快照（全球态势监控）：把"当日真实输入"按固定口径物化下来。
-- 目的是让历史趋势只依赖当次写入的真实值，而不是事后用"当前 raw_heat"回填历史。
-- caliber_version: 1 = 本次引入的固定口径；0 = 由 radar_event_snapshots 反推的旧口径（输入不全）。
CREATE TABLE IF NOT EXISTS radar_threat_daily (
    date TEXT PRIMARY KEY,
    caliber_version INTEGER NOT NULL DEFAULT 1,
    activity REAL NOT NULL DEFAULT 0,
    credibility REAL,
    activity_components TEXT NOT NULL DEFAULT '{}',
    credibility_components TEXT NOT NULL DEFAULT '{}',
    event_count INTEGER NOT NULL DEFAULT 0,
    story_count INTEGER NOT NULL DEFAULT 0,
    item_count INTEGER NOT NULL DEFAULT 0,
    platform_count INTEGER NOT NULL DEFAULT 0,
    source_count INTEGER NOT NULL DEFAULT 0,
    unverified_share REAL,
    concentration_share REAL,
    evidence_gap_count INTEGER,
    continuing_event_count INTEGER NOT NULL DEFAULT 0,
    grew_event_count INTEGER NOT NULL DEFAULT 0,
    top_activity_heat_mean REAL,
    inputs_available TEXT NOT NULL DEFAULT '[]',
    missing_inputs TEXT NOT NULL DEFAULT '[]',
    computed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_radar_threat_daily_date ON radar_threat_daily(date DESC);

-- 结构化地理点（地震/灾害等）。与新闻聚类完全隔离：
-- 这些"标题"是程序生成的，塞进 token 相似度会污染新闻事件，而且量级会挤爆 O(n²) 聚类。
-- (layer, external_id) 唯一 → 重复采集幂等更新，不产生重复行。
CREATE TABLE IF NOT EXISTS radar_geo_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    layer TEXT NOT NULL,
    external_id TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    title TEXT,
    detail TEXT,
    magnitude REAL,
    severity TEXT,
    observed_at TEXT,
    source_id TEXT,
    url TEXT,
    collected_at TEXT NOT NULL,
    raw TEXT NOT NULL DEFAULT '{}',
    -- 灾害类型（earthquake/volcano/wildfire/flood/storm/...），供前端按类型开关
    hazard TEXT,
    magnitude_unit TEXT,
    -- 几何：Point 才是精确点位；Polygon 只保存原始多边形，前端按面绘制
    geometry_type TEXT,
    geometry_json TEXT,
    -- 位置精度：point=来源点 / area=面范围 / approximate=近似
    position_precision TEXT,
    closed_at TEXT,
    updated_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_radar_geo_points_ext
    ON radar_geo_points(layer, external_id);
CREATE INDEX IF NOT EXISTS idx_radar_geo_points_layer
    ON radar_geo_points(layer, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_radar_geo_points_collected
    ON radar_geo_points(collected_at DESC);
"""


def init() -> None:
    """Create radar tables if missing. Safe to call on every start."""
    with db.connect() as conn:
        conn.executescript(SCHEMA)
        # 向后兼容的增量迁移：只加列，不动已有数据
        _ensure_column(conn, "radar_events", "story_id", "TEXT")
        _ensure_column(conn, "radar_sources", "paused_reason", "TEXT")
        # 地理维度：本阶段只预留字段（全部保持 NULL），不做任何自动回填。
        # 报道来源国与事件发生国必须是两个字段，不能互相顶替。
        _ensure_column(conn, "radar_events", "origin_country_code", "TEXT")
        _ensure_column(conn, "radar_events", "event_country_code", "TEXT")
        _ensure_column(conn, "radar_events", "geo_lat", "REAL")
        _ensure_column(conn, "radar_events", "geo_lon", "REAL")
        _ensure_column(conn, "radar_events", "geo_label", "TEXT")
        _ensure_column(conn, "radar_events", "geo_confidence", "TEXT")
        _ensure_column(conn, "radar_events", "geo_method", "TEXT")
        _ensure_column(conn, "radar_events", "geo_source", "TEXT")
        _ensure_column(conn, "radar_items", "origin_country_code", "TEXT")
        _ensure_column(conn, "radar_items", "event_country_code", "TEXT")
        # 独立的分类字段：不覆盖 category（category 服务于兴趣主题，语义保持不变）
        _ensure_column(conn, "radar_events", "theme", "TEXT")
        _ensure_column(conn, "radar_events", "region", "TEXT")
        # 结构化地理图层的扩展列（阶段四 B：EONET/GDACS 需要灾害类型与几何）
        _ensure_column(conn, "radar_geo_points", "hazard", "TEXT")
        _ensure_column(conn, "radar_geo_points", "magnitude_unit", "TEXT")
        _ensure_column(conn, "radar_geo_points", "geometry_type", "TEXT")
        _ensure_column(conn, "radar_geo_points", "geometry_json", "TEXT")
        _ensure_column(conn, "radar_geo_points", "position_precision", "TEXT")
        _ensure_column(conn, "radar_geo_points", "closed_at", "TEXT")
        _ensure_column(conn, "radar_geo_points", "updated_at", "TEXT")
        conn.commit()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
    except sqlite3.Error:
        return
    if column in existing:
        return
    try:
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))
        print("[radar] migrated: %s.%s" % (table, column))
    except sqlite3.Error as exc:
        print("[radar] migration skipped %s.%s: %s" % (table, column, exc))


# ---------- sources ----------

def ensure_source(conn: sqlite3.Connection, spec: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO radar_sources(source_id, platform, name, kind, url, enabled, notes)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(source_id) DO UPDATE SET
             platform=excluded.platform, name=excluded.name, kind=excluded.kind,
             url=excluded.url, enabled=excluded.enabled, notes=excluded.notes""",
        (
            spec["source_id"], spec.get("platform", ""), spec.get("name", ""),
            spec.get("kind", "rss"), spec.get("url", ""),
            1 if spec.get("enabled", True) else 0, spec.get("notes", ""),
        ),
    )


def record_source_result(conn: sqlite3.Connection, source_id: str, ok: bool,
                         item_count: int = 0, latency_ms: Optional[int] = None,
                         error: Optional[str] = None, now: str = "") -> None:
    if ok:
        conn.execute(
            """UPDATE radar_sources SET status='ok', last_ok_at=?, last_error=NULL,
                   consecutive_failures=0, last_item_count=?, last_latency_ms=?, paused_reason=NULL
               WHERE source_id=?""",
            (now, item_count, latency_ms, source_id),
        )
    else:
        conn.execute(
            """UPDATE radar_sources SET status='error', last_error=?, last_error_at=?,
                   consecutive_failures=consecutive_failures+1, last_latency_ms=?
               WHERE source_id=?""",
            ((error or "")[:500], now, latency_ms, source_id),
        )


def mark_source_paused(conn: sqlite3.Connection, source_id: str, reason: str) -> None:
    conn.execute(
        "UPDATE radar_sources SET status='paused', paused_reason=? WHERE source_id=?",
        (reason[:200], source_id),
    )


def sources(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT * FROM radar_sources ORDER BY platform, source_id").fetchall()
    return [dict(r) for r in rows]


# ---------- items ----------

def insert_items(conn: sqlite3.Connection, items: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """Insert new items, refresh ``last_seen_at`` for ones already stored."""
    inserted = 0
    seen = 0
    for item in items:
        cur = conn.execute(
            """INSERT INTO radar_items(source_id, platform, title, url, url_hash, author_name,
                   published_at, collected_at, language, engagement, content_hash, raw_content,
                   source_type, fetch_status, last_seen_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(url_hash) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
            (
                item["source_id"], item.get("platform", ""), item["title"], item.get("url", ""),
                item["url_hash"], item.get("author_name", ""), item.get("published_at", ""),
                item["collected_at"], item.get("language", ""),
                json.dumps(item.get("engagement") or {}, ensure_ascii=False),
                item["content_hash"], (item.get("raw_content") or "")[:4000],
                item.get("source_type", "news"), item.get("fetch_status", "ok"),
                item["last_seen_at"],
            ),
        )
        if cur.rowcount and cur.lastrowid:
            inserted += 1
        else:
            seen += 1
    return {"inserted": inserted, "seen": seen}


def record_ranks(conn: sqlite3.Connection, items: Iterable[Dict[str, Any]], date: str,
                 captured_at: str) -> int:
    """记录本轮每条材料在榜单上的位次（没有位次字段的源会跳过）。"""
    rows = []
    for item in items:
        engagement = item.get("engagement") or {}
        rank = engagement.get("rank") or engagement.get("position")
        try:
            rank = int(rank)
        except (TypeError, ValueError):
            continue
        if rank <= 0:
            continue
        rows.append((date, item.get("source_id", ""), item.get("url_hash", ""), rank, captured_at))
    if not rows:
        return 0
    conn.executemany(
        "INSERT INTO radar_rank_history(date, source_id, url_hash, rank, captured_at) "
        "VALUES(?,?,?,?,?)", rows)
    return len(rows)


def last_collect_at(conn: sqlite3.Connection) -> str:
    """最近一次采集时间（用于界面显示「上次更新」）。"""
    row = conn.execute("SELECT MAX(collected_at) AS t FROM radar_items").fetchone()
    return (row["t"] if row and row["t"] else "") or ""


def rank_map(conn: sqlite3.Connection, url_hashes: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """返回 {url_hash: {rank, prev_rank, captured_at, delta}}，只取最近两次位次。"""
    hashes = [h for h in url_hashes if h]
    out: Dict[str, Dict[str, Any]] = {}
    # SQLite 变量上限是 999，分批查询
    for start in range(0, len(hashes), 500):
        chunk = hashes[start:start + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT url_hash, rank, captured_at FROM radar_rank_history "
            "WHERE url_hash IN (%s) ORDER BY captured_at DESC" % placeholders, chunk).fetchall()
        for row in rows:
            key = row["url_hash"]
            entry = out.setdefault(key, {"rank": None, "prev_rank": None,
                                         "captured_at": None, "delta": None})
            if entry["rank"] is None:
                entry["rank"] = row["rank"]
                entry["captured_at"] = row["captured_at"]
            elif entry["prev_rank"] is None:
                entry["prev_rank"] = row["rank"]
                entry["delta"] = entry["prev_rank"] - entry["rank"]
    return out


def recent_items(conn: sqlite3.Connection, since_iso: str, limit: int = 2000) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """SELECT * FROM radar_items WHERE collected_at >= ?
           ORDER BY collected_at DESC LIMIT ?""",
        (since_iso, limit),
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        try:
            d["engagement"] = json.loads(d.get("engagement") or "{}")
        except ValueError:
            d["engagement"] = {}
        out.append(d)
    return out


def item_by_id(conn: sqlite3.Connection, item_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM radar_items WHERE id=?", (item_id,)).fetchone()
    return dict(row) if row else None


def items_for_event(conn: sqlite3.Connection, event_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM radar_items WHERE event_id=? ORDER BY COALESCE(published_at, collected_at) ASC",
        (event_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def assign_event(conn: sqlite3.Connection, item_ids: Iterable[int], event_id: str) -> None:
    for item_id in item_ids:
        conn.execute("UPDATE radar_items SET event_id=? WHERE id=?", (event_id, item_id))


# ---------- events ----------

def upsert_event(conn: sqlite3.Connection, event: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO radar_events(id, date, title, summary, category, keywords, heat, importance,
               confidence, item_count, platform_count, source_count, first_seen_at,
               last_updated_at, origin_item_id, tech_flag, investigation_state, metrics, story_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             date=excluded.date,
             title=excluded.title, summary=excluded.summary, category=excluded.category,
             keywords=excluded.keywords, heat=excluded.heat, importance=excluded.importance,
             confidence=excluded.confidence, item_count=excluded.item_count,
             platform_count=excluded.platform_count, source_count=excluded.source_count,
             last_updated_at=excluded.last_updated_at, origin_item_id=excluded.origin_item_id,
             tech_flag=excluded.tech_flag, investigation_state=excluded.investigation_state,
             metrics=excluded.metrics, story_id=excluded.story_id""",
        (
            event["id"], event["date"], event["title"], event.get("summary", ""),
            event.get("category", ""), json.dumps(event.get("keywords") or [], ensure_ascii=False),
            float(event.get("heat") or 0), float(event.get("importance") or 0),
            event.get("confidence", "unverified"), int(event.get("item_count") or 0),
            int(event.get("platform_count") or 0), int(event.get("source_count") or 0),
            event["first_seen_at"], event["last_updated_at"], event.get("origin_item_id"),
            1 if event.get("tech_flag") else 0, event.get("investigation_state", "none"),
            json.dumps(event.get("metrics") or {}, ensure_ascii=False),
            event.get("story_id"),
        ),
    )


def events_for_date(conn: sqlite3.Connection, date: str, limit: int = 200) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM radar_events WHERE date=? ORDER BY heat DESC LIMIT ?", (date, limit)
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        try:
            d["keywords"] = json.loads(d.get("keywords") or "[]")
        except ValueError:
            d["keywords"] = []
        try:
            d["metrics"] = json.loads(d.get("metrics") or "{}")
        except ValueError:
            d["metrics"] = {}
        d["tech_flag"] = bool(d.get("tech_flag"))
        out.append(d)
    return out


def event_by_id(conn: sqlite3.Connection, event_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM radar_events WHERE id=?", (event_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    for key, default in (("keywords", []), ("metrics", {})):
        try:
            d[key] = json.loads(d.get(key) or ("[]" if default == [] else "{}"))
        except ValueError:
            d[key] = default
    d["tech_flag"] = bool(d.get("tech_flag"))
    return d


def save_event_snapshot(conn: sqlite3.Connection, event_id: str, date: str,
                        item_count: int, platform_count: int, heat: float) -> None:
    conn.execute(
        """INSERT INTO radar_event_snapshots(event_id, date, item_count, platform_count, heat)
           VALUES(?,?,?,?,?)
           ON CONFLICT(event_id, date) DO UPDATE SET
             item_count=excluded.item_count, platform_count=excluded.platform_count,
             heat=excluded.heat""",
        (event_id, date, item_count, platform_count, heat),
    )


def previous_snapshot(conn: sqlite3.Connection, event_id: str, before_date: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM radar_event_snapshots WHERE event_id=? AND date<? ORDER BY date DESC LIMIT 1",
        (event_id, before_date),
    ).fetchone()
    return dict(row) if row else None


# ---------- reports ----------

def save_report(conn: sqlite3.Connection, date: str, kind: str, payload: Dict[str, Any],
                generated_at: str, version: int = 1) -> None:
    conn.execute(
        """INSERT INTO radar_reports(date, kind, payload, generated_at, version) VALUES(?,?,?,?,?)
           ON CONFLICT(date, kind) DO UPDATE SET
             payload=excluded.payload, generated_at=excluded.generated_at, version=excluded.version""",
        (date, kind, json.dumps(payload, ensure_ascii=False), generated_at, version),
    )


def latest_report(conn: sqlite3.Connection, kind: str = "daily") -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM radar_reports WHERE kind=? ORDER BY date DESC LIMIT 1", (kind,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["payload"] = json.loads(d["payload"])
    except ValueError:
        return None
    return d


def report_history(conn: sqlite3.Connection, kind: str = "daily", limit: int = 30) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT date, generated_at, version FROM radar_reports WHERE kind=? ORDER BY date DESC LIMIT ?",
        (kind, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- runs ----------

def start_run(conn: sqlite3.Connection, run_id: str, kind: str, started_at: str) -> None:
    conn.execute(
        "INSERT INTO radar_runs(id, kind, started_at, status) VALUES(?,?,?,'running')",
        (run_id, kind, started_at),
    )


def finish_run(conn: sqlite3.Connection, run_id: str, finished_at: str, status: str,
               stats: Dict[str, Any], error: Optional[str] = None) -> None:
    conn.execute(
        "UPDATE radar_runs SET finished_at=?, status=?, stats=?, error=? WHERE id=?",
        (finished_at, status, json.dumps(stats, ensure_ascii=False), (error or "")[:1000] or None, run_id),
    )


def last_runs(conn: sqlite3.Connection, limit: int = 10) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT * FROM radar_runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        try:
            d["stats"] = json.loads(d.get("stats") or "{}")
        except ValueError:
            d["stats"] = {}
        out.append(d)
    return out


def finish_stale_runs(conn: sqlite3.Connection, grace_minutes: int = 180) -> int:
    """收尾进程被中断后永久停在 running 的运行记录，返回处理条数。

    调度器只在启动时调用一次：把明显早于当前时间、仍标记 running 的记录
    置为 failed 并写入原因，避免"数据库里永远挂着一个正在运行的任务"。
    宽限期内（可能是另一个仍在运行的进程刚开的）不动。
    """
    import datetime as dt

    now = dt.datetime.now().astimezone()
    cutoff = (now - dt.timedelta(minutes=max(1, int(grace_minutes)))).isoformat(timespec="seconds")
    cur = conn.execute(
        "UPDATE radar_runs SET status='failed', finished_at=?, "
        "error=COALESCE(NULLIF(error, ''), '运行未正常结束（进程中断），启动时自动收尾') "
        "WHERE status='running' AND started_at < ?",
        (now.isoformat(timespec="seconds"), cutoff),
    )
    conn.commit()
    return cur.rowcount or 0


# ---------- threat snapshots（全球态势监控：每日固定口径快照） ----------

def save_threat_daily(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    """按主键 upsert 当日态势快照。只写本地计算出的真实输入，不做任何推测。"""
    conn.execute(
        """INSERT INTO radar_threat_daily(
               date, caliber_version, activity, credibility, activity_components,
               credibility_components, event_count, story_count, item_count, platform_count,
               source_count, unverified_share, concentration_share, evidence_gap_count,
               continuing_event_count, grew_event_count, top_activity_heat_mean,
               inputs_available, missing_inputs, computed_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(date) DO UPDATE SET
             caliber_version=excluded.caliber_version, activity=excluded.activity,
             credibility=excluded.credibility,
             activity_components=excluded.activity_components,
             credibility_components=excluded.credibility_components,
             event_count=excluded.event_count, story_count=excluded.story_count,
             item_count=excluded.item_count, platform_count=excluded.platform_count,
             source_count=excluded.source_count, unverified_share=excluded.unverified_share,
             concentration_share=excluded.concentration_share,
             evidence_gap_count=excluded.evidence_gap_count,
             continuing_event_count=excluded.continuing_event_count,
             grew_event_count=excluded.grew_event_count,
             top_activity_heat_mean=excluded.top_activity_heat_mean,
             inputs_available=excluded.inputs_available,
             missing_inputs=excluded.missing_inputs, computed_at=excluded.computed_at""",
        (
            row["date"], int(row.get("caliber_version") or 1), float(row.get("activity") or 0),
            None if row.get("credibility") is None else float(row["credibility"]),
            json.dumps(row.get("activity_components") or {}, ensure_ascii=False),
            json.dumps(row.get("credibility_components") or {}, ensure_ascii=False),
            int(row.get("event_count") or 0), int(row.get("story_count") or 0),
            int(row.get("item_count") or 0), int(row.get("platform_count") or 0),
            int(row.get("source_count") or 0),
            None if row.get("unverified_share") is None else float(row["unverified_share"]),
            None if row.get("concentration_share") is None else float(row["concentration_share"]),
            None if row.get("evidence_gap_count") is None else int(row["evidence_gap_count"]),
            int(row.get("continuing_event_count") or 0), int(row.get("grew_event_count") or 0),
            None if row.get("top_activity_heat_mean") is None else float(row["top_activity_heat_mean"]),
            json.dumps(row.get("inputs_available") or [], ensure_ascii=False),
            json.dumps(row.get("missing_inputs") or [], ensure_ascii=False),
            row["computed_at"],
        ),
    )


def threat_daily_range(conn: sqlite3.Connection, since_date: str, until_date: str) -> List[Dict[str, Any]]:
    """读取 [since_date, until_date] 区间内已物化的固定口径快照。"""
    rows = conn.execute(
        "SELECT * FROM radar_threat_daily WHERE date >= ? AND date <= ? ORDER BY date ASC",
        (since_date, until_date),
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        for key in ("activity_components", "credibility_components", "inputs_available",
                    "missing_inputs"):
            try:
                d[key] = json.loads(d.get(key) or ("[]" if key.endswith("inputs") else "{}"))
            except ValueError:
                d[key] = [] if key.endswith("inputs") else {}
        out.append(d)
    return out


def threat_daily_by_date(conn: sqlite3.Connection, date: str) -> Optional[Dict[str, Any]]:
    rows = threat_daily_range(conn, date, date)
    return rows[0] if rows else None


# ---------- 地理：事件地理标注与结构化地理点 ----------

GEO_EVENT_COLUMNS = ("origin_country_code", "event_country_code", "geo_lat", "geo_lon",
                     "geo_label", "geo_confidence", "geo_method", "geo_source")


def apply_event_geo(conn: sqlite3.Connection, event_id: str, result: Dict[str, Any]) -> None:
    """把地理识别结果写入事件。**只写识别出来的值**，识别不出来就写 NULL。"""
    conn.execute(
        """UPDATE radar_events SET
             origin_country_code=?, event_country_code=?, geo_lat=?, geo_lon=?,
             geo_label=?, geo_confidence=?, geo_method=?, geo_source=?
           WHERE id=?""",
        (
            result.get("origin_country_code"),
            result.get("country_code"),
            result.get("latitude"),
            result.get("longitude"),
            result.get("label") or None,
            result.get("confidence"),
            result.get("method"),
            result.get("source") or None,
            event_id,
        ),
    )


def events_for_geo_tagging(conn: sqlite3.Connection, limit: int = 500,
                           force: bool = False) -> List[Dict[str, Any]]:
    """需要做地理标注的事件。

    默认只取"从未标注 / 标注为 none"的；``force=True`` 时取全部事件，
    用于规则变更后按新口径重算（例如体育队名误判收紧之后）。
    """
    if force:
        rows = conn.execute(
            "SELECT * FROM radar_events ORDER BY date DESC, heat DESC LIMIT ?",
            (limit,)).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM radar_events
               WHERE geo_method IS NULL OR geo_method IN ('none', '')
               ORDER BY date DESC, heat DESC LIMIT ?""", (limit,)).fetchall()
    out = []
    for row in rows:
        data = dict(row)
        try:
            data["keywords"] = json.loads(data.get("keywords") or "[]")
        except ValueError:
            data["keywords"] = []
        out.append(data)
    return out


def geo_tag_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    row = conn.execute(
        """SELECT COUNT(1) total,
                  SUM(CASE WHEN geo_confidence IS NOT NULL AND geo_confidence <> 'none'
                           THEN 1 ELSE 0 END) located,
                  SUM(CASE WHEN event_country_code IS NOT NULL THEN 1 ELSE 0 END) with_country,
                  SUM(CASE WHEN geo_confidence = 'high' THEN 1 ELSE 0 END) high,
                  SUM(CASE WHEN geo_confidence = 'medium' THEN 1 ELSE 0 END) medium,
                  SUM(CASE WHEN geo_confidence = 'low' THEN 1 ELSE 0 END) low
           FROM radar_events""").fetchone()
    total = int((row["total"] if row else 0) or 0)
    located = int((row["located"] if row else 0) or 0)
    return {
        "events_total": total,
        "events_located": located,
        "events_with_country": int((row["with_country"] if row else 0) or 0),
        "confidence": {"high": int((row["high"] if row else 0) or 0),
                       "medium": int((row["medium"] if row else 0) or 0),
                       "low": int((row["low"] if row else 0) or 0)},
        "hit_rate": (round(located / float(total), 4) if total else None),
    }


def upsert_geo_points(conn: sqlite3.Connection, points: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """幂等写入地理点：同一 (layer, external_id) 只更新，不新增。"""
    inserted = 0
    updated = 0
    for point in points:
        exists = conn.execute(
            "SELECT 1 FROM radar_geo_points WHERE layer=? AND external_id=?",
            (point["layer"], str(point["external_id"]))).fetchone()
        conn.execute(
            """INSERT INTO radar_geo_points(layer, external_id, latitude, longitude, title,
                   detail, magnitude, severity, observed_at, source_id, url, collected_at, raw,
                   hazard, magnitude_unit, geometry_type, geometry_json, position_precision,
                   closed_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(layer, external_id) DO UPDATE SET
                 latitude=excluded.latitude, longitude=excluded.longitude, title=excluded.title,
                 detail=excluded.detail, magnitude=excluded.magnitude, severity=excluded.severity,
                 observed_at=excluded.observed_at, url=excluded.url,
                 collected_at=excluded.collected_at, raw=excluded.raw,
                 hazard=excluded.hazard, magnitude_unit=excluded.magnitude_unit,
                 geometry_type=excluded.geometry_type, geometry_json=excluded.geometry_json,
                 position_precision=excluded.position_precision,
                 closed_at=excluded.closed_at, updated_at=excluded.updated_at""",
            (
                point["layer"], str(point["external_id"]), float(point["lat"]),
                float(point["lon"]), point.get("title"), point.get("detail"),
                point.get("magnitude"), point.get("severity"), point.get("observed_at"),
                point.get("source_id"), point.get("url"), point.get("collected_at"),
                json.dumps(point.get("raw") or {}, ensure_ascii=False),
                point.get("hazard"), point.get("magnitude_unit"),
                point.get("geometry_type"), json.dumps(point.get("geometry_json"), ensure_ascii=False)
                if point.get("geometry_json") is not None else None,
                point.get("position_precision"), point.get("closed_at"), point.get("updated_at"),
            ),
        )
        if exists:
            updated += 1
        else:
            inserted += 1
    return {"inserted": inserted, "updated": updated}


# 判断一个灾害点是否"落在时间窗内"：取**活动区间**与窗口是否重叠，
# 而不是只看起始时间。一场 10-14 开始、10-25 才结束的风暴，在"最近 7 天"里仍然应该出现。
GEO_TIME_EXPR = ("COALESCE(NULLIF(closed_at, ''), NULLIF(observed_at, ''), collected_at)")


def geo_points(conn: sqlite3.Connection, layer: Optional[str] = None,
               since_iso: Optional[str] = None, limit: int = 800) -> List[Dict[str, Any]]:
    where, params = [], []
    if layer:
        where.append("layer = ?")
        params.append(layer)
    if since_iso:
        where.append("%s >= ?" % GEO_TIME_EXPR)
        params.append(since_iso)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        "SELECT * FROM radar_geo_points%s ORDER BY %s DESC LIMIT ?" % (clause, GEO_TIME_EXPR),
        params + [limit]).fetchall()
    out = []
    for row in rows:
        data = dict(row)
        try:
            data["raw"] = json.loads(data.get("raw") or "{}")
        except ValueError:
            data["raw"] = {}
        out.append(data)
    return out


def geo_point_counts(conn: sqlite3.Connection,
                     since_iso: Optional[str] = None) -> Dict[str, int]:
    if since_iso:
        rows = conn.execute(
            "SELECT layer, COUNT(1) c FROM radar_geo_points "
            "WHERE %s >= ? GROUP BY layer" % GEO_TIME_EXPR, (since_iso,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT layer, COUNT(1) c FROM radar_geo_points GROUP BY layer").fetchall()
    return {row["layer"]: int(row["c"]) for row in rows}


def geo_hazard_counts(conn: sqlite3.Connection,
                      since_iso: Optional[str] = None) -> Dict[str, int]:
    """按灾害类型统计点数（前端按 地震/火山/野火/洪水/其他 分开关）。"""
    if since_iso:
        rows = conn.execute(
            "SELECT COALESCE(NULLIF(hazard,''),'other') h, COUNT(1) c FROM radar_geo_points "
            "WHERE %s >= ? GROUP BY h" % GEO_TIME_EXPR,
            (since_iso,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT COALESCE(NULLIF(hazard,''),'other') h, COUNT(1) c "
            "FROM radar_geo_points GROUP BY h").fetchall()
    return {row["h"]: int(row["c"]) for row in rows}


def last_geo_collect_at(conn: sqlite3.Connection, layer: Optional[str] = None) -> Optional[str]:
    if layer:
        row = conn.execute("SELECT MAX(collected_at) m FROM radar_geo_points WHERE layer=?",
                           (layer,)).fetchone()
    else:
        row = conn.execute("SELECT MAX(collected_at) m FROM radar_geo_points").fetchone()
    return (row["m"] if row else None) or None


# ---------- push ----------

def push_state(conn: sqlite3.Connection, date: str, channel: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM radar_push_log WHERE date=? AND channel=?", (date, channel)
    ).fetchone()
    return dict(row) if row else None


def record_push(conn: sqlite3.Connection, date: str, channel: str, status: str,
                title: str, content_hash: str, detail: str, attempts: int,
                created_at: str, updated_at: str) -> None:
    conn.execute(
        """INSERT INTO radar_push_log(date, channel, status, title, content_hash, detail, attempts, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(date, channel) DO UPDATE SET
             status=excluded.status, title=excluded.title, content_hash=excluded.content_hash,
             detail=excluded.detail, attempts=excluded.attempts, updated_at=excluded.updated_at""",
        (date, channel, status, title[:200], content_hash, detail[:1000], attempts, created_at, updated_at),
    )


# ---------- investigation ----------

def replace_claims(conn: sqlite3.Connection, event_id: str, claims: List[Dict[str, Any]]) -> List[int]:
    """Claims are regenerated per investigation run; ids are returned in insert order."""
    conn.execute("DELETE FROM radar_claim_evidence WHERE claim_id IN (SELECT id FROM radar_claims WHERE event_id=?)", (event_id,))
    conn.execute("DELETE FROM radar_claims WHERE event_id=?", (event_id,))
    conn.execute("DELETE FROM radar_evidence WHERE event_id=?", (event_id,))
    ids: List[int] = []
    for claim in claims:
        cur = conn.execute(
            """INSERT INTO radar_claims(event_id, text, claimant, stated_at, status, confidence,
                   rationale, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                event_id, claim["text"][:1000], claim.get("claimant", ""), claim.get("stated_at", ""),
                claim.get("status", "insufficient"), claim.get("confidence", "low"),
                claim.get("rationale", "")[:2000], claim["created_at"], claim["updated_at"],
            ),
        )
        ids.append(int(cur.lastrowid))
    return ids


def add_evidence(conn: sqlite3.Connection, evidence: Dict[str, Any]) -> int:
    cur = conn.execute(
        """INSERT INTO radar_evidence(event_id, claim_id, url, source_id, platform, source_type,
               quote, published_at, collected_at, language, first_hand, independent_group, notes)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            evidence["event_id"], evidence.get("claim_id"), evidence.get("url", ""),
            evidence.get("source_id", ""), evidence.get("platform", ""),
            evidence.get("source_type", "news"), (evidence.get("quote") or "")[:2000],
            evidence.get("published_at", ""), evidence["collected_at"],
            evidence.get("language", ""), 1 if evidence.get("first_hand") else 0,
            evidence.get("independent_group", ""), (evidence.get("notes") or "")[:500],
        ),
    )
    return int(cur.lastrowid)


def link_evidence(conn: sqlite3.Connection, claim_id: int, evidence_id: int,
                  relation: str, independence: str, limits: str = "") -> None:
    conn.execute(
        """INSERT OR REPLACE INTO radar_claim_evidence(claim_id, evidence_id, relation, independence, limits)
           VALUES(?,?,?,?,?)""",
        (claim_id, evidence_id, relation, independence, limits[:500]),
    )


def claims_for_event(conn: sqlite3.Connection, event_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT * FROM radar_claims WHERE event_id=? ORDER BY id", (event_id,)).fetchall()
    return [dict(r) for r in rows]


def evidence_for_claim(conn: sqlite3.Connection, claim_id: int) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """SELECT e.*, ce.relation, ce.independence, ce.limits
           FROM radar_claim_evidence ce JOIN radar_evidence e ON e.id = ce.evidence_id
           WHERE ce.claim_id=? ORDER BY e.id""",
        (claim_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def evidence_for_event(conn: sqlite3.Connection, event_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM radar_evidence WHERE event_id=? ORDER BY COALESCE(published_at, collected_at) ASC",
        (event_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def set_investigation_state(conn: sqlite3.Connection, event_id: str, state: str) -> None:
    conn.execute("UPDATE radar_events SET investigation_state=? WHERE id=?", (state, event_id))


# ---------- stories ----------

def upsert_story(conn: sqlite3.Connection, story: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO radar_stories(id, date, title, summary, category, heat, importance,
               confidence, event_count, item_count, platform_count, first_seen_at,
               last_updated_at, metrics)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             date=excluded.date, title=excluded.title, summary=excluded.summary,
             category=excluded.category, heat=excluded.heat, importance=excluded.importance,
             confidence=excluded.confidence, event_count=excluded.event_count,
             item_count=excluded.item_count, platform_count=excluded.platform_count,
             last_updated_at=excluded.last_updated_at, metrics=excluded.metrics""",
        (
            story["id"], story["date"], story["title"], story.get("summary", ""),
            story.get("category", ""), float(story.get("heat") or 0),
            float(story.get("importance") or 0), story.get("confidence", "unverified"),
            int(story.get("event_count") or 0), int(story.get("item_count") or 0),
            int(story.get("platform_count") or 0), story["first_seen_at"],
            story["last_updated_at"], json.dumps(story.get("metrics") or {}, ensure_ascii=False),
        ),
    )


def replace_stories(conn: sqlite3.Connection, date: str, stories: List[Dict[str, Any]]) -> None:
    """故事是纯聚合结果，每天重建：先清掉当天旧故事，避免历史残留与新结果并存。

    同时清掉当天事件上失效的 story_id：同一天多次运行时，上一轮的事件如果没进入本轮
    的聚合范围，不能继续指向一个已经不存在的故事。
    """
    member_ids = []
    for story in stories:
        member_ids.extend((story.get("metrics") or {}).get("event_ids") or [])
    conn.execute("DELETE FROM radar_stories WHERE date=?", (date,))
    if member_ids:
        placeholders = ",".join("?" for _ in member_ids)
        conn.execute(
            "UPDATE radar_events SET story_id=NULL WHERE date=? AND id NOT IN (%s)" % placeholders,
            [date] + member_ids,
        )
    else:
        conn.execute("UPDATE radar_events SET story_id=NULL WHERE date=?", (date,))
    for story in stories:
        upsert_story(conn, story)


def stories_for_date(conn: sqlite3.Connection, date: str, limit: int = 80) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM radar_stories WHERE date=? ORDER BY importance DESC, heat DESC LIMIT ?",
        (date, limit),
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        try:
            d["metrics"] = json.loads(d.get("metrics") or "{}")
        except ValueError:
            d["metrics"] = {}
        out.append(d)
    return out


def events_for_story(conn: sqlite3.Connection, story_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM radar_events WHERE story_id=? ORDER BY heat DESC", (story_id,)
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        for key, default in (("keywords", []), ("metrics", {})):
            try:
                d[key] = json.loads(d.get(key) or ("[]" if default == [] else "{}"))
            except ValueError:
                d[key] = default
        d["tech_flag"] = bool(d.get("tech_flag"))
        out.append(d)
    return out


def assign_story(conn: sqlite3.Connection, event_ids: Iterable[str], story_id: str) -> None:
    for event_id in event_ids:
        conn.execute("UPDATE radar_events SET story_id=? WHERE id=?", (story_id, event_id))


# ---------- 摘要缓存 ----------

def get_summary(conn: sqlite3.Connection, cache_key: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM radar_summaries WHERE cache_key=?", (cache_key,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["points"] = json.loads(d.get("points") or "[]")
    except ValueError:
        d["points"] = []
    return d


def save_summary(conn: sqlite3.Connection, cache_key: str, payload: Dict[str, Any],
                 now: str) -> None:
    conn.execute(
        """INSERT INTO radar_summaries(cache_key, event_id, provider, model, content_hash,
               headline, summary, points, created_at)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(cache_key) DO UPDATE SET
             provider=excluded.provider, model=excluded.model, content_hash=excluded.content_hash,
             headline=excluded.headline, summary=excluded.summary, points=excluded.points,
             created_at=excluded.created_at""",
        (
            cache_key, payload.get("event_id", ""), payload.get("provider", ""),
            payload.get("model", ""), payload.get("content_hash", ""),
            payload.get("headline", ""), payload.get("summary", ""),
            json.dumps(payload.get("points") or [], ensure_ascii=False), now,
        ),
    )


# ---------- 数据源熔断 ----------

def reset_source_failures(conn: sqlite3.Connection, source_id: Optional[str] = None) -> int:
    if source_id:
        cur = conn.execute(
            """UPDATE radar_sources SET consecutive_failures=0, status='unknown',
                   paused_reason=NULL WHERE source_id=?""", (source_id,))
    else:
        cur = conn.execute(
            """UPDATE radar_sources SET consecutive_failures=0, status='unknown',
                   paused_reason=NULL WHERE consecutive_failures>0""")
    return cur.rowcount or 0


def prune(conn: sqlite3.Connection, days: int) -> Dict[str, int]:
    """Retention: drop old raw items, keep events/reports (they are the history)."""
    if days <= 0:
        return {}
    import datetime as dt

    cutoff = (dt.datetime.now() - dt.timedelta(days=days)).isoformat(timespec="seconds")
    cur = conn.execute("DELETE FROM radar_items WHERE collected_at < ?", (cutoff,))
    items = cur.rowcount or 0
    cur = conn.execute("DELETE FROM radar_runs WHERE started_at < ?", (cutoff,))
    runs = cur.rowcount or 0
    return {"items": items, "runs": runs}
