"""按需采集指定数据源（不跑全量）。

用途：新增/调试某个源时，只跑它，避免把全部源都打一遍。

    python scripts/collect-now.py --only eonet,gdacs --list
    python scripts/collect-now.py --only bbc_world,guardian_world
    python scripts/collect-now.py --only eonet --force     # 忽略熔断/节流

注意事项：
* 走的是**同一个** `sources.collect()`，所以源健康、限流、退避、熔断、
  `radar_geo_points` 幂等写入全部一致，不是另起一套采集；
* 未点名的源会被临时置为 disabled（只影响本次调用，不改配置文件）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import db                          # noqa: E402
from core.radar import config as rcfg        # noqa: E402
from core.radar import sources, store        # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="按需采集指定数据源")
    parser.add_argument("--only", default="", help="逗号分隔的 source_id")
    parser.add_argument("--force", action="store_true", help="忽略熔断与节流")
    parser.add_argument("--list", action="store_true", help="只列出可用 source_id")
    args = parser.parse_args(argv)

    if args.list or not args.only:
        for spec in sources.SOURCES:
            print("%-18s %-8s %s" % (spec["source_id"], spec.get("kind"), spec.get("name")))
        return 0

    wanted = {part.strip() for part in args.only.split(",") if part.strip()}
    known = {spec["source_id"] for spec in sources.SOURCES}
    unknown = sorted(wanted - known)
    if unknown:
        print("未知来源: %s" % ", ".join(unknown), file=sys.stderr)
        return 2

    cfg = rcfg.load()
    overrides = dict(cfg.get("sources") or {})
    for source_id in known:
        if source_id not in wanted:
            overrides[source_id] = {"enabled": False}
    cfg["sources"] = overrides

    store.init()
    conn = db.connect()
    try:
        summary = sources.collect(conn, cfg, force=args.force)
    finally:
        conn.close()

    print("启用 %d / 成功 %d / 失败 %d / 跳过 %d"
          % (summary["sources_enabled"], summary["sources_ok"],
             summary["sources_failed"], summary["sources_skipped"]))
    for row in summary.get("per_source", []):
        print("  %-18s raw=%-3d kept=%-3d new=%-3d"
              % (row["source_id"], row["raw"], row["kept"], row["new"]))
    for row in summary.get("geo_layers", []):
        print("  %-18s points=%-4d inserted=%-4d updated=%d"
              % (row["layer"], row["points"], row["inserted"], row["updated"]))
    for row in summary.get("failures", []):
        print("  FAIL %-16s %s" % (row["source_id"], row["error"][:120]))
    for row in summary.get("skipped", []):
        print("  SKIP %-16s %s" % (row["source_id"], row["reason"][:100]))
    return 0 if not summary["sources_failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())