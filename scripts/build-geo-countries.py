#!/usr/bin/env python3
"""Build the offline country reference dataset for the news geocoding module.

Fetches Natural Earth's 10m ``admin_0`` countries GeoJSON and writes one record
per country to ``core/geo/data/countries.json``.

The script is one-shot and re-runnable.  It uses only the Python standard
library (``urllib``), keeps TLS certificate verification enabled, and writes the
output atomically (temporary file + ``os.replace``) so a failed download can
never leave a partial dataset behind.

Usage::

    python scripts/build-geo-countries.py             # write the dataset
    python scripts/build-geo-countries.py --dry-run   # print, write nothing

Each record looks like::

    {"iso2": "CN", "iso3": "CHN", "name_en": "China", "name_zh": "\u4e2d\u56fd",
     "lat": 36.5618, "lon": 104.2614, "bbox": [73.6, 18.2, 134.8, 53.6]}

``lat``/``lon`` are the country's representative point: Natural Earth's
``LABEL_Y``/``LABEL_X`` when they are present and usable, otherwise the
area-weighted centroid of the largest polygon in the geometry.  A bounding-box
centre is never used.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import ssl
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SOURCE = "Natural Earth 10m admin_0 countries"
SOURCE_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
    "master/geojson/ne_10m_admin_0_countries.geojson"
)
LICENSE = "Public domain (Natural Earth)"

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "core" / "geo" / "data" / "countries.json"
INIT_FILE = REPO_ROOT / "core" / "geo" / "__init__.py"

# Natural Earth uses -99 in text fields to mean "no data".
MISSING_SENTINEL = "-99"

USER_AGENT = "filter-bubble-build-geo-countries/1.0 (+stdlib urllib)"


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------
def fetch_source(url: str, timeout: float) -> dict:
    """Download and parse the source GeoJSON. Raises RuntimeError on failure."""
    # Explicit default context: certificate + hostname verification stay ON.
    context = ssl.create_default_context()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise RuntimeError("HTTP %s from %s" % (status, url))
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError("HTTP error %s while fetching %s" % (exc.code, url)) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "network error while fetching %s: %s" % (url, exc.reason)
        ) from exc
    except (ssl.SSLError, OSError, TimeoutError) as exc:
        raise RuntimeError("failed to fetch %s: %s" % (url, exc)) from exc

    if not raw.strip():
        raise RuntimeError("empty response body from %s" % url)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("response from %s is not valid UTF-8 JSON: %s" % (url, exc)) from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
        raise RuntimeError("unexpected GeoJSON from %s: no 'features' array" % url)
    if not payload["features"]:
        raise RuntimeError("GeoJSON from %s contains zero features" % url)
    return payload


# --------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------
def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _iter_positions(node):
    """Yield every (lon, lat) coordinate pair in a nested GeoJSON coordinate tree."""
    if isinstance(node, (list, tuple)):
        if len(node) >= 2 and _is_number(node[0]) and _is_number(node[1]):
            yield float(node[0]), float(node[1])
            return
        for child in node:
            yield from _iter_positions(child)


def _polygons(geometry):
    """Return the geometry as a list of polygons; each polygon is a list of rings."""
    if not isinstance(geometry, dict):
        return []
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Polygon":
        return [coords] if coords else []
    if gtype == "MultiPolygon":
        return [poly for poly in (coords or []) if poly]
    return []


def _ring_area_and_centroid(ring):
    """Shoelace area and centroid of a closed ring. Returns (area, (lon, lat)) or None."""
    points = [p for p in ring if isinstance(p, (list, tuple)) and len(p) >= 2]
    # Drop a duplicated closing vertex: the modulo walk below still closes the ring.
    if len(points) >= 2 and points[0][0] == points[-1][0] and points[0][1] == points[-1][1]:
        points = points[:-1]
    if len(points) < 3:
        return None

    twice_area = 0.0
    cx = 0.0
    cy = 0.0
    count = len(points)
    for i in range(count):
        x0, y0 = float(points[i][0]), float(points[i][1])
        x1, y1 = float(points[(i + 1) % count][0]), float(points[(i + 1) % count][1])
        cross = x0 * y1 - x1 * y0
        twice_area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross

    area = twice_area / 2.0
    if abs(area) < 1e-12:
        return None
    return abs(area), (cx / (3.0 * twice_area), cy / (3.0 * twice_area))


def _largest_polygon_centroid(geometry):
    """Centroid of the largest polygon by exterior-ring area, or None."""
    best = None  # (area, centroid)
    for polygon in _polygons(geometry):
        if not polygon:
            continue
        result = _ring_area_and_centroid(polygon[0])
        if result is None:
            continue
        area, centroid = result
        if best is None or area > best[0]:
            best = (area, centroid)
    if best is None:
        return None

    lon, lat = best[1]
    if math.isfinite(lon) and math.isfinite(lat) and -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0:
        return lon, lat
    return None


def _geometry_bbox(geometry):
    """Bounding box [min_lon, min_lat, max_lon, max_lat] of the geometry, or None."""
    positions = list(_iter_positions(geometry.get("coordinates") if isinstance(geometry, dict) else None))
    if not positions:
        return None
    lons = [p[0] for p in positions]
    lats = [p[1] for p in positions]
    return [min(lons), min(lats), max(lons), max(lats)]


def _round4(value: float) -> float:
    """Round to 4 decimals and normalise -0.0 to 0.0."""
    return round(float(value), 4) + 0.0


# --------------------------------------------------------------------------
# record building
# --------------------------------------------------------------------------
def _clean_text(value) -> str:
    """Return a usable string, mapping null / -99 sentinels to ''."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text == MISSING_SENTINEL:
        return ""
    return text


