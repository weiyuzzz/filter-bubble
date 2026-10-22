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

# 媒体名与套话会被切成多个「稀有」二元组，导致误判为同一件事。
# 只要 token 是这些片段的子串，就不参与「稀有词共现」的判定。
STORY_STOP_FRAGMENTS = (
    "人民日报", "新华社", "央视", "中新网", "环球时报", "澎湃",
    "记者", "报道", "消息", "官方", "新闻", "视频", "直播", "评论",
    "网友", "回应", "表示", "发布", "通报", "宣布",
)


def _is_story_stop_token(token: str) -> bool:
    return any(token in fragment for fragment in STORY_STOP_FRAGMENTS)


TOPIC_KEYWORDS: Dict[str, List[str]] = {    "AI 与大模型": ["ai", "人工智能", "大模型", "llm", "gpt", "gemini", "claude", "deepseek",
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
    """Create/refresh events for ``date``. Returns scored event dicts (not yet persisted).

    跨轮匹配不再写死 0.55：阈值来自 ``cluster.match_threshold``（默认 0.35）。
    同一轮新建的事件也会立刻进入比对池，避免「同一批材料里的相近事件各建各的」。
    """
    from . import score

    cluster_cfg = cfg.get("cluster") or {}
    within = float(cluster_cfg.get("within_run_threshold") or 0.30)
    match_threshold = float(cluster_cfg.get("match_threshold") or 0.35)

    existing = {e["id"]: e for e in store.events_for_date(conn, date, limit=500)}
    # also consider yesterday so a continuing story keeps its id
    for prev in store.events_for_date(conn, _day_before(date), limit=500):
        existing.setdefault(prev["id"], prev)
    known_titles = {eid: tokens(e.get("title", "")) for eid, e in existing.items()}

    groups = cluster_items(items, threshold=within)
    events: List[Dict[str, Any]] = []
    touched_previous = False
    merged_same_run = 0

    for group in groups:
        titles = [g.get("title", "") for g in group]
        canonical = _canonical_title(titles)
        group_tokens = tokens(canonical)
        if not group_tokens:
            continue

        matched: Optional[Dict[str, Any]] = None
        best = 0.0
        for event_id, title_tokens in known_titles.items():
            sim = similarity(group_tokens, title_tokens)
            if sim > best and sim >= match_threshold:
                best, matched = sim, existing.get(event_id)
        if matched is None:
            event_id = _event_id(date, canonical)
        else:
            event_id = matched["id"]
            if matched.get("date") != date:
                touched_previous = True
            elif not matched.get("_persisted", True):
                merged_same_run += 1

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
            "_persisted": False,
        }
        events.append(event)

        # 让本轮新事件立刻可被后续分组匹配（这正是之前 75% 事件只有一条材料的原因之一）
        existing[event_id] = event
        known_titles[event_id] = group_tokens

    scored = score.score_events(conn, date, events, cfg, now)
    for event in scored:
        tech = score.detect_tech_burst(event, cfg)
        event["tech_flag"] = bool(tech) if cfg.get("investigation", {}).get("tech_auto_check", True) else False
        event["tech_reason"] = tech

    if touched_previous:
        print("[radar] some clusters matched events from a previous day")
    if merged_same_run:
        print("[radar] %d groups merged into events created in the same run" % merged_same_run)
    return scored


def build_stories(conn, date: str, events: List[Dict[str, Any]], cfg: Dict[str, Any],
                  now: str) -> List[Dict[str, Any]]:
    """事件之上再聚一层「故事」：同一件事的不同角度合成一条。

    分组信号有两个：
    * 标题相似度（包含系数），用于几乎相同的措辞；
    * **稀有词共现**：两个事件共享 ≥2 个当日少见的关键词（地名、机构名、专有名词），
      即视为同一件事的不同角度。这条规则能合并「西藏地震 / 那曲最大地震 / 亲历者讲述」，
      但不会因为共享「中国」「报道」这类烂大街的词而误合并。
    """
    cluster_cfg = cfg.get("cluster") or {}
    threshold = float(cluster_cfg.get("story_threshold") or 0.42)
    if not events:
        return []

    token_sets = [tokens(e.get("title", "")) for e in events]
    total = len(events)
    # 文档频率：一个词出现在多少个事件里
    df: Dict[str, int] = {}
    for token_set in token_sets:
        for token in token_set:
            df[token] = df.get(token, 0) + 1

    def is_rare(token: str) -> bool:
        if len(token) < 2:
            return False
        if _is_story_stop_token(token):
            return False
        return df.get(token, 0) <= max(2, int(total * 0.08))

    pairs: List[Tuple[int, int]] = []
    for i in range(total):
        if not token_sets[i]:
            continue
        for j in range(i + 1, total):
            other = token_sets[j]
            if not other:
                continue
            inter = token_sets[i] & other
            if not inter:
                continue
            containment = len(inter) / float(min(len(token_sets[i]), len(other)))
            shared_rare = sum(1 for token in inter if is_rare(token))
            if containment >= threshold or shared_rare >= 2:
                pairs.append((i, j))

    roots = _union_find(total, pairs)

    grouped: Dict[int, List[int]] = {}
    for index, root in enumerate(roots):
        grouped.setdefault(root, []).append(index)

    stories: List[Dict[str, Any]] = []
    for indices in grouped.values():
        members = [events[i] for i in indices]
        # 代表事件：覆盖材料最多的事件（标题最能代表这件事），热度作为并列条件
        representative = max(members, key=lambda e: (int(e.get("item_count") or 0), e.get("heat", 0)))
        title = representative.get("title", "")
        # 故事 id 由成员事件集合决定：标题相近的两个故事不会再撞 id
        member_ids = sorted(str(e["id"]) for e in members)
        story_id = "st-" + hashlib.sha1("|".join(member_ids).encode("utf-8")).hexdigest()[:14]
        heat = max(float(e.get("heat") or 0) for e in members)
        angle_bonus = min(6.0, 2.0 * (len(members) - 1))
        platforms = set()
        for member in members:
            platforms |= set((member.get("metrics") or {}).get("platforms") or [])
        story = {
            "id": story_id,
            "date": date,
            "title": title,
            "summary": "",
            "category": representative.get("category", ""),
            "heat": round(min(100.0, heat + angle_bonus), 1),
            "importance": round(max(float(e.get("importance") or 0) for e in members), 1),
            "confidence": representative.get("confidence", "unverified"),
            "event_count": len(members),
            "item_count": sum(int(e.get("item_count") or 0) for e in members),
            "platform_count": len(platforms),
            "first_seen_at": min((e.get("first_seen_at") or now) for e in members),
            "last_updated_at": now,
            "metrics": {
                "angles": [
                    {"event_id": e["id"], "title": e.get("title", ""),
                     "heat": e.get("heat"), "platform_count": e.get("platform_count")}
                    for e in sorted(members, key=lambda x: x.get("heat", 0), reverse=True)
                ],
                "event_ids": member_ids,
                "angle_count": len(members),
            },
        }
        for member in members:
            member["story_id"] = story_id
        stories.append(story)

    stories.sort(key=lambda s: (s["importance"], s["heat"]), reverse=True)
    return stories


def _day_before(date: str) -> str:
    import datetime as dt

    try:
        parsed = dt.date.fromisoformat(date)
    except ValueError:
        return date
    return (parsed - dt.timedelta(days=1)).isoformat()
