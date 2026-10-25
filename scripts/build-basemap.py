"""生成离线的世界底图 ``web/world-110m.js``（一次生成，运行时零网络）。

做法：把 Natural Earth 110m admin_0 countries 多边形投影到 Web Mercator，
输出成两条 SVG path：
* ``land``    —— 填充用的陆地轮廓；
* ``borders`` —— 国界描边（同一份多边形，只描边不填充）。

前端用同一个投影公式（``web/radar-intel.js`` 里的 ``project``），所以底图点位与
数据库坐标严格一致。完全断网时地图仍然可用，不依赖任何瓦片服务器。

用法：
    python scripts/build-basemap.py            # 生成 web/world-110m.js
    python scripts/build-basemap.py --dry-run  # 只打印规模，不写文件
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

SOURCE_URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
              "master/geojson/ne_110m_admin_0_countries.geojson")
LICENSE = "Public domain (Natural Earth)"
WIDTH, HEIGHT = 3600.0, 1800.0
LAT_LIMIT = 85.05112878
MIN_RING_SPAN = 0.35      # 度：太小的岛不画，省体积
ROUND = 1                 # 坐标保留小数位


def project(lon: float, lat: float):
    lat = max(-LAT_LIMIT, min(LAT_LIMIT, float(lat)))
    x = (float(lon) + 180.0) / 360.0 * WIDTH
    rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0 * HEIGHT
    return x, y


def ring_to_path(ring):
    parts = []
    last = None
    for coord in ring:
        if not isinstance(coord, (list, tuple)) or len(coord) < 2:
            continue
        try:
            x, y = project(coord[0], coord[1])
        except (TypeError, ValueError):
            continue
        point = (round(x, ROUND), round(y, ROUND))
        if point == last:
            continue
        last = point
        parts.append("%g %g" % point)
    if len(parts) < 4:
        return ""
    return "M" + "L".join(parts) + "Z"


def polygons_of(geometry):
    if not isinstance(geometry, dict):
        return []
    coords = geometry.get("coordinates") or []
    kind = geometry.get("type")
    if kind == "Polygon":
        return [coords]
    if kind == "MultiPolygon":
        return coords
    return []


def build_paths(geojson):
    """返回 (paths, skipped)。同一份路径前端既用作填充也用作描边，省一半体积。"""
    paths = []
    skipped = 0
    for feature in geojson.get("features", []):
        for polygon in polygons_of(feature.get("geometry")):
            total = ""
            for ring in polygon:
                pts = [c for c in ring if isinstance(c, (list, tuple)) and len(c) >= 2]
                if len(pts) < 4:
                    skipped += 1
                    continue
                xs = [c[0] for c in pts]
                ys = [c[1] for c in pts]
                if (max(ys) - min(ys)) < MIN_RING_SPAN and (max(xs) - min(xs)) < MIN_RING_SPAN:
                    skipped += 1
                    continue
                frag = ring_to_path(pts)
                if frag:
                    total += frag
            if total:
                paths.append(total)
    return "".join(paths), skipped


def fetch(url: str, timeout: int = 60):
    request = urllib.request.Request(url, headers={
        "User-Agent": "filter-bubble-basemap/1.0",
        "Accept": "application/geo+json,application/json",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成离线世界底图")
    parser.add_argument("--out", default="web/world-110m.js")
    parser.add_argument("--url", default=SOURCE_URL)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        geojson = fetch(args.url)
    except Exception as exc:  # noqa: BLE001
        print("底图数据下载失败：%s" % exc, file=sys.stderr)
        return 1

    features = geojson.get("features") or []
    paths, skipped = build_paths(geojson)
    if not paths:
        print("没有生成任何路径，已中止（不写文件）", file=sys.stderr)
        return 1

    payload = (
        "/* 自动生成，请勿手改。\n"
        " * 来源: %s\n"
        " * 许可: %s\n"
        " * 生成: python scripts/build-basemap.py\n"
        " * 投影: Web Mercator，画布 %gx%g，与 core/geo/project.py 一致\n"
        " * 同一份 paths 既用作陆地填充，也用作国界描边（省体积）\n"
        " */\n"
        "window.FB_WORLD = {\n"
        "  width: %g, height: %g, latLimit: %g,\n"
        "  source: %s, license: %s,\n"
        "  paths: %s\n"
        "};\n"
    ) % (
        args.url, LICENSE, WIDTH, HEIGHT,
        WIDTH, HEIGHT, LAT_LIMIT,
        json.dumps("Natural Earth 110m admin_0 countries"), json.dumps(LICENSE),
        json.dumps(paths, ensure_ascii=False),
    )

    print("features=%d  path_chars=%d  skipped_rings=%d"
          % (len(features), len(paths), skipped))
    if args.dry_run:
        print("dry-run：不写文件")
        return 0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=str(out.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
        os.replace(tmp, out)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    print("wrote %s (%.1f KB)" % (out, out.stat().st_size / 1024.0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())