def representative_point(properties: dict, geometry):
    """Representative point of a country, or None when the geometry offers nothing.

    Order: LABEL_X/LABEL_Y, then the largest-polygon centroid, then the mean of
    the geometry's coordinates (which is exact for a Point geometry).  A
    bounding-box centre is never used.
    """
    label_lon = properties.get("LABEL_X")
    label_lat = properties.get("LABEL_Y")
    if _is_number(label_lon) and _is_number(label_lat):
        lon, lat = float(label_lon), float(label_lat)
        usable = (
            -180.0 <= lon <= 180.0
            and -90.0 <= lat <= 90.0
            and not (lon == -99.0 and lat == -99.0)
        )
        if usable:
            return lon, lat

    centroid = _largest_polygon_centroid(geometry)
    if centroid is not None:
        return centroid

    positions = list(
        _iter_positions(geometry.get("coordinates") if isinstance(geometry, dict) else None)
    )
    if positions:
        lon = sum(p[0] for p in positions) / len(positions)
        lat = sum(p[1] for p in positions) / len(positions)
        if math.isfinite(lon) and math.isfinite(lat) and -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0:
            return lon, lat

    return None


def build_record(feature: dict):
    """Build one country record, or None when the feature must be skipped."""
    properties = feature.get("properties") or {}
    geometry = feature.get("geometry")

    raw_iso2 = properties.get("ISO_A2")
    iso2 = _clean_text(raw_iso2).upper()
    if len(iso2) != 2 or not iso2.isalpha():
        # 这一版 Natural Earth 把法国/挪威/科索沃等写成 -99，台湾写成 CN-TW。
        # ISO_A2_EH 是"有效代码"字段，用它兜底才能保住这些新闻高频国家。
        fallback = _clean_text(properties.get("ISO_A2_EH")).upper()
        if len(fallback) == 2 and fallback.isalpha():
            iso2 = fallback
        else:
            if raw_iso2 is None:
                reason = "ISO_A2 is null"
            elif str(raw_iso2).strip() == "":
                reason = "ISO_A2 is empty"
            elif str(raw_iso2).strip() == MISSING_SENTINEL:
                reason = "ISO_A2 is -99 and ISO_A2_EH is unusable"
            else:
                reason = "ISO_A2 %r is not exactly 2 letters" % (raw_iso2,)
            return None, "%s: %s" % (
                _clean_text(properties.get("ADMIN") or properties.get("NAME")) or "<unnamed>",
                reason,
            )

    point = representative_point(properties, geometry)
    bbox = _geometry_bbox(geometry)
    if point is None or bbox is None:
        return None, "%s: no usable point/geometry in the source feature" % iso2
    lon, lat = point

    record = {
        "iso2": iso2,
        "iso3": _clean_text(properties.get("ISO_A3")),
        "name_en": _clean_text(properties.get("ADMIN") or properties.get("NAME_EN")),
        "name_zh": _clean_text(properties.get("NAME_ZH")),
        "lat": _round4(lat),
        "lon": _round4(lon),
        "bbox": [_round4(bbox[0]), _round4(bbox[1]), _round4(bbox[2]), _round4(bbox[3])],
        # 主权国家 vs 属地/争议区：ISO_A2_EH 兜底后会出现同码冲突（FR 的法国与克利珀顿岛），
        # 靠 Natural Earth 的 TYPE 决定谁代表这个国家代码。
        "ne_type": _clean_text(properties.get("TYPE")),
    }
    return record, None


