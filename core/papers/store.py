"""论文存储：paper_* 三张表，与雷达的 radar_* 表逻辑隔离。

去重优先级：DOI → PMID → arXiv ID → 规范化标题。
同一篇论文被多个来源收录时只保留一行，把来源合并进 ``sources_json``。
原始摘要一旦写入不会被空值或 AI 结果覆盖。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
from typing import Any, Dict, Iterable, List, Optional

from .. import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_items (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    abstract TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    doi TEXT,
    pmid TEXT,
    arxiv_id TEXT,
    venue TEXT,
    disciplines TEXT NOT NULL DEFAULT '[]',
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    sources_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_paper_published ON paper_items(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_paper_source ON paper_items(source);
CREATE INDEX IF NOT EXISTS idx_paper_doi ON paper_items(doi);

CREATE TABLE IF NOT EXISTS paper_ai_overviews (
    paper_id TEXT PRIMARY KEY,
    prompt_version INTEGER NOT NULL DEFAULT 1,
    model TEXT,
    content_hash TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    overview TEXT,
    error TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_source_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    fetched INTEGER NOT NULL DEFAULT 0,
    with_abstract INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_paper_runs ON paper_source_runs(source, started_at DESC);
"""


def init() -> None:
    conn = db.connect()
    try:
        conn.executescript(SCHEMA)
        # 兼容旧数据：把 "2026-Oct-21" / "2026-Dec" 这类日期归一成 ISO，保证排序正确
        rows = conn.execute(
            "SELECT id, published_at FROM paper_items "
            "WHERE published_at <> '' AND published_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"
        ).fetchall()
        for row in rows:
            fixed = _iso_date(row["published_at"])
            if fixed and fixed != row["published_at"]:
                conn.execute("UPDATE paper_items SET published_at=? WHERE id=?", (fixed, row["id"]))
        conn.commit()
    finally:
        conn.close()


