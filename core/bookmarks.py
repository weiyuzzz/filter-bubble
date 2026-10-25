"""收藏夹：收藏项目、按文件夹分类、在收藏里搜索。

原来的收藏只是 ``bookmarks`` 表里一个 ``full_name``，只能在当天日报里点亮书签，
一旦项目掉出日报就看不到了。这里做了三件事：

* **快照元数据**：收藏时把简介、语言、star、topics 一起存下来，收藏列表不再依赖
  当天日报，也不依赖 ``repos`` 表（旧收藏会用 ``repos`` 里已有的数据回填一次）；
* **文件夹**：``bookmark_folders`` 表，收藏项挂在 ``folder_id`` 上，NULL = 未分类；
* **搜索**：在名称、简介、语言、topics 上做匹配。

表结构是**增量**的：``bookmarks`` 表本身仍在 ``db.SCHEMA`` 里声明（原有回归测试要求），
这里只做 ``ALTER TABLE`` 加列，不改动原有声明。
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any, Dict, List, Optional

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookmark_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

MAX_FOLDERS = 50
MAX_FOLDER_NAME = 30
UNFILED = "none"

# bookmarks 表的增量列：旧库只存了 full_name + created_at
_COLUMNS = (
    ("folder_id", "INTEGER"),
    ("url", "TEXT"),
    ("description", "TEXT"),
    ("language", "TEXT"),
    ("stars", "INTEGER"),
    ("topics", "TEXT"),
    ("avatar", "TEXT"),
    ("meta_at", "TEXT"),
)


def init(conn: sqlite3.Connection) -> None:
    """建表 + 加列 + 给旧收藏回填一次元数据（幂等）。"""
    conn.executescript(SCHEMA)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(bookmarks)").fetchall()}
    for name, ddl in _COLUMNS:
        if name not in cols:
            conn.execute("ALTER TABLE bookmarks ADD COLUMN %s %s" % (name, ddl))
    # 旧收藏（只有 full_name）从 repos 表里补一次元数据，避免收藏列表一片空白
    conn.execute(
        """UPDATE bookmarks SET
             description = COALESCE(description, (SELECT r.description FROM repos r
                                                  WHERE r.full_name = bookmarks.full_name)),
             language    = COALESCE(language, (SELECT r.language FROM repos r
                                               WHERE r.full_name = bookmarks.full_name)),
             stars       = COALESCE(stars, (SELECT r.stars FROM repos r
                                            WHERE r.full_name = bookmarks.full_name)),
             topics      = COALESCE(topics, (SELECT r.topics FROM repos r
                                             WHERE r.full_name = bookmarks.full_name)),
             avatar      = COALESCE(avatar, (SELECT r.avatar FROM repos r
                                             WHERE r.full_name = bookmarks.full_name)),
             url         = COALESCE(url, (SELECT r.url FROM repos r
                                          WHERE r.full_name = bookmarks.full_name))
           WHERE description IS NULL OR url IS NULL""")
    conn.commit()


def _clean(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _json_topics(value: Any) -> str:
    topics = value if isinstance(value, list) else []
    return json.dumps([str(t)[:40] for t in topics[:12]], ensure_ascii=False)


def _topics_of(value: Any) -> List[str]:
    try:
        parsed = json.loads(value or "[]")
    except ValueError:
        return []
    return [str(t) for t in parsed] if isinstance(parsed, list) else []


def _like(query: str) -> str:
    """转义 LIKE 通配符，用户输 % 时不该匹配一切。"""
    return "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# ---------- 文件夹 ----------

def folders(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """SELECT f.id, f.name, f.created_at,
                  (SELECT COUNT(*) FROM bookmarks b WHERE b.folder_id = f.id) AS count
           FROM bookmark_folders f
           ORDER BY f.id ASC"""
    ).fetchall()
    return [dict(r) for r in rows]


def counts(conn: sqlite3.Connection) -> Dict[str, int]:
    total = conn.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0]
    unfiled = conn.execute("SELECT COUNT(*) FROM bookmarks WHERE folder_id IS NULL").fetchone()[0]
    folder_count = conn.execute("SELECT COUNT(*) FROM bookmark_folders").fetchone()[0]
    return {"total": int(total), "unfiled": int(unfiled), "folders": int(folder_count)}


def create_folder(conn: sqlite3.Connection, name: str) -> Dict[str, Any]:
    name = _clean(name, MAX_FOLDER_NAME)
    if not name:
        raise ValueError("文件夹名不能为空")
    count = conn.execute("SELECT COUNT(*) FROM bookmark_folders").fetchone()[0]
    if count >= MAX_FOLDERS:
        raise ValueError("最多 %d 个文件夹" % MAX_FOLDERS)
    exists = conn.execute(
        "SELECT id FROM bookmark_folders WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    if exists:
        raise ValueError("已经有同名文件夹")
    cur = conn.execute("INSERT INTO bookmark_folders(name, created_at) VALUES(?,?)",
                       (name, db.now_iso()))
    conn.commit()
    row = conn.execute("SELECT * FROM bookmark_folders WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


def rename_folder(conn: sqlite3.Connection, folder_id: int, name: str) -> None:
    name = _clean(name, MAX_FOLDER_NAME)
    if not name:
        raise ValueError("文件夹名不能为空")
    dup = conn.execute(
        "SELECT id FROM bookmark_folders WHERE name = ? COLLATE NOCASE AND id != ?",
        (name, folder_id),
    ).fetchone()
    if dup:
        raise ValueError("已经有同名文件夹")
    cur = conn.execute("UPDATE bookmark_folders SET name=? WHERE id=?", (name, folder_id))
    if not cur.rowcount:
        raise ValueError("文件夹不存在")
    conn.commit()


def delete_folder(conn: sqlite3.Connection, folder_id: int) -> None:
    """删文件夹，里面的收藏回到「未分类」，不连收藏一起删。"""
    cur = conn.execute("DELETE FROM bookmark_folders WHERE id=?", (folder_id,))
    if not cur.rowcount:
        raise ValueError("文件夹不存在")
    conn.execute("UPDATE bookmarks SET folder_id = NULL WHERE folder_id=?", (folder_id,))
    conn.commit()


def _folder_id(conn: sqlite3.Connection, value: Any) -> Optional[int]:
    if value in (None, "", UNFILED, "0"):
        return None
    try:
        folder_id = int(value)
    except (TypeError, ValueError):
        return None
    row = conn.execute("SELECT id FROM bookmark_folders WHERE id=?", (folder_id,)).fetchone()
    return folder_id if row else None


# ---------- 收藏项 ----------

def _snapshot(item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    item = item or {}
    return {
        "url": _clean(item.get("url"), 500),
        "description": _clean(item.get("summary_zh") or item.get("description"), 600),
        "language": _clean(item.get("language"), 40),
        "stars": int(item.get("stars") or 0),
        "topics": _json_topics(item.get("topics")),
        "avatar": _clean(item.get("avatar"), 500) or
                  "https://github.com/%s.png?size=96" % (item.get("owner") or
                                                         (item.get("full_name") or "/").split("/")[0]),
    }


def add(conn: sqlite3.Connection, repo: str, item: Optional[Dict[str, Any]] = None,
        folder_id: Any = None) -> None:
    snap = _snapshot(item)
    conn.execute(
        """INSERT INTO bookmarks(full_name, created_at, folder_id, url, description,
                                 language, stars, topics, avatar, meta_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(full_name) DO UPDATE SET
             folder_id = excluded.folder_id,
             url = COALESCE(NULLIF(excluded.url, ''), bookmarks.url),
             description = COALESCE(NULLIF(excluded.description, ''), bookmarks.description),
             language = COALESCE(NULLIF(excluded.language, ''), bookmarks.language),
             stars = MAX(excluded.stars, COALESCE(bookmarks.stars, 0)),
             topics = COALESCE(NULLIF(excluded.topics, '[]'), bookmarks.topics),
             avatar = COALESCE(NULLIF(excluded.avatar, ''), bookmarks.avatar),
             meta_at = excluded.meta_at""",
        (repo, db.now_iso(), _folder_id(conn, folder_id), snap["url"], snap["description"],
         snap["language"], snap["stars"], snap["topics"], snap["avatar"], db.now_iso()),
    )
    conn.commit()


def remove(conn: sqlite3.Connection, repo: str) -> None:
    conn.execute("DELETE FROM bookmarks WHERE full_name=?", (repo,))
    conn.commit()


def exists(conn: sqlite3.Connection, repo: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM bookmarks WHERE full_name=?", (repo,)).fetchone())


def toggle(conn: sqlite3.Connection, repo: str, item: Optional[Dict[str, Any]] = None,
           folder_id: Any = None) -> bool:
    """收藏 / 取消收藏。返回 True 表示现在已收藏。"""
    if exists(conn, repo):
        remove(conn, repo)
        return False
    add(conn, repo, item, folder_id)
    return True


def move(conn: sqlite3.Connection, repo: str, folder_id: Any) -> bool:
    cur = conn.execute("UPDATE bookmarks SET folder_id=? WHERE full_name=?",
                       (_folder_id(conn, folder_id), repo))
    conn.commit()
    return bool(cur.rowcount)


def items(conn: sqlite3.Connection, folder: Any = "all", query: str = "") -> List[Dict[str, Any]]:
    """列出收藏。folder 取 "all" / "none"(未分类) / 文件夹 id；query 为搜索词。"""
    where, params = [], []
    if folder == UNFILED:
        where.append("b.folder_id IS NULL")
    elif folder not in (None, "", "all"):
        fid = _folder_id(conn, folder)
        if fid is None:
            where.append("b.folder_id IS NULL")
        else:
            where.append("b.folder_id = ?")
            params.append(fid)

    query = _clean(query, 60)
    if query:
        pattern = _like(query)
        where.append(
            """(b.full_name LIKE ? ESCAPE '\\' OR b.description LIKE ? ESCAPE '\\'
                OR b.language LIKE ? ESCAPE '\\' OR b.topics LIKE ? ESCAPE '\\')"""
        )
        params.extend([pattern] * 4)

    sql = """SELECT b.*, f.name AS folder_name,
                    (SELECT r.forks FROM repos r WHERE r.full_name = b.full_name) AS forks,
                    (SELECT r.homepage FROM repos r WHERE r.full_name = b.full_name) AS homepage
             FROM bookmarks b
             LEFT JOIN bookmark_folders f ON f.id = b.folder_id"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY b.created_at DESC, b.full_name ASC"

    out = []
    for row in conn.execute(sql, params).fetchall():
        data = dict(row)
        data["topics"] = _topics_of(row["topics"])
        data["owner"] = (data.get("full_name") or "/").split("/")[0]
        data["name"] = (data.get("full_name") or "/").split("/")[-1]
        out.append(data)
    return out


def state(conn: sqlite3.Connection, folder: Any = "all", query: str = "") -> Dict[str, Any]:
    """收藏页需要的全部数据一次返回。"""
    return {
        "folders": folders(conn),
        "items": items(conn, folder, query),
        "folder": "all" if folder in (None, "") else folder,
        "query": _clean(query, 60),
        "counts": counts(conn),
    }


def names(conn: sqlite3.Connection) -> set:
    return {row["full_name"] for row in conn.execute("SELECT full_name FROM bookmarks").fetchall()}