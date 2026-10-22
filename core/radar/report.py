"""Daily radar briefing. Deterministic, evidence-linked, and explicit about gaps.

报告以「故事」为主线：一个故事 = 同一天同一件事的多个角度（事件）。
摘要优先调用可选大模型，未配置时使用抽取式摘要（直接引用已采集标题）。
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional

from . import score, store, summarize

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


def _latest(items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    dated = [i for i in items if i.get("published_at")]
    if not dated:
        return None
    return max(dated, key=lambda i: i.get("published_at"))


def highlight_story(conn, story: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """把一个故事渲染成日报条目：代表事件 + 其他角度 + 摘要。"""
    members = store.events_for_story(conn, story["id"])
    if members:
        representative = members[0]
    else:
        representative = {
            "id": "", "title": story.get("title", ""), "heat": story.get("heat"),
            "importance": story.get("importance"), "confidence": story.get("confidence"),
            "category": story.get("category"), "tech_flag": False, "metrics": {},
            "item_count": story.get("item_count"), "investigation_state": "none",
        }

    items = store.items_for_event(conn, representative["id"]) if representative["id"] else []
    # 摘要用整个故事的材料，而不是只取代表事件：否则多角度故事会只剩一条标题
    if len(members) > 1:
        seen = {i.get("url_hash") for i in items}
        for member in members[1:]:
            for item in store.items_for_event(conn, member["id"]):
                if item.get("url_hash") in seen:
                    continue
                seen.add(item.get("url_hash"))
                items.append(item)
    items.sort(key=lambda i: (i.get("published_at") or "", -int(i.get("id") or 0)))
    summary = summarize.summarize_event(conn, representative, items, cfg)
    spread = _spread(items)
    first = _earliest(items)
    latest = _latest(items)

    claims = store.claims_for_event(conn, representative["id"]) if representative["id"] else []
    claim_summary = ""
    if claims:
        states: Dict[str, int] = {}
        for claim in claims:
            states[claim["status"]] = states.get(claim["status"], 0) + 1
        claim_summary = "；".join("%s %d 项" % (k, v) for k, v in sorted(states.items()))

    angles = (story.get("metrics") or {}).get("angles") or []
    min_heat = float(((cfg.get("investigation") or {}).get("min_heat")) or 70)
    trend = _rank_trend(conn, items)
    return {
        "story_id": story["id"],
        "event_id": representative.get("id", ""),
        "title": story.get("title", ""),
        "summary": summary.get("summary") or "",
        "headline": summary.get("headline") or story.get("title", ""),
        "points": summary.get("points") or [],
        "summary_provider": summary.get("provider", "extractive"),
        "summary_cached": bool(summary.get("cached")),
        "category": story.get("category", ""),
        "heat": story.get("heat"),
        "importance": story.get("importance"),
        "confidence": story.get("confidence"),
        "confidence_label": CONFIDENCE_LABEL.get(story.get("confidence", ""), ""),
        "angle_count": int(story.get("event_count") or len(angles) or 1),
        "angles": angles[:5],
        "item_count": int(story.get("item_count") or 0),
        "lifecycle": (representative.get("metrics") or {}).get("lifecycle"),
        "flags": (representative.get("metrics") or {}).get("flags") or [],
        "tech_flag": bool(representative.get("tech_flag")),
        "platforms": spread["platforms"],
        "spread": spread,
        "earliest_source": _item_brief(first) if first else None,
        "latest_update": _item_brief(latest) if latest else None,
        "integrity_note": ("已找到最早可查公开来源，但不等于事件真正起源"
                           if first else "尚未找到可追溯来源"),
        "claim_summary": claim_summary,
        "sources": [_item_brief(i) for i in items[:4]],
        "suggest_investigation": bool(story.get("heat", 0) >= min_heat and not representative.get("tech_flag")),
        "investigation_state": representative.get("investigation_state", "none"),
        "rank_trend": trend,
        "metrics": representative.get("metrics") or {},
    }


def _rank_trend(conn, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按材料最近两次位次判断升温/降温。没有位次历史时返回 unknown。"""
    hashes = [item.get("url_hash") for item in items if item.get("url_hash")]
    if not hashes:
        return {"state": "unknown", "delta": None, "rank": None}
    ranks = store.rank_map(conn, hashes)
    best = None
    for entry in ranks.values():
        if entry.get("rank") is None:
            continue
        if best is None or entry["rank"] < best["rank"]:
            best = entry
    if not best:
        return {"state": "unknown", "delta": None, "rank": None}

    delta = best.get("delta")
    if delta is None:
        state = "new"
    elif delta > 0:
        state = "up"
    elif delta < 0:
        state = "down"
    else:
        state = "flat"
    return {"state": state, "delta": delta, "rank": best.get("rank"),
            "captured_at": best.get("captured_at")}


