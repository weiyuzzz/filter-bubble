"""一次性地理补全脚本（幂等，可重复执行）。

用途：
1. 给历史事件补齐地理标注（``radar_events.geo_*`` / ``theme`` / ``region``）；
2. 可选：抓取 USGS 地震图层写入 ``radar_geo_points``。

用法：
    python scripts/geo-backfill.py                 # 只补事件地理标注
    python scripts/geo-backfill.py --fetch-usgs     # 同时抓一次 USGS 地震
    python scripts/geo-backfill.py --limit 500 --force

注意：
* 纯本地计算部分（地理标注）不联网；
* ``--fetch-usgs`` 会发一次外部 HTTP 请求，写库仍是短事务，且网络请求在事务之外；
* 识别不出来的事件保持 NULL，不写猜测坐标。
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import db                                  # noqa: E402
from core.geo import sources as geo_sources          # noqa: E402
from core.radar import config as radar_config        # noqa: E402
from core.radar import geo_tag, sources, store       # noqa: E402


def _fetch_usgs(conn, cfg) -> dict:
    """抓一次 USGS（网络在事务外），再用短事务写库。"""
    spec = next((item for item in sources.SOURCES if item["source_id"] == "usgs_quake"), None)
    if spec is None:
        return {"ok": False, "error": "registry 里找不到 usgs_quake"}
    spec = dict(spec)
    spec.update((cfg.get("sources") or {}).get("usgs_quake") or {})
    try:
        points = geo_sources.fetch_points(spec)
    except Exception as exc:  # noqa: BLE001
        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        store.record_source_result(conn, spec["source_id"], False, 0, 0,
                                   "%s: %s" % (type(exc).__name__, exc), now)
        conn.commit()
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    for point in points:
        point["collected_at"] = now
    counts = store.upsert_geo_points(conn, points)
    store.record_source_result(conn, spec["source_id"], True, len(points), 0, None, now)
    conn.commit()
    return {"ok": True, "points": len(points), "inserted": counts["inserted"],
            "updated": counts["updated"], "collected_at": now,
            "layers": store.geo_point_counts(conn)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="补齐事件地理标注 / 抓取 USGS 图层")
    parser.add_argument("--limit", type=int, default=2000, help="最多处理多少条待标注事件")
    parser.add_argument("--force", action="store_true", help="重算已标注过的事件")
    parser.add_argument("--fetch-usgs", action="store_true", help="同时抓取 USGS 地震图层")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    cfg = radar_config.cached()
    store.init()
    print("数据库:", db.DB_PATH)

    conn = db.connect()
    try:
        before = store.geo_tag_stats(conn)
        print("标注前:", before)

        stats = geo_tag.backfill(conn, limit=args.limit, force=args.force)
        print("本次处理: %d 条，定位成功 %d 条，跳过 %d 条"
              % (stats.get("processed", 0), stats.get("located", 0), stats.get("skipped", 0)))
        print("置信度分布:", stats.get("by_confidence"))
        print("主题分布:", stats.get("by_theme"))
        print("方法分布:", stats.get("by_method"))
        print("标注后:", store.geo_tag_stats(conn))

        if args.fetch_usgs:
            print("抓取 USGS …")
            result = _fetch_usgs(conn, cfg)
            print("USGS:", result)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())