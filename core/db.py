"""SQLite persistence. Everything lives in one local file."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from .config import DB_PATH

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS repos (
    full_name TEXT PRIMARY KEY,
    owner TEXT, name TEXT, description TEXT, url TEXT,
    language TEXT, stars INTEGER DEFAULT 0, forks INTEGER DEFAULT 0,
    topics TEXT DEFAULT '[]', avatar TEXT, homepage TEXT, license TEXT,
    created_at TEXT, pushed_at TEXT, meta_at TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    full_name TEXT,
    date TEXT,
    stars INTEGER,
    stars_today INTEGER,
    stars_week INTEGER,
    daily_rank INTEGER,
    weekly_rank INTEGER,
    primary key (full_name, date)
);

CREATE TABLE IF NOT EXISTS digests (
    date TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    signal INTEGER NOT NULL,
    features TEXT DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weights (
    feature TEXT PRIMARY KEY,
    weight REAL NOT NULL,
    pos INTEGER DEFAULT 0,
    neg INTEGER DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS bookmarks (
    full_name TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS translations (
    key TEXT PRIMARY KEY,
    zh TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readmes (
    full_name TEXT PRIMARY KEY,
    excerpt TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def get_meta(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def save_repo(conn: sqlite3.Connection, repo: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO repos(full_name, owner, name, description, url, language, stars, forks,
                             topics, avatar, homepage, license, created_at, pushed_at, meta_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(full_name) DO UPDATE SET
             description=excluded.description, language=excluded.language, stars=excluded.stars,
             forks=excluded.forks, topics=excluded.topics, avatar=excluded.avatar,
             homepage=excluded.homepage, license=excluded.license, created_at=excluded.created_at,
             pushed_at=excluded.pushed_at, meta_at=excluded.meta_at""",
        (
            repo["full_name"], repo.get("owner"), repo.get("name"), repo.get("description"),
            repo.get("url"), repo.get("language"), int(repo.get("stars") or 0),
            int(repo.get("forks") or 0), json.dumps(repo.get("topics") or [], ensure_ascii=False),
            repo.get("avatar"), repo.get("homepage"), repo.get("license"), repo.get("created_at"),
            repo.get("pushed_at"), repo.get("meta_at"),
        ),
    )


def get_repo(conn: sqlite3.Connection, full_name: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM repos WHERE full_name=?", (full_name,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["topics"] = json.loads(d.get("topics") or "[]")
    return d


def save_snapshot(conn: sqlite3.Connection, repo: Dict[str, Any], date: str) -> None:
    conn.execute(
        """INSERT INTO snapshots(full_name, date, stars, stars_today, stars_week, daily_rank, weekly_rank)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(full_name, date) DO UPDATE SET
             stars=excluded.stars, stars_today=excluded.stars_today, stars_week=excluded.stars_week,
             daily_rank=excluded.daily_rank, weekly_rank=excluded.weekly_rank""",
        (
            repo["full_name"], date, int(repo.get("stars") or 0),
            repo.get("stars_today"), repo.get("stars_week"),
            repo.get("daily_rank"), repo.get("weekly_rank"),
        ),
    )


def history(conn: sqlite3.Connection, full_name: str, limit: int = 30) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM snapshots WHERE full_name=? ORDER BY date ASC LIMIT ?", (full_name, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def feedback_map(conn: sqlite3.Connection) -> Dict[str, int]:
    """Latest signal per repo."""
    rows = conn.execute(
        """SELECT f.full_name, f.signal FROM feedback f
           JOIN (SELECT full_name, MAX(id) AS mid FROM feedback GROUP BY full_name) x
             ON x.mid = f.id"""
    ).fetchall()
    return {r["full_name"]: int(r["signal"]) for r in rows}


def bookmarks(conn: sqlite3.Connection) -> set:
    return {r["full_name"] for r in conn.execute("SELECT full_name FROM bookmarks").fetchall()}
