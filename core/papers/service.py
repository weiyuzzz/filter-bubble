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
import sys
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
_STORE_READY = False
_SWEEP_INTERVAL = float(os.environ.get("PAPERS_SWEEP_SECONDS", "300") or 300)


def log(message: str) -> None:
    """论文模块的关键事件写 stderr（stdout 被重定向后是块缓冲的，会看不见）。"""
    try:
        print("[papers] %s" % message, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


def ensure_store() -> None:
    """建表只做一次：以前每次请求都 executescript（含 PRAGMA journal_mode=WAL），
    在别的进程正在写时很容易撞出 unable to open database file。"""
    global _STORE_READY
    if _STORE_READY:
        return
    try:
        store.init()
        _STORE_READY = True
    except Exception as exc:  # noqa: BLE001
        log("store init failed: %s" % exc)


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
        # 论文每天更新一次（不影响雷达的每小时采集）
        "interval_hours": 24,
        "lookback_days": 3,
        "fetch_per_source": 30,
        "per_discipline_limit": 10,
        "sources": {"crossref": True, "pubmed": True, "europepmc": True,
                    "biorxiv": True, "medrxiv": True, "huggingface": True,
                    "openalex": False},
        "openalex_api_key_env": "OPENALEX_API_KEY",
        "crossref_mailto": "",
        # 论文模块不再单独限制 AI 次数，成本由全局 token 限额控制
        "ai": {"enabled": True, "concurrency": 2},
    }
    merged = dict(defaults)
    merged.update({k: v for k, v in papers.items() if k in defaults})
    merged["sources"] = {**defaults["sources"], **(papers.get("sources") or {})}
    merged["ai"] = {**defaults["ai"], **(papers.get("ai") or {})}
    return merged


# ---------------- 采集 ----------------

def _run_source(name: str, cfg: Dict[str, Any]
                ) -> Tuple[str, List[Dict[str, Any]], str, int, Dict[str, Any]]:
    block = _cfg_block(cfg)
    rows = int(block.get("fetch_per_source") or 30)
    days = int(block.get("lookback_days") or 3)
    adapter = sources.ADAPTERS.get(name)
    started = time.time()
    if not adapter:
        return name, [], "未知来源", 0, {}
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
        return (name, items or [], "", int((time.time() - started) * 1000),
                _fetch_meta())
    except Exception as exc:  # noqa: BLE001 - 单个来源失败不影响其他来源
        return (name, [], "%s: %s" % (type(exc).__name__, exc),
                int((time.time() - started) * 1000), _fetch_meta())


def _fetch_meta() -> Dict[str, Any]:
    """论文模块复用雷达的 HTTP 层，顺便拿真实 HTTP 状态给监控用。"""
    try:
        from ..radar import fetch as _fetch
        return _fetch.last_call() or {}
    except Exception:  # noqa: BLE001
        return {}


