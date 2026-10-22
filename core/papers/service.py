"""论文模块编排：采集 → 分类 → 入库 → AI 概况队列 → 查询。

复用现有能力，不另起服务：
* HTTP 走 ``core.radar.fetch``（含速率限制与重试）；
* 大模型走全局 ``core.ai_client``（与 GitHub 解读、雷达摘要同一套配置）；
* 数据库走 ``core.db``；
* 调度在 ``app.serve()`` 里与雷达调度并列启动，互不影响。
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import os
import queue
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .. import ai_settings, db
from . import ai as paper_ai
from . import classify, sources, store

_QUEUE: "queue.Queue[str]" = queue.Queue()
_WORKERS: List[threading.Thread] = []
_WORKER_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {"running": False, "stage": "idle", "started_at": None,
                          "finished_at": None, "last_error": None, "last_result": None}


def _set(stage: str, **kwargs: Any) -> None:
    with _STATE_LOCK:
        _STATE["stage"] = stage
        _STATE.update(kwargs)


def get_state() -> Dict[str, Any]:
    with _STATE_LOCK:
        return dict(_STATE)


def _cfg_block(cfg: Dict[str, Any]) -> Dict[str, Any]:
    papers = cfg.get("papers") or {}
    defaults = {
        "enabled": True,
        "daily_time": "08:30",
        "lookback_days": 3,
        "fetch_per_source": 30,
        "per_discipline_limit": 10,
        "sources": {"crossref": True, "pubmed": True, "europepmc": True,
                    "biorxiv": True, "medrxiv": True, "huggingface": True,
                    "openalex": False},
        "openalex_api_key_env": "OPENALEX_API_KEY",
        "crossref_mailto": "",
        "ai": {"enabled": True, "max_per_run": 24, "concurrency": 2},
    }
    merged = dict(defaults)
    merged.update({k: v for k, v in papers.items() if k in defaults})
    merged["sources"] = {**defaults["sources"], **(papers.get("sources") or {})}
    merged["ai"] = {**defaults["ai"], **(papers.get("ai") or {})}
    return merged


# ---------------- 采集 ----------------

def _run_source(name: str, cfg: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]], str, int]:
    block = _cfg_block(cfg)
    rows = int(block.get("fetch_per_source") or 30)
    days = int(block.get("lookback_days") or 3)
    adapter = sources.ADAPTERS.get(name)
    started = time.time()
    if not adapter:
        return name, [], "未知来源", 0
    try:
        if name == "crossref":
            per_topic = max(4, rows // max(1, len(sources.CROSSREF_TOPICS)))
            items = adapter(sources.CROSSREF_TOPICS, rows, days,
                            block.get("crossref_mailto") or "", per_topic)
        elif name == "pubmed":
            items = adapter(rows, days)
        elif name == "europepmc":
            items = adapter(rows, days)
        elif name in ("biorxiv", "medrxiv", "huggingface"):
            items = adapter(rows, days)
        elif name == "openalex":
            key = os.environ.get(block.get("openalex_api_key_env") or "OPENALEX_API_KEY", "")
            items = adapter(rows, days, "", key)
        else:
            items = adapter(rows, days)
        return name, items or [], "", int((time.time() - started) * 1000)
    except Exception as exc:  # noqa: BLE001 - 单个来源失败不影响其他来源
        return name, [], "%s: %s" % (type(exc).__name__, exc), int((time.time() - started) * 1000)


def collect(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """抓取所有启用的论文来源，分类、去重、入库。"""
    block = _cfg_block(cfg)
    enabled = [name for name, on in (block.get("sources") or {}).items() if on]
    conn = db.connect()
    store.init()
    started = time.time()
    summary: Dict[str, Any] = {"sources": {}, "fetched": 0, "inserted": 0, "merged": 0,
                               "failed": [], "skipped_sources": []}
    try:
        _set("collecting papers", running=True, started_at=_now(), last_error=None)
        run_ids = {name: store.start_run(conn, name) for name in enabled}

        results: List[Tuple[str, List[Dict[str, Any]], str, int]] = []
        with cf.ThreadPoolExecutor(max_workers=3) as pool:
            for result in pool.map(lambda n: _run_source(n, cfg), enabled):
                results.append(result)

        for name, items, error, duration in results:
            with_abstract = sum(1 for i in items if (i.get("abstract") or "").strip())
            if error:
                store.finish_run(conn, run_ids[name], "failed", 0, 0, duration, error)
                summary["failed"].append({"source": name, "error": error})
                summary["sources"][name] = {"status": "failed", "fetched": 0,
                                            "with_abstract": 0, "error": error,
                                            "duration_ms": duration}
                continue

            prepared = []
            for item in items:
                disciplines = classify.classify(
                    item.get("source", ""), item.get("title", ""), item.get("abstract", ""),
                    item.get("venue", ""), item.get("subjects") or [])
                item["disciplines"] = disciplines
                prepared.append(item)

            counts = store.upsert_papers(conn, prepared)
            store.finish_run(conn, run_ids[name], "ok", len(items), with_abstract, duration)
            summary["fetched"] += len(items)
            summary["inserted"] += counts["inserted"]
            summary["merged"] += counts["merged"]
            summary["sources"][name] = {"status": "ok", "fetched": len(items),
                                        "with_abstract": with_abstract,
                                        "inserted": counts["inserted"],
                                        "merged": counts["merged"],
                                        "duration_ms": duration, "error": ""}

        summary["duration_ms"] = int((time.time() - started) * 1000)
        summary["stats"] = store.stats(conn)
        _set("idle", running=False, finished_at=_now(), last_result=summary)
    except Exception as exc:  # noqa: BLE001
        summary["error"] = "%s: %s" % (type(exc).__name__, exc)
        _set("idle", running=False, finished_at=_now(), last_error=summary["error"])
    finally:
        conn.close()
    return summary


def collect_async(cfg: Dict[str, Any]) -> bool:
    if get_state().get("running"):
        return False
    threading.Thread(target=collect, args=(cfg,), name="papers-collect", daemon=True).start()
    return True


# ---------------- AI 概况队列 ----------------

def _worker(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            paper_id = _QUEUE.get(timeout=1.0)
        except queue.Empty:
            continue
        if paper_id is None:
            _QUEUE.task_done()
            break
        try:
            _analyze_one(paper_id)
        except Exception as exc:  # noqa: BLE001 - 队列线程不能死
            print("[papers] AI worker error:", exc)
        finally:
            _QUEUE.task_done()


def _analyze_one(paper_id: str) -> None:
    settings = ai_settings.load()
    conn = db.connect()
    try:
        paper = store.get_paper(conn, paper_id)
        if not paper:
            return
        store.save_overview(conn, paper_id, "pending", None, "", paper_ai.PROMPT_VERSION,
                            content_hash_value=store.content_hash(paper))
        overview, error, model = paper_ai.analyze(paper, settings)
        if overview:
            store.save_overview(conn, paper_id, "done", overview, model,
                                paper_ai.PROMPT_VERSION,
                                content_hash_value=store.content_hash(paper))
        else:
            store.save_overview(conn, paper_id, "failed", None, model,
                                paper_ai.PROMPT_VERSION, error=error or "未知错误",
                                content_hash_value=store.content_hash(paper))
    finally:
        conn.close()


def start_worker(stop: threading.Event, concurrency: int = 2) -> None:
    global _WORKERS
    with _WORKER_LOCK:
        alive = [t for t in _WORKERS if t.is_alive()]
        if alive:
            return
        _WORKERS = []
        for idx in range(max(1, min(4, concurrency))):
            thread = threading.Thread(target=_worker, args=(stop,), daemon=True,
                                      name="papers-ai-%d" % idx)
            thread.start()
            _WORKERS.append(thread)


def enqueue(papers: List[Dict[str, Any]], limit: int) -> int:
    added = 0
    for paper in papers:
        if added >= limit:
            break
        _QUEUE.put(paper["id"])
        added += 1
    return added


def analyze_pending(cfg: Dict[str, Any], limit: Optional[int] = None) -> int:
    block = _cfg_block(cfg)
    ai_cfg = block.get("ai") or {}
    if not ai_cfg.get("enabled", True):
        return 0
    cap = int(limit if limit is not None else ai_cfg.get("max_per_run") or 24)
    conn = db.connect()
    try:
        papers = store.pending_papers(conn, cap, paper_ai.PROMPT_VERSION)
    finally:
        conn.close()
    return enqueue(papers, cap)


def retry_one(paper_id: str) -> bool:
    conn = db.connect()
    try:
        paper = store.get_paper(conn, paper_id)
        if not paper:
            return False
        store.save_overview(conn, paper_id, "pending", None, "", paper_ai.PROMPT_VERSION,
                            content_hash_value=store.content_hash(paper))
    finally:
        conn.close()
    _QUEUE.put(paper_id)
    return True


def queue_size() -> int:
    return _QUEUE.qsize()


# ---------------- 查询 ----------------

def list_papers(discipline: str, limit: int, offset: int = 0) -> Dict[str, Any]:
    key = classify.normalize_discipline(discipline)
    conn = db.connect()
    try:
        papers = store.list_papers(conn, key, limit, offset)
        counts = store.discipline_counts(conn)
        for paper in papers:
            overview = store.get_overview(conn, paper["id"])
            paper["ai"] = {
                "status": (overview or {}).get("status") or "pending",
                "overview": (overview or {}).get("overview"),
                "model": (overview or {}).get("model") or "",
                "error": (overview or {}).get("error") or "",
                "updated_at": (overview or {}).get("updated_at") or "",
            }
            if paper["ai"]["status"] == "pending" and overview is None:
                paper["ai"]["status"] = "queued"
            paper["discipline_labels"] = classify.describe(paper)
        return {"papers": papers, "counts": counts, "discipline": key,
                "label": classify.label_for(key), "total": counts.get(key, 0)}
    finally:
        conn.close()


def status(cfg: Dict[str, Any]) -> Dict[str, Any]:
    block = _cfg_block(cfg)
    conn = db.connect()
    try:
        store.init()
        runs = store.latest_runs(conn)
        stats = store.stats(conn)
        counts = store.discipline_counts(conn)
    finally:
        conn.close()
    return {
        "enabled": bool(block.get("enabled", True)),
        "state": get_state(),
        "queue": queue_size(),
        "schedule": {"daily_time": block.get("daily_time"), "lookback_days": block.get("lookback_days")},
        "limits": {"per_discipline_limit": block.get("per_discipline_limit"),
                   "fetch_per_source": block.get("fetch_per_source"),
                   "ai_max_per_run": (block.get("ai") or {}).get("max_per_run")},
        "stats": stats,
        "counts": counts,
        "sources": runs,
        "sources_enabled": [n for n, on in (block.get("sources") or {}).items() if on],
        "disciplines": classify.DISCIPLINES,
    }


# ---------------- 调度 ----------------

def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _minutes_since(iso: Any) -> float:
    if not iso:
        return 1e9
    try:
        then = dt.datetime.fromisoformat(str(iso))
        return (dt.datetime.now(dt.timezone.utc) - then).total_seconds() / 60.0
    except (TypeError, ValueError):
        return 1e9


def scheduler_loop(stop: threading.Event) -> None:
    """每天到点采集一次论文，并把待分析队列补满。失败不影响其他模块。"""
    print("[papers] scheduler started")
    while not stop.wait(60):
        try:
            cfg = _current_cfg()
            block = _cfg_block(cfg)
            if not block.get("enabled", True):
                continue
            now = dt.datetime.now().astimezone()
            daily_time = str(block.get("daily_time") or "08:30")
            if now.strftime("%H:%M") < daily_time:
                continue
            conn = db.connect()
            try:
                store.init()
                last = conn.execute("SELECT MAX(finished_at) t FROM paper_source_runs").fetchone()["t"]
            finally:
                conn.close()
            if _minutes_since(last) < 60 * 12:
                continue
            if get_state().get("running"):
                continue
            print("[papers] daily collect at", now.isoformat(timespec="seconds"))
            summary = collect(cfg)
            _set("idle", last_result=summary)
            analyze_pending(cfg)
        except Exception as exc:  # noqa: BLE001 - 调度线程不能死
            print("[papers] scheduler error:", exc)


def _current_cfg() -> Dict[str, Any]:
    try:
        from ..radar import config as radar_config
        return radar_config.cached()
    except Exception:  # noqa: BLE001
        return {}


def start_scheduler(stop: threading.Event) -> Optional[threading.Thread]:
    thread = threading.Thread(target=scheduler_loop, args=(stop,), daemon=True,
                              name="papers-scheduler")
    thread.start()
    return thread
