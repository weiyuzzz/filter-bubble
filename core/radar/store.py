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
"""


def init() -> None:
    """Create radar tables if missing. Safe to call on every start."""
    with db.connect() as conn:
        conn.executescript(SCHEMA)
        # 向后兼容的增量迁移：只加列，不动已有数据
        _ensure_column(conn, "radar_events", "story_id", "TEXT")
        _ensure_column(conn, "radar_sources", "paused_reason", "TEXT")
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
    """故事是纯聚合结果，每天重建：先清掉当天旧故事，避免历史残留与新结果并存。"""
    conn.execute("DELETE FROM radar_stories WHERE date=?", (date,))
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
