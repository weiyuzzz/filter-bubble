"""Source registry + collection runner.

Each source is an independent adapter. One adapter raising never stops the others:
failures are recorded per source and the run continues (requirement §11.2).
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import hashlib
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Tuple

from ... import keywords
from .. import store, urls
from . import anysearch, hotlists, intl, newsnow, rss

NON_FREE_NOTE = "无合规免费入口，未接入（不绕过登录/验证码/访问控制）"

# ``adapter`` values are resolved through ADAPTERS.
SOURCES: List[Dict[str, Any]] = [
    # ---- 中文新闻 RSS（实测可用） ----
    {"source_id": "cn_scroll", "platform": "中国新闻网", "name": "中国新闻网·滚动", "kind": "rss",
     "adapter": "rss", "url": "https://www.chinanews.com.cn/rss/scroll-news.xml", "language": "zh",
     "source_type": "news", "enabled": True},
    {"source_id": "cn_china", "platform": "中国新闻网", "name": "中国新闻网·国内", "kind": "rss",
     "adapter": "rss", "url": "https://www.chinanews.com.cn/rss/china.xml", "language": "zh",
     "source_type": "news", "enabled": True},
    {"source_id": "cn_world", "platform": "中国新闻网", "name": "中国新闻网·国际", "kind": "rss",
     "adapter": "rss", "url": "https://www.chinanews.com.cn/rss/world.xml", "language": "zh",
     "source_type": "news", "enabled": True},
    {"source_id": "sina_news", "platform": "新浪新闻", "name": "新浪新闻·国内", "kind": "rss",
     "adapter": "rss", "url": "https://rss.sina.com.cn/news/china/focus15.xml", "language": "zh",
     "source_type": "news", "enabled": True},
    {"source_id": "ithome", "platform": "IT之家", "name": "IT之家", "kind": "rss",
     "adapter": "rss", "url": "https://www.ithome.com/rss/", "language": "zh",
     "source_type": "news", "enabled": True},
    {"source_id": "qbitai", "platform": "量子位", "name": "量子位", "kind": "rss",
     "adapter": "rss", "url": "https://www.qbitai.com/feed", "language": "zh",
     "source_type": "news", "enabled": True},

    # ---- 中文热榜（实测可用） ----
    {"source_id": "weibo_hot", "platform": "微博", "name": "微博热搜", "kind": "hotlist",
     "adapter": "weibo_hot", "url": "https://weibo.com/ajax/side/hotSearch", "language": "zh",
     "source_type": "social_trend", "enabled": True, "max_items": 40},
    {"source_id": "baidu_hot", "platform": "百度", "name": "百度热搜", "kind": "hotlist",
     "adapter": "baidu_hot", "url": "https://top.baidu.com/board?tab=realtime", "language": "zh",
     "source_type": "social_trend", "enabled": True, "max_items": 30},
    {"source_id": "bilibili_popular", "platform": "哔哩哔哩", "name": "B站热门", "kind": "hotlist",
     "adapter": "bilibili_popular", "url": "https://api.bilibili.com/x/web-interface/popular?ps=30&pn=1",
     "language": "zh", "source_type": "social_trend", "enabled": True, "max_items": 30},
    {"source_id": "toutiao_hot", "platform": "今日头条", "name": "头条热榜", "kind": "hotlist",
     "adapter": "toutiao_hot", "url": "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
     "language": "zh", "source_type": "social_trend", "enabled": True, "max_items": 30},
    {"source_id": "netease_hot", "platform": "网易", "name": "网易热榜", "kind": "hotlist",
     "adapter": "netease_hot", "url": "https://m.163.com/fe/api/hot/news/flow", "language": "zh",
     "source_type": "social_trend", "enabled": True, "max_items": 30},
    {"source_id": "v2ex_hot", "platform": "V2EX", "name": "V2EX 热门", "kind": "hotlist",
     "adapter": "v2ex_hot", "url": "https://www.v2ex.com/api/topics/hot.json", "language": "zh",
     "source_type": "community", "enabled": True, "max_items": 25},

    # ---- 国际（实测可用 / 部分受限） ----
    {"source_id": "dw_zh", "platform": "德国之声", "name": "德国之声·中文", "kind": "rss",
     "adapter": "rss", "url": "https://rss.dw.com/rdf/rss-chi-all", "language": "zh",
     "source_type": "news", "enabled": True},
    {"source_id": "bbc_zh", "platform": "BBC", "name": "BBC 中文", "kind": "rss",
     "adapter": "rss", "url": "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml", "language": "zh",
     "source_type": "news", "enabled": True,
     "notes": "本机直连会被重置，需要本地代理（HTTPS_PROXY）才能采集"},
    {"source_id": "hackernews", "platform": "Hacker News", "name": "Hacker News 首页", "kind": "api",
     "adapter": "hackernews_front",
     "url": "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30",
     "language": "en", "source_type": "community", "enabled": True, "max_items": 25},
    {"source_id": "techcrunch", "platform": "TechCrunch", "name": "TechCrunch", "kind": "rss",
     "adapter": "rss", "url": "https://techcrunch.com/feed/", "language": "en",
     "source_type": "news", "enabled": True},
    {"source_id": "gdelt", "platform": "GDELT", "name": "GDELT 全球新闻", "kind": "api",
     "adapter": "gdelt_doc",
     "url": "https://api.gdeltproject.org/api/v2/doc/doc",
     "query": "(artificial intelligence OR semiconductor OR china tech)",
     "timespan": "1d", "language": "en", "source_type": "news", "enabled": True, "max_items": 25,
     "notes": "GDELT 仅用于发现报道，不作为事实真伪判据；公共额度紧张时会返回 429"},

    # ---- 已知受限，默认关闭，可在配置里开启尝试 ----
    {"source_id": "arxiv", "platform": "arXiv", "name": "arXiv cs.AI", "kind": "rss",
     "adapter": "arxiv_search",
     "url": "http://export.arxiv.org/api/query?search_query=cat:cs.AI&sortBy=submittedDate&sortOrder=descending&max_results=20",
     "language": "en", "source_type": "paper", "enabled": False,
     "notes": "本机 TLS 证书被中间人拦截（CERTIFICATE_VERIFY_FAILED），默认关闭"},
    {"source_id": "zhihu_hot", "platform": "知乎", "name": "知乎热榜", "kind": "hotlist",
     "adapter": "rss", "url": "https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total?limit=50",
     "language": "zh", "source_type": "social_trend", "enabled": False,
     "notes": "匿名访问返回 401，需要登录态，未接入"},
    {"source_id": "reddit_tech", "platform": "Reddit", "name": "Reddit r/technology", "kind": "api",
     "adapter": "reddit_hot", "url": "https://www.reddit.com/r/technology/hot.json?limit=25",
     "language": "en", "source_type": "community", "enabled": False,
     "notes": "本机返回 403 Blocked，默认关闭"},
    {"source_id": "bluesky", "platform": "Bluesky", "name": "Bluesky 搜索", "kind": "api",
     "adapter": "bluesky_search",
     "url": "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts?q=technology&limit=20",
     "language": "en", "source_type": "social_post", "enabled": False,
     "notes": "本机 403/超时，默认关闭"},

    # ---- 通过第三方聚合 API 接入（无可直连公开接口的平台） ----
    {"source_id": "douyin", "platform": "抖音", "name": "抖音热点", "kind": "hotlist",
     "adapter": "newsnow", "api_id": "douyin", "expected_domain": "douyin.com",
     "url": "", "language": "zh", "source_type": "social_trend", "enabled": True,
     "max_items": 30,
     "notes": "直连需要登录态与签名，改由 newsnow 聚合接入；只作为热点发现，无正文"},
    {"source_id": "tieba", "platform": "百度贴吧", "name": "贴吧热点", "kind": "hotlist",
     "adapter": "newsnow", "api_id": "tieba", "expected_domain": "baidu.com",
     "url": "", "language": "zh", "source_type": "community", "enabled": True,
     "max_items": 30,
     "notes": "直连需要登录态，改由 newsnow 聚合接入"},
    {"source_id": "zhihu_hot", "platform": "知乎", "name": "知乎热榜", "kind": "hotlist",
     "adapter": "newsnow", "api_id": "zhihu", "expected_domain": "zhihu.com",
     "url": "", "language": "zh", "source_type": "social_trend", "enabled": True,
     "max_items": 30,
     "notes": "匿名直连返回 401，改由 newsnow 聚合接入"},

    # ---- 财经/时政（新增，聚合接入） ----
    {"source_id": "wallstreetcn", "platform": "华尔街见闻", "name": "华尔街见闻热榜", "kind": "api",
     "adapter": "newsnow", "api_id": "wallstreetcn-hot", "expected_domain": "wallstreetcn.com",
     "url": "", "language": "zh", "source_type": "news", "enabled": True, "max_items": 20},
    {"source_id": "cls_news", "platform": "财联社", "name": "财联社热门", "kind": "api",
     "adapter": "newsnow", "api_id": "cls-hot", "expected_domain": "cls.cn",
     "url": "", "language": "zh", "source_type": "news", "enabled": True, "max_items": 20},
    {"source_id": "thepaper", "platform": "澎湃新闻", "name": "澎湃新闻", "kind": "api",
     "adapter": "newsnow", "api_id": "thepaper", "expected_domain": "thepaper.cn",
     "url": "", "language": "zh", "source_type": "news", "enabled": True, "max_items": 25},
    {"source_id": "ifeng", "platform": "凤凰网", "name": "凤凰网", "kind": "api",
     "adapter": "newsnow", "api_id": "ifeng", "expected_domain": "ifeng.com",
     "url": "", "language": "zh", "source_type": "news", "enabled": True, "max_items": 20},

    # ---- 无合规免费入口：显式标记，绝不抓取 ----
    {"source_id": "x_twitter", "platform": "X", "name": "X / Twitter", "kind": "unavailable",
     "adapter": "unavailable", "url": "", "language": "en", "source_type": "social_post",
     "enabled": False, "notes": NON_FREE_NOTE},
    {"source_id": "xiaohongshu", "platform": "小红书", "name": "小红书公开内容", "kind": "unavailable",
     "adapter": "unavailable", "url": "", "language": "zh", "source_type": "social_post",
     "enabled": False, "notes": NON_FREE_NOTE},
    {"source_id": "instagram", "platform": "Instagram", "name": "Instagram", "kind": "unavailable",
     "adapter": "unavailable", "url": "", "language": "en", "source_type": "social_post",
     "enabled": False, "notes": NON_FREE_NOTE},
    {"source_id": "threads", "platform": "Threads", "name": "Threads", "kind": "unavailable",
     "adapter": "unavailable", "url": "", "language": "en", "source_type": "social_post",
     "enabled": False, "notes": NON_FREE_NOTE},
    {"source_id": "tiktok", "platform": "TikTok", "name": "TikTok", "kind": "unavailable",
     "adapter": "unavailable", "url": "", "language": "en", "source_type": "social_post",
     "enabled": False, "notes": NON_FREE_NOTE},
]

ADAPTERS: Dict[str, Callable[..., List[Dict[str, Any]]]] = {
    "rss": rss.fetch,
    "newsnow": newsnow.fetch,
    "anysearch": anysearch.fetch,
    **hotlists.ADAPTERS,
    **intl.ADAPTERS,
}


def unavailable(spec: Dict[str, Any], make_item) -> List[Dict[str, Any]]:
    raise RuntimeError("未接入：%s" % (spec.get("notes") or "无合规免费访问方式"))


ADAPTERS["unavailable"] = unavailable


def _hash(value: str) -> str:
    return hashlib.sha1((value or "").encode("utf-8", "ignore")).hexdigest()


def make_item(spec: Dict[str, Any], title: str, url: str = "", published_at: str = "",
              author_name: str = "", raw_content: str = "", engagement: Dict[str, Any] = None,
              source_type: str = "", language: str = "") -> Dict[str, Any]:
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    title = (title or "").strip()
    # Some aggregator APIs omit a URL; keep a stable pseudo-URL so dedupe still works.
    url = (url or "").strip() or ("radar://%s/%s" % (spec["source_id"], _hash(title)[:16]))
    content = rss.content_hash(title)
    return {
        "source_id": spec["source_id"],
        "platform": spec.get("platform", ""),
        "title": title[:300],
        "url": url[:1000],
        # 用标准化后的 URL 做去重键：微博等平台的 band_rank/utm 参数每轮都变
        "url_hash": _hash(urls.normalize(url, spec["source_id"])),
        "author_name": (author_name or "")[:120],
        "published_at": published_at or "",
        "collected_at": now,
        "language": language or spec.get("language", ""),
        "engagement": engagement or {},
        "content_hash": content,
        "raw_content": (raw_content or "")[:4000],
        "source_type": source_type or spec.get("source_type", "news"),
        "fetch_status": "ok",
        "last_seen_at": now,
    }


_OVERRIDABLE = ("url", "enabled", "max_items", "query", "timespan", "headers", "notes",
                "language", "api_id", "expected_domain", "verify_domain", "timeout",
                "tag", "zone", "max_results")


def _pick(*values, default=None):
    """取第一个"显式给过"的值。

    不能用 ``a or b or DEFAULT``：``0`` 是合法值（0 = 不限速），用 or 会被当成没给，
    于是没法把某条查询的间隔调成 0。
    """
    for value in values:
        if value is not None:
            return value
    return default


def _anysearch_specs(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把配置里的每条查询展开成一个发现源（加查询不用改代码）。

    每条查询都是一次独立调用，而 AnySearch 的限流窗口只有 20 次，所以 spec 里带
    ``min_interval_minutes``：距上次成功采集不足这个间隔就整条跳过，避免每小时采集
    把额度烧光（节流逻辑在 ``collect`` 里统一处理）。
    """
    section = cfg.get("anysearch") or {}
    if not section.get("enabled"):
        return []
    entries = section.get("queries") or []
    specs: List[Dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, str):
            entry = {"query": entry}
        if not isinstance(entry, dict):
            continue
        query = str(entry.get("query") or "").strip()
        if not query:
            continue
        name = str(entry.get("name") or query).strip()
        query_id = str(entry.get("id") or name).strip()
        max_results = int(_pick(entry.get("max_results"), section.get("max_results"), default=10))
        specs.append({
            "source_id": "anysearch_" + query_id,
            "platform": anysearch.PLATFORM,
            "name": "AnySearch·" + name,
            "kind": "api",
            "adapter": "anysearch",
            "url": "",
            "query": query,
            "tag": str(entry.get("tag") or section.get("tag") or ""),
            "zone": str(entry.get("zone") or section.get("zone") or ""),
            "language": str(entry.get("language") or section.get("language") or "zh"),
            # 垂直域的必填/可选参数（如 finance.quote 需要 type）
            "params": dict(entry.get("params")) if isinstance(entry.get("params"), dict) else {},
            "source_type": anysearch.SOURCE_TYPE,
            "enabled": bool(entry.get("enabled", True)),
            "max_items": max_results,
            "max_results": max_results,
            # 0 = 每条查询都跑（不节流）；不写则跟随 anysearch.min_interval_minutes
            "min_interval_minutes": float(_pick(entry.get("min_interval_minutes"),
                                                section.get("min_interval_minutes"), default=240)),
            # 适配器只拿到 spec，把全局段带进去（key 名、base、超时都在里面）
            "_anysearch_cfg": dict(section),
            "notes": "统一搜索引擎源（配置在 social_radar.json → anysearch.queries）",
        })
    return specs


