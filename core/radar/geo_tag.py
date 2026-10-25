"""事件地理标注编排（写入 ``radar_events`` 的地理列与 ``theme`` / ``region``）。

分工：
* ``core/geo/geocode.py`` 是**纯函数**（不碰数据库），负责"能不能确定位置、置信度多少"；
* 本模块负责**持久化与调度**：读取待标注事件 → 解析 → 短事务写入。

纪律：
* 识别不出来就写 NULL，**绝不写猜测坐标**；
* 幂等：已标注过（``geo_method`` 不是空/none）的事件默认不重复处理；
* 只处理新采集或需要重算的记录，避免每次打开页面重算全量；
* 每个批次一个短事务，不在事务里做任何网络请求（本模块完全不联网）。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from ..geo import countries, gazetteer, geocode
from . import classify, store

# 媒体/平台所属国家：**只用于 origin_country_code（报道来源国）**，
# 绝不参与 event_country_code（事件发生国）的判定。
SOURCE_COUNTRY: Dict[str, str] = {
    "cn_scroll": "CN", "cn_china": "CN", "cn_world": "CN", "sina_news": "CN",
    "ithome": "CN", "qbitai": "CN", "thepaper": "CN", "ifeng": "CN",
    "wallstreetcn": "CN", "cls_news": "CN", "weibo_hot": "CN", "baidu_hot": "CN",
    "bilibili_popular": "CN", "toutiao_hot": "CN", "netease_hot": "CN",
    "v2ex_hot": "CN", "zhihu_hot": "CN", "douyin": "CN", "tieba": "CN",
    "bbc_zh": "GB", "dw_zh": "DE", "techcrunch": "US", "hackernews": "US",
    "reddit_tech": "US", "bluesky": "US", "arxiv": "US",
    "anysearch_ai": "", "anysearch_chip": "", "gdelt": "",
}

CN_REGION_CODES = {"CN", "HK", "MO", "TW"}


def _origin_country(items: Iterable[Dict[str, Any]]) -> Optional[str]:
    """事件的"报道来源国"：取成员来源的国家，出现多国或未知则不臆断。"""
    codes = set()
    for item in items:
        code = SOURCE_COUNTRY.get(str(item.get("source_id") or ""))
        if code:
            codes.add(code)
    if len(codes) == 1:
        return next(iter(codes))
    return None


def _region_for(country_code: Optional[str]) -> str:
    if not country_code:
        return "unknown"
    return "cn" if country_code.upper() in CN_REGION_CODES else "intl"


def tag_event(conn, event: Dict[str, Any], items: Optional[List[Dict[str, Any]]] = None,
              force: bool = False) -> Dict[str, Any]:
    """给单个事件做地理标注 + 主题分类，返回落库后的结果（供统计与审计）。"""
    items = items or []
    current = str(event.get("geo_method") or "")
    if current and current != geocode.METHOD_NONE and not force:
        return {"skipped": True, "reason": "already_tagged", "event_id": event.get("id")}

    item_titles = [(item.get("title") or "") for item in items][:geocode.MAX_ITEM_TITLES]
    result = geocode.resolve_event(str(event.get("title") or ""), item_titles=item_titles)

    source_coords = None
    for item in items:
        point = item.get("coordinates") or item.get("geo") or None
        if isinstance(point, dict) and point.get("lat") is not None:
            source_coords = point
            break
    if source_coords:
        result = geocode.resolve_event(
            str(event.get("title") or ""), item_titles=item_titles, source_coords=source_coords,
            source_name=str(source_coords.get("source") or "item"))

    result["origin_country_code"] = _origin_country(items)
    theme = classify.classify_event(event)

    store.apply_event_geo(conn, event["id"], result)
    conn.execute(
        "UPDATE radar_events SET theme=?, region=? WHERE id=?",
        (theme["theme"], _region_for(result.get("country_code")), event["id"]),
    )
    result["event_id"] = event["id"]
    result["theme"] = theme["theme"]
    result["region"] = _region_for(result.get("country_code"))
    result["skipped"] = False
    return result


def _items_for(conn, event_id: str) -> List[Dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        "SELECT source_id, platform, title, language, url FROM radar_items WHERE event_id=?",
        (event_id,)).fetchall()]


def tag_events(conn, events: List[Dict[str, Any]], force: bool = False,
               commit_every: int = 50) -> Dict[str, Any]:
    """批量标注；每 ``commit_every`` 条提交一次（短事务）。"""
    stats = {"processed": 0, "located": 0, "skipped": 0,
             "by_confidence": {"high": 0, "medium": 0, "low": 0, "none": 0},
             "by_theme": {}, "by_method": {}, "ambiguous": 0}
    batch = 0
    for event in events:
        try:
            result = tag_event(conn, event, _items_for(conn, event["id"]), force=force)
        except Exception as exc:  # noqa: BLE001 - 单条失败不能中断整批
            print("[geo] tag failed for %s: %s" % (event.get("id"), exc))
            result = {"skipped": True, "reason": "error"}
        if result.get("skipped"):
            stats["skipped"] += 1
            continue
        stats["processed"] += 1
        confidence = str(result.get("confidence") or "none")
        stats["by_confidence"][confidence] = stats["by_confidence"].get(confidence, 0) + 1
        if confidence != geocode.NONE:
            stats["located"] += 1
        if result.get("ambiguous"):
            stats["ambiguous"] += 1
        theme = str(result.get("theme") or "")
        stats["by_theme"][theme] = stats["by_theme"].get(theme, 0) + 1
        method = str(result.get("method") or "")
        stats["by_method"][method] = stats["by_method"].get(method, 0) + 1
        batch += 1
        if batch >= commit_every:
            conn.commit()
            batch = 0
    conn.commit()
    return stats


def backfill(conn, limit: int = 2000, force: bool = False) -> Dict[str, Any]:
    """一次性补齐历史事件的地理标注（幂等，可重复执行）。

    ``force=True`` 会按当前规则**重算全部事件**（包括已标注的），
    用于收紧/放宽规则之后把存量数据拉到新口径。
    """
    events = store.events_for_geo_tagging(conn, limit=limit, force=force)
    stats = tag_events(conn, events, force=force)
    stats["candidates"] = len(events)
    stats["coverage_after"] = store.geo_tag_stats(conn)
    return stats


def tag_day(conn, date: str, cfg: Optional[Dict[str, Any]] = None,
            commit: bool = True) -> Dict[str, Any]:
    """给某一天的事件做标注——挂在采集流水线里，只处理当天的事件。"""
    events = store.events_for_date(conn, date, limit=2000)
    stats = tag_events(conn, events, force=False, commit_every=len(events) or 1)
    stats["date"] = date
    if commit:
        conn.commit()
    return stats


def metadata() -> Dict[str, Any]:
    return {
        "source_country_map": SOURCE_COUNTRY,
        "cn_region_codes": sorted(CN_REGION_CODES),
        "note": "origin_country_code 只表示报道来源国，绝不用于事件发生国；"
                "两者语义不同，不得互相顶替。",
        "gazetteer": {
            "entries": len(gazetteer.ENTRIES),
            "countries": countries.metadata(),
        },
    }