"""Chinese hot-list adapters. Each one is independent: a failure only disables itself."""
from __future__ import annotations

import datetime as dt
import json
import re
import urllib.parse
from typing import Any, Dict, List

from ..fetch import clean_text, get, get_json, strip_tags

WEIBO_SEARCH = "https://s.weibo.com/weibo?q=%s"
BAIDU_SEARCH = "https://www.baidu.com/s?wd=%s"


def _epoch(value: Any) -> str:
    try:
        ts = int(float(value))
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds")


# ---------------- Weibo ----------------

def weibo_hot(spec: Dict[str, Any], make_item) -> List[Dict[str, Any]]:
    headers = {"Referer": "https://weibo.com/", "X-Requested-With": "XMLHttpRequest"}
    data, result = get_json(spec["url"], headers=headers, rate_key=spec["source_id"], min_interval=1.0)
    if not data:
        raise RuntimeError(result.error or "weibo returned no data")
    entries = ((data.get("data") or {}).get("realtime") or [])
    if not entries:
        raise RuntimeError("weibo payload had no realtime list")
    items = []
    for idx, entry in enumerate(entries[: int(spec.get("max_items") or 50)], start=1):
        word = clean_text(entry.get("word") or entry.get("note") or "", 200)
        if not word:
            continue
        engagement = {}
        for key in ("num", "raw_hot", "fun_word_num"):
            if entry.get(key) is not None:
                engagement[key] = entry.get(key)
        engagement["rank"] = idx
        note = clean_text(entry.get("note") or "", 300)
        items.append(make_item(
            spec,
            title=word,
            url=WEIBO_SEARCH % urllib.parse.quote("#%s#" % word),
            author_name="微博热搜",
            raw_content=note or ("微博热搜第 %d 位" % idx),
            engagement=engagement,
            source_type="social_trend",
            language="zh",
        ))
    return items


# ---------------- Baidu ----------------

_SDATA = re.compile(r"<!--s-data:(\{.*?\})-->", re.S)
_WORD = re.compile(r'"word"\s*:\s*"((?:[^"\\]|\\.)*)"')


def baidu_hot(spec: Dict[str, Any], make_item) -> List[Dict[str, Any]]:
    result = get(spec["url"], rate_key=spec["source_id"], min_interval=1.0,
                 headers={"Referer": "https://top.baidu.com/"})
    if not result.ok:
        raise RuntimeError(result.error or "fetch failed")

    cards = []
    match = _SDATA.search(result.text)
    if match:
        try:
            payload = json.loads(match.group(1))
            cards = ((payload.get("data") or {}).get("cards") or [])
        except ValueError:
            cards = []

    entries: List[Dict[str, Any]] = []
    for card in cards:
        for entry in card.get("content") or []:
            if entry.get("word"):
                entries.append(entry)
    if not entries:
        # Fallback: the page still embeds the ranking words in its inline JSON.
        entries = [{"word": json.loads('"%s"' % w)} for w in _WORD.findall(result.text)[: int(spec.get("max_items") or 30)]]
    if not entries:
        raise RuntimeError("could not extract Baidu hot list")

    items = []
    for idx, entry in enumerate(entries[: int(spec.get("max_items") or 30)], start=1):
        word = clean_text(entry.get("word"), 200)
        if not word:
            continue
        url = entry.get("rawUrl") or entry.get("url") or (BAIDU_SEARCH % urllib.parse.quote(word))
        engagement = {"rank": idx}
        for key in ("hotScore", "hot_change", "index"):
            if entry.get(key) is not None:
                engagement[key] = entry.get(key)
        items.append(make_item(
            spec,
            title=word,
            url=url,
            author_name="百度热搜",
            raw_content=strip_tags(entry.get("desc") or "", 300) or ("百度热搜第 %d 位" % idx),
            engagement=engagement,
            source_type="social_trend",
            language="zh",
        ))
    return items


# ---------------- Bilibili ----------------

def bilibili_popular(spec: Dict[str, Any], make_item) -> List[Dict[str, Any]]:
    data, result = get_json(spec["url"], headers={"Referer": "https://www.bilibili.com/"},
                            rate_key=spec["source_id"], min_interval=1.0)
    if not data:
        raise RuntimeError(result.error or "bilibili returned no data")
    entries = ((data.get("data") or {}).get("list") or [])
    if not entries:
        raise RuntimeError("bilibili payload had no list")
    items = []
    for idx, entry in enumerate(entries[: int(spec.get("max_items") or 30)], start=1):
        title = clean_text(entry.get("title"), 300)
        if not title:
            continue
        stat = entry.get("stat") or {}
        engagement = {
            "rank": idx,
            "view": stat.get("view"),
            "like": stat.get("like"),
            "danmaku": stat.get("danmaku"),
            "reply": stat.get("reply"),
            "share": stat.get("share"),
        }
        bvid = entry.get("bvid") or ""
        url = entry.get("short_link_v2") or ("https://www.bilibili.com/video/%s" % bvid if bvid else "")
        items.append(make_item(
            spec,
            title=title,
            url=url or ("https://www.bilibili.com/video/%s" % bvid),
            published_at=_epoch(entry.get("pubdate")),
            author_name=clean_text((entry.get("owner") or {}).get("name"), 120),
            raw_content=clean_text(entry.get("desc"), 400),
            engagement=engagement,
            source_type="social_trend",
            language="zh",
        ))
    return items


