"""Daily radar briefing. Deterministic, evidence-linked, and explicit about gaps."""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional

from . import store
from . import score

DISCLAIMER = ("热度分是本站对当前可采集材料的相对排序，不是平台官方指标；"
              "事实状态只描述「现有证据支持程度」，不构成司法或专业鉴定结论。")

CONFIDENCE_LABEL = {
    "verified_multi": "多个独立媒体来源报道",
    "reported": "有媒体报道，尚未多方独立验证",
    "unverified": "仅见于热榜/社交平台，缺少核实",
}


def _item_brief(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "title": item.get("title", ""),
        "url": item.get("url", ""),
        "platform": item.get("platform", ""),
        "source_type": item.get("source_type", ""),
        "published_at": item.get("published_at", ""),
        "language": item.get("language", ""),
        "author": item.get("author_name", ""),
    }


def _spread(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    zh = sum(1 for i in items if (i.get("language") or "").startswith("zh"))
    en = sum(1 for i in items if (i.get("language") or "").startswith("en"))
    platforms = sorted({i.get("platform", "") for i in items if i.get("platform")})
    return {"zh_items": zh, "intl_items": en, "platforms": platforms}


def _earliest(items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    dated = [i for i in items if i.get("published_at")]
    if not dated:
        return items[0] if items else None
    return min(dated, key=lambda i: i.get("published_at"))


def highlight(conn, event: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    items = store.items_for_event(conn, event["id"])
    spread = _spread(items)
    first = _earliest(items)
    latest = None
    dated = [i for i in items if i.get("published_at")]
    if dated:
        latest = max(dated, key=lambda i: i.get("published_at"))

    claims = store.claims_for_event(conn, event["id"])
    claim_summary = ""
    if claims:
        states: Dict[str, int] = {}
        for claim in claims:
            states[claim["status"]] = states.get(claim["status"], 0) + 1
        claim_summary = "；".join("%s %d 项" % (k, v) for k, v in sorted(states.items()))

    min_heat = float(((cfg.get("investigation") or {}).get("min_heat")) or 70)
    suggest = bool(event.get("heat", 0) >= min_heat and not event.get("tech_flag"))
    return {
        "event_id": event["id"],
        "title": event.get("title", ""),
        "summary": event.get("summary") or score.summary_line(event),
        "category": event.get("category", ""),
        "heat": event.get("heat"),
        "importance": event.get("importance"),
        "confidence": event.get("confidence"),
        "confidence_label": CONFIDENCE_LABEL.get(event.get("confidence", ""), ""),
        "lifecycle": (event.get("metrics") or {}).get("lifecycle"),
        "flags": (event.get("metrics") or {}).get("flags") or [],
        "tech_flag": bool(event.get("tech_flag")),
        "platforms": spread["platforms"],
        "spread": spread,
        "earliest_source": _item_brief(first) if first else None,
        "latest_update": _item_brief(latest) if latest else None,
        "integrity_note": ("已找到最早可查公开来源，但不等于事件真正起源"
                           if first else "尚未找到可追溯来源"),
        "claim_summary": claim_summary,
        "sources": [_item_brief(i) for i in items[:4]],
        "suggest_investigation": suggest,
        "investigation_state": event.get("investigation_state", "none"),
        "metrics": event.get("metrics") or {},
    }


def build(conn, date: str, cfg: Dict[str, Any], collect_summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    events = store.events_for_date(conn, date, limit=300)
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    limits = (cfg.get("report") or {}).get("section_limits") or {}
    top_n = int((cfg.get("report") or {}).get("top_events") or 12)

    ranked = sorted(events, key=lambda e: (e.get("importance", 0) * 0.5 + e.get("heat", 0) * 0.5), reverse=True)
    highlights = [highlight(conn, e, cfg) for e in ranked[:top_n]]

    # 6.2 中文网络热点：热榜/社交来源
    zh_events = [e for e in ranked if (e.get("metrics") or {}).get("platforms")
                 and any(p in ("微博", "百度", "哔哩哔哩", "今日头条", "网易", "知乎")
                         for p in (e.get("metrics") or {}).get("platforms", []))]
    zh_web = []
    for event in zh_events[: int(limits.get("zh_web") or 8)]:
        items = store.items_for_event(conn, event["id"])
        platforms = sorted({i.get("platform", "") for i in items if i.get("platform")})
        dup = len(items) > len({i.get("content_hash") for i in items})
        zh_web.append({
            "event_id": event["id"],
            "title": event.get("title"),
            "heat": event.get("heat"),
            "platforms": platforms,
            "item_count": event.get("item_count"),
            "duplication_note": ("存在重复转载/同源内容" if dup else "未检测到明显重复转载"),
            "confidence": event.get("confidence"),
            "note": "热榜位置与互动量不代表真实民意",
        })

    # 6.3 国际热点
    intl_events = [e for e in ranked if "zh" not in (e.get("metrics") or {}).get("platforms", [])
                   and any(p in ("BBC", "德国之声", "Hacker News", "TechCrunch", "GDELT", "Reddit", "Bluesky")
                           for p in (e.get("metrics") or {}).get("platforms", []))]
    intl = []
    for event in intl_events[: int(limits.get("intl") or 8)]:
        items = store.items_for_event(conn, event["id"])
        intl.append({
            "event_id": event["id"],
            "title": event.get("title"),
            "heat": event.get("heat"),
            "platforms": sorted({i.get("platform") for i in items if i.get("platform")}),
            "chinese_coverage": sum(1 for i in items if (i.get("language") or "").startswith("zh")),
            "confidence": event.get("confidence"),
            "note": "尚未进入中文信息环境的国际议题" if not any(
                (i.get("language") or "").startswith("zh") for i in items) else "",
        })

    # 6.4 科技与 AI 专题
    tech = []
    for event in [e for e in ranked if e.get("tech_flag")][: int(limits.get("tech") or 8)]:
        claims = store.claims_for_event(conn, event["id"])
        tech.append({
            "event_id": event["id"],
            "title": event.get("title"),
            "heat": event.get("heat"),
            "confidence": event.get("confidence"),
            "independently_verified": event.get("confidence") == "verified_multi",
            "tech_reason": (event.get("metrics") or {}).get("interest_hits") or [],
            "claims_count": len(claims),
            "note": ("未经独立验证，仅为发布方/媒体说法"
                     if event.get("confidence") != "verified_multi" else ""),
        })

    # 6.5 事件调查更新
    investigated = [e for e in ranked if e.get("investigation_state") not in (None, "", "none")]
    updates = []
    for event in investigated[: int(limits.get("investigations") or 5)]:
        claims = store.claims_for_event(conn, event["id"])
        updates.append({
            "event_id": event["id"],
            "title": event.get("title"),
            "state": event.get("investigation_state"),
            "claims": len(claims),
            "updated_at": event.get("last_updated_at"),
            "statuses": sorted({c["status"] for c in claims}),
        })

    # 6.6 数据源覆盖说明
    all_sources = store.sources(conn)
    coverage = {
        "ok": [{"platform": s["platform"], "name": s["name"], "items": s["last_item_count"]}
               for s in all_sources if s["status"] == "ok"],
        "failed": [{"platform": s["platform"], "name": s["name"],
                    "error": s["last_error"], "at": s["last_error_at"]}
                   for s in all_sources if s["status"] == "error"],
        "disabled": [{"platform": s["platform"], "name": s["name"], "notes": s["notes"]}
                     for s in all_sources if not s["enabled"]],
        "unavailable": [{"platform": s["platform"], "name": s["name"], "notes": s["notes"]}
                        for s in all_sources if s["kind"] == "unavailable"],
        "collect": collect_summary or {},
    }

    payload = {
        "date": date,
        "generated_at": now,
        "version": 1,
        "module": "radar",
        "stats": {
            "events": len(events),
            "items": sum(int(e.get("item_count") or 0) for e in events),
            "sources_ok": len(coverage["ok"]),
            "sources_failed": len(coverage["failed"]),
            "sources_unavailable": len(coverage["unavailable"]),
            "tech_events": len([e for e in events if e.get("tech_flag")]),
        },
        "highlights": highlights,
        "zh_web": zh_web,
        "intl": intl,
        "tech": tech,
        "investigation_updates": updates,
        "coverage": coverage,
        "evidence_gaps": _evidence_gaps(conn, ranked),
        "disclaimer": DISCLAIMER,
        "scoring_weights": (cfg.get("scoring") or {}),
    }
    return payload


def _evidence_gaps(conn, events: List[Dict[str, Any]]) -> List[str]:
    gaps: List[str] = []
    for event in events[:8]:
        items = store.items_for_event(conn, event["id"])
        if not any(i.get("source_type") in score.NEWS_TYPES for i in items):
            gaps.append("「%s」暂无媒体报道，仅有热榜/社交信号" % event.get("title", "")[:40])
        elif not any(i.get("published_at") for i in items):
            gaps.append("「%s」缺少可核对的发布时间" % event.get("title", "")[:40])
    return gaps[:6]


def render_push(payload: Dict[str, Any], limit: int = 5) -> Dict[str, str]:
    """Compact Markdown for WeChat, mirroring the GitHub brief's tone."""
    date_label = str(payload.get("date", ""))[5:].replace("-", "·")
    highlights = payload.get("highlights") or []
    top = sorted(highlights, key=lambda h: (h.get("importance", 0) * 0.5 + h.get("heat", 0) * 0.5),
                 reverse=True)[:limit]
    title = "全球热点雷达 %s｜%d 个事件" % (date_label, len(highlights))

    lines = ["**全球热点雷达 · %s**" % date_label,
             "%d 个事件 · %d 条材料 · %d 个源在线" % (
                 payload.get("stats", {}).get("events", 0),
                 payload.get("stats", {}).get("items", 0),
                 payload.get("stats", {}).get("sources_ok", 0)),
             "", "---", ""]
    for idx, item in enumerate(top, start=1):
        lines.append("### %02d · %s　🔥 %s" % (idx, item.get("title", "")[:60], item.get("heat")))
        lines.append("")
        lines.append("**是什么**：%s" % item.get("summary", ""))
        lines.append("")
        lines.append("**核验状态**：%s" % item.get("confidence_label", ""))
        lines.append("")
        earliest = item.get("earliest_source") or {}
        if earliest.get("url"):
            lines.append("**最早可查来源**：%s（%s）" % (
                earliest.get("platform", ""), earliest.get("url", "")))
            lines.append("")
        lines.append("[打开雷达](http://127.0.0.1:%s/radar.html)" % _port())
        lines.append("")
        lines.append("---")
        lines.append("")
    lines.append("> " + DISCLAIMER)
    return {"title": title[:64], "body": "\n".join(lines)}


def _port() -> str:
    import os

    return os.environ.get("GITPULSE_PORT", "8791")
