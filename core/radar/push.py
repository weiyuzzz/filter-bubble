"""Radar push: independent channel, independent log, duplicate protection, limited retries."""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from .. import config as app_config
from . import store

API_BASE = os.environ.get("RADAR_SERVERCHAN_API_BASE", "https://sctapi.ftqq.com").rstrip("/")


def resolve_sendkey(cfg: Dict[str, Any]) -> tuple:
    """Radar key first, then (optionally) the existing GitHub digest key."""
    push = cfg.get("push") or {}
    env_name = push.get("sendkey_env") or "RADAR_SERVERCHAN_SENDKEY"
    key = (os.environ.get(env_name) or "").strip()
    if key:
        return key, env_name
    if push.get("fallback_to_main_key", True):
        main = (app_config.SERVERCHAN_SENDKEY or "").strip()
        if main:
            return main, "SERVERCHAN_SENDKEY"
    return "", env_name


def _post(sendkey: str, title: str, body: str, timeout: float = 15.0) -> tuple:
    url = "%s/%s.send" % (API_BASE, urllib.parse.quote(sendkey))
    data = urllib.parse.urlencode({"title": title[:100], "desp": body[:30000]}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        payload = json.loads(raw) if raw.strip().startswith("{") else {}
        code = payload.get("code")
        if code in (0, "0"):
            return True, "pushid=%s" % (payload.get("data", {}).get("pushid", ""))
        return False, ("code=%s %s" % (code, payload.get("message") or payload.get("info") or ""))[:300]
    except Exception as exc:  # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)


def status(conn, cfg: Dict[str, Any], date: str) -> Dict[str, Any]:
    push = cfg.get("push") or {}
    key, source = resolve_sendkey(cfg)
    channel = push.get("channel") or "radar-wechat"
    row = store.push_state(conn, date, channel)
    return {
        "configured": bool(key),
        "key_source": source,
        "channel": channel,
        "enabled": bool(push.get("enabled")),
        "date": date,
        "status": (row or {}).get("status"),
        "detail": (row or {}).get("detail"),
        "attempts": (row or {}).get("attempts"),
        "updated_at": (row or {}).get("updated_at"),
    }


def send_daily(conn, payload: Dict[str, Any], cfg: Dict[str, Any], force: bool = False,
               manual: bool = False) -> Dict[str, Any]:
    from . import report as report_mod

    push_cfg = cfg.get("push") or {}
    if not push_cfg.get("enabled") and not manual:
        return {"ok": False, "skipped": True,
                "reason": "雷达推送未启用（social_radar.json → push.enabled）"}
    channel = push_cfg.get("channel") or "radar-wechat"
    date = str(payload.get("date") or "")
    key, key_source = resolve_sendkey(cfg)
    if not key:
        return {"ok": False, "skipped": True,
                "reason": "未配置 RADAR_SERVERCHAN_SENDKEY（也可回退使用 SERVERCHAN_SENDKEY）"}
    if key.lower().startswith("sctp"):
        return {"ok": False, "skipped": True,
                "reason": "这是 Server酱³ 的 key，微信通道请使用 SCT 开头的 Turbo SendKey"}

    existing = store.push_state(conn, date, channel)
    rendered = report_mod.render_push(payload, limit=int(push_cfg.get("items") or 5))
    content_hash = hashlib.sha1(rendered["body"].encode("utf-8")).hexdigest()
    if existing and existing["status"] == "sent" and not force:
        return {"ok": True, "skipped": True, "reason": "今天已经推送过",
                "date": date, "hash": existing["content_hash"]}

    attempts = int((existing or {}).get("attempts") or 0)
    max_attempts = int(push_cfg.get("max_attempts") or 3)
    now = store.db.now_iso()
    detail = ""
    while attempts < max_attempts:
        attempts += 1
        ok, detail = _post(key, rendered["title"], rendered["body"])
        store.record_push(conn, date, channel, "sent" if ok else "failed",
                          rendered["title"], content_hash, detail, attempts, now, store.db.now_iso())
        conn.commit()
        if ok:
            return {"ok": True, "skipped": False, "date": date, "attempts": attempts,
                    "title": rendered["title"], "detail": detail, "key_source": key_source}
        time.sleep(min(5.0, 1.5 * attempts))
    return {"ok": False, "skipped": False, "date": date, "attempts": attempts,
            "reason": "推送失败，已达到重试上限", "detail": detail}


def send_alert(conn, event: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Optional immediate alert for a single major event (max one per day)."""
    push_cfg = cfg.get("push") or {}
    if not push_cfg.get("enabled"):
        return {"ok": False, "skipped": True, "reason": "推送未启用"}
    key, _ = resolve_sendkey(cfg)
    if not key:
        return {"ok": False, "skipped": True, "reason": "未配置 SendKey"}
    date = str(event.get("date") or store.db.now_iso()[:10])
    channel = (push_cfg.get("channel") or "radar-wechat") + "-alert"
    existing = store.push_state(conn, date, channel)
    if existing and existing["status"] == "sent":
        return {"ok": True, "skipped": True, "reason": "今日已发送过重大提醒"}

    title = "重大热点｜%s" % (event.get("title", "")[:40])
    body = ("### %s\n\n热度 %s · 重要度 %s · 状态 %s\n\n%s\n\n[详情](http://127.0.0.1:%s/radar.html)\n"
            % (event.get("title", ""), event.get("heat"), event.get("importance"),
               event.get("confidence"), event.get("summary") or "",
               os.environ.get("GITPULSE_PORT", "8791")))
    ok, detail = _post(key, title, body)
    now = store.db.now_iso()
    store.record_push(conn, date, channel, "sent" if ok else "failed", title,
                      hashlib.sha1(body.encode("utf-8")).hexdigest(), detail, 1, now, now)
    conn.commit()
    return {"ok": ok, "detail": detail}
