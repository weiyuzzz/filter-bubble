#!/usr/bin/env python3
"""Assemble the self-contained payload for the World-Monitor-style prototype.

Two inputs, both real:
  1. raw/   fixtures fetched over the network (Natural Earth land, USGS quakes,
            NASA EONET, GDACS, OpenSky flights).
  2. the filter-bubble SQLite database, read-only, for the news items the user's
            own radar has already collected.

Output: payload.json  ->  {land, points, news, layers, meta}

This script belongs to the prototype, not to the application. It never writes to
the database and never touches any application file.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw")
DB = os.path.abspath(os.path.join(HERE, "..", "data", "filter-bubble.db"))

W, H = 2000, 1000
LAT_LIMIT = 85.05112878


# --------------------------------------------------------------------------- map
def project(lon, lat):
    lat = max(-LAT_LIMIT, min(LAT_LIMIT, float(lat)))
    x = (float(lon) + 180.0) / 360.0 * W
    rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0 * H
    return x, y


def ring_to_path(ring):
    parts, last = [], None
    for coord in ring:
        if not isinstance(coord, (list, tuple)) or len(coord) < 2:
            continue
        x, y = project(coord[0], coord[1])
        pt = (round(x, 1), round(y, 1))
        if pt == last:
            continue
        last = pt
        parts.append("%g %g" % pt)
    if len(parts) < 3:
        return ""
    return "M" + "L".join(parts) + "Z"


def build_land():
    with open(os.path.join(RAW, "ne_110m_land.geojson"), encoding="utf-8") as fh:
        geo = json.load(fh)
    out = []
    for feature in geo.get("features", []):
        geom = feature.get("geometry") or {}
        coords = geom.get("coordinates") or []
        gtype = geom.get("type")
        polygons = [coords] if gtype == "Polygon" else (coords if gtype == "MultiPolygon" else [])
        for polygon in polygons:
            for ring in polygon:
                pts = [c for c in ring if isinstance(c, (list, tuple)) and len(c) >= 2]
                if len(pts) < 4:
                    continue
                xs = [c[0] for c in pts]
                ys = [c[1] for c in pts]
                if (max(ys) - min(ys)) < 0.4 and (max(xs) - min(xs)) < 0.4:
                    continue
                frag = ring_to_path(pts)
                if frag:
                    out.append(frag)
    return "".join(out)


def load_json(name):
    with open(os.path.join(RAW, name), encoding="utf-8-sig") as fh:
        return json.load(fh)


# --------------------------------------------------------------- live geo layers
def parse_quakes():
    out = []
    for feature in load_json("quakes.geojson").get("features", []):
        geom = feature.get("geometry") or {}
        coords = geom.get("coordinates") or []
        props = feature.get("properties") or {}
        if geom.get("type") != "Point" or len(coords) < 2:
            continue
        out.append({
            "layer": "quake", "lon": round(float(coords[0]), 3), "lat": round(float(coords[1]), 3),
            "title": props.get("place") or "地震", "mag": props.get("mag"),
            "time": props.get("time"), "url": props.get("url") or "", "source": "USGS",
            "detail": "M%.1f" % (props.get("mag") or 0),
        })
    return out


EONET_CATEGORY = {
    "wildfires": ("fire", "林火"), "severeStorms": ("storm", "强风暴"),
    "floods": ("flood", "洪涝"), "volcanoes": ("volcano", "火山"),
    "drought": ("drought", "干旱"), "earthquakes": ("quake", "地震"),
    "seaLakeIce": ("ice", "海冰"), "dustHaze": ("dust", "沙尘"),
    "landslides": ("landslide", "滑坡"), "manmade": ("manmade", "人为事件"),
    "snow": ("snow", "降雪"), "tempExtremes": ("temp", "极端温度"),
    "waterColor": ("water", "水体异常"),
}


def parse_eonet():
    out = []
    for event in load_json("eonet.txt").get("events", []):
        geometry = event.get("geometry") or []
        if not geometry:
            continue
        latest = geometry[-1]
        coords = latest.get("coordinates") or []
        if latest.get("type") != "Point" or len(coords) < 2:
            continue
        cat_id = ((event.get("categories") or [{}])[0] or {}).get("id") or "manmade"
        layer, _label = EONET_CATEGORY.get(cat_id, ("manmade", cat_id))
        mag = latest.get("magnitudeValue")
        out.append({
            "layer": layer, "lon": round(float(coords[0]), 3), "lat": round(float(coords[1]), 3),
            "title": event.get("title") or "", "time": latest.get("date"),
            "url": event.get("link") or "", "source": "NASA EONET",
            "detail": ("%s %s" % (mag, latest.get("magnitudeUnit") or "")).strip(),
        })
    return out


GDACS_LAYER = {
    "EQ": ("quake", "地震"), "TC": ("storm", "热带气旋"), "FL": ("flood", "洪水"),
    "WF": ("fire", "林火"), "DR": ("drought", "干旱"), "VO": ("volcano", "火山"),
    "TS": ("tsunami", "海啸"),
}


def parse_gdacs():
    out = []
    for feature in load_json("gdacs.txt").get("features", []):
        geom = feature.get("geometry") or {}
        props = feature.get("properties") or {}
        if geom.get("type") != "Point":
            continue
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        event_type = props.get("eventtype") or ""
        layer, label = GDACS_LAYER.get(event_type, ("manmade", event_type or "灾害"))
        alert = (props.get("alertlevel") or "").lower()
        url = props.get("url")
        if isinstance(url, dict):
            url = url.get("report") or url.get("geometry") or ""
        out.append({
            "layer": layer, "lon": round(float(coords[0]), 3), "lat": round(float(coords[1]), 3),
            "title": props.get("eventname") or props.get("name") or label,
            "time": props.get("todate") or props.get("fromdate"), "url": url or "",
            "source": "GDACS", "alert": alert,
            "detail": {"green": "绿色警报", "orange": "橙色警报", "red": "红色警报"}.get(alert, label),
        })
    return out


def parse_opensky(max_flights=430):
    out = []
    for state in load_json("opensky.txt").get("states") or []:
        if not isinstance(state, list) or len(state) < 11:
            continue
        lon, lat = state[5], state[6]
        if lon is None or lat is None:
            continue
        out.append({
            "layer": "flight", "lon": round(float(lon), 3), "lat": round(float(lat), 3),
            "title": (state[1] or "").strip() or state[0], "time": state[4],
            "url": "", "source": "OpenSky Network", "country": state[2],
            "heading": state[10], "alt": state[13],
            "detail": "%s · %s km" % (state[2] or "", round((state[13] or 0) / 1000)),
        })
    if len(out) > max_flights:
        step = len(out) / float(max_flights)
        out = [out[int(i * step)] for i in range(max_flights)]
    return out


# ------------------------------------------------------------- static POI layers
# Public, well-known locations. This is a curated static registry (like the
# upstream project's own curated registries), not observed events.
CHOKEPOINTS = [
    ("霍尔木兹海峡", 56.5, 26.6, "全球约 1/5 原油经此"), ("马六甲海峡", 100.5, 2.5, "亚欧主航道"),
    ("苏伊士运河", 32.55, 30.0, "亚欧海运捷径"), ("巴拿马运河", -79.6, 9.1, "美洲东西岸"),
    ("曼德海峡", 43.3, 12.6, "红海南口"), ("直布罗陀海峡", -5.6, 35.95, "地中海西口"),
    ("博斯普鲁斯海峡", 29.0, 41.1, "黑海唯一出海口"), ("台湾海峡", 119.5, 24.5, "西太平洋要冲"),
    ("对马海峡", 129.3, 34.5, "东海—日本海"), ("厄勒海峡", 12.7, 55.6, "波罗的海出海口"),
]
NUCLEAR = [
    ("福岛第一核电站", 141.03, 37.42), ("切尔诺贝利", 30.10, 51.39), ("扎波罗热核电站", 34.58, 47.51),
    ("布什尔核电站", 50.89, 28.83), ("宁边核设施", 125.75, 39.80), ("大亚湾核电站", 114.55, 22.60),
    ("库丹库拉姆", 77.71, 8.17), ("巴拉卡核电站", 52.79, 24.15), ("迪亚布洛峡谷", -120.85, 35.31),
    ("格罗瑙铀浓缩厂", 7.05, 52.21),
]
CABLES = [
    ("新加坡登陆站", 103.85, 1.29), ("香港舂坎角", 114.20, 22.21), ("上海崇明登陆站", 121.80, 31.50),
    ("马赛登陆站", 5.36, 43.30), ("马尼拉登陆站", 120.98, 14.60), ("纽约夏馨小屋", -74.02, 40.60),
    ("洛杉矶圣莫尼卡", -118.49, 34.02), ("开普敦登陆站", 18.42, -33.92), ("孟买登陆站", 72.83, 19.07),
    ("吉布提登陆站", 43.15, 11.60),
]
DATACENTERS = [
    ("北弗吉尼亚集群", -77.49, 39.04), ("俄勒冈集群", -121.18, 44.30), ("爱荷华集群", -95.86, 41.26),
    ("都柏林集群", 6.30, 53.35), ("新加坡裕廊", 103.70, 1.33), ("上海临港", 121.90, 30.90),
    ("张家口集群", 114.88, 40.77), ("贵安集群", 106.63, 26.65), ("内华达集群", -119.81, 39.53),
]
FINANCE = [
    ("纽约", -74.01, 40.71), ("伦敦", -0.12, 51.51), ("东京", 139.69, 35.69), ("香港", 114.17, 22.32),
    ("上海", 121.47, 31.23), ("新加坡", 103.82, 1.35), ("法兰克福", 8.68, 50.11), ("迪拜", 55.27, 25.20),
    ("首尔", 126.98, 37.57), ("孟买", 72.88, 19.08), ("圣保罗", -46.63, -23.55), ("悉尼", 151.21, -33.87),
]
BASES = [
    ("关岛安德森基地", 144.79, 13.58), ("冲绳嘉手纳", 127.77, 26.36), ("迪戈加西亚", 72.42, -7.31),
    ("巴林第五舰队", 50.61, 26.21), ("拉姆施泰因", 7.60, 49.44), ("诺福克海军基地", -76.30, 36.85),
    ("横须贺", 139.67, 35.29), ("达尔文", 130.85, -12.46), ("吉布提莱蒙尼尔", 43.15, 11.55),
]


def static_layer(layer, rows, with_note=False):
    out = []
    for row in rows:
        name, lon, lat = row[0], row[1], row[2]
        note = row[3] if with_note and len(row) > 3 else ""
        out.append({"layer": layer, "lon": lon, "lat": lat, "title": name,
                    "source": "精选静态注册表", "detail": note, "time": None, "url": ""})
    return out


# ------------------------------------------------------------- news geotagging
# A deliberately small gazetteer. It is the proof-of-concept for "geo-tag the
# news we already collect"; the real module would use a proper gazetteer.
GAZETTEER = [
    ("北京", "beijing", 116.41, 39.90, "中国"), ("上海", "shanghai", 121.47, 31.23, "中国"),
    ("深圳", "shenzhen", 114.06, 22.54, "中国"), ("广州", "guangzhou", 113.26, 23.13, "中国"),
    ("香港", "hong kong", 114.17, 22.32, "中国香港"), ("台湾", "taiwan", 121.0, 23.7, "中国台湾"),
    ("台北", "taipei", 121.56, 25.03, "中国台湾"), ("新疆", "", 87.62, 43.79, "中国"),
    ("西藏", "tibet", 91.11, 29.65, "中国"), ("海南", "hainan", 110.35, 20.02, "中国"),
    ("四川", "sichuan", 104.07, 30.65, "中国"), ("山东", "shandong", 117.02, 36.67, "中国"),
    ("河南", "henan", 113.65, 34.76, "中国"), ("云南", "yunnan", 102.71, 25.04, "中国"),
    ("武汉", "wuhan", 114.30, 30.59, "中国"), ("杭州", "hangzhou", 120.15, 30.27, "中国"),
    ("南京", "nanjing", 118.80, 32.06, "中国"), ("西安", "xian", 108.94, 34.34, "中国"),
    ("重庆", "chongqing", 106.55, 29.56, "中国"), ("天津", "tianjin", 117.20, 39.13, "中国"),
    ("成都", "chengdu", 104.07, 30.57, "中国"), ("贵阳", "guiyang", 106.63, 26.65, "中国"),
    ("中国", "china", 116.41, 39.90, "中国"),
    ("美国", "united states", -77.04, 38.91, "美国"), ("华盛顿", "washington", -77.04, 38.91, "美国"),
    ("纽约", "new york", -74.01, 40.71, "美国"), ("硅谷", "silicon valley", -122.03, 37.37, "美国"),
    ("加州", "california", -119.42, 36.78, "美国"), ("得州", "texas", -97.56, 30.27, "美国"),
    ("日本", "japan", 139.69, 35.69, "日本"), ("东京", "tokyo", 139.69, 35.69, "日本"),
    ("韩国", "korea", 126.98, 37.57, "韩国"), ("首尔", "seoul", 126.98, 37.57, "韩国"),
    ("朝鲜", "north korea", 125.75, 39.03, "朝鲜"), ("印度", "india", 77.21, 28.61, "印度"),
    ("新德里", "delhi", 77.21, 28.61, "印度"), ("俄罗斯", "russia", 37.62, 55.75, "俄罗斯"),
    ("莫斯科", "moscow", 37.62, 55.75, "俄罗斯"), ("乌克兰", "ukraine", 30.52, 50.45, "乌克兰"),
    ("基辅", "kyiv", 30.52, 50.45, "乌克兰"), ("以色列", "israel", 35.21, 31.77, "以色列"),
    ("加沙", "gaza", 34.47, 31.50, "巴勒斯坦"), ("耶路撒冷", "jerusalem", 35.21, 31.77, "以色列"),
    ("伊朗", "iran", 51.39, 35.69, "伊朗"), ("德黑兰", "tehran", 51.39, 35.69, "伊朗"),
    ("沙特", "saudi", 46.72, 24.71, "沙特"), ("土耳其", "turkey", 32.85, 39.93, "土耳其"),
    ("叙利亚", "syria", 36.29, 33.51, "叙利亚"), ("黎巴嫩", "lebanon", 35.50, 33.89, "黎巴嫩"),
    ("也门", "yemen", 44.21, 15.35, "也门"), ("埃及", "egypt", 31.24, 30.04, "埃及"),
    ("伊拉克", "iraq", 44.36, 33.31, "伊拉克"), ("阿富汗", "afghanistan", 69.18, 34.53, "阿富汗"),
    ("英国", "britain", -0.12, 51.51, "英国"), ("伦敦", "london", -0.12, 51.51, "英国"),
    ("法国", "france", 2.35, 48.86, "法国"), ("巴黎", "paris", 2.35, 48.86, "法国"),
    ("德国", "germany", 13.40, 52.52, "德国"), ("柏林", "berlin", 13.40, 52.52, "德国"),
    ("意大利", "italy", 12.50, 41.90, "意大利"), ("西班牙", "spain", -3.70, 40.42, "西班牙"),
    ("荷兰", "netherlands", 4.90, 52.37, "荷兰"), ("欧盟", "european union", 4.35, 50.85, "欧盟"),
    ("波兰", "poland", 21.01, 52.23, "波兰"), ("瑞典", "sweden", 18.07, 59.33, "瑞典"),
    ("巴西", "brazil", -47.88, -15.79, "巴西"), ("阿根廷", "argentina", -58.38, -34.60, "阿根廷"),
    ("墨西哥", "mexico", -99.13, 19.43, "墨西哥"), ("加拿大", "canada", -75.70, 45.42, "加拿大"),
    ("澳大利亚", "australia", 149.13, -35.28, "澳大利亚"), ("新加坡", "singapore", 103.82, 1.35, "新加坡"),
    ("越南", "vietnam", 105.83, 21.03, "越南"), ("泰国", "thailand", 100.50, 13.76, "泰国"),
    ("菲律宾", "philippines", 120.98, 14.60, "菲律宾"), ("印尼", "indonesia", 106.85, -6.21, "印度尼西亚"),
    ("马来西亚", "malaysia", 101.69, 3.14, "马来西亚"), ("巴基斯坦", "pakistan", 73.06, 33.68, "巴基斯坦"),
    ("缅甸", "myanmar", 96.16, 16.87, "缅甸"), ("南非", "south africa", 28.05, -26.20, "南非"),
    ("尼日利亚", "nigeria", 7.49, 9.06, "尼日利亚"), ("肯尼亚", "kenya", 36.82, -1.29, "肯尼亚"),
    ("埃塞俄比亚", "ethiopia", 38.75, 9.02, "埃塞俄比亚"), ("刚果", "congo", 15.31, -4.32, "刚果"),
    ("欧洲", "europe", 4.35, 50.85, "欧洲"), ("中东", "middle east", 44.36, 33.31, "中东"),
    ("非洲", "africa", 28.05, -26.20, "非洲"), ("东南亚", "southeast asia", 103.82, 1.35, "东南亚"),
]


def gazetteer_hit(title, language):
    """Return (lon, lat, label) for the first gazetteer term in the title."""
    low = (title or "").lower()
    for entry in GAZETTEER:
        zh, en, lon, lat, label = entry
        if zh and zh in title:
            return lon, lat, label
    for entry in GAZETTEER:
        zh, en, lon, lat, label = entry
        if en and re.search(r"\b" + re.escape(en) + r"\b", low):
            return lon, lat, label
    return None, None, ""


THEMES = [
    ("ai", "科技与 AI", ["ai", "人工智能", "大模型", "模型", "机器人", "算力", "gpu", "llm",
                        "机器学习", "自动驾驶", "开源", "github", "quantum", "量子", "科技"]),
    ("chip", "芯片半导体", ["芯片", "半导体", "光刻", "制程", "晶圆", "台积电", "英伟达", "nvidia",
                          "semiconductor", "chip", "tsmc", "asml", "存储"]),
    ("money", "财经市场", ["股", "市场", "经济", "金融", "gdp", "央行", "利率", "通胀", "关税",
                         "涨", "跌", "上市", "融资", "投资", "market", "stock", "tariff", "inflation",
                         "economy", "fed", "earnings", "比特", "加密"]),
    ("geo", "地缘冲突", ["冲突", "战争", "袭击", "制裁", "军事", "导弹", "停火", "外交", "抗议",
                       "示威", "选举", "政变", "领土", "war", "strike", "sanction", "military",
                       "ceasefire", "missile", "protest", "election", "troops", "nato", "核武"]),
    ("disaster", "灾害自然", ["地震", "台风", "飓风", "洪水", "暴雨", "山火", "火山", "干旱",
                            "灾害", "救援", "疏散", "earthquake", "typhoon", "hurricane", "flood",
                            "wildfire", "volcano", "storm", "disaster", "heatwave"]),
    ("cyber", "网络安全", ["漏洞", "勒索", "黑客", "数据泄露", "攻击", "木马", "钓鱼", "apt",
                         "vulnerability", "ransomware", "hack", "breach", "malware", "cve",
                         "exploit", "phishing", "zero-day"]),
    ("energy", "能源资源", ["石油", "原油", "天然气", "lng", "电力", "核电", "光伏", "储能",
                          "煤炭", "油价", "天然气价", "oil", "gas", "energy", "nuclear", "solar",
                          "pipeline", "opec", "grid"]),
]


def classify_theme(title):
    low = (title or "").lower()
    best, best_hits = "other", 0
    for key, label, words in THEMES:
        hits = sum(1 for w in words if w in low)
        if hits > best_hits:
            best, best_hits = key, hits
    return best


def load_news(limit=150):
    if not os.path.exists(DB):
        return [], "database not found"
    conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT title, url, platform, language, source_type, published_at, collected_at
           FROM radar_items
           WHERE title IS NOT NULL AND length(title) > 6
           ORDER BY id DESC LIMIT 1200""").fetchall()
    conn.close()

    seen, items = set(), []
    for row in rows:
        title = (row["title"] or "").strip()
        norm = re.sub(r"\W+", "", title.lower())[:60]
        if not norm or norm in seen:
            continue
        seen.add(norm)
        lang = (row["language"] or "").strip().lower()
        is_zh = lang.startswith("zh") or lang in ("chinese",)
        lon, lat, label = gazetteer_hit(title, lang)
        if label == "中国" and lon == 116.41:
            region = "cn"
        elif label in ("中国香港", "中国台湾"):
            region = "cn"
        elif label:
            region = "cn" if label.startswith("中国") else "intl"
        else:
            region = "cn" if is_zh else "intl"
        items.append({
            "title": title[:160], "url": row["url"] or "", "platform": row["platform"] or "",
            "lang": "zh" if is_zh else "en", "region": region, "theme": classify_theme(title),
            "time": row["published_at"] or row["collected_at"] or "",
            "source_type": row["source_type"] or "news",
            "geo": label, "lon": lon, "lat": lat,
        })
    # keep the newest per theme so the rail shows a spread rather than one feed
    buckets = {}
    for item in items:
        buckets.setdefault(item["theme"], []).append(item)
    picked = []
    per_theme = max(6, limit // max(1, len(THEMES)))
    for theme, group in buckets.items():
        picked.extend(group[:per_theme])
    picked.sort(key=lambda d: d["time"] or "", reverse=True)
    return picked[:limit], None


# ------------------------------------------------------------------------ layer catalog
LAYER_CATALOG = [
    # group, id, name, source, access, status, note
    ("自然灾害", "quake", "地震", "USGS GeoJSON", "免费免 key", "live", "全球 M4.5+，每分钟更新"),
    ("自然灾害", "storm", "强风暴/气旋", "GDACS + NASA EONET", "免费免 key", "live", "热带气旋与强对流"),
    ("自然灾害", "flood", "洪涝", "GDACS", "免费免 key", "live", "含橙色/红色警报"),
    ("自然灾害", "fire", "林火", "NASA EONET（+FIRMS）", "EONET 免 key / FIRMS 免费注册", "live", "FIRMS 可到 375m 火点"),
    ("自然灾害", "volcano", "火山", "GDACS + EONET", "免费免 key", "live", ""),
    ("自然灾害", "drought", "干旱", "GDACS", "免费免 key", "live", ""),
    ("自然灾害", "ice", "海冰/冰山", "NASA EONET", "免费免 key", "live", ""),
    ("航空与海上", "flight", "航班轨迹", "OpenSky Network", "免费账号", "live", "实测本次为东半球空域快照"),
    ("航空与海上", "emergency", "航空紧急代码", "adsb.lol", "免费免 key (ODbL)", "planned", "7500/7600/7700，60s 轮询"),
    ("航空与海上", "ship", "船舶 AIS", "AISStream.io", "免费 key", "planned", "WebSocket 中继，2 万船上限"),
    ("地缘与冲突", "news", "新闻地理位置", "本项目 radar_items + 地名库", "本项目自采", "live", "把已采集标题地理编码后上图"),
    ("地缘与冲突", "conflict", "武装冲突事件", "ACLED / UCDP", "ACLED 需凭据 / UCDP 免费 token", "planned", "无凭据时该层置空，不伪造点位"),
    ("地缘与冲突", "protest", "抗议示威", "GDELT", "免费免 key", "degraded", "公共额度紧张，实测 429"),
    ("地缘与冲突", "base", "军事基地", "精选静态注册表", "免费", "static", "公开资料收录"),
    ("网络与基础设施", "cyber", "网络威胁", "abuse.ch Feodo / URLhaus", "Feodo 免 key / URLhaus 需 key", "planned", ""),
    ("网络与基础设施", "outage", "网络中断", "Cloudflare Radar", "需 Bearer token", "planned", "国家/地区级中断"),
    ("网络与基础设施", "cable", "海底光缆登陆站", "TeleGeography 精选", "免费", "static", ""),
    ("网络与基础设施", "nuclear", "核设施", "IAEA / 精选注册表", "免费", "static", ""),
    ("网络与基础设施", "datacenter", "AI 数据中心", "Epoch AI 精选", "免费", "static", "313 集群可全量导入"),
    ("网络与基础设施", "chokepoint", "航道咽喉点", "精选注册表 + IMF PortWatch", "免费免 key", "static", "PortWatch 可补通航量"),
    ("市场与节点", "finance", "金融中心/交易所", "精选注册表", "免费", "static", ""),
    ("市场与节点", "prediction", "预测市场", "Polymarket Gamma API", "免费免 key", "planned", "需浏览器直连绕过 Cloudflare"),
    ("市场与节点", "weather", "强天气警报", "NWS + ECCC + WMO SWIC", "免费免 key", "planned", "CAP 聚合"),
]

LAYER_META = {
    "quake": {"color": "#f0616d", "shape": "circle"},
    "storm": {"color": "#4ea8f5", "shape": "circle"},
    "flood": {"color": "#3fb6c8", "shape": "circle"},
    "fire": {"color": "#ff9b3d", "shape": "circle"},
    "volcano": {"color": "#e0603a", "shape": "circle"},
    "drought": {"color": "#c9a227", "shape": "circle"},
    "ice": {"color": "#9fd8ef", "shape": "circle"},
    "dust": {"color": "#c8a97e", "shape": "circle"},
    "snow": {"color": "#cfe3f5", "shape": "circle"},
    "temp": {"color": "#f2a33c", "shape": "circle"},
    "landslide": {"color": "#b07d4a", "shape": "circle"},
    "manmade": {"color": "#9aa6b6", "shape": "circle"},
    "water": {"color": "#4fb3a5", "shape": "circle"},
    "flight": {"color": "#8ea4ff", "shape": "plane"},
    "news": {"color": "#5fd18c", "shape": "news"},
    "base": {"color": "#c78bd6", "shape": "square"},
    "cable": {"color": "#5ad0e0", "shape": "square"},
    "nuclear": {"color": "#e8bb5c", "shape": "square"},
    "datacenter": {"color": "#a99cff", "shape": "square"},
    "chokepoint": {"color": "#f2847d", "shape": "square"},
    "finance": {"color": "#7fd4a0", "shape": "square"},
}


def main():
    land = build_land()
    points = (parse_quakes() + parse_eonet() + parse_gdacs() + parse_opensky()
              + static_layer("chokepoint", CHOKEPOINTS, with_note=True)
              + static_layer("nuclear", NUCLEAR) + static_layer("cable", CABLES)
              + static_layer("datacenter", DATACENTERS) + static_layer("finance", FINANCE)
              + static_layer("base", BASES))

    news, news_error = load_news()
    # news that resolved to a coordinate also becomes a mappable point
    news_points = []
    for item in news:
        if item["lon"] is not None:
            news_points.append({
                "layer": "news", "lon": item["lon"], "lat": item["lat"],
                "title": item["title"], "url": item["url"], "platform": item["platform"],
                "time": item["time"], "source": item["platform"] or "自采",
                "detail": item["geo"], "theme": item["theme"], "region": item["region"],
            })
    points += news_points

    counts = {}
    for pt in points:
        counts[pt["layer"]] = counts.get(pt["layer"], 0) + 1

    payload = {
        "land": land,
        "points": points,
        "news": news,
        "layerCatalog": [dict(zip(("group", "id", "name", "source", "access", "status", "note"), row))
                         for row in LAYER_CATALOG],
        "layerMeta": LAYER_META,
        "counts": counts,
        "newsError": news_error,
        "generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "attribution": [
            {"name": "USGS", "url": "https://earthquake.usgs.gov/", "use": "地震目录"},
            {"name": "NASA EONET", "url": "https://eonet.gsfc.nasa.gov/", "use": "自然事件"},
            {"name": "GDACS", "url": "https://www.gdacs.org/", "use": "灾害警报"},
            {"name": "OpenSky Network", "url": "https://opensky-network.org/", "use": "航班快照"},
            {"name": "Natural Earth", "url": "https://www.naturalearthdata.com/", "use": "世界底图（公有领域）"},
            {"name": "filter-bubble radar", "url": "", "use": "新闻条目（本机自采）"},
        ],
    }

    out = os.path.join(HERE, "payload.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    print("land path chars :", len(land))
    print("points          :", len(points), counts)
    print("news            :", len(news), "geotagged:", len(news_points))
    if news_error:
        print("news error      :", news_error)
    print("payload bytes   :", os.path.getsize(out))


if __name__ == "__main__":
    main()