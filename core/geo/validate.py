"""坐标有效性校验：任何进入数据库或地图的坐标都必须先过这里。"""
from __future__ import annotations

from typing import Any, Dict, Optional

# 几内亚湾"空岛"：(0,0) 是缺值最常见的伪装，除非来源明确声明，否则视为无效
NULL_ISLAND = (0.0, 0.0)
NULL_ISLAND_TOLERANCE = 0.0005


def parse_pair(lat: Any, lon: Any) -> Optional[Dict[str, float]]:
    """把任意输入转成合法坐标；非法返回 ``None``（绝不返回 (0,0) 顶替）。"""
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return None
    if lat_f != lat_f or lon_f != lon_f:  # NaN
        return None
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        return None
    if abs(lat_f) < NULL_ISLAND_TOLERANCE and abs(lon_f) < NULL_ISLAND_TOLERANCE:
        return None
    return {"lat": round(lat_f, 4), "lon": round(lon_f, 4)}


def normalise_longitude(lon: float) -> float:
    """把任意经度收敛到 [-180, 180)，供 ±180° 回绕场景使用。"""
    value = float(lon)
    value = (value + 180.0) % 360.0
    if value < 0:
        value += 360.0
    return value - 180.0


def wrap_delta(lon_a: float, lon_b: float) -> float:
    """两个经度之间的最短差值（考虑国际日期变更线回绕）。"""
    return normalise_longitude(float(lon_a) - float(lon_b))


def centroid(points, weights=None) -> Optional[Dict[str, float]]:
    """加权地理质心；**经度用圆均值**，避免 ±180° 附近算出错误的中间点。

    仅用于"同一事件确有多条带坐标材料"的情形；跨国有歧义时调用方不得使用它。
    """
    rows = [p for p in (points or []) if p and p.get("lat") is not None and p.get("lon") is not None]
    if not rows:
        return None
    if weights is None:
        weights = [1.0] * len(rows)
    if len(weights) != len(rows):
        weights = [1.0] * len(rows)

    total_w = sum(float(w) for w in weights) or 1.0
    lat_sum = 0.0
    x_sum = 0.0
    y_sum = 0.0
    import math

    for row, weight in zip(rows, weights):
        w = float(weight)
        lat_sum += w * float(row["lat"])
        rad = math.radians(float(row["lon"]))
        x_sum += w * math.cos(rad)
        y_sum += w * math.sin(rad)
    lat = lat_sum / total_w
    lon = math.degrees(math.atan2(y_sum / total_w, x_sum / total_w))
    if abs(x_sum) < 1e-12 and abs(y_sum) < 1e-12:
        # 精确对跖点：圆均值退化，如实返回不使用
        return None
    return parse_pair(lat, lon)