def highlight(conn, event: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """兼容入口：把一个事件当成只含一个角度的故事来展示。"""
    story = {
        "id": event.get("story_id") or ("st-" + str(event.get("id", ""))),
        "title": event.get("title", ""),
        "category": event.get("category", ""),
        "heat": event.get("heat"),
        "importance": event.get("importance"),
        "confidence": event.get("confidence"),
        "event_count": 1,
        "item_count": event.get("item_count"),
    }
    items = store.items_for_event(conn, event["id"])
    summary = summarize.summarize_event(conn, event, items, cfg)
    spread = _spread(items)
    first = _earliest(items)
    return {
        "story_id": story["id"],
        "event_id": event["id"],
        "title": event.get("title", ""),
        "summary": summary.get("summary") or "",
        "headline": summary.get("headline") or event.get("title", ""),
        "points": summary.get("points") or [],
        "summary_provider": summary.get("provider", "extractive"),
        "category": event.get("category", ""),
        "heat": event.get("heat"),
        "importance": event.get("importance"),
        "confidence": event.get("confidence"),
        "confidence_label": CONFIDENCE_LABEL.get(event.get("confidence", ""), ""),
        "angle_count": 1,
        "angles": [],
        "item_count": int(event.get("item_count") or 0),
        "lifecycle": (event.get("metrics") or {}).get("lifecycle"),
        "flags": (event.get("metrics") or {}).get("flags") or [],
        "tech_flag": bool(event.get("tech_flag")),
        "platforms": spread["platforms"],
        "spread": spread,
        "earliest_source": _item_brief(first) if first else None,
        "latest_update": _item_brief(_latest(items)) if _latest(items) else None,
        "integrity_note": ("已找到最早可查公开来源，但不等于事件真正起源"
                           if first else "尚未找到可追溯来源"),
        "claim_summary": "",
        "sources": [_item_brief(i) for i in items[:4]],
        "suggest_investigation": bool(event.get("heat", 0) >= 70 and not event.get("tech_flag")),
        "investigation_state": event.get("investigation_state", "none"),
        "metrics": event.get("metrics") or {},
    }


def _stored_today(conn, date: str) -> int:
    try:
        row = conn.execute(
            "SELECT COUNT(*) c FROM radar_items WHERE substr(collected_at,1,10)=?", (date,)
        ).fetchone()
        return int(row["c"] if row else 0)
    except Exception:  # noqa: BLE001
        return 0


def build(conn, date: str, cfg: Dict[str, Any], collect_summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    events = store.events_for_date(conn, date, limit=400)
    stories = store.stories_for_date(conn, date, limit=200)
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    limits = (cfg.get("report") or {}).get("section_limits") or {}
    top_n = int((cfg.get("report") or {}).get("top_events") or 12)

    # 故事排序：重要性与热度各占一半，避免低热度的「重要」条目压过当日热点
    stories = sorted(stories, key=lambda s: (s.get("importance", 0) * 0.5 + s.get("heat", 0) * 0.5),
                     reverse=True)

    ranked = sorted(events, key=lambda e: (e.get("importance", 0) * 0.5 + e.get("heat", 0) * 0.5), reverse=True)

    if stories:
        highlights = [highlight_story(conn, s, cfg) for s in stories[:top_n]]
    else:  # 旧数据没有故事层时，退化为按事件展示
        highlights = [highlight(conn, e, cfg) for e in ranked[:top_n]]

    # 中文网络热点
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

    # 国际热点
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

    # 科技与 AI 专题
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

    # 事件调查更新
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

    # 数据源覆盖说明
    all_sources = store.sources(conn)
    collect = collect_summary or {}
    collected = int(collect.get("items_raw") or 0)
    stored = int(collect.get("items_kept") or 0) or _stored_today(conn, date)
    covered = sum(int(s.get("item_count") or 0) for s in stories)
    if not stories:
        covered = sum(int(e.get("item_count") or 0) for e in events)

    coverage = {
        "ok": [{"platform": s["platform"], "name": s["name"], "items": s["last_item_count"]}
               for s in all_sources if s["status"] == "ok"],
        "failed": [{"platform": s["platform"], "name": s["name"],
                    "error": s["last_error"], "at": s["last_error_at"]}
                   for s in all_sources if s["status"] == "error"],
        "paused": [{"platform": s["platform"], "name": s["name"],
                    "reason": s.get("paused_reason") or "连续失败已自动停用"}
                   for s in all_sources if s["status"] == "paused"],
        "skipped": collect.get("skipped") or [],
        "disabled": [{"platform": s["platform"], "name": s["name"], "notes": s["notes"]}
                     for s in all_sources if not s["enabled"]],
        "unavailable": [{"platform": s["platform"], "name": s["name"], "notes": s["notes"]}
                        for s in all_sources if s["kind"] == "unavailable"],
        "collect": collect,
    }

    # NEW 标签：只有用户打开过「深入调查」的故事才不再标记为新
    from .. import seen as seen_store
    new_count = seen_store.annotate("radar", highlights, "story_id")

    payload = {
        "date": date,
        "generated_at": now,
        "updated_at": now,
        "last_collect": collect.get("collected_at") or now,
        "new_count": new_count,
        "version": 2,
        "module": "radar",
        "stats": {
            "events": len(events),
            "stories": len(stories),
            # 口径说明：采集 = 拉取到的原始条数；入库 = 过滤后保留；覆盖 = 被故事引用
            "collected": collected,
            "filtered": int(collect.get("items_filtered") or 0),
            "stored": stored,
            "covered": covered,
            "items": stored,  # 兼容旧字段
            "sources_ok": len(coverage["ok"]),
            "sources_failed": len(coverage["failed"]),
            "sources_paused": len(coverage["paused"]),
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
        "summary_backend": summarize.backend_status(cfg),
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
    stats = payload.get("stats") or {}
    title = "全球热点雷达 %s｜%d 个事件" % (date_label, len(highlights))

    lines = ["**全球热点雷达 · %s**" % date_label,
             "%d 个故事 · 采集 %d 条 · 覆盖 %d 条 · %d 个源在线" % (
                 stats.get("stories", 0), stats.get("collected", 0),
                 stats.get("covered", 0), stats.get("sources_ok", 0)),
             "", "---", ""]
    for idx, item in enumerate(top, start=1):
        lines.append("### %02d · %s　🔥 %s" % (idx, item.get("headline") or item.get("title", ""), item.get("heat")))
        lines.append("")
        if item.get("summary"):
            lines.append(item["summary"])
            lines.append("")
        angles = item.get("angles") or []
        if len(angles) > 1:
            lines.append("**其他角度**：" + "；".join(
                (a.get("title") or "")[:36] for a in angles[1:4]))
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
