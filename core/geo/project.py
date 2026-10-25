"""Web Mercator 投影：后端与前端必须使用同一组公式。

前端在 ``web/radar-intel.js`` 里有等价实现（``IntelMap.project``）；两处任何改动
都必须同步，否则后端算出的点会在地图上偏一格。
"""
from __future__ import annotations

import math
from typing import Dict, Optional

# Web Mercator 的纬度截断（与主流切片方案一致）
LAT_LIMIT = 85.05112878
# 与前端底图一致的画布尺寸：2:1 的世界，1 单位 = 1/360 经度
WIDTH = 3600.0
HEIGHT = 1800.0


def clamp_lat(lat: float) -> float:
    return max(-LAT_LIMIT, min(LAT_LIMIT, float(lat)))


def project(lon: float, lat: float, width: float = WIDTH, height: float = HEIGHT) -> Dict[str, float]:
    """经纬度 → 平面像素坐标（左上原点）。"""
    x = (float(lon) + 180.0) / 360.0 * width
    rad = math.radians(clamp_lat(lat))
    y = (1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0 * height
    return {"x": round(x, 3), "y": round(y, 3)}


def unproject(x: float, y: float, width: float = WIDTH,
              height: float = HEIGHT) -> Dict[str, float]:
    """平面像素坐标 → 经纬度（缩放到任意范围时要先换算回比例）。"""
    lon = float(x) / width * 360.0 - 180.0
    n = math.pi * (1.0 - 2.0 * float(y) / height)
    lat = math.degrees(math.atan(math.sinh(n)))
    return {"lon": round(lon, 4), "lat": round(lat, 4)}


def unit(lon: float, lat: float) -> Dict[str, float]:
    """投影到 [0,1] 归一化坐标——前端 SVG viewBox 直接用它。"""
    return project(lon, lat, width=1.0, height=1.0)


def bounds_for_zoom(zoom: float) -> Optional[Dict[str, float]]:
    """给定缩放级别返回可见经纬度范围（以赤道为中心），供前端做可见性剔除。"""
    span = 360.0 / max(1.0, float(zoom))
    return {"min_lon": -180.0, "max_lon": 180.0, "span_lon": span}