def _rss_specs(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把配置里的自定义 RSS 源展开成 source spec（加源不用改代码）。"""
    feeds = (cfg.get("rss") or {}).get("feeds") or []
    specs: List[Dict[str, Any]] = []
    for feed in feeds:
        if not isinstance(feed, dict):
            continue
        url = str(feed.get("url") or "").strip()
        name = str(feed.get("name") or feed.get("id") or "").strip()
        if not url or not name:
            continue
        feed_id = str(feed.get("id") or name).strip()
        specs.append({
            "source_id": "rss_" + feed_id,
            "platform": str(feed.get("platform") or name),
            "name": name,
            "kind": "rss",
            "adapter": "rss",
            "url": url,
            "language": str(feed.get("language") or "zh"),
            "source_type": str(feed.get("source_type") or "news"),
            "enabled": bool(feed.get("enabled", True)),
            "max_items": int(feed.get("max_items") or 30),
            "notes": "自定义 RSS 源（配置在 social_radar.json → rss.feeds）",
        })
    return specs


def _merged(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """静态清单 + 自定义 RSS + AnySearch 查询 + 用户覆盖 + newsnow 全局设置。"""
    overrides = (cfg.get("sources") or {})
    newsnow_cfg = (cfg.get("newsnow") or {})
    api_url = str(newsnow_cfg.get("api_url") or "").strip()
    verify = bool(newsnow_cfg.get("verify_domain", True))

    merged_all: List[Dict[str, Any]] = []
    for spec in SOURCES + _rss_specs(cfg) + _anysearch_specs(cfg):
        merged = dict(spec)
        if merged.get("adapter") == "newsnow":
            merged["verify_domain"] = verify
            if api_url:
                merged["api_url"] = api_url
        override = overrides.get(spec["source_id"]) or {}
        if isinstance(override, dict):
            merged.update({k: v for k, v in override.items() if k in _OVERRIDABLE})
        merged_all.append(merged)
    return merged_all


def active_specs(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Config may enable/disable/override any source without touching this file."""
    return [spec for spec in _merged(cfg) if spec.get("enabled")]


def all_specs(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    return _merged(cfg)


def _run_one(spec: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]], str, int]:
    adapter = ADAPTERS.get(spec.get("adapter", "rss"), rss.fetch)
    started = time.time()
    try:
        items = adapter(spec, make_item) or []
        return spec, items, "", int((time.time() - started) * 1000)
    except Exception as exc:  # noqa: BLE001 - isolate every adapter
        return spec, [], "%s: %s" % (type(exc).__name__, exc), int((time.time() - started) * 1000)