def collect(cfg: Dict[str, Any], trigger: str = "manual") -> Dict[str, Any]:
    """抓取所有启用的论文来源，分类、去重、入库。"""
    block = _cfg_block(cfg)
    enabled = [name for name, on in (block.get("sources") or {}).items() if on]
    conn = db.connect()
    store.init()
    started = time.time()
    summary: Dict[str, Any] = {"sources": {}, "fetched": 0, "inserted": 0, "merged": 0,
                               "failed": [], "skipped_sources": []}
    from .. import monitor

    mon = monitor.run("papers_collect", trigger=trigger)
    mon.start()
    try:
        _set("collecting papers", running=True, started_at=_now(), last_error=None)
        run_ids = {name: store.start_run(conn, name) for name in enabled}
        mon.set_counts(sources_attempted=len(enabled))

        results: List[Tuple[str, List[Dict[str, Any]], str, int, Dict[str, Any]]] = []
        with cf.ThreadPoolExecutor(max_workers=3) as pool:
            for result in pool.map(lambda n: _run_source(n, cfg), enabled):
                results.append(result)

        for name, items, error, duration, meta in results:
            with_abstract = sum(1 for i in items if (i.get("abstract") or "").strip())
            if error:
                store.finish_run(conn, run_ids[name], "failed", 0, 0, duration, error)
                summary["failed"].append({"source": name, "error": error})
                summary["sources"][name] = {"status": "failed", "fetched": 0,
                                            "with_abstract": 0, "error": error,
                                            "duration_ms": duration}
                mon.source_result(name, "failed", name=name, kind="paper",
                                  latency_ms=duration, http_status=meta.get("status") or None,
                                  fetched=0, parsed=0, error=error)
                mon.add_error("论文来源 %s 失败：%s" % (name, error), source_id=name,
                              stage="fetch",
                              details={"latency_ms": duration,
                                       "http_status": meta.get("status") or None,
                                       "url": meta.get("url") or ""})
                continue

            prepared = []
            for item in items:
                disciplines = classify.classify(
                    item.get("source", ""), item.get("title", ""), item.get("abstract", ""),
                    item.get("venue", ""), item.get("subjects") or [])
                item["disciplines"] = disciplines
                prepared.append(item)

            trace: List[Dict[str, Any]] = []
            counts = store.upsert_papers(conn, prepared, trace=trace)
            store.finish_run(conn, run_ids[name], "ok", len(items), with_abstract, duration)
            summary["fetched"] += len(items)
            summary["inserted"] += counts["inserted"]
            summary["merged"] += counts["merged"]
            summary["sources"][name] = {"status": "ok", "fetched": len(items),
                                        "with_abstract": with_abstract,
                                        "inserted": counts["inserted"],
                                        "merged": counts["merged"],
                                        "duration_ms": duration, "error": ""}
            mon.source_result(name, "ok" if items else "empty", name=name, kind="paper",
                              latency_ms=duration, http_status=meta.get("status") or None,
                              fetched=len(items), parsed=len(items),
                              items_new=counts["inserted"],
                              items_duplicate=counts["merged"],
                              items_filtered=counts["skipped"],
                              reason=None if items else "本次返回 0 篇（不等于失败）",
                              detail={"with_abstract": with_abstract,
                                      "url": meta.get("url") or ""})
            mon.items([{
                "source_id": name, "kind": "paper", "title": entry.get("title"),
                "url": entry.get("url"), "published_at": entry.get("published_at"),
                "collected_at": _now(), "item_id": entry.get("id"),
                "action": {"new": "new", "merged": "duplicate", "skipped": "filtered"}[
                    entry.get("action") or "new"],
                "reason": entry.get("reason"), "detail": {"paper_action": entry.get("action"),
                                                          "dedupe_key": entry.get("id")},
            } for entry in trace])

        summary["duration_ms"] = int((time.time() - started) * 1000)
        summary["stats"] = store.stats(conn)
        failed = summary["failed"]
        mon.stage("fetch", "ok" if not failed else ("partial" if summary["fetched"] else "failed"),
                  {"sources": len(enabled), "sources_failed": len(failed),
                   "items_fetched": summary["fetched"],
                   "note": "每个来源的真实耗时与 HTTP 状态见来源结果"})
        mon.stage("normalize", "ok" if summary["fetched"] else "not_applicable",
                  {"note": "学科分类（classify）在抓取后立即执行，与解析同一阶段"})
        mon.stage("persist", "ok" if summary["fetched"] else "not_applicable",
                  {"items_new": summary["inserted"], "items_duplicate": summary["merged"],
                   "note": "去重键：DOI → PMID → arXiv ID → 规范化标题"})
        mon.set_counts(items_fetched=summary["fetched"], items_new=summary["inserted"],
                       items_duplicate=summary["merged"])
        mon.finish(status="ok" if not failed else ("partial" if summary["fetched"] else "failed"),
                   stats={"trigger_detail": summary.get("sources"),
                          "duration_ms": summary["duration_ms"]})
        _set("idle", running=False, finished_at=_now(), last_result=summary)
    except Exception as exc:  # noqa: BLE001
        summary["error"] = "%s: %s" % (type(exc).__name__, exc)
        mon.add_error(summary["error"], stage="persist")
        mon.finish(status="failed", error=summary["error"])
        _set("idle", running=False, finished_at=_now(), last_error=summary["error"])
    finally:
        conn.close()
    return summary


