"""已读状态：NEW 标签只在用户真正看过之后消失。

规则：
* 一条内容第一次出现在日报里时标记为 NEW；
* 用户打开「项目速读」（GitHub）或「深入调查」（热点雷达）后写入已读；
* 已读是按内容标识记录的，所以刷新排序、重新聚类都不会让标签复活。
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any, Dict, Iterable, Set

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_items (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);
"""


def _conn() -> sqlite3.Connection:
    conn = db.connect()
    conn.executescript(SCHEMA)
    return conn


def mark(kind: str, keys: Iterable[str]) -> int:
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    rows = [(kind, str(key), now) for key in keys if str(key or "").strip()]
    if not rows:
        return 0
    conn = _conn()
    try:
        conn.executemany(
            "INSERT INTO seen_items(kind, key, seen_at) VALUES(?,?,?) "
            "ON CONFLICT(kind, key) DO UPDATE SET seen_at=excluded.seen_at", rows)
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def seen_keys(kind: str) -> Set[str]:
    conn = _conn()
    try:
        return {row["key"] for row in conn.execute(
            "SELECT key FROM seen_items WHERE kind=?", (kind,))}
    finally:
        conn.close()


def annotate(kind: str, items: Iterable[Dict[str, Any]], key_field: str) -> int:
    """给条目加上 is_new；返回新条目数量。"""
    seen = seen_keys(kind)
    fresh = 0
    for item in items:
        key = str(item.get(key_field) or "")
        is_new = bool(key) and key not in seen
        item["is_new"] = is_new
        if is_new:
            fresh += 1
    return fresh


def count(kind: str) -> int:
    conn = _conn()
    try:
        return conn.execute("SELECT COUNT(1) c FROM seen_items WHERE kind=?", (kind,)).fetchone()["c"]
    finally:
        conn.close()
