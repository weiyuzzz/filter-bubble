"""结构化地理图层适配器：USGS 地震 / NASA EONET / GDACS 灾害。

设计约定（与既有新闻源适配器一致，但产出的是"地理点"而不是新闻条目）：

* 适配器是**纯函数**：只做网络请求 + 解析，**不碰数据库**。写库由调用方在短事务里完成，
  因此外部 HTTP 永远发生在 SQLite 写事务之外；
* 每个点都带 ``layer`` / ``external_id``，外部 ID 用于幂等 upsert，重复采集不产生重复行；
* 坏 JSON、缺字段、无效经纬度、超时、HTTP 429、上游异常一律抛 ``RuntimeError``，
  交给既有的源健康状态、失败记录、降频与退避机制处理；
* 单个图层失败**不会**中断新闻采集、日报或其他任务（调用方逐个隔离）；
* **几何诚实**：``Point`` 才是来源给出的点位（``position_precision="point"``）；
  ``Polygon`` 只保留原始多边形交给前端按面绘制，其"中心"仅用于排序/视野，
  标为 ``position_precision="area"``，**不得冒充精确事发点**。

实测记录（2026-10-26，本机）：

* EONET ``/api/v3/events?status=open&days=7`` → HTTP 200，JSON（尽管响应头写的是
  ``application/rss+xml``，**不能相信 Content-Type，必须直接按 JSON 解析**）；
* GDACS ``geteventlist/SEARCH`` → HTTP 200，FeatureCollection，86 个要素、6 种灾害类型；
* USGS 4.5/week → HTTP 200。
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, List, Optional, Tuple

from ..radar.fetch import get
from . import validate

USGS_TEMPLATE = ("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/"
                 "%s_%s.geojson")
EONET_TEMPLATE = "https://eonet.gsfc.nasa.gov/api/v3/events?status=open&days=%d&limit=%d"
GDACS_SEARCH = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"

# ---------------------------------------------------------------- 灾害类型
EARTHQUAKE = "earthquake"
VOLCANO = "volcano"
WILDFIRE = "wildfire"
FLOOD = "flood"
STORM = "storm"
DROUGHT = "drought"
OTHER = "other"

HAZARD_LABELS: Dict[str, str] = {
    EARTHQUAKE: "地震", VOLCANO: "火山", WILDFIRE: "野火", FLOOD: "洪水",
    STORM: "风暴", DROUGHT: "干旱", OTHER: "其他灾害",
}

# 前端图层开关分组（只包含实际已接入的类型）
HAZARD_GROUPS: List[Dict[str, Any]] = [
    {"key": EARTHQUAKE, "label": "地震"},
    {"key": VOLCANO, "label": "火山"},
    {"key": WILDFIRE, "label": "野火"},
    {"key": FLOOD, "label": "洪水"},
    {"key": STORM, "label": "风暴"},
    {"key": OTHER, "label": "其他灾害"},
]

EONET_HAZARD = {
    "earthquakes": EARTHQUAKE, "volcanoes": VOLCANO, "wildfires": WILDFIRE,
    "floods": FLOOD, "severeStorms": STORM, "drought": DROUGHT,
    "dustHaze": OTHER, "landslides": OTHER, "manmade": OTHER, "seaLakeIce": OTHER,
    "snow": OTHER, "tempExtremes": OTHER, "waterColor": OTHER,
}

GDACS_HAZARD = {
    "EQ": EARTHQUAKE, "VO": VOLCANO, "WF": WILDFIRE, "FL": FLOOD,
    "TC": STORM, "DR": DROUGHT, "TS": FLOOD,
}

# 图层目录：既给采集用，也给前端图例用。`connected=False` 的层在前端显示"未接入"，
# 绝不用假点位填充。
LAYER_CATALOG: List[Dict[str, Any]] = [
    {"key": "usgs_quake", "label": "地震（USGS）", "connected": True,
     "hazards": [EARTHQUAKE],
     "source": "USGS Earthquake Hazards Program", "license": "美国联邦政府作品，公共领域",
     "url": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/",
     "note": "免 key；GeoJSON；提供 Point 坐标"},
    {"key": "eonet", "label": "自然事件（NASA EONET）", "connected": True,
     "hazards": [WILDFIRE, VOLCANO, FLOOD, STORM, EARTHQUAKE, DROUGHT, OTHER],
     "source": "NASA EONET v3", "license": "公共领域（NASA）",
     "url": "https://eonet.gsfc.nasa.gov/docs/v3",
     "note": "免 key；JSON；含 Point 与 Polygon，Polygon 按面绘制不当作精确点"},
    {"key": "gdacs", "label": "灾害警报（GDACS）", "connected": True,
     "hazards": [EARTHQUAKE, VOLCANO, WILDFIRE, FLOOD, STORM, DROUGHT],
     "source": "GDACS / EU JRC", "license": "需署名；使用条款需人工核对",
     "url": "https://www.gdacs.org/gdacsapi/swagger/index.html",
     "note": "免 key；GeoJSON FeatureCollection；含 alertlevel 警戒等级"},
]

IMPLEMENTED_LAYERS = {"usgs_quake", "eonet", "gdacs"}


def layer_catalog() -> List[Dict[str, Any]]:
    return [dict(row) for row in LAYER_CATALOG]


def hazard_groups() -> List[Dict[str, Any]]:
    return [dict(row) for row in HAZARD_GROUPS]


# ---------------------------------------------------------------- 小工具
def _epoch_ms_to_iso(value: Any) -> str:
    try:
        seconds = float(value) / 1000.0
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).replace(
        microsecond=0).isoformat(timespec="seconds")


def _text(value: Any, limit: int = 300) -> str:
    return str(value or "").strip()[:limit]


def _polygon_center(coords: Any) -> Optional[Dict[str, float]]:
    """多边形外环的**近似中心**。

    明确不是"事件精确坐标"：调用方必须把 ``position_precision`` 标成 ``area``。
    只用外环顶点均值，避免引入复杂几何库（零依赖）。
    """
    if not isinstance(coords, list) or not coords:
        return None
    ring = coords[0] if isinstance(coords[0], list) else coords
    if not isinstance(ring, list):
        return None
    points = []
    for item in ring:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            point = validate.parse_pair(item[1], item[0])
            if point:
                points.append(point)
    if not points:
        return None
    lat = sum(p["lat"] for p in points) / len(points)
    lon = sum(p["lon"] for p in points) / len(points)
    return validate.parse_pair(lat, lon)


def _json_or_none(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise RuntimeError("上游返回的不是合法 JSON：%s" % exc)


# ---------------------------------------------------------------- USGS
def usgs_url(spec: Dict[str, Any]) -> str:
    return USGS_TEMPLATE % (str(spec.get("feed") or "4.5"), str(spec.get("period") or "week"))


def _parse_usgs(payload: Any, spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        raise RuntimeError("usgs: response is not a JSON object")
    features = payload.get("features")
    if features is None:
        raise RuntimeError("usgs: response has no features field")
    if not isinstance(features, list):
        raise RuntimeError("usgs: features is not a list")

    layer = str(spec.get("layer") or "usgs_quake")
    max_items = int(spec.get("max_items") or 500)
    max_age_days = spec.get("max_age_days")
    cutoff = None
    if max_age_days:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=float(max_age_days))

    points: List[Dict[str, Any]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry") or {}
        props = feature.get("properties") or {}
        coords = geometry.get("coordinates")
        if geometry.get("type") != "Point" or not isinstance(coords, list) or len(coords) < 2:
            continue
        point = validate.parse_pair(coords[1], coords[0])
        if not point:
            continue
        observed_at = _epoch_ms_to_iso(props.get("time"))
        if cutoff and observed_at:
            try:
                if dt.datetime.fromisoformat(observed_at) < cutoff:
                    continue
            except ValueError:
                pass
        external_id = _text(props.get("ids") or feature.get("id"), 120)
        if not external_id:
            continue
        magnitude = props.get("mag")
        try:
            magnitude = float(magnitude)
        except (TypeError, ValueError):
            magnitude = None
        place = _text(props.get("place"))
        title = _text(props.get("title")) or (
            ("M%.1f %s" % (magnitude, place)).strip() if magnitude is not None else place)
        points.append({
            "layer": layer, "external_id": external_id,
            "lat": point["lat"], "lon": point["lon"],
            "title": title, "detail": place,
            "magnitude": magnitude, "magnitude_unit": "M",
            "severity": _text(props.get("alert")) or None,
            "hazard": EARTHQUAKE,
            "observed_at": observed_at, "closed_at": None,
            "updated_at": _epoch_ms_to_iso(props.get("updated")),
            "source_id": spec["source_id"],
            "url": _text(props.get("url"), 1000),
            "geometry_type": "Point", "geometry_json": [coords[0], coords[1]],
            "position_precision": "point",
            "raw": {"type": props.get("type"), "sig": props.get("sig"),
                    "tsunami": props.get("tsunami"),
                    "depth_km": coords[2] if len(coords) > 2 else None,
                    "felt": props.get("felt"), "mmi": props.get("mmi")},
        })
        if len(points) >= max_items:
            break
    return points


# ---------------------------------------------------------------- EONET
def eonet_url(spec: Dict[str, Any]) -> str:
    days = int(spec.get("days") or 7)
    limit = int(spec.get("max_items") or 200)
    return EONET_TEMPLATE % (max(1, min(60, days)), max(1, min(500, limit)))


def _parse_eonet(payload: Any, spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        raise RuntimeError("eonet: response is not a JSON object")
    events = payload.get("events")
    if events is None:
        raise RuntimeError("eonet: response has no events field")
    if not isinstance(events, list):
        raise RuntimeError("eonet: events is not a list")

    layer = str(spec.get("layer") or "eonet")
    max_items = int(spec.get("max_items") or 200)
    wanted = spec.get("hazards") or None
    points: List[Dict[str, Any]] = []
    skipped_no_geo = 0

    for event in events:
        if not isinstance(event, dict):
            continue
        external_id = _text(event.get("id"), 120)
        if not external_id:
            continue
        categories = event.get("categories") or []
        category = categories[0] if categories and isinstance(categories[0], dict) else {}
        hazard = EONET_HAZARD.get(str(category.get("id") or ""), OTHER)
        if wanted and hazard not in wanted:
            continue

        geometry_list = event.get("geometry") or []
        if not isinstance(geometry_list, list) or not geometry_list:
            skipped_no_geo += 1
            continue
        # 取时间最新的一条几何（EONET 的 geometry 是时间序列）
        latest = geometry_list[-1] if isinstance(geometry_list[-1], dict) else {}
        gtype = _text(latest.get("type")) or "Point"
        coords = latest.get("coordinates")
        if gtype == "Point":
            point = (validate.parse_pair(coords[1], coords[0])
                     if isinstance(coords, list) and len(coords) >= 2 else None)
            precision = "point"
        elif gtype == "Polygon":
            point = _polygon_center(coords)
            precision = "area"
        else:
            point = None
            precision = "area"
        if not point:
            skipped_no_geo += 1
            continue

        sources = event.get("sources") or []
        source = sources[0] if sources and isinstance(sources[0], dict) else {}
        magnitude = latest.get("magnitudeValue")
        try:
            magnitude = float(magnitude)
        except (TypeError, ValueError):
            magnitude = None
        title = _text(event.get("title")) or _text(event.get("description")) or external_id
        points.append({
            "layer": layer, "external_id": external_id,
            "lat": point["lat"], "lon": point["lon"],
            "title": title, "detail": _text(event.get("description")),
            "magnitude": magnitude,
            "magnitude_unit": _text(latest.get("magnitudeUnit"), 40) or None,
            "severity": None, "hazard": hazard,
            "observed_at": _text(latest.get("date"), 40) or None,
            "closed_at": _text(event.get("closed"), 40) or None,
            "updated_at": _text(latest.get("date"), 40) or None,
            "source_id": spec["source_id"],
            "url": _text(source.get("url"), 1000),
            "geometry_type": gtype,
            "geometry_json": coords if gtype == "Polygon" else (
                [coords[0], coords[1]] if isinstance(coords, list) else None),
            "position_precision": precision,
            "raw": {
                "category": category, "sources": sources,
                "geometry_count": len(geometry_list),
                "skipped_no_geometry": skipped_no_geo,
                "link": _text(event.get("link"), 300),
            },
        })
        if len(points) >= max_items:
            break
    return points


# ---------------------------------------------------------------- GDACS
def gdacs_url(spec: Dict[str, Any]) -> str:
    return str(spec.get("url") or GDACS_SEARCH)


def _gdacs_link(props: Dict[str, Any]) -> str:
    raw = props.get("url")
    if isinstance(raw, dict):
        for key in ("report", "details", "geometry", "warnings"):
            if raw.get(key):
                return _text(raw.get(key), 1000)
        for value in raw.values():
            if isinstance(value, str) and value.startswith("http"):
                return _text(value, 1000)
        return ""
    return _text(raw, 1000)


def _parse_gdacs(payload: Any, spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        raise RuntimeError("gdacs: response is not a JSON object")
    features = payload.get("features")
    if features is None:
        raise RuntimeError("gdacs: response has no features field")
    if not isinstance(features, list):
        raise RuntimeError("gdacs: features is not a list")

    layer = str(spec.get("layer") or "gdacs")
    max_items = int(spec.get("max_items") or 300)
    wanted = spec.get("hazards") or None
    current_only = bool(spec.get("current_only", True))
    points: List[Dict[str, Any]] = []

    for feature in features:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        event_type = _text(props.get("eventtype"), 8).upper()
        hazard = GDACS_HAZARD.get(event_type, OTHER)
        if wanted and hazard not in wanted:
            continue
        if current_only and str(props.get("iscurrent", "true")).lower() == "false":
            continue

        coords = geometry.get("coordinates")
        point = None
        gtype = _text(geometry.get("type")) or "Point"
        if gtype == "Point" and isinstance(coords, list) and len(coords) >= 2:
            point = validate.parse_pair(coords[1], coords[0])
        elif gtype in ("Polygon", "MultiPolygon"):
            point = _polygon_center(coords[0] if gtype == "Polygon" else
                                    (coords[0][0] if isinstance(coords, list) and coords else None))
        if not point:
            # 坐标无效：跳过，不写入任何"猜测点位"
            continue

        event_id = _text(props.get("eventid"), 40)
        episode_id = _text(props.get("episodeid"), 20)
        if not event_id:
            continue
        external_id = "%s-%s-%s" % (event_type or "XX", event_id, episode_id or "0")

        alert = _text(props.get("alertlevel"), 12).lower() or None
        severity = None
        severity_data = props.get("severitydata")
        magnitude = None
        magnitude_unit = None
        if isinstance(severity_data, dict):
            try:
                magnitude = float(severity_data.get("severity"))
            except (TypeError, ValueError):
                magnitude = None
            magnitude_unit = _text(severity_data.get("severityunit"), 24) or None
            severity = _text(severity_data.get("severitytext"), 160) or None
        if not severity:
            severity = alert

        points.append({
            "layer": layer, "external_id": external_id,
            "lat": point["lat"], "lon": point["lon"],
            "title": _text(props.get("description")) or _text(props.get("eventname")) or external_id,
            "detail": _text(props.get("country"), 200),
            "magnitude": magnitude, "magnitude_unit": magnitude_unit,
            "severity": severity,
            "hazard": hazard,
            "observed_at": _text(props.get("fromdate"), 40) or None,
            "closed_at": _text(props.get("todate"), 40) or None,
            "updated_at": _text(props.get("datemodified"), 40) or None,
            "source_id": spec["source_id"],
            "url": _gdacs_link(props),
            "geometry_type": gtype,
            "geometry_json": coords if gtype != "Point" else (
                [coords[0], coords[1]] if isinstance(coords, list) else None),
            "position_precision": "point" if gtype == "Point" else "area",
            "raw": {"eventtype": event_type, "alertlevel": alert,
                    "alertscore": props.get("alertscore"), "episodeid": episode_id,
                    "eventid": event_id, "eventname": props.get("eventname"),
                    "glide": props.get("glide"), "source": props.get("source"),
                    "iso3": props.get("iso3"), "iscurrent": props.get("iscurrent")},
        })
        if len(points) >= max_items:
            break
    return points


# ---------------------------------------------------------------- 分发
ADAPTERS = {
    "usgs": _parse_usgs,
    "eonet": _parse_eonet,
    "gdacs": _parse_gdacs,
}

URL_BUILDERS = {
    "usgs": usgs_url,
    "eonet": eonet_url,
    "gdacs": gdacs_url,
}


def fetch_points(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """抓取并解析一个结构化图层，返回归一化的地理点列表（**不写库**）。"""
    adapter = str(spec.get("adapter") or "")
    parser = ADAPTERS.get(adapter)
    if parser is None:
        raise RuntimeError("图层未接入：%s（接口已预留，尚未实现）" % (spec.get("layer") or adapter))

    url = str(spec.get("url") or "") or URL_BUILDERS[adapter](spec)
    result = get(url, rate_key=spec["source_id"],
                 min_interval=float(spec.get("min_interval_seconds") or 2.0))
    if not result.ok:
        raise RuntimeError(result.error or "%s fetch failed" % adapter)
    payload = _json_or_none(result.text)
    return parser(payload, spec)


def metadata() -> Dict[str, Any]:
    return {"implemented": sorted(IMPLEMENTED_LAYERS),
            "catalog": layer_catalog(),
            "hazard_groups": hazard_groups(),
            "note": "未接入的图层只登记目录与状态，不产生任何点位；"
                    "Polygon 几何只按面绘制，其中心不作为精确事发点。"}