def collect_async(cfg: Dict[str, Any], trigger: str = "api") -> bool:
    if get_state().get("running"):
        return False
    threading.Thread(target=collect, args=(cfg, trigger), name="papers-collect",
                     daemon=True).start()
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
    from .. import monitor

    mon = monitor.run("summary", trigger="queue", run_id=None,
                      stats={"paper_id": paper_id,
                             "note": "论文 AI 概况：一次真实模型调用记一条运行"})
    mon.start()
    try:
        paper = store.get_paper(conn, paper_id)
        if not paper:
            mon.log("WARNING", "论文已不存在，跳过概况生成：%s" % paper_id, stage="summary")
            mon.finish(status="skipped", stats={"reason": "paper not found"})
            return
        mon.stats["title"] = paper.get("title")
        mon.stats["source"] = paper.get("source")
        mon.set_counts(items_fetched=1)
        try:
            store.save_overview(conn, paper_id, "pending", None, "", paper_ai.PROMPT_VERSION,
                                content_hash_value=store.content_hash(paper))
            with mon.phase("summary") as phase:
                overview, error, model = paper_ai.analyze(paper, settings)
                phase.result = {"model": model, "has_overview": bool(overview),
                                "paper_id": paper_id}
            if overview:
                store.save_overview(conn, paper_id, "done", overview, model,
                                    paper_ai.PROMPT_VERSION,
                                    content_hash_value=store.content_hash(paper))
                mon.finish(status="ok", stats={"model": model})
            else:
                store.save_overview(conn, paper_id, "failed", None, model,
                                    paper_ai.PROMPT_VERSION, error=error or "未知错误",
                                    content_hash_value=store.content_hash(paper))
                log("分析失败 %s: %s" % (paper_id[:40], error or "未知错误"))
                mon.add_error(error or "未知错误", stage="summary", details={"paper_id": paper_id,
                                                                             "model": model})
                mon.finish(status="failed", error=error or "未知错误")
        except Exception as exc:  # noqa: BLE001 - 单篇失败不能拖死队列
            detail = "%s: %s" % (type(exc).__name__, exc)
            log("分析异常 %s: %s" % (paper_id[:40], detail))
            try:
                store.save_overview(conn, paper_id, "failed", None, "", paper_ai.PROMPT_VERSION,
                                    error=detail, content_hash_value=store.content_hash(paper))
            except Exception:  # noqa: BLE001 - 连失败也写不进去就只记日志
                pass
            mon.add_error(detail, stage="summary", details={"paper_id": paper_id})
            mon.finish(status="failed", error=detail)
    finally:
        conn.close()
        mon.finish(status=mon.status if mon.status != "running" else "interrupted")


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
        sweeper = threading.Thread(target=_sweeper, args=(stop,), daemon=True,
                                   name="papers-ai-sweeper")
        sweeper.start()
        _WORKERS.append(sweeper)


def _sweeper(stop: threading.Event) -> None:
    """定时补齐待分析队列。

    以前只有「每天采集一次」之后才会入队，一旦那次入队失败（或应用重启、队列丢在内存里），
    论文就永远停在「已排队，等待分析…」。现在每 5 分钟自己扫一遍，自己会好。
    """
    first = True
    while not stop.is_set():
        delay = 20.0 if first else _SWEEP_INTERVAL
        first = False
        if stop.wait(delay):
            break
        try:
            cfg = _current_cfg()
            block = _cfg_block(cfg)
            if not block.get("enabled", True) or not (block.get("ai") or {}).get("enabled", True):
                continue
            queued = analyze_pending(cfg, limit=120)
            if queued:
                log("补队列：%d 篇待分析（队列 %d）" % (queued, queue_size()))
        except Exception as exc:  # noqa: BLE001 - 巡检线程不能死
            log("sweeper error: %s: %s" % (type(exc).__name__, exc))


def enqueue(papers: List[Dict[str, Any]], limit: int) -> int:
    added = 0
    for paper in papers:
        if added >= limit:
            break
        _QUEUE.put(paper["id"])
        added += 1
    return added


def pending_count(limit: int = 5000) -> int:
    def _read() -> int:
        conn = db.connect()
        try:
            ensure_store()
            return len(store.pending_papers(conn, limit, paper_ai.PROMPT_VERSION))
        finally:
            conn.close()

    try:
        return db.retry(_read)
    except Exception as exc:  # noqa: BLE001
        log("pending_count failed: %s: %s" % (type(exc).__name__, exc))
        return -1


def analyze_pending(cfg: Dict[str, Any], limit: Optional[int] = None) -> int:
    """把所有待分析论文补进队列。

    不再做「每次 / 每天多少篇」的限制：成本统一由全局 token 限额控制
    （主接口 token 用尽切备用，两个都用尽才暂停整个 AI）。
    """
    block = _cfg_block(cfg)
    ai_cfg = block.get("ai") or {}
    if not ai_cfg.get("enabled", True):
        return 0

    def _read() -> List[Dict[str, Any]]:
        conn = db.connect()
        try:
            ensure_store()
            return store.pending_papers(conn, int(limit or 500), paper_ai.PROMPT_VERSION)
        finally:
            conn.close()

    try:
        papers = db.retry(_read)
    except Exception as exc:  # noqa: BLE001 - 入队失败要留下痕迹，不能静默
        log("analyze_pending failed: %s: %s" % (type(exc).__name__, exc))
        return 0
    return enqueue(papers, len(papers))


