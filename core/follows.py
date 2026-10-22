"""Publisher follows + a separate release feed.

Detection is intentionally lightweight:

* GitHub's public Atom feed (``github.com/<owner>.atom``) gives releases for
  individual publishers without consuming API quota.
* For organisations (whose user feed is often empty) we also check the release
  Atom feed of up to two known repositories.
* One REST call per publisher discovers newly created repositories.

The feed keeps the last 14 days, so following someone immediately shows useful
recent context and future releases/new repos keep landing there.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import sqlite3
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from . import db, github

ATOM_NS = {"a": "http://www.w3.org/2005/Atom", "media": "http://search.yahoo.com/mrss/"}
MAX_FOLLOWS = 20
LOOKBACK_DAYS = 14
REFRESH_MINUTES = 20
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


def _clean_html(value: str, limit: int = 360) -> str:
    if not value:
        return ""
    value = html.unescape(value)
    value = re.sub(r"<script.*?</script>", " ", value, flags=re.S | re.I)
    value = re.sub(r"<style.*?</style>", " ", value, flags=re.S | re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"\s*(Read more|View details)\s*$", "", value, flags=re.I)
    return value[:limit].rstrip()


def _extract_release_body(content: str) -> str:
    if not content:
        return ""
    decoded = html.unescape(content)
    marker = decoded.find("markdown-body")
    if marker >= 0:
        start = decoded.find(">", marker)
        if start >= 0:
            return _clean_html(decoded[start + 1 :])
    return _clean_html(decoded)


def _parse_time(value: str) -> Optional[dt.datetime]:
    if not value:
        return None
    value = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _iso(value: str) -> str:
    parsed = _parse_time(value)
    if not parsed:
        return db.now_iso()
    return parsed.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _feed_events(owner: str, raw: bytes, repo_hint: str = "") -> List[Dict[str, Any]]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    out: List[Dict[str, Any]] = []
    for entry in root.findall("a:entry", ATOM_NS):
        entry_id = (entry.findtext("a:id", default="", namespaces=ATOM_NS) or "").strip()
        title = (entry.findtext("a:title", default="", namespaces=ATOM_NS) or "").strip()
        title = _clean_html(title, 200)
        link_el = entry.find("a:link", ATOM_NS)
        link = link_el.get("href") if link_el is not None else ""
        published = entry.findtext("a:published", default="", namespaces=ATOM_NS) or entry.findtext(
            "a:updated", default="", namespaces=ATOM_NS
        ) or ""
        content = entry.findtext("a:content", default="", namespaces=ATOM_NS) or ""
        avatar_el = entry.find("media:thumbnail", ATOM_NS)
        avatar = avatar_el.get("url") if avatar_el is not None else ""
        stamp = _iso(published)
        if repo_hint:
            repo = repo_hint
            version = title or "新版本"
        else:
            match = re.match(r"^(\S+) released (.+?) at ([\w.-]+/[\w.-]+)$", title)
            if not match:
                continue
            repo = match.group(3)
            version = match.group(2).strip()
        out.append(
            {
                "id": "atom:" + (entry_id or "%s:%s:%s" % (owner, repo, version)),
                "owner": owner,
                "repo": repo,
                "kind": "release",
                "title": version,
                "body": _extract_release_body(content),
                "url": link or "https://github.com/%s" % repo,
                "created_at": stamp,
                "avatar": avatar,
            }
        )
    return out


def _fetch_atom(url: str) -> Optional[bytes]:
    return github.http_get(url, timeout=15)


def _known_repos(conn: sqlite3.Connection, owner: str) -> List[str]:
    rows = conn.execute(
        "SELECT full_name FROM repos WHERE owner = ? COLLATE NOCASE ORDER BY stars DESC LIMIT 2",
        (owner,),
    ).fetchall()
    return [r["full_name"] for r in rows]


def _new_repo_events(owner: str, since: dt.datetime) -> List[Dict[str, Any]]:
    repos = github.fetch_owner_repos(owner, per_page=10)
    out = []
    for repo in repos:
        if repo.get("fork"):
            continue
        created = _parse_time(repo.get("created_at") or "")
        if not created or created < since:
            continue
        out.append(
            {
                "id": "repo:%s" % repo["full_name"],
                "owner": owner,
                "repo": repo["full_name"],
                "kind": "repo",
                "title": repo.get("name") or repo["full_name"].split("/")[-1],
                "body": repo.get("description") or "新建了一个公开仓库",
                "url": repo.get("html_url") or "https://github.com/%s" % repo["full_name"],
                "created_at": _iso(repo.get("created_at") or ""),
                "avatar": (repo.get("owner") or {}).get("avatar_url") or "",
            }
        )
    return out


def _refresh_owner(conn: sqlite3.Connection, row: Dict[str, Any]) -> bool:
    owner = row["owner"]
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=LOOKBACK_DAYS)
    events: List[Dict[str, Any]] = []
    ok = False

    user_feed = _fetch_atom("https://github.com/%s.atom" % urllib.parse.quote(owner))
    if user_feed is not None:
        ok = True
        events.extend(_feed_events(owner, user_feed))

    # Organisations often have an empty user feed; their project release feeds
    # are the useful signal.
    if not events:
        for full_name in _known_repos(conn, owner):
            feed = _fetch_atom(
                "https://github.com/%s/releases.atom" % urllib.parse.quote(full_name)
            )
            if feed is not None:
                ok = True
                events.extend(_feed_events(owner, feed, repo_hint=full_name))

    try:
        events.extend(_new_repo_events(owner, since))
        ok = True
    except Exception:  # noqa: BLE001
        pass

    if not ok:
        return False

    seen = set()
    now = db.now_iso()
    for event in events:
        stamp = _parse_time(event["created_at"])
        if not stamp or stamp < since:
            continue
        if event["id"] in seen:
            continue
        seen.add(event["id"])
        conn.execute(
            """INSERT INTO follow_events(id, owner, repo, kind, title, body, url, created_at, fetched_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 body=excluded.body, title=excluded.title, url=excluded.url""",
            (
                event["id"], owner, event.get("repo"), event.get("kind", "release"),
                event.get("title"), event.get("body"), event.get("url"),
                event["created_at"], now,
            ),
        )
    conn.execute(
        "UPDATE follows SET last_checked_at=? WHERE owner=?",
        (now, owner),
    )
    conn.commit()
    return True


def refresh(conn: sqlite3.Connection, owners: Optional[List[str]] = None,
            force: bool = False, max_seconds: int = 28) -> Dict[str, Any]:
    if owners:
        placeholders = ",".join("?" for _ in owners)
        rows = conn.execute(
            "SELECT * FROM follows WHERE owner COLLATE NOCASE IN (%s)" % placeholders,
            owners,
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM follows ORDER BY created_at ASC").fetchall()
    deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=max_seconds)
    for row in rows:
        if dt.datetime.now(dt.timezone.utc) >= deadline:
            break
        last = _parse_time(row["last_checked_at"] or "")
        if not force and last and (dt.datetime.now(dt.timezone.utc) - last).total_seconds() < REFRESH_MINUTES * 60:
            continue
        try:
            _refresh_owner(conn, dict(row))
        except Exception as exc:  # noqa: BLE001
            print("[follows] refresh failed for %s: %s" % (row["owner"], exc))
    return summary(conn)


def owners(conn: sqlite3.Connection) -> List[str]:
    return [r["owner"] for r in conn.execute("SELECT owner FROM follows ORDER BY owner COLLATE NOCASE")]


def is_following(conn: sqlite3.Connection, owner: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM follows WHERE owner=? COLLATE NOCASE", (owner,)).fetchone())


def toggle(conn: sqlite3.Connection, owner: str) -> Dict[str, Any]:
    owner = (owner or "").strip()
    if not OWNER_RE.match(owner):
        raise ValueError("invalid GitHub owner")
    row = conn.execute("SELECT * FROM follows WHERE owner=? COLLATE NOCASE", (owner,)).fetchone()
    if row:
        conn.execute("DELETE FROM follow_events WHERE owner=? COLLATE NOCASE", (owner,))
        conn.execute("DELETE FROM follows WHERE owner=? COLLATE NOCASE", (owner,))
        conn.commit()
        return {"following": False, "follows": summary(conn)}
    count = conn.execute("SELECT COUNT(*) FROM follows").fetchone()[0]
    if count >= MAX_FOLLOWS:
        raise ValueError("最多关注 %d 位发布者" % MAX_FOLLOWS)
    conn.execute(
        "INSERT INTO follows(owner, avatar, created_at) VALUES(?,?,?)",
        (owner, "https://github.com/%s.png?size=96" % owner, db.now_iso()),
    )
    conn.commit()
    try:
        refresh(conn, [owner], force=True, max_seconds=12)
    except Exception as exc:  # noqa: BLE001
        print("[follows] initial refresh failed:", exc)
    return {"following": True, "follows": summary(conn)}


def summary(conn: sqlite3.Connection) -> Dict[str, Any]:
    owner_rows = conn.execute(
        """SELECT f.owner, f.avatar, f.created_at, f.last_checked_at,
                  COUNT(e.id) AS event_count, MAX(e.created_at) AS last_event_at
           FROM follows f
           LEFT JOIN follow_events e ON e.owner = f.owner COLLATE NOCASE
           GROUP BY f.owner
           ORDER BY (last_event_at IS NULL), last_event_at DESC, f.owner COLLATE NOCASE"""
    ).fetchall()
    owners_out = [dict(r) for r in owner_rows]
    event_rows = conn.execute(
        "SELECT * FROM follow_events ORDER BY created_at DESC LIMIT 60"
    ).fetchall()
    events = []
    per_owner: Dict[str, int] = {}
    for row in event_rows:
        owner = row["owner"]
        if per_owner.get(owner, 0) >= 5:
            continue
        per_owner[owner] = per_owner.get(owner, 0) + 1
        event = dict(row)
        event["body"] = _clean_html(event.get("body") or "", 300)
        events.append(event)
    visible_counts: Dict[str, int] = {}
    for event in events:
        visible_counts[event["owner"]] = visible_counts.get(event["owner"], 0) + 1
    for owner in owners_out:
        owner["event_count"] = visible_counts.get(owner["owner"], 0)
    return {
        "owners": owners_out,
        "events": events,
        "counts": {
            "owners": len(owners_out),
            "events": len(events),
            "new": len(events),
        },
        "refreshed_at": max(
            [o.get("last_checked_at") or "" for o in owners_out] or [""]
        ) or None,
        "max_follows": MAX_FOLLOWS,
        "lookback_days": LOOKBACK_DAYS,
    }


def event_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM follow_events").fetchone()[0])
