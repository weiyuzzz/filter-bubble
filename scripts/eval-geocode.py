"""地理识别质量评估：用人工标注样本算精确率 / 召回率 / 各置信等级准确率。

样本文件：``tests/fixtures/geo-labelled-sample.tsv``，每行
``event_id<TAB>期望国家ISO2<TAB>期望主题<TAB>备注``，期望国家为 ``-`` 表示
"标题没有单一国家依据、应判为未知"。

评估口径（写进报告里，避免误解）：
* **标题级**：只对标题调用 ``geocode.resolve_title``，与人工标注直接可比；
* **生产级**：数据库里实际存的值（标题 + 成员条目标题），会额外命中标题里没有的地名，
  因此会略高于标题级，本报告同时给出两者。

用法：
    python scripts/eval-geocode.py                 # 打印摘要
    python scripts/eval-geocode.py --json out.json  # 另存明细
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.geo import geocode  # noqa: E402

SAMPLE = ROOT / "tests" / "fixtures" / "geo-labelled-sample.tsv"
VALIDATION = ROOT / "tests" / "fixtures" / "geo-validation-sample.tsv"
DB = ROOT / "data" / "filter-bubble.db"


def load_sample(path=None) -> list:
    target = Path(path) if path else SAMPLE
    rows = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        event_id, expected_country, expected_theme = parts[0], parts[1], parts[2]
        note = parts[3] if len(parts) > 3 else ""
        rows.append({"event_id": event_id.strip(),
                     "expected_country": "" if expected_country.strip() in ("-", "") else expected_country.strip().upper(),
                     "expected_theme": expected_theme.strip(),
                     "note": note.strip()})
    return rows


def load_auto() -> dict:
    if not DB.is_file():
        return {}
    conn = sqlite3.connect("file:%s?mode=ro" % DB.as_posix(), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, title, event_country_code, geo_confidence, geo_method, theme "
            "FROM radar_events").fetchall()
    finally:
        conn.close()
    return {row["id"]: dict(row) for row in rows}


def _score(pairs) -> dict:
    tp = sum(1 for auto, exp in pairs if auto and exp and auto == exp)
    fp = sum(1 for auto, exp in pairs if auto and (not exp or auto != exp))
    fn = sum(1 for auto, exp in pairs if not auto and exp)
    tn = sum(1 for auto, exp in pairs if not auto and not exp)
    precision = round(tp / (tp + fp), 4) if (tp + fp) else None
    recall = round(tp / (tp + fn), 4) if (tp + fn) else None
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": precision, "recall": recall}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="地理识别质量评估")
    parser.add_argument("--json", default="", help="把明细写入该 JSON 文件")
    parser.add_argument("--sample", default="", help="标注样本路径（默认调参集）")
    parser.add_argument("--validation", action="store_true", help="使用独立验证集")
    parser.add_argument("--theme-json", default="", help="输出主题混淆矩阵 JSON")
    args = parser.parse_args(argv)

    path = args.sample or (str(VALIDATION) if args.validation else str(SAMPLE))
    sample = load_sample(path)
    print("样本文件:", path)
    auto = load_auto()
    if not auto:
        print("找不到数据库或库为空：%s" % DB, file=sys.stderr)
        return 1

    details = []
    title_pairs, prod_pairs = [], []
    per_conf = {"high": [], "medium": [], "low": []}
    theme_ok = theme_total = 0
    failures = []
    confusion = {}

    THEME_SET = ["conflict", "unrest", "politics", "security", "cyber", "disaster",
                 "infrastructure", "technology", "general_news"]
    for name in THEME_SET:
        confusion[name] = {other: 0 for other in THEME_SET}

    for row in sample:
        record = auto.get(row["event_id"])
        if record is None:
            continue
        title = record.get("title") or ""
        expected = row["expected_country"]

        resolved = geocode.resolve_title(title)
        auto_title_cc = (resolved.get("country_code") or "").upper()
        auto_prod_cc = (record.get("event_country_code") or "").upper()

        title_pairs.append((auto_title_cc, expected))
        prod_pairs.append((auto_prod_cc, expected))
        confidence = (record.get("geo_confidence") or "none").lower()
        if confidence in per_conf:
            per_conf[confidence].append((auto_prod_cc, expected))

        expected_theme = row["expected_theme"]
        theme_total += 1
        expected_theme_key = row["expected_theme"] if row["expected_theme"] in confusion else ""
        auto_theme_key = record.get("theme") if record.get("theme") in confusion else ""
        if expected_theme_key and auto_theme_key:
            confusion[expected_theme_key][auto_theme_key] += 1
        if expected_theme and record.get("theme") == expected_theme:
            theme_ok += 1

        if expected and auto_prod_cc != expected:
            failures.append({
                "event_id": row["event_id"], "title": title[:60],
                "expected": expected, "auto": auto_prod_cc or "(未定位)",
                "confidence": confidence, "method": record.get("geo_method"),
                "reason": resolved.get("reason"), "note": row["note"],
            })
        elif auto_prod_cc and not expected:
            failures.append({
                "event_id": row["event_id"], "title": title[:60],
                "expected": "(应判未知)", "auto": auto_prod_cc,
                "confidence": confidence, "method": record.get("geo_method"),
                "reason": resolved.get("reason"), "note": row["note"],
            })

        details.append({
            "event_id": row["event_id"], "title": title,
            "expected_country": expected, "expected_theme": expected_theme,
            "auto_title_country": auto_title_cc, "auto_title_method": resolved.get("method"),
            "auto_title_confidence": resolved.get("confidence"),
            "auto_stored_country": auto_prod_cc,
            "auto_stored_confidence": record.get("geo_confidence"),
            "auto_stored_method": record.get("geo_method"),
            "auto_theme": record.get("theme"),
            "reason": resolved.get("reason"), "note": row["note"],
        })

    known = sum(1 for row in sample if row["expected_country"])
    report = {
        "sample_size": len(details),
        "labelled_with_country": known,
        "labelled_unknown": len(details) - known,
        "title_level": _score(title_pairs),
        "production_level": _score(prod_pairs),
        "per_confidence_production": {key: _score(value) for key, value in per_conf.items() if value},
        "theme_accuracy": round(theme_ok / theme_total, 4) if theme_total else None,
        "theme_total": theme_total,
        "theme_confusion": confusion,
        "details": details,
        "failures": failures,
    }

    print("样本 %d 条（有国家依据 %d 条，应判未知 %d 条）"
          % (report["sample_size"], known, report["sample_size"] - known))
    for level in ("title_level", "production_level"):
        block = report[level]
        print("%-16s 精确率=%s 召回率=%s (TP=%d FP=%d FN=%d TN=%d)"
              % (level, block["precision"], block["recall"],
                 block["tp"], block["fp"], block["fn"], block["tn"]))
    for key, block in report["per_confidence_production"].items():
        print("  置信度 %-7s 样本=%d 准确率=%s" % (key, block["tp"] + block["fp"], block["precision"]))
    print("主题准确率: %s (%d 条)" % (report["theme_accuracy"], report["theme_total"]))
    print("错误/偏差 %d 条" % len(failures))

    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        print("明细写入:", args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())