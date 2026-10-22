"""数据源实测：真实请求每个论文来源，统计字段完整率、耗时与错误。

可以用两种方式运行：
* CLI：``python app.py radar papers test-sources``
* 页面：论文页 → 数据源 → 「测试数据源」，走 ``POST /api/radar/papers/test-sources``

只统计真实响应，不构造演示数据。
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime
from typing import Any, Dict, List

from . import classify, sources, store
from .service import _cfg_block


def _timed(fn, *args, **kwargs) -> Dict[str, Any]:
    started = time.time()
    try:
        items = fn(*args, **kwargs) or []
        error = ""
    except Exception as exc:  # noqa: BLE001
        items = []
        error = "%s: %s" % (type(exc).__name__, exc)
    elapsed = int((time.time() - started) * 1000)

    with_title = sum(1 for i in items if (i.get("title") or "").strip())
    with_abstract = sum(1 for i in items if (i.get("abstract") or "").strip())
    with_date = sum(1 for i in items if (i.get("published_at") or "").strip())
    with_link = sum(1 for i in items if (i.get("url") or "").strip())
    keys = [store.paper_key(i) for i in items]
    duplicates = len(keys) - len(set(keys))

    disciplines: Dict[str, int] = {}
    for item in items:
        for key in classify.classify(item.get("source", ""), item.get("title", ""),
                                     item.get("abstract", ""), item.get("venue", ""),
                                     item.get("subjects") or []):
            disciplines[key] = disciplines.get(key, 0) + 1

    sample = next((i for i in items if (i.get("abstract") or "").strip()), None)
    return {
        "records": len(items),
        "with_title": with_title,
        "with_abstract": with_abstract,
        "abstract_ratio": round(with_abstract / len(items), 3) if items else 0.0,
        "with_date": with_date,
        "with_link": with_link,
        "duplicates": duplicates,
        "duration_ms": elapsed,
        "error": error,
        "disciplines": disciplines,
        "sample": {
            "title": (sample or {}).get("title", "")[:120],
            "abstract": (sample or {}).get("abstract", "")[:200],
            "published_at": (sample or {}).get("published_at", ""),
            "url": (sample or {}).get("url", ""),
        } if sample else None,
    }


def run(cfg: Dict[str, Any], only: str = "") -> Dict[str, Any]:
    block = _cfg_block(cfg)
    rows = int(block.get("fetch_per_source") or 30)
    days = int(block.get("lookback_days") or 3)
    mailto = block.get("crossref_mailto") or ""
    key = os.environ.get(block.get("openalex_api_key_env") or "OPENALEX_API_KEY", "")

    calls = {
        "crossref": lambda: sources.crossref_fetch(sources.CROSSREF_TOPICS, rows, days, mailto,
                                                   max(3, rows // len(sources.CROSSREF_TOPICS))),
        "pubmed": lambda: sources.pubmed_fetch(rows, days),
        "europepmc": lambda: sources.europepmc_fetch(rows, days),
        "biorxiv": lambda: sources.biorxiv_fetch(rows, days),
        "medrxiv": lambda: sources.medrxiv_fetch(rows, days),
        "huggingface": lambda: sources.huggingface_fetch(rows, days),
        "openalex": lambda: sources.openalex_fetch(rows, days, "", key),
    }
    urls = {
        "crossref": "https://api.crossref.org/works?filter=has-abstract:true,from-pub-date:%s" % (
            date.today().isoformat()),
        "pubmed": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed",
        "europepmc": "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
        "biorxiv": "https://api.biorxiv.org/details/biorxiv/<start>/<end>",
        "medrxiv": "https://api.medrxiv.org/details/medrxiv/<start>/<end>",
        "huggingface": "https://huggingface.co/api/daily_papers",
        "openalex": "https://api.openalex.org/works?filter=has_abstract:true",
    }

    names = [only] if only else list(calls)
    results: List[Dict[str, Any]] = []
    for name in names:
        if name not in calls:
            continue
        data = _timed(calls[name])
        entry = {
            "source": name,
            "tested_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "url": urls.get(name, ""),
            "usable": bool(data["records"]) and data["abstract_ratio"] > 0 and not data["error"],
            **data,
        }
        if name == "openalex" and not key and "429" in (data["error"] or ""):
            entry["hint"] = "免费共享预算耗尽。设置环境变量 OPENALEX_API_KEY 可获得独立额度后重试。"
        if not entry["usable"] and not entry["error"]:
            entry["hint"] = entry.get("hint") or "没有拿到同时具备标题与摘要的记录"
        results.append(entry)

    return {
        "tested_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "lookback_days": days,
        "fetch_per_source": rows,
        "results": results,
        "usable": [r["source"] for r in results if r["usable"]],
        "unusable": [r["source"] for r in results if not r["usable"]],
    }


def format_text(report: Dict[str, Any]) -> str:
    lines = ["论文数据源实测  %s" % report.get("tested_at", ""),
             "回溯 %s 天 · 每源最多 %s 条" % (report.get("lookback_days"), report.get("fetch_per_source")),
             "-" * 78]
    for item in report.get("results") or []:
        flag = "✅ 可用" if item["usable"] else "❌ 不可用"
        lines.append("%s  %-12s %s" % (flag, item["source"], item["url"][:70]))
        if item.get("error"):
            lines.append("      错误：%s" % item["error"][:160])
        else:
            lines.append("      记录 %d | 标题 %d | 摘要 %d (%.0f%%) | 日期 %d | 链接 %d | 重复 %d | %dms"
                         % (item["records"], item["with_title"], item["with_abstract"],
                            item["abstract_ratio"] * 100, item["with_date"], item["with_link"],
                            item["duplicates"], item["duration_ms"]))
        if item.get("disciplines"):
            lines.append("      学科命中：%s" % item["disciplines"])
        if item.get("hint"):
            lines.append("      建议：%s" % item["hint"])
        if item.get("sample"):
            lines.append("      样例：%s" % item["sample"]["title"][:80])
    lines.append("-" * 78)
    lines.append("可用：%s" % (", ".join(report.get("usable") or []) or "无"))
    lines.append("不可用：%s" % (", ".join(report.get("unusable") or []) or "无"))
    return "\n".join(lines)
