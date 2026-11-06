#!/usr/bin/env python3
"""Build the "map integrated into radar.html" mockup.

Differences from the standalone prototype:
  * the story cards come from the **real latest daily report** in the database
  * each story gets a geographic centroid computed from its member items
    (this is the algorithm the plan proposes putting into cluster.py)
  * the page markup + CSS reproduce the real radar.html / styles.css verbatim,
    so what you see is what it would actually look like in the app

Read-only on the database. Writes only into prototype/.
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_data  # noqa: E402  (reuse gazetteer / land / geo sources / classifiers)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
DB = os.path.join(ROOT, "data", "filter-bubble.db")
OUT_HTML = os.path.join(HERE, "radar-map-mockup.html")
OUT_JSON = os.path.join(HERE, "mock-payload.json")


def connect():
    return sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)


def items_for_event(conn, event_id):
    rows = conn.execute(
        "SELECT title, url, platform, language, published_at, source_type "
        "FROM radar_items WHERE event_id=? ORDER BY id", (event_id,)).fetchall()
    cols = ("title", "url", "platform", "language", "published_at", "source_type")
    return [dict(zip(cols, r)) for r in rows]


def circular_mean(lons):
    """Longitude mean that survives the ±180 wrap."""
    xs = sum(math.cos(math.radians(v)) for v in lons) / len(lons)
    ys = sum(math.sin(math.radians(v)) for v in lons) / len(lons)
    return math.degrees(math.atan2(ys, xs))


def story_geo(conn, event_ids):
    """Weighted centroid of every member item that the gazetteer can place."""
    lons, lats, labels, hits = [], [], {}, []
    for eid in event_ids:
        for item in items_for_event(conn, eid):
            lon, lat, label = build_data.gazetteer_hit(item["title"], item["language"])
            if lon is None:
                continue
            lons.append(lon)
            lats.append(lat)
            labels[label] = labels.get(label, 0) + 1
            hits.append({"title": item["title"][:90], "label": label,
                         "platform": item["platform"], "lon": lon, "lat": lat})
    if not lons:
        return None
    platforms = {h["platform"] for h in hits}
    return {
        "lon": round(circular_mean(lons), 3),
        "lat": round(sum(lats) / len(lats), 3),
        "label": max(labels.items(), key=lambda kv: kv[1])[0],
        "labels": labels,
        "hit_count": len(hits),
        "platforms": sorted(p for p in platforms if p),
        # 置信度：命中 >=2 条来自不同平台的材料才算高
        "confidence": "high" if len(hits) >= 2 and len(platforms) >= 2 else "low",
        "hits": hits[:4],
    }


def story_region(geo, member_items):
    if geo and geo["labels"]:
        if any(k.startswith("中国") for k in geo["labels"]):
            return "cn"
        return "intl"
    langs = [(i["language"] or "").lower() for i in member_items]
    zh = sum(1 for l in langs if l.startswith("zh") or l == "chinese")
    return "cn" if zh >= max(1, len(langs) // 2) else "intl"


def collect_story(conn, row):
    """row is a dict with event_id / story_id / headline / title."""
    event_ids = []
    if row.get("event_id"):
        event_ids.append(row["event_id"])
    for angle in row.get("angles") or []:
        eid = angle.get("event_id")
        if eid and eid not in event_ids:
            event_ids.append(eid)
    members = []
    for eid in event_ids:
        members.extend(items_for_event(conn, eid))
    geo = story_geo(conn, event_ids)
    title = row.get("headline") or row.get("title") or ""
    return {
        "story_id": row.get("story_id") or row.get("event_id"),
        "event_id": row.get("event_id"),
        "title": row.get("title") or title,
        "headline": title,
        "summary": (row.get("summary") or "")[:240],
        "points": (row.get("points") or [])[:4],
        "category": row.get("category") or "综合",
        "heat": row.get("heat"),
        "importance": row.get("importance"),
        "confidence": row.get("confidence"),
        "confidence_label": row.get("confidence_label") or "",
        "angle_count": row.get("angle_count") or 1,
        "item_count": row.get("item_count") or len(members),
        "platforms": row.get("platforms") or sorted({m["platform"] for m in members if m["platform"]}),
        "lifecycle": row.get("lifecycle"),
        "flags": row.get("flags") or [],
        "tech_flag": bool(row.get("tech_flag")),
        "rank_trend": row.get("rank_trend") or {},
        "is_new": bool(row.get("is_new")),
        "suggest_investigation": bool(row.get("suggest_investigation")),
        "earliest_source": row.get("earliest_source"),
        "geo": geo,
        "region": story_region(geo, members),
        "theme": build_data.classify_theme(title + " " + " ".join(
            (a.get("title") or "") for a in (row.get("angles") or [])[:3])),
    }


def main():
    conn = connect()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT date, generated_at, payload FROM radar_reports "
                       "WHERE kind='daily' ORDER BY date DESC LIMIT 1").fetchone()
    if not row:
        raise SystemExit("no daily report in the database")
    report = json.loads(row["payload"])

    stories = []
    seen = set()
    for h in report.get("highlights") or []:
        if h.get("story_id") in seen:
            continue
        seen.add(h.get("story_id"))
        stories.append(collect_story(conn, h))

    # zh_web / intl 是事件级表格，也一并算出坐标（供“主题 × 地域”区使用）
    browse = []
    for key, region_hint in (("zh_web", "cn"), ("intl", "intl")):
        for r in report.get(key) or []:
            if not r.get("event_id"):
                continue
            c = collect_story(conn, {"event_id": r["event_id"], "title": r.get("title"),
                                     "heat": r.get("heat"), "confidence": r.get("confidence")})
            c["region"] = region_hint
            c["section"] = key
            c["note"] = r.get("note") or ""
            c["duplication_note"] = r.get("duplication_note") or ""
            c["chinese_coverage"] = r.get("chinese_coverage")
            c["platforms"] = r.get("platforms") or c["platforms"]
            browse.append(c)

    sources = [dict(r) for r in conn.execute(
        "SELECT source_id, platform, name, kind, status, enabled, last_item_count, "
        "last_error, consecutive_failures FROM radar_sources ORDER BY platform").fetchall()]
    conn.close()

    base = json.load(open(os.path.join(HERE, "payload.json"), encoding="utf-8"))
    geo_layers = [p for p in base["points"] if p["layer"] != "news"]
    # 融入后，新闻不再单独做图层：故事点就是新闻的空间表达
    catalog = [l for l in base["layerCatalog"] if l["id"] != "news"]

    payload = {
        "land": base["land"],
        "report": {
            "date": row["date"],
            "generated_at": row["generated_at"],
            "stats": report.get("stats") or {},
            "new_count": report.get("new_count") or 0,
            "coverage": report.get("coverage") or {},
            "disclaimer": report.get("disclaimer") or "",
            "summary_backend": report.get("summary_backend") or {},
        },
        "stories": stories,
        "browse": browse,
        "geoPoints": geo_layers,
        "sources": sources,
        "layerCatalog": catalog,
        "layerMeta": base["layerMeta"],
        "attribution": base["attribution"],
    }

    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))

    placed = [s for s in stories if s["geo"]]
    print("report          :", row["date"], row["generated_at"])
    print("stories         :", len(stories), "| geotagged:", len(placed), "/", len(stories))
    print("browse rows     :", len(browse))
    print("geo layer points:", len(geo_layers))
    print("sources         :", len(sources))
    for s in stories:
        g = s["geo"]
        print("   %-6s %-4s %-10s %s" % (
            s["region"], s["theme"], (g["label"] if g else "—"),
            (s["headline"] or "")[:48]))
    print("mock payload bytes:", os.path.getsize(OUT_JSON))


if __name__ == "__main__":
    main()