# ---------------- Toutiao ----------------

def toutiao_hot(spec: Dict[str, Any], make_item) -> List[Dict[str, Any]]:
    data, result = get_json(spec["url"], headers={"Referer": "https://www.toutiao.com/"},
                            rate_key=spec["source_id"], min_interval=1.0)
    if not data:
        raise RuntimeError(result.error or "toutiao returned no data")
    entries = data.get("data") or []
    if not entries:
        raise RuntimeError("toutiao payload had no data list")
    items = []
    for idx, entry in enumerate(entries[: int(spec.get("max_items") or 30)], start=1):
        title = clean_text(entry.get("Title"), 300)
        if not title:
            continue
        cluster = str(entry.get("ClusterIdStr") or entry.get("ClusterId") or "")
        url = entry.get("Url") or ("https://www.toutiao.com/trending/%s/" % cluster if cluster else "")
        if not url:
            continue
        items.append(make_item(
            spec,
            title=title,
            url=url,
            author_name="今日头条热榜",
            raw_content=clean_text(entry.get("LabelDesc") or "", 200) or ("头条热榜第 %d 位" % idx),
            engagement={"rank": idx, "hot_value": entry.get("HotValue"), "label": entry.get("Label")},
            source_type="social_trend",
            language="zh",
        ))
    return items


# ---------------- NetEase ----------------

def netease_hot(spec: Dict[str, Any], make_item) -> List[Dict[str, Any]]:
    data, result = get_json(spec["url"], headers={"Referer": "https://m.163.com/"},
                            rate_key=spec["source_id"], min_interval=1.0)
    if not data:
        raise RuntimeError(result.error or "netease returned no data")
    entries = ((data.get("data") or {}).get("list") or [])
    if not entries:
        raise RuntimeError("netease payload had no list")
    items = []
    for idx, entry in enumerate(entries[: int(spec.get("max_items") or 30)], start=1):
        title = clean_text(entry.get("title"), 300)
        if not title:
            continue
        doc_id = entry.get("docid") or entry.get("skipID") or ""
        url = entry.get("url") or ("https://3g.163.com/news/article/%s.html" % doc_id if doc_id else "")
        if not url:
            continue
        items.append(make_item(
            spec,
            title=title,
            url=url,
            author_name=clean_text(entry.get("source"), 120) or "网易热榜",
            raw_content=clean_text(entry.get("digest") or "", 300),
            engagement={"rank": idx},
            source_type="social_trend",
            language="zh",
        ))
    return items


# ---------------- V2EX ----------------

def v2ex_hot(spec: Dict[str, Any], make_item) -> List[Dict[str, Any]]:
    data, result = get_json(spec["url"], headers={"Referer": "https://www.v2ex.com/"},
                            rate_key=spec["source_id"], min_interval=2.0)
    if data is None:
        raise RuntimeError(result.error or "v2ex returned no data")
    if not isinstance(data, list) or not data:
        raise RuntimeError("v2ex payload was not a list")
    items = []
    for entry in data[: int(spec.get("max_items") or 30)]:
        title = clean_text(entry.get("title"), 300)
        url = entry.get("url") or ""
        if not title or not url:
            continue
        node = (entry.get("node") or {}).get("name") or ""
        items.append(make_item(
            spec,
            title=title,
            url=url,
            published_at=_epoch(entry.get("created")),
            author_name=clean_text((entry.get("member") or {}).get("username"), 120),
            raw_content=clean_text(entry.get("content") or entry.get("content_rendered") or "", 400) or ("节点 " + node),
            engagement={"replies": entry.get("replies"), "node": node},
            source_type="community",
            language="zh",
        ))
    return items


ADAPTERS = {
    "weibo_hot": weibo_hot,
    "baidu_hot": baidu_hot,
    "bilibili_popular": bilibili_popular,
    "toutiao_hot": toutiao_hot,
    "netease_hot": netease_hot,
    "v2ex_hot": v2ex_hot,
}