def retry_one(paper_id: str) -> bool:
    """手动重试单篇：直接入队，成本由全局 token 限额约束。"""
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
    ai_cfg = block.get("ai") or {}
    conn = db.connect()
    try:
        ensure_store()
        runs = store.latest_runs(conn)
        stats = store.stats(conn)
        counts = store.discipline_counts(conn)
        last_run = conn.execute("SELECT MAX(finished_at) t FROM paper_source_runs").fetchone()["t"]
        pending = len(store.pending_papers(conn, 5000, paper_ai.PROMPT_VERSION))
    except Exception as exc:  # noqa: BLE001
        log("status failed: %s: %s" % (type(exc).__name__, exc))
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        conn = db.connect()  # 换一条连接再试一次（CANTOPEN 往往是瞬时的）
        try:
            runs = store.latest_runs(conn)
            stats = store.stats(conn)
            counts = store.discipline_counts(conn)
            last_run = conn.execute("SELECT MAX(finished_at) t FROM paper_source_runs").fetchone()["t"]
            pending = len(store.pending_papers(conn, 5000, paper_ai.PROMPT_VERSION))
            log("status recovered on retry")
        except Exception as exc2:  # noqa: BLE001
            log("status retry failed: %s: %s" % (type(exc2).__name__, exc2))
            runs, stats, counts, last_run, pending = [], {}, {}, "", -1
    finally:
        conn.close()
    # AI 成本由全局 token 限额控制，这里只回报限额本身，方便页面提示
    try:
        from .. import ai_client, ai_settings
        usage = ai_client.status(ai_settings.load()).get("usage") or {}
        ai_state = ai_client.status(ai_settings.load())
    except Exception:  # noqa: BLE001
        usage, ai_state = {}, {}
    return {
        "enabled": bool(block.get("enabled", True)),
        "state": get_state(),
        "queue": queue_size(),
        "pending": pending,          # 还没出概况、也没排队的篇数（界面显示「已排队」的就是这些）
        "schedule": {"daily_time": block.get("daily_time"),
                     "interval_hours": block.get("interval_hours"),
                     "lookback_days": block.get("lookback_days"),
                     "last_run_at": last_run or ""},
        "limits": {"per_discipline_limit": block.get("per_discipline_limit"),
                   "fetch_per_source": block.get("fetch_per_source")},
        "ai": {"enabled": bool(ai_cfg.get("enabled", True)),
               "paused": bool(usage.get("paused")),
               "paused_until": usage.get("paused_until") or "",
               "available": bool(ai_state.get("available")),
               "reason": ai_state.get("reason") or "",
               "active_slot": ai_state.get("active_slot") or "",
               "last_error": ai_state.get("last_error") or "",
               "warnings": ai_state.get("warnings") or []},
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
    """论文每天只跑一次：到点采集，然后按每日额度补 AI 队列。

    频率由 ``papers.interval_hours``（默认 24）控制，与雷达的每小时采集完全独立；
    这样既不会反复抓取，也不会反复调用模型。失败不影响其他模块。
    """
    log("scheduler started")
    while not stop.wait(60):
        try:
            cfg = _current_cfg()
            block = _cfg_block(cfg)
            if not block.get("enabled", True):
                continue
            now = dt.datetime.now().astimezone()
            daily_time = str(block.get("daily_time") or "08:30")
            interval_hours = float(block.get("interval_hours") or 24)
            if now.strftime("%H:%M") < daily_time:
                continue
            conn = db.connect()
            try:
                ensure_store()
                last = conn.execute("SELECT MAX(finished_at) t FROM paper_source_runs").fetchone()["t"]
            finally:
                conn.close()
            if _minutes_since(last) < interval_hours * 60:
                continue
            if get_state().get("running"):
                continue
            log("daily collect at %s" % now.isoformat(timespec="seconds"))
            summary = collect(cfg, trigger="schedule")
            _set("idle", last_result=summary)
            queued = analyze_pending(cfg)
            if queued:
                log("采集后入队 %d 篇待分析" % queued)
        except Exception as exc:  # noqa: BLE001 - 调度线程不能死
            log("scheduler error: %s: %s" % (type(exc).__name__, exc))


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
