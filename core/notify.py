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
    title = ("Filter Bubble %s｜%s" % (date_label, title_prefix)).replace("\n", " ")[:100]

    lines = [
        "**Filter Bubble 日报 · %s**" % date_label,
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
        detail = _clean(brief.get("detail"), 400)
        highlights = [_clean(x, 60) for x in (brief.get("highlights") or []) if _clean(x, 60)][:4]
        limits = _clean(brief.get("limits"), 140)
        is_ai = (brief.get("source") == "ai") or bool(brief.get("ai"))

        lines.append("### %02d · %s/%s　🔥 %s" % (index, owner, name, item.get("heat", "—")))
        lines.append("")
        lines.append("**是什么**：%s" % what)
        lines.append("")
        if detail:
            lines.append(detail)
            lines.append("")
        for point in highlights:
            lines.append("- %s" % point)
        if highlights:
            lines.append("")
        if limits:
            lines.append("**注意**：%s" % limits)
            lines.append("")
        # 标明这条解读是模型写的还是 GitHub 原简介
        lines.append("> %s" % ("AI 解读" if is_ai else "GitHub 简介（未使用大模型）"))
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

    lines.append("Filter Bubble · 手机简报 · 每天 08:00")
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


def _switch_enabled() -> bool:
    """全局设置里的推送开关（存在 app_settings.json → push.enabled）。"""
    try:
        from . import ai_settings

        return bool((ai_settings.load().get("push") or {}).get("enabled", True))
    except Exception as exc:  # noqa: BLE001 - 配置读坏了不该让推送崩掉
        print("[push] settings read failed:", exc)
        return False


def state() -> Dict[str, Any]:
    """推送能不能发：开关 + SendKey。

    ``ready`` 才是真正决定要不要发的字段；``reason`` 说明为什么发不了，
    系统状态页直接用这个字段显示。
    """
    enabled = _switch_enabled()
    key = config.SERVERCHAN_SENDKEY
    configured = bool(key)
    reason = ""
    if not enabled:
        reason = "推送开关已关闭（全局设置 → 推送）"
    elif not configured:
        reason = "未配置 SERVERCHAN_SENDKEY"
    elif key.lower().startswith("sctp"):
        reason = "当前 key 是 Server酱³（sctp），微信通道需要 SCT 开头的 Turbo SendKey"
    return {
        "enabled": enabled,
        "configured": configured,
        "ready": enabled and configured and not reason,
        "reason": reason,
        "provider": "Server酱 Turbo",
        "time": config.REFRESH_TIME,
        "items": config.PUSH_ITEMS,
    }


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
        **state(),
        "date": row["date"] if row else None,
        "status": row["status"] if row else None,
        "detail": row["detail"] if row else None,
        "updated_at": row["updated_at"] if row else None,
        "last_pushed_date": db.get_meta(conn, "last_pushed_date") or None,
    }


def send_daily(payload: Dict[str, Any], conn: Optional[sqlite3.Connection] = None,
               force: bool = False) -> Dict[str, Any]:
    push = state()
    # 开关是权威的：关掉了就连手动「立即推送」也不发，免得用户以为还在推
    if not push["ready"]:
        return {"ok": False, "skipped": True, "reason": push["reason"], "state": push}

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
