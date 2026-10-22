"""Event clustering: turn a pile of items into distinct events.

Design notes:
* exact duplicates collapse by ``content_hash``;
* near duplicates collapse by token-set similarity (CJK bigrams + latin words);
* an event that already exists today keeps its id, so history accumulates.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import store

CJK = re.compile(r"[\u4e00-\u9fff]")
LATIN = re.compile(r"[a-zA-Z][a-zA-Z0-9+#.\-]{2,}")

STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "are", "was", "were", "has", "have",
    "will", "its", "you", "your", "our", "not", "but", "they", "their", "how", "why", "what",
    "new", "news", "said", "says", "after", "over", "into", "about", "more", "than", "today",
    "中国", "美国", "报道", "记者", "新闻", "视频", "最新", "我们", "他们", "这个", "那个",
    "可以", "已经", "表示", "认为", "今天", "昨日", "刚刚", "网友", "官方", "发布",
}

TOPIC_KEYWORDS: Dict[str, List[str]] = {
    "AI 与大模型": ["ai", "人工智能", "大模型", "llm", "gpt", "gemini", "claude", "deepseek",
                    "machine learning", "机器学习", "算法", "agent", "智能体"],
    "芯片与半导体": ["芯片", "半导体", "光刻", "晶圆", "台积电", "nvidia", "英伟达", "gpu",
                     "chip", "semiconductor", "asic", "hbm", "eda"],
    "开源软件": ["开源", "open source", "github", "linux", "kubernetes", "框架", "sdk"],
    "国际科技公司": ["apple", "谷歌", "google", "微软", "microsoft", "meta", "openai",
                     "特斯拉", "tesla", "亚马逊", "amazon", "字节跳动", "腾讯", "阿里"],
    "游戏与消费电子": ["游戏", "game", "steam", "主机", "手机", "iphone", "android", "显卡",
                       "任天堂", "playstation", "xbox"],
    "国际新闻": ["选举", "战争", "停火", "制裁", "关税", "外交", "峰会", "协议", "election",
                 "sanction", "treaty", "conflict"],
    "中国科技发展与科研成果": ["国产", "自主", "突破", "首次实现", "科研", "实验室",
                               "高校", "量产", "国产化", "中科院", "国产芯片"],
    "产业竞争与经济事件": ["经济", "产业", "供应链", "产能", "出口", "市场", "价格", "财报",
                           "投资", "收购", "gdp", "inflation"],
}


def tokens(text: str) -> set:
    text = (text or "").lower()
    out = set()
    for word in LATIN.findall(text):
        word = word.lower()
        if word not in STOPWORDS and not word.isdigit():
            out.add(word)
    chars = "".join(CJK.findall(text))
    for i in range(len(chars) - 1):
        gram = chars[i:i + 2]
        if gram not in STOPWORDS:
            out.add(gram)
    return out


def similarity(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / float(len(a | b))


def _canonical_title(titles: Iterable[str]) -> str:
    titles = [t.strip() for t in titles if t and t.strip()]
    if not titles:
        return "未命名事件"
    # Prefer the most informative short headline: highest token coverage, bounded length.
    scored = sorted(titles, key=lambda t: (-len(tokens(t)), abs(len(t) - 28)))
    return scored[0][:120]


def _event_id(date: str, title: str) -> str:
    digest = hashlib.sha1(("%s|%s" % (date, tokens(title) and "|".join(sorted(tokens(title))[:12]) or title)).encode("utf-8")).hexdigest()
    return "ev-" + digest[:14]


def _union_find(n: int, pairs: List[Tuple[int, int]]) -> List[int]:
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    return [find(i) for i in range(n)]


def cluster_items(items: List[Dict[str, Any]], threshold: float = 0.30) -> List[List[Dict[str, Any]]]:
    """Group items into candidate events. Never drops an item."""
    if not items:
        return []

    # 1) exact content duplicates
    by_hash: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        by_hash.setdefault(item.get("content_hash") or item.get("url_hash"), []).append(item)

    uniques = [group[0] for group in by_hash.values()]
    token_sets = [tokens(item.get("title", "")) for item in uniques]

    # 2) near duplicates
    pairs: List[Tuple[int, int]] = []
    for i in range(len(uniques)):
        if not token_sets[i]:
            continue
        for j in range(i + 1, len(uniques)):
            if similarity(token_sets[i], token_sets[j]) >= threshold:
                pairs.append((i, j))

    roots = _union_find(len(uniques), pairs)
    groups: Dict[int, List[Dict[str, Any]]] = {}
    for idx, root in enumerate(roots):
        groups.setdefault(root, []).extend(by_hash[uniques[idx].get("content_hash") or uniques[idx]["url_hash"]])
    return list(groups.values())


def build_events(conn, date: str, items: List[Dict[str, Any]], cfg: Dict[str, Any],
                 now: str) -> List[Dict[str, Any]]:
    """Create/refresh events for ``date``. Returns scored event dicts (not yet persisted)."""
    from . import score

    min_heat = float(((cfg.get("investigation") or {}).get("min_heat")) or 0)
    existing = {e["id"]: e for e in store.events_for_date(conn, date, limit=500)}
    # also consider yesterday so a continuing story keeps its id
    for prev in store.events_for_date(conn, _day_before(date), limit=500):
        existing.setdefault(prev["id"], prev)

    groups = cluster_items(items)
    events: List[Dict[str, Any]] = []
    touched_previous = False

    for group in groups:
        titles = [g.get("title", "") for g in group]
        canonical = _canonical_title(titles)
        group_tokens = tokens(canonical)
        if not group_tokens:
            continue

        matched: Optional[Dict[str, Any]] = None
        best = 0.0
        for event in existing.values():
            sim = similarity(group_tokens, tokens(event.get("title", "")))
            if sim > best and sim >= 0.55:
                best, matched = sim, event
        event_id = matched["id"] if matched else _event_id(date, canonical)
        if matched and matched.get("date") != date:
            touched_previous = True

        item_ids = [g["id"] for g in group if g.get("id")]
        published = [g.get("published_at") for g in group if g.get("published_at")]
        earliest = min(published) if published else now
        origin = None
        for g in sorted(group, key=lambda x: (x.get("published_at") or now)):
            origin = g.get("id")
            break

        event = {
            "id": event_id,
            "date": date,
            "title": canonical,
            "summary": "",
            "category": "",
            "keywords": sorted({t for t in group_tokens if len(t) > 1})[:12],
            "heat": 0.0,
            "importance": 0.0,
            "confidence": "unverified",
            "item_count": len(group),
            "platform_count": len({g.get("platform") for g in group}),
            "source_count": len({g.get("source_id") for g in group}),
            "first_seen_at": earliest,
            "last_updated_at": now,
            "origin_item_id": origin,
            "tech_flag": False,
            "investigation_state": (matched or {}).get("investigation_state", "none"),
            "metrics": {},
            "_items": group,
            "_item_ids": item_ids,
        }
        events.append(event)

    # score afterwards: scoring needs the full candidate set for percentile ranking
    scored = score.score_events(conn, date, events, cfg, now)
    for event in scored:
        tech = score.detect_tech_burst(event, cfg)
        event["tech_flag"] = bool(tech) if cfg.get("investigation", {}).get("tech_auto_check", True) else False
        event["tech_reason"] = tech

    if touched_previous:
        print("[radar] some clusters matched events from a previous day")
    if min_heat > 0:
        pass
    return scored


def _day_before(date: str) -> str:
    import datetime as dt

    try:
        parsed = dt.date.fromisoformat(date)
    except ValueError:
        return date
    return (parsed - dt.timedelta(days=1)).isoformat()