# TYPE 为 "Country" 的才是主权国家；其余（Dependency/Disputed 等）在同码时让位
_SOVEREIGN_TYPES = ("country", "sovereign country", "disputed", "indeterminate")


def _sovereignty_rank(record: dict) -> tuple:
    """同码冲突时的优先级：主权国家 > 名字更短 > 名字更长的（越短越像国家本身）。"""
    ne_type = str(record.get("ne_type") or "").strip().lower()
    is_country = 0 if ne_type == "country" else 1
    name = str(record.get("name_en") or "")
    return (is_country, len(name), name)


def dedupe_by_iso2(records: list) -> tuple:
    """同码只留代表记录，返回 (records, dropped_notes)。"""
    best = {}
    dropped = []
    for record in records:
        code = record["iso2"]
        current = best.get(code)
        if current is None:
            best[code] = record
            continue
        winner, loser = sorted((current, record), key=_sovereignty_rank)
        if winner is current:
            dropped.append("%s: dropped %s (%s) in favour of %s" % (
                code, loser.get("name_en"), loser.get("ne_type") or "?", winner.get("name_en")))
        else:
            dropped.append("%s: dropped %s (%s) in favour of %s" % (
                code, current.get("name_en"), current.get("ne_type") or "?", winner.get("name_en")))
            best[code] = record
    return list(best.values()), dropped


def build_dataset(features) -> tuple:
    """Return (dataset dict, list of skip notes) built from the GeoJSON features."""
    records = []
    skipped = []
    for feature in features:
        if not isinstance(feature, dict):
            skipped.append("<non-object feature>")
            continue
        record, note = build_record(feature)
        if record is None:
            skipped.append(note)
            continue
        records.append(record)

    records, dropped = dedupe_by_iso2(records)
    skipped.extend(dropped)

    # Deterministic ordering; Python's sort is stable, so ties keep source order.
    records.sort(key=lambda item: item["iso2"])

    dataset = {
        "source": SOURCE,
        "source_url": SOURCE_URL,
        "license": LICENSE,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "count": len(records),
        "countries": records,
    }
    return dataset, skipped


def write_dataset(dataset: dict, out_path: Path) -> None:
    """Write the dataset atomically: temp file in the target dir, then os.replace."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the package importable without shipping a non-empty __init__.py.
    if not INIT_FILE.exists():
        INIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        INIT_FILE.write_text("", encoding="utf-8")

    text = json.dumps(dataset, ensure_ascii=False, indent=1) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=str(out_path.parent), prefix=".countries-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, out_path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def duplicate_iso2(records) -> list:
    seen = set()
    duplicates = []
    for record in records:
        code = record["iso2"]
        if code in seen and code not in duplicates:
            duplicates.append(code)
        seen.add(code)
    return sorted(duplicates)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the count and the first 3 records; write nothing")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output path (default: %(default)s)")
    parser.add_argument("--url", default=SOURCE_URL, help="source GeoJSON URL (default: the Natural Earth 10m file)")
    parser.add_argument("--timeout", type=float, default=120.0, help="download timeout in seconds (default: %(default)s)")
    args = parser.parse_args(argv)

    out_path = Path(args.out).expanduser().resolve()

    print("source: %s" % args.url)
    try:
        payload = fetch_source(args.url, args.timeout)
    except RuntimeError as exc:
        print("error: %s" % exc, file=sys.stderr)
        print("error: nothing written to %s" % out_path, file=sys.stderr)
        return 1

    features = payload["features"]
    print("downloaded %d features" % len(features))

    try:
        dataset, skipped = build_dataset(features)
    except (ValueError, TypeError, KeyError) as exc:
        print("error: could not build dataset: %s" % exc, file=sys.stderr)
        print("error: nothing written to %s" % out_path, file=sys.stderr)
        return 1

    with_zh = sum(1 for record in dataset["countries"] if record["name_zh"])
    print("kept %d countries (%d with a Chinese name)" % (dataset["count"], with_zh))
    if skipped:
        print("skipped %d feature(s) without a usable ISO_A2:" % len(skipped))
        for note in skipped:
            print("  - %s" % note)

    duplicates = duplicate_iso2(dataset["countries"])
    if duplicates:
        print("warning: duplicate ISO_A2 codes: %s" % ", ".join(duplicates), file=sys.stderr)

    if args.dry_run:
        print("dry run: no file written")
        print(json.dumps(dataset["countries"][:3], ensure_ascii=False, indent=1))
        return 0

    try:
        write_dataset(dataset, out_path)
    except OSError as exc:
        print("error: could not write %s: %s" % (out_path, exc), file=sys.stderr)
        return 1

    print("wrote %s" % out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())