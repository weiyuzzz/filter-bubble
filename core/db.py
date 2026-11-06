"""SQLite persistence. Everything lives in one local file."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from .config import DB_PATH

# SQLite 排序/连接会用临时文件，默认落在系统的 %TEMP%；在受限环境里那里可能写不进去，
# 于是大查询直接报 "unable to open database file"（SQLITE_CANTOPEN）。
# 把临时目录指到我们自己的 data 目录（再配合 temp_store=MEMORY）。
os.environ.setdefault("SQLITE_TMPDIR", str(DB_PATH.parent))
os.environ.setdefault("SQLITE_TEMP_STORE", "2")

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

CREATE TABLE IF NOT EXISTS follows (
    owner TEXT PRIMARY KEY COLLATE NOCASE,
    avatar TEXT,
    created_at TEXT NOT NULL,
    last_checked_at TEXT
);

CREATE TABLE IF NOT EXISTS follow_events (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL COLLATE NOCASE,
    repo TEXT,
    kind TEXT NOT NULL,
    title TEXT,
    body TEXT,
    url TEXT,
    created_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_follow_events_created
    ON follow_events(created_at DESC);

CREATE TABLE IF NOT EXISTS push_log (
    date TEXT NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT,
    content_hash TEXT,
    detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (date, channel)
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
    """打开数据库连接。

    Windows + WAL 下，多进程/多线程同时读写时，新建连接偶尔会拿到
    ``unable to open database file``（SQLITE_CANTOPEN：抢不到 -wal/-shm 或回滚日志），
    它**不受 busy_timeout 影响、会立刻失败**，所以这里做几次短重试。
    """
    last: Optional[Exception] = None
    for attempt in range(4):
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=15)
            conn.row_factory = sqlite3.Row
            # 关键：CANTOPEN 往往发生在「第一条语句」——那时 SQLite 才去打开 -wal/-shm、
            # 回滚日志或**临时文件**。先在这里跑几句，把它纳入重试范围，
            # 否则调用方第一次 execute 就炸（例如 pending_papers 的大排序）。
            conn.execute("PRAGMA temp_store=MEMORY")   # 排序/连接全在内存里，不碰 %TEMP%
            conn.execute("PRAGMA busy_timeout=15000")
            # WAL 下 NORMAL 是官方推荐组合：不再每条提交都 fsync，
            # 写锁持有时间明显变短，多线程/多进程并发时「database is locked」大幅减少。
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("SELECT 1").fetchone()
            return conn
        except sqlite3.OperationalError as exc:
            last = exc
            message = str(exc).lower()
            if ("unable to open database file" not in message
                    and "database is locked" not in message
                    and "database table is locked" not in message):
                raise
            time.sleep(0.2 * (attempt + 1))
    if last:
        raise last
    raise sqlite3.OperationalError("cannot open database")


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
    keep_alive()


_KEEPER: Optional[sqlite3.Connection] = None


def keep_alive() -> Optional[sqlite3.Connection]:
    """常驻一条空闲连接。

    WAL 模式下，**最后一个连接关闭时 SQLite 会删掉 -wal/-shm**；此时若有别的线程/进程
    正好在新建连接，就会偶发 ``unable to open database file``（SQLITE_CANTOPEN）。
    这在「多线程 worker + 每个请求新建连接」的用法里非常致命：一次失败就可能让
    入队/查询整段跳过。留一条常驻连接，文件就不会被删。
    """
    global _KEEPER
    if _KEEPER is not None:
        try:
            _KEEPER.execute("SELECT 1").fetchone()
            return _KEEPER
        except Exception:  # noqa: BLE001 - 连接已失效就重建
            try:
                _KEEPER.close()
            except Exception:  # noqa: BLE001
                pass
            _KEEPER = None
    try:
        _KEEPER = connect()
        _KEEPER.execute("PRAGMA busy_timeout=15000")
    except Exception as exc:  # noqa: BLE001
        print("[db] keep-alive connection failed:", exc, file=sys.stderr, flush=True)
        _KEEPER = None
    return _KEEPER


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


_RETRY_MARKERS = ("unable to open database file", "database is locked",
                  "database table is locked")


def retry(fn, attempts: int = 4, delay: float = 0.2):
    """对 SQLite 的 CANTOPEN/锁错误做**整段重试**。

    新建连接的预热只能挡住「连接时」的失败；在连接建立之后、下一条语句执行时，
    仍可能在 WAL 文件被别的连接重建的瞬间拿到 CANTOPEN。所以对「打开→查询→关闭」
    这类短操作整体重试一次比逐个语句补救简单可靠。fn 自己负责连接开关。
    """
    last: Optional[Exception] = None
    for index in range(max(1, attempts)):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if not any(marker in message for marker in _RETRY_MARKERS):
                raise
            last = exc
            time.sleep(delay * (index + 1))
    if last:
        raise last
    raise sqlite3.OperationalError("database unavailable")


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
