"""International adapters: Hacker News, GDELT, arXiv, Reddit, Bluesky.

Adapters that are known to be blocked from this network stay in ``DISABLED_BY_DEFAULT``
sources and report their real status instead of pretending to work.
"""
from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from typing import Any, Dict, List

from ..fetch import clean_text, get_json, strip_tags

HN_ITEM = "https://news.ycombinator.com/item?id=%s"


def _gdelt_date(raw: str) -> str:
    try:
        return dt.datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=dt.timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return ""


def hackernews_front(spec: Dict[str, Any], make_item) -> List[Dict[str, Any]]:
    data, result = get_json(spec["url"], rate_key=spec["source_id"], min_interval=1.0)
    if not data:
        raise RuntimeError(result.error or "hacker news returned no data")
    hits = data.get("hits") or []
    if not hits:
        raise RuntimeError("hacker news returned no hits")
    items = []
    for entry in hits[: int(spec.get("max_items") or 25)]:
        title = clean_text(entry.get("title") or entry.get("story_title"), 300)
        if not title:
            continue
        url = entry.get("url") or entry.get("story_url") or (HN_ITEM % entry.get("objectID"))
        items.append(make_item(
            spec,
            title=title,
            url=url,
            published_at=str(entry.get("created_at") or ""),
            author_name=clean_text(entry.get("author"), 120),
            raw_content="Hacker News front page · %s points · %s comments" % (
                entry.get("points"), entry.get("num_comments")),
            engagement={"points": entry.get("points"), "comments": entry.get("num_comments")},
            source_type="community",
            language="en",
        ))
    return items


def gdelt_doc(spec: Dict[str, Any], make_item) -> List[Dict[str, Any]]:
    """GDELT discovers coverage; it is never treated as a truth verdict."""
    query = spec.get("query") or "(artificial intelligence OR chip OR semiconductor)"
    timespan = spec.get("timespan") or "1d"
    maxrecords = int(spec.get("max_items") or 25)
    url = ("https://api.gdeltproject.org/api/v2/doc/doc?query=%s&mode=artlist&maxrecords=%d"
           "&format=json&timespan=%s&sort=datedesc") % (
        urllib.parse.quote(query), maxrecords, urllib.parse.quote(timespan))
    data, result = get_json(url, rate_key="gdelt", min_interval=15.0)
    if not data:
        raise RuntimeError(result.error or "gdelt returned no data")
    articles = data.get("articles") or []
    if not articles:
        raise RuntimeError("gdelt returned no articles for this query")
    items = []
    for entry in articles[:maxrecords]:
        title = clean_text(entry.get("title"), 300)
        link = entry.get("url") or ""
        if not title or not link:
            continue
        items.append(make_item(
            spec,
            title=title,
            url=link,
            published_at=_gdelt_date(entry.get("seendate", "")),
            author_name=clean_text(entry.get("domain"), 120),
            raw_content="GDELT · %s · %s" % (entry.get("domain", ""), entry.get("sourcecountry", "")),
            engagement={},
            source_type="news",
            language=str(entry.get("language") or "")[:8],
        ))
    return items


def arxiv_search(spec: Dict[str, Any], make_item) -> List[Dict[str, Any]]:
    data = spec.get("url") or "http://export.arxiv.org/api/query?search_query=cat:cs.AI&max_results=20"
    from .rss import parse_feed
    from ..fetch import get

    result = get(data, rate_key=spec["source_id"], min_interval=3.0)
    if not result.ok:
        raise RuntimeError(result.error or "arxiv fetch failed")
    entries = parse_feed(result.text)
    if not entries:
        raise RuntimeError("arxiv feed had no entries")
    items = []
    for entry in entries[: int(spec.get("max_items") or 20)]:
        items.append(make_item(
            spec,
            title=entry["title"],
            url=entry["url"],
            published_at=entry["published_at"],
            author_name=entry.get("author", ""),
            raw_content=entry.get("summary", ""),
            source_type="paper",
            language="en",
        ))
    return items


def reddit_hot(spec: Dict[str, Any], make_item) -> List[Dict[str, Any]]:
    data, result = get_json(spec["url"], rate_key=spec["source_id"], min_interval=2.0)
    if not data:
        raise RuntimeError(result.error or "reddit returned no data")
    children = ((data.get("data") or {}).get("children") or [])
    if not children:
        raise RuntimeError("reddit returned no children")
    items = []
    for child in children[: int(spec.get("max_items") or 25)]:
        entry = child.get("data") or {}
        title = clean_text(entry.get("title"), 300)
        link = "https://www.reddit.com" + str(entry.get("permalink") or "")
        if not title or link == "https://www.reddit.com":
            continue
        items.append(make_item(
            spec,
            title=title,
            url=link,
            published_at=dt.datetime.fromtimestamp(
                float(entry.get("created_utc") or 0), dt.timezone.utc).isoformat(timespec="seconds"),
            author_name=clean_text(entry.get("author"), 120),
            raw_content=strip_tags(entry.get("selftext") or "", 400),
            engagement={"ups": entry.get("ups"), "comments": entry.get("num_comments")},
            source_type="community",
            language="en",
        ))
    return items


def bluesky_search(spec: Dict[str, Any], make_item) -> List[Dict[str, Any]]:
    data, result = get_json(spec["url"], rate_key=spec["source_id"], min_interval=2.0)
    if not data:
        raise RuntimeError(result.error or "bluesky returned no data")
    posts = data.get("posts") or []
    if not posts:
        raise RuntimeError("bluesky returned no posts")
    items = []
    for post in posts[: int(spec.get("max_items") or 20)]:
        record = post.get("record") or {}
        text = clean_text(record.get("text"), 300)
        uri = post.get("uri") or ""
        if not text or not uri:
            continue
        handle = (post.get("author") or {}).get("handle") or ""
        rkey = uri.rsplit("/", 1)[-1]
        items.append(make_item(
            spec,
            title=text,
            url="https://bsky.app/profile/%s/post/%s" % (handle, rkey),
            published_at=str(record.get("createdAt") or ""),
            author_name=clean_text(handle, 120),
            raw_content=text,
            engagement={"likes": post.get("likeCount"), "reposts": post.get("repostCount")},
            source_type="social_post",
            language="en",
        ))
    return items


ADAPTERS = {
    "hackernews_front": hackernews_front,
    "gdelt_doc": gdelt_doc,
    "arxiv_search": arxiv_search,
    "reddit_hot": reddit_hot,
    "bluesky_search": bluesky_search,
}
