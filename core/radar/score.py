"""Explainable heat / importance scoring.

Weights come from config (defaults 30/20/20/20/10). Every component is stored in the
event's ``metrics`` so the UI can explain a score instead of showing a magic number.
"""
from __future__ import annotations

import datetime as dt
import math
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from . import store
from .cluster import TOPIC_KEYWORDS, tokens

NEWS_TYPES = {"news", "paper", "official", "press_release"}
OFFICIAL_HINTS = ("gov.cn", "gov.", ".gov", "cas.cn", "edu.cn", "ac.cn", "nature.com",
                  "science.org", "arxiv.org", "ieee.org")


def _domain(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url or "").netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _is_official(url: str, platform: str) -> bool:
    host = _domain(url)
    if any(hint in host for hint in OFFICIAL_HINTS):
        return True
    return platform in ("中国政府网", "中国科学院")


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def engagement_score(items: List[Dict[str, Any]]) -> float:
    """Normalise per platform, never compare raw like-counts across platforms."""
    by_platform: Dict[str, List[float]] = {}
    for item in items:
        engagement = item.get("engagement") or {}
        if not isinstance(engagement, dict):
            continue
        value = None
        rank = engagement.get("rank")
        if isinstance(rank, (int, float)) and rank > 0:
            # rank 1 is the top of that platform's list
            value = 1.0 - min(1.0, (float(rank) - 1) / 30.0)
        for key in ("likes", "like", "view", "points", "ups", "num", "raw_hot", "hot_value", "hotScore"):
            candidate = engagement.get(key)
            if isinstance(candidate, (int, float)) and candidate > 0:
                scaled = math.log10(1 + float(candidate)) / 6.0  # ~1M -> 1.0
                value = max(value or 0.0, _clamp(scaled))
        if value is not None:
            by_platform.setdefault(item.get("platform", "?"), []).append(value)
    if not by_platform:
        return 0.0
    platform_means = [sum(v) / len(v) for v in by_platform.values()]
    return _clamp(sum(platform_means) / len(platform_means))


def interest_score(event: Dict[str, Any], cfg: Dict[str, Any]) -> Tuple[float, str, List[str]]:
    interests = cfg.get("interests") or {}
    text = (event.get("title", "") + " " + " ".join(event.get("keywords") or [])).lower()
    hits: List[str] = []
    best_topic = ""
    best_count = 0
    for topic in interests.get("topics") or []:
        words = list(TOPIC_KEYWORDS.get(topic, []))
        count = sum(1 for w in words if w.lower() in text)
        if count > best_count:
            best_count, best_topic = count, topic
        if count:
            hits.append("%s(%d)" % (topic, count))

    # A single generic keyword is not enough to claim an event belongs to a topic.
    if best_count < 2:
        best_topic = ""

    for word in interests.get("keywords") or []:
        if word and word.lower() in text:
            hits.append(word)
    synonyms = interests.get("synonyms") or {}
    for word, alts in synonyms.items():
        for alt in (alts or []):
            if alt and alt.lower() in text:
                hits.append("%s~%s" % (word, alt))

    excluded = [w for w in (interests.get("exclude") or []) if w and w.lower() in text]
    raw = _clamp(len(hits) / 3.0)
    if excluded:
        raw *= 0.4
    return raw, best_topic, hits[:6]


def _recency_score(published: str, now_iso: str) -> float:
    try:
        then = dt.datetime.fromisoformat(published)
        now = dt.datetime.fromisoformat(now_iso)
        hours = (now - then).total_seconds() / 3600.0
    except (TypeError, ValueError):
        return 0.4
    if hours <= 6:
        return 1.0
    if hours <= 24:
        return 0.8
    if hours <= 48:
        return 0.5
    if hours <= 24 * 7:
        return 0.25
    return 0.05


def _lifecycle(event: Dict[str, Any], items: List[Dict[str, Any]], prev: Optional[Dict[str, Any]],
               now_iso: str) -> str:
    """new / continuing / recirculated — old material resurfacing is called out."""
    ages = [_recency_score(i.get("published_at") or i.get("collected_at") or now_iso, now_iso) for i in items]
    newest = max(ages) if ages else 0.0
    oldest = min(ages) if ages else 0.0
    if prev and int(prev.get("item_count") or 0) > 0:
        return "continuing"
    if newest <= 0.25 and oldest <= 0.25:
        return "recirculated"
    return "new"