_MONTHS = {name: idx for idx, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def _iso_date(value: str) -> str:
    text = (value or "").strip()
    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        return "%04d-%02d-%02d" % (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    match = re.match(r"^(\d{4})-([A-Za-z]{3})-(\d{1,2})", text)
    if match:
        month = _MONTHS.get(match.group(2).lower())
        if month:
            return "%04d-%02d-%02d" % (int(match.group(1)), month, int(match.group(3)))
    match = re.match(r"^(\d{4})-([A-Za-z]{3})$", text)
    if match:
        month = _MONTHS.get(match.group(2).lower())
        if month:
            return "%04d-%02d-01" % (int(match.group(1)), month)
    match = re.match(r"^(\d{4})-(\d{1,2})$", text)
    if match:
        return "%04d-%02d-01" % (int(match.group(1)), int(match.group(2)))
    match = re.match(r"^(\d{4})$", text)
    if match:
        return "%s-01-01" % match.group(1)
    return text


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _norm_title(title: str) -> str:
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", (title or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def paper_key(item: Dict[str, Any]) -> str:
    """稳定去重键：优先强标识符，其次规范化标题。"""
    doi = (item.get("doi") or "").strip().lower()
    if doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/"):]
    if doi:
        return "doi:" + doi
    pmid = (item.get("pmid") or "").strip()
    if pmid:
        return "pmid:" + pmid
    arxiv_id = (item.get("arxiv_id") or "").strip().lower()
    if arxiv_id:
        return "arxiv:" + re.sub(r"v\d+$", "", arxiv_id)
    digest = hashlib.sha1(_norm_title(item.get("title", "")).encode("utf-8")).hexdigest()[:20]
    return "title:" + digest


def upsert_papers(conn: sqlite3.Connection, items: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """写入论文。返回 {inserted, merged, skipped}。"""
    inserted = merged = skipped = 0
    now = _now()
    for item in items:
        title = (item.get("title") or "").strip()
        if not title:
            skipped += 1
            continue
        key = paper_key(item)
        source_entry = {"source": item.get("source", ""), "source_id": item.get("source_id", ""),
                        "url": item.get("url", "")}
        row = conn.execute("SELECT * FROM paper_items WHERE id=?", (key,)).fetchone()
        if row:
            sources = json.loads(row["sources_json"] or "[]")
            if not any(s.get("source") == source_entry["source"] and
                       s.get("source_id") == source_entry["source_id"] for s in sources):
                sources.append(source_entry)
            # 只在原值为空时补全，绝不用空值或新摘要覆盖已有原文
            conn.execute(
                """UPDATE paper_items SET
                     abstract = CASE WHEN abstract='' THEN ? ELSE abstract END,
                     url = CASE WHEN url='' THEN ? ELSE url END,
                     venue = CASE WHEN venue IS NULL OR venue='' THEN ? ELSE venue END,
                     published_at = CASE WHEN published_at IS NULL OR published_at='' THEN ? ELSE published_at END,
                     disciplines = CASE WHEN disciplines='[]' THEN ? ELSE disciplines END,
                     sources_json = ?,
                     raw_json = CASE WHEN raw_json='{}' THEN ? ELSE raw_json END
                   WHERE id=?""",
                (item.get("abstract", ""), item.get("url", ""), item.get("venue", ""),
                 item.get("published_at", ""), json.dumps(item.get("disciplines") or [], ensure_ascii=False),
                 json.dumps(sources, ensure_ascii=False),
                 json.dumps(item.get("raw") or {}, ensure_ascii=False), key))
            merged += 1
            continue

        conn.execute(
            """INSERT INTO paper_items(id, source, source_id, title, abstract, url, doi, pmid,
                   arxiv_id, venue, disciplines, published_at, fetched_at, first_seen_at,
                   sources_json, raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, item.get("source", ""), item.get("source_id", ""), title,
             item.get("abstract", ""), item.get("url", ""), item.get("doi", ""),
             item.get("pmid", ""), item.get("arxiv_id", ""), item.get("venue", ""),
             json.dumps(item.get("disciplines") or [], ensure_ascii=False),
             item.get("published_at", ""), now, now,
             json.dumps([source_entry], ensure_ascii=False),
             json.dumps(item.get("raw") or {}, ensure_ascii=False)))
        inserted += 1
    conn.commit()
    return {"inserted": inserted, "merged": merged, "skipped": skipped}


def _row_to_paper(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "source": row["source"],
        "source_id": row["source_id"],
        "title": row["title"],
        "abstract": row["abstract"] or "",
        "url": row["url"] or "",
        "doi": row["doi"] or "",
        "pmid": row["pmid"] or "",
        "arxiv_id": row["arxiv_id"] or "",
        "venue": row["venue"] or "",
        "disciplines": json.loads(row["disciplines"] or "[]"),
        "published_at": row["published_at"] or "",
        "fetched_at": row["fetched_at"],
        "sources": json.loads(row["sources_json"] or "[]"),
    }


def _discipline_pattern(discipline: str) -> str:
    """disciplines 存的是 JSON 数组，用 %"key"% 做子串匹配。"""
    return '%"' + discipline + '"%'


def list_papers(conn: sqlite3.Connection, discipline: str = "all", limit: int = 10,
                offset: int = 0) -> List[Dict[str, Any]]:
    """按学科筛选，按发布日期倒序。"""
    if discipline and discipline != "all":
        pattern = _discipline_pattern(discipline)
        rows = conn.execute(
            "SELECT * FROM paper_items WHERE disciplines LIKE ? "
            "ORDER BY COALESCE(NULLIF(published_at,''), fetched_at) DESC LIMIT ? OFFSET ?",
            (pattern, limit, offset)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM paper_items "
            "ORDER BY COALESCE(NULLIF(published_at,''), fetched_at) DESC LIMIT ? OFFSET ?",
            (limit, offset)).fetchall()
    return [_row_to_paper(row) for row in rows]


def count_papers(conn: sqlite3.Connection, discipline: str = "all") -> int:
    if discipline and discipline != "all":
        return conn.execute("SELECT COUNT(1) c FROM paper_items WHERE disciplines LIKE ?",
                            (_discipline_pattern(discipline),)).fetchone()["c"]
    return conn.execute("SELECT COUNT(1) c FROM paper_items").fetchone()["c"]


def discipline_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    out = {"all": count_papers(conn, "all")}
    for row in conn.execute("SELECT disciplines FROM paper_items").fetchall():
        for key in json.loads(row["disciplines"] or "[]"):
            out[key] = out.get(key, 0) + 1
    return out


def get_paper(conn: sqlite3.Connection, paper_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM paper_items WHERE id=?", (paper_id,)).fetchone()
    return _row_to_paper(row) if row else None


# ---------------- AI 概况 ----------------

def content_hash(paper: Dict[str, Any]) -> str:
    raw = "%s|%s|%s" % (paper.get("title", ""), paper.get("abstract", ""),
                        paper.get("source", ""))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def get_overview(conn: sqlite3.Connection, paper_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM paper_ai_overviews WHERE paper_id=?", (paper_id,)).fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        data["overview"] = json.loads(row["overview"]) if row["overview"] else None
    except ValueError:
        data["overview"] = None
    return data


def save_overview(conn: sqlite3.Connection, paper_id: str, status: str, overview: Optional[Dict[str, Any]],
                  model: str, prompt_version: int, error: str = "",
                  content_hash_value: str = "") -> None:
    conn.execute(
        """INSERT INTO paper_ai_overviews(paper_id, prompt_version, model, content_hash, status,
               overview, error, updated_at)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(paper_id) DO UPDATE SET
             prompt_version=excluded.prompt_version, model=excluded.model,
             content_hash=excluded.content_hash, status=excluded.status,
             overview=excluded.overview, error=excluded.error, updated_at=excluded.updated_at""",
        (paper_id, prompt_version, model, content_hash_value, status,
         json.dumps(overview, ensure_ascii=False) if overview else None,
         error[:500], _now()))
    conn.commit()


def pending_papers(conn: sqlite3.Connection, limit: int, prompt_version: int) -> List[Dict[str, Any]]:
    """没有成功概况、或内容已变化、或需要重试的论文。"""
    rows = conn.execute(
        """SELECT p.*, o.status AS ai_status, o.content_hash AS ai_hash,
                  o.prompt_version AS ai_prompt
           FROM paper_items p
           LEFT JOIN paper_ai_overviews o ON o.paper_id = p.id
           WHERE p.abstract <> ''
             AND (o.paper_id IS NULL
                  OR o.status = 'failed'
                  OR o.content_hash <> ? 
                  OR o.status = 'done' AND o.prompt_version < ?)
           ORDER BY COALESCE(NULLIF(p.published_at,''), p.fetched_at) DESC
           LIMIT ?""",
        ("", prompt_version, limit)).fetchall()
    papers = []
    for row in rows:
        paper = _row_to_paper(row)
        if row["ai_status"] == "done" and row["ai_hash"] == content_hash(paper):
            continue
        papers.append(paper)
    return papers


# ---------------- 来源运行记录 ----------------

def start_run(conn: sqlite3.Connection, source: str) -> int:
    cur = conn.execute(
        "INSERT INTO paper_source_runs(source, started_at, status) VALUES(?,?,'running')",
        (source, _now()))
    conn.commit()
    return int(cur.lastrowid or 0)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, fetched: int,
               with_abstract: int, duration_ms: int, error: str = "") -> None:
    conn.execute(
        """UPDATE paper_source_runs SET finished_at=?, status=?, fetched=?, with_abstract=?,
               duration_ms=?, error=? WHERE id=?""",
        (_now(), status, fetched, with_abstract, duration_ms, error[:500], run_id))
    conn.commit()


def latest_runs(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """SELECT r.* FROM paper_source_runs r
           JOIN (SELECT source, MAX(id) AS mid FROM paper_source_runs GROUP BY source) m
             ON m.mid = r.id
           ORDER BY r.source""").fetchall()
    return [dict(row) for row in rows]


def run_history(conn: sqlite3.Connection, limit: int = 20) -> List[Dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        "SELECT * FROM paper_source_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    total = conn.execute("SELECT COUNT(1) c FROM paper_items").fetchone()["c"]
    with_abs = conn.execute("SELECT COUNT(1) c FROM paper_items WHERE abstract <> ''").fetchone()["c"]
    done = conn.execute("SELECT COUNT(1) c FROM paper_ai_overviews WHERE status='done'").fetchone()["c"]
    failed = conn.execute("SELECT COUNT(1) c FROM paper_ai_overviews WHERE status='failed'").fetchone()["c"]
    last = conn.execute("SELECT MAX(fetched_at) t FROM paper_items").fetchone()["t"] or ""
    return {"total": total, "with_abstract": with_abs,
            "abstract_ratio": round(with_abs / total, 4) if total else 0.0,
            "ai_done": done, "ai_failed": failed, "last_fetched_at": last}