def _haystack(item: Dict[str, Any]) -> str:
    return ((item.get("title") or "") + " " + (item.get("raw_content") or "")).lower()


def filter_items(spec: Dict[str, Any], items: List[Dict[str, Any]],
                 cfg: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], int, int, int]:
    """价值过滤 + 截断。返回 (保留, 噪音丢弃数, 互动不达标丢弃数, 截断数)。"""
    filters = cfg.get("filters") or {}
    noise = [w.lower() for w in (filters.get("noise_keywords") or []) if w]
    noise += [w.lower() for w in (spec.get("blacklist") or []) if w]
    thresholds = (filters.get("min_engagement") or {}).get(spec["source_id"]) or {}

    # 关键词语法：!排除词 命中即丢；+必须词 只在出现了必须词组时才生效
    rules = keywords.parse(filters.get("rules") or [])
    must_groups = [g for g in rules if g["must"]]
    must_tokens = [t for g in must_groups for t in g["must"]]

    kept: List[Dict[str, Any]] = []
    dropped_noise = 0
    dropped_low = 0
    for item in items:
        blob = _haystack(item)
        if any(word in blob for word in noise):
            dropped_noise += 1
            continue
        if rules and keywords.excluded(rules, blob):
            dropped_noise += 1
            continue
        if must_tokens and not any(keywords._hit(token, blob) for token in must_tokens):
            dropped_noise += 1
            continue
        engagement = item.get("engagement") or {}
        if thresholds and not all(
            (engagement.get(key) or 0) >= minimum for key, minimum in thresholds.items()
        ):
            dropped_low += 1
            continue
        kept.append(item)

    # 热榜类只取前 N 条，避免单一榜单按条数刷屏；新闻类保留全量
    limits = cfg.get("limits") or {}
    kind = spec.get("kind")
    cap = int(spec.get("max_items") or 0)
    if kind == "hotlist":
        cap = min(cap or 999, int(limits.get("hotlist_top_n") or 10))
    elif kind in ("rss", "api"):
        cap = cap or int(limits.get("news_top_n") or 40)
    truncated = max(0, len(kept) - cap) if cap else 0
    if cap:
        kept = kept[:cap]
    return kept, dropped_noise, dropped_low, truncated


