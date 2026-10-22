"""Server酱 Turbo WeChat brief.

One short, readable message per day.  The full digest stays in the local UI;
the phone gets the top projects with a "what / why / how / scenarios" brief and
a direct GitHub link for each one.

Only Server酱 Turbo (SendKey starts with ``SCT``) delivers to WeChat.  SC3 keys
(``sctp...``) target the standalone app, so they are rejected with a clear
message instead of silently going to the wrong channel.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from . import config, db, follows

CHANNEL = "serverchan"


def _clean(text: Any, limit: int = 280) -> str:
    value = str(text or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit].rstrip()


def _pick_items(payload: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    items = sorted(payload.get("items") or [], key=lambda x: x.get("rank") or 999)
    selected: List[Dict[str, Any]] = []
    owners = set()
    for item in items:
        owner = (item.get("owner") or item["full_name"].split("/")[0]).lower()
        if owner in owners:
            continue
        selected.append(item)
        owners.add(owner)
        if len(selected) >= limit:
            return selected
    for item in items:
        if item not in selected:
            selected.append(item)
        if len(selected) >= limit:
            break
    return selected[:limit]


def _date_label(date: str) -> str:
    try:
        _, month, day = date.split("-")
        return "%s·%s" % (month, day)
    except ValueError:
        return date


def _follow_section(conn: sqlite3.Connection) -> Tuple[str, int]:
    try:
        summary = follows.summary(conn)
    except Exception:  # noqa: BLE001
        return "", 0
    events = summary.get("events") or []
    if not events:
        return "", 0
    lines = ["**你关注的人**", ""]
    for event in events[:2]:
        repo = event.get("repo") or ""
        title = event.get("title") or ""
        if event.get("kind") == "release":
            label = "发布 %s" % title
        else:
            label = "新建仓库"
        body = _clean(event.get("body") or "", 90)
        line = "· @%s %s" % (event.get("owner"), label)
        if repo:
            line += " · `%s`" % repo
        line += "  [查看](%s)" % (event.get("url") or "https://github.com/%s" % repo)
        if body:
            line += "\n  %s" % body
        lines.append(line)
    lines.append("")
    return "\n".join(lines), len(events[:2])


def render(payload: Dict[str, Any], conn: sqlite3.Connection) -> Dict[str, str]:
    items = _pick_items(payload, config.PUSH_ITEMS)
    stats = payload.get("stats") or {}
    total = int(stats.get("total") or len(payload.get("items") or []))
    gained = int(stats.get("stars_today") or 0)
    daily = sum(1 for item in payload.get("items") or [] if "daily" in (item.get("sources") or []))
    date_label = _date_label(str(payload.get("date") or ""))

    title_prefix = "为你挑了 %d 个" % len(items) if (payload.get("profile") or {}).get("ready") else "%d 个项目值得看" % len(items)
    title = ("GitPulse %s｜%s" % (date_label, title_prefix)).replace("\n", " ")[:100]

    lines = [
        "**GitPulse 日报 · %s**" % date_label,
        "%d 个信号 · 今日 +%s★ · %d 个日榜项目" % (total, format(gained, ","), daily),
        "",
        "---",
        "",
    ]

    for index, item in enumerate(items, 1):
        brief = item.get("brief") or {}
        owner = item.get("owner") or item["full_name"].split("/")[0]
        name = item.get("name") or item["full_name"].split("/")[-1]
        what = _clean(brief.get("what") or item.get("summary_zh") or item.get("description") or "暂无简介", 180)
        problem = _clean(brief.get("problem") or "把一类重复的技术工作变成可复用的工具", 140)
        audience = _clean(brief.get("audience") or "开发者与技术团队", 120)
        scenarios = "、".join(_clean(x, 50) for x in (brief.get("scenarios") or [])[:2])
        flow = [str(x) for x in (brief.get("flow") or []) if x][:4]
        if not flow:
            flow = ["按 README 安装并完成最小配置", "运行官方示例确认环境", "接入自己的数据或流程"]

        lines.append("### %02d · %s/%s　🔥 %s" % (index, owner, name, item.get("heat", "—")))
        lines.append("")
        lines.append("**是什么**：%s" % what)
        lines.append("")
        lines.append("**解决什么问题**：%s" % problem)
        lines.append("")
        lines.append("**适合谁**：%s" % audience)
        lines.append("")
        lines.append("**完整使用流程**")
        for step_index, step in enumerate(flow, 1):
            lines.append("%d. %s" % (step_index, step))
        lines.append("")
        if scenarios:
            lines.append("**典型场景**：%s" % scenarios)
            lines.append("")
        lines.append("[在 GitHub 查看 →](%s)" % (item.get("url") or "https://github.com/%s" % item["full_name"]))
        lines.append("")
        lines.append("---")
        lines.append("")

    follow_text, _ = _follow_section(conn)
    if follow_text:
        lines.append(follow_text)
        lines.append("---")
        lines.append("")

    lines.append("GitPulse · 手机简报 · 每天 08:00")
    lines.append("流程为通用上手路径，具体命令与限制以项目 README 为准。")
    return {"title": title, "body": "\n".join(lines).strip()}


def _post(title: str, body: str) -> Tuple[bool, str]:
    key = config.SERVERCHAN_SENDKEY
    url = "%s/%s.send" % (config.SERVERCHAN_API_BASE, urllib.parse.quote(key, safe=""))
    data = json.dumps({"title": title, "desp": body}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json;charset=utf-8",
            "User-Agent": config.USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=config.PUSH_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return False, "network: %s" % exc
    code = payload.get("code")
    if str(code) == "0":
        pushid = (payload.get("data") or {}).get("pushid") if isinstance(payload.get("data"), dict) else ""
        return True, "pushid=%s" % (pushid or "ok")
    return False, "serverchan code=%s message=%s" % (code, payload.get("message") or payload.get("info") or "")


def _log(conn: sqlite3.Connection, date: str, status: str, title: str,
         content_hash: str, detail: str) -> None:
    now = db.now_iso()
    conn.execute(
        """INSERT INTO push_log(date, channel, status, title, content_hash, detail, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(date, channel) DO UPDATE SET
             status=excluded.status, title=excluded.title, content_hash=excluded.content_hash,
             detail=excluded.detail, updated_at=excluded.updated_at""",
        (date, CHANNEL, status, title, content_hash, detail[:500], now, now),
    )
    conn.commit()


def status(conn: sqlite3.Connection, date: Optional[str] = None) -> Dict[str, Any]:
    if date:
        row = conn.execute(
            "SELECT * FROM push_log WHERE date=? AND channel=?", (date, CHANNEL)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM push_log WHERE channel=? ORDER BY date DESC LIMIT 1", (CHANNEL,)
        ).fetchone()
    return {
        "configured": config.PUSH_ENABLED,
        "date": row["date"] if row else None,
        "status": row["status"] if row else None,
        "detail": row["detail"] if row else None,
        "updated_at": row["updated_at"] if row else None,
        "provider": "Server酱 Turbo",
    }


def send_daily(payload: Dict[str, Any], conn: Optional[sqlite3.Connection] = None,
               force: bool = False) -> Dict[str, Any]:
    if not config.SERVERCHAN_SENDKEY:
        return {"ok": False, "skipped": True, "reason": "未配置 SERVERCHAN_SENDKEY"}
    if config.SERVERCHAN_SENDKEY.lower().startswith("sctp"):
        return {"ok": False, "skipped": True,
                "reason": "这是 Server酱³ 的 key，微信通道请使用 SCT 开头的 Turbo SendKey"}

    own_conn = conn is None
    if conn is None:
        conn = db.connect()
    try:
        date = str(payload.get("date") or "")
        existing = conn.execute(
            "SELECT * FROM push_log WHERE date=? AND channel=?", (date, CHANNEL)
        ).fetchone()
        if existing and existing["status"] == "sent" and not force:
            return {"ok": True, "skipped": True, "reason": "今天已经发送过",
                    "date": date, "status": "sent"}

        rendered = render(payload, conn)
        content_hash = hashlib.sha1(rendered["body"].encode("utf-8")).hexdigest()
        ok, detail = _post(rendered["title"], rendered["body"])
        _log(conn, date, "sent" if ok else "failed", rendered["title"], content_hash, detail)
        return {
            "ok": ok, "skipped": False, "date": date,
            "status": "sent" if ok else "failed",
            "title": rendered["title"], "detail": detail,
        }
    finally:
        if own_conn:
            conn.close()