def score_events(conn, date: str, events: List[Dict[str, Any]], cfg: Dict[str, Any],
                 now_iso: str) -> List[Dict[str, Any]]:
    weights = cfg.get("scoring") or {}
    w_velocity = float(weights.get("velocity", 0.30))
    w_cross = float(weights.get("cross_platform", 0.20))
    w_engage = float(weights.get("engagement", 0.20))
    w_verify = float(weights.get("verification", 0.20))
    w_interest = float(weights.get("interest", 0.10))
    total_w = (w_velocity + w_cross + w_engage + w_verify + w_interest) or 1.0

    for event in events:
        items = event.get("_items") or []
        prev = store.previous_snapshot(conn, event["id"], date)

        platforms = {i.get("platform") for i in items if i.get("platform")}
        sources = {i.get("source_id") for i in items if i.get("source_id")}
        domains = {_domain(i.get("url", "")) for i in items if i.get("url")}
        domains.discard("")

        news_domains = {_domain(i.get("url", "")) for i in items
                        if i.get("source_type") in NEWS_TYPES and _domain(i.get("url", ""))}
        official = any(_is_official(i.get("url", ""), i.get("platform", "")) for i in items)

        # --- velocity: growth versus yesterday, or "everything is new" on first sight
        item_count = len(items)
        if prev and int(prev.get("item_count") or 0) > 0:
            growth = (item_count - int(prev["item_count"])) / float(prev["item_count"])
            velocity = _clamp(0.5 + growth)
        else:
            velocity = _clamp(item_count / 6.0)
        cross_platform = _clamp(len(platforms) / 4.0)
        engagement = engagement_score(items)
        verification = _clamp(len(news_domains) / 3.0)
        if official:
            verification = max(verification, 0.6)
        interest, topic, hits = interest_score(event, cfg)

        heat = 100.0 * (w_velocity * velocity + w_cross * cross_platform + w_engage * engagement
                        + w_verify * verification + w_interest * interest) / total_w

        # --- concentration / suspected manipulation penalty
        penalty = 1.0
        flags: List[str] = []
        if item_count >= 3:
            top_source = max((sum(1 for i in items if i.get("source_id") == sid) for sid in sources), default=0)
            if sources and top_source / float(item_count) >= 0.7:
                penalty *= 0.85
                flags.append("来源集中")
            if len(platforms) == 1 and len(items) >= 5:
                penalty *= 0.9
                flags.append("单一平台")
        heat = _clamp(heat * penalty / 100.0) * 100.0

        tech = detect_tech_burst(event, cfg)
        importance = 100.0 * (0.45 * verification + 0.25 * cross_platform
                              + 0.15 * (1.0 if tech else 0.0)
                              + 0.15 * _recency_score(event.get("first_seen_at") or now_iso, now_iso))

        lifecycle = _lifecycle(event, items, prev, now_iso)
        if lifecycle == "recirculated":
            flags.append("旧闻重传")
            heat *= 0.8

        if verification >= 0.66 and len(news_domains) >= 3:
            confidence = "verified_multi"
        elif news_domains:
            confidence = "reported"
        else:
            confidence = "unverified"

        event["heat"] = round(_clamp(heat / 100.0) * 100.0, 1)
        event["importance"] = round(_clamp(importance / 100.0) * 100.0, 1)
        event["confidence"] = confidence
        event["category"] = topic or "综合"
        event["metrics"] = {
            "velocity": round(velocity, 3),
            "cross_platform": round(cross_platform, 3),
            "engagement": round(engagement, 3),
            "verification": round(verification, 3),
            "interest": round(interest, 3),
            "weights": {"velocity": w_velocity, "cross_platform": w_cross,
                        "engagement": w_engage, "verification": w_verify, "interest": w_interest},
            "platforms": sorted(platforms),
            "sources": len(sources),
            "independent_domains": len(domains),
            "news_domains": len(news_domains),
            "official_source": official,
            "lifecycle": lifecycle,
            "flags": flags,
            "interest_hits": hits,
            "previous_item_count": (prev or {}).get("item_count"),
        }

    # Relative 0-100 heat, like the GitHub module: rescale against the day's strongest
    # event so the numbers are readable, and keep the raw value for explainability.
    raw_max = max((float(e.get("heat") or 0) for e in events), default=0.0)
    for event in events:
        raw = float(event.get("heat") or 0)
        metrics = event.setdefault("metrics", {})
        metrics["raw_heat"] = round(raw, 1)
        if raw_max > 0:
            event["heat"] = round(92.0 * ((raw / raw_max) ** 0.85), 1)
    return events


def detect_tech_burst(event: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    keywords = ((cfg.get("investigation") or {}).get("tech_keywords")) or []
    text = (event.get("title", "") + " " + " ".join(event.get("keywords") or [])).lower()
    for word in keywords:
        if word.lower() in text:
            return word
    return ""


def summary_line(event: Dict[str, Any]) -> str:
    """Short deterministic summary used in the report when no model is configured."""
    metrics = event.get("metrics") or {}
    bits = []
    if metrics.get("platforms"):
        bits.append("%d 个平台" % len(metrics["platforms"]))
    bits.append("%d 条相关报道" % int(event.get("item_count") or 0))
    lifecycle = metrics.get("lifecycle")
    if lifecycle == "continuing":
        bits.append("持续发酵")
    elif lifecycle == "recirculated":
        bits.append("旧内容重新传播")
    else:
        bits.append("今日新出现")
    if metrics.get("flags"):
        bits.append("注意：" + "、".join(metrics["flags"]))
    return "，".join(bits)