def _source_state(conn) -> Dict[str, Dict[str, Any]]:
    return {row["source_id"]: row for row in store.sources(conn)}


def _minutes_since(iso: Any) -> float:
    if not iso:
        return 1e9
    try:
        then = dt.datetime.fromisoformat(str(iso))
        return (dt.datetime.now(dt.timezone.utc) - then).total_seconds() / 60.0
    except (TypeError, ValueError):
        return 1e9


def collect(conn, cfg: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
    """Fetch every enabled source concurrently, persist items, record per-source health.

    数据源熔断：
    * 连续失败 >= degrade_after → 降频（每 degrade_interval_minutes 才重试一次）
    * 连续失败 >= pause_after   → 自动停用，直到手动重置
    """
    specs = active_specs(cfg)
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    for spec in all_specs(cfg):
        store.ensure_source(conn, spec)
    conn.commit()

    backoff = cfg.get("backoff") or {}
    degrade_after = int(backoff.get("degrade_after") or 3)
    pause_after = int(backoff.get("pause_after") or 6)
    degrade_interval = float(backoff.get("degrade_interval_minutes") or 60)
    states = _source_state(conn)

    runnable: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    for spec in specs:
        state = states.get(spec["source_id"]) or {}
        failures = int(state.get("consecutive_failures") or 0)
        if force:
            runnable.append(spec)
            continue
        if failures >= pause_after:
            skipped.append({"source_id": spec["source_id"], "name": spec["name"],
                            "reason": "连续失败 %d 次，已自动停用（可在数据源管理里重置）" % failures})
            store.mark_source_paused(conn, spec["source_id"],
                                     "连续失败 %d 次，已自动停用" % failures)
            continue
        if failures >= degrade_after and _minutes_since(state.get("last_error_at")) < degrade_interval:
            skipped.append({"source_id": spec["source_id"], "name": spec["name"],
                            "reason": "连续失败 %d 次，降频中" % failures})
            continue
        # 单源节流：只有显式声明 min_interval_minutes 的源才生效（例如 AnySearch 有额度限制）
        min_interval = float(spec.get("min_interval_minutes") or 0)
        if min_interval > 0 and _minutes_since(state.get("last_ok_at")) < min_interval:
            skipped.append({"source_id": spec["source_id"], "name": spec["name"],
                            "reason": "距上次成功不足 %d 分钟，本轮跳过" % int(min_interval)})
            continue
        runnable.append(spec)
    conn.commit()

    ok_sources = 0
    failed: List[Dict[str, str]] = []
    total_inserted = 0
    total_seen = 0
    total_raw = 0
    total_kept = 0
    total_filtered = 0
    total_ranks = 0
    per_source: List[Dict[str, Any]] = []

    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(_run_one, spec) for spec in runnable]
        for future in cf.as_completed(futures):
            spec, items, error, latency = future.result()
            if error:
                store.record_source_result(conn, spec["source_id"], False, 0, latency, error, now)
                failed.append({"source_id": spec["source_id"], "name": spec["name"], "error": error})
            else:
                total_raw += len(items)
                kept, noise, low, truncated = filter_items(spec, items, cfg)
                filtered = len(items) - len(kept)
                total_filtered += filtered
                total_kept += len(kept)
                counts = store.insert_items(conn, kept)
                total_inserted += counts["inserted"]
                total_seen += counts["seen"]
                total_ranks += store.record_ranks(conn, kept, now[:10], now)
                store.record_source_result(conn, spec["source_id"], True, len(kept), latency, None, now)
                ok_sources += 1
                per_source.append({
                    "source_id": spec["source_id"], "raw": len(items), "kept": len(kept),
                    "items": len(kept), "new": counts["inserted"],
                    "dropped_noise": noise, "dropped_low_engagement": low, "truncated": truncated,
                })
            conn.commit()

    return {
        "sources_enabled": len(specs),
        "sources_ok": ok_sources,
        "sources_failed": len(failed),
        "sources_skipped": len(skipped),
        "failures": failed,
        "skipped": skipped,
        "items_raw": total_raw,
        "items_kept": total_kept,
        "items_filtered": total_filtered,
        "items_new": total_inserted,
        "items_seen": total_seen,
        "ranks_recorded": total_ranks,
        "per_source": per_source,
        "collected_at": now,
    }
