"""真实流水线埋点：把一次真实执行写成监控记录。

原则
----
* **只记录真的发生过的事**：每次运行、每个阶段、每个来源的返回、每条条目的处理结果、
  每条诊断日志，都来自真实执行路径上的一次真实调用。
* **监控永远不能改变业务行为**：所有写入都包在 ``_safe`` 里，失败只打一行 stderr，
  绝不向调用方抛异常；埋点用的是独立连接，不参与业务事务的提交/回滚。
* **没执行 ≠ 成功**：阶段只在自己真的跑过时写记录；没跑的阶段由
  ``store.compose_stages`` 补成 skipped / not_applicable / pending。

用法::

    with monitor.run("event_report", trigger="schedule") as mon:
        with mon.phase("fetch") as ph:
            summary = sources.collect(conn, cfg, run=mon)
            ph.result = {"sources": summary["sources_ok"]}
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import threading
import time
import traceback
import uuid
from typing import Any, Callable, Dict, List, Optional

from .. import db
from . import redact, store

_LOCAL = threading.local()

#: 单次运行最多逐条记录的条目数（防止一次运行写爆监控表）；超出部分在 stats 里说明
MAX_ITEMS_PER_RUN = store.MAX_ITEMS_PER_RUN

_RETRYABLE = ("database is locked", "database table is locked",
              "unable to open database file")

#: 监控连接等的写锁时间上限。比业务连接（15s）短得多：宁可少记一条监控，
#: 也不要让采集流水线为了等监控写入而变慢。
MONITOR_BUSY_MS = 400

#: 写不进去（写锁被别人占着）时的返回值标记；调用方据此停止继续尝试
DROPPED = object()

#: 独立日志（log_event）遇到写锁占用后的冷却截止时间，避免反复空等
_LOG_COOLDOWN_UNTIL = 0.0


def monitor_connect() -> sqlite3.Connection:
    conn = db.connect()
    conn.execute("PRAGMA busy_timeout=%d" % MONITOR_BUSY_MS)
    return conn


def current() -> Optional["Run"]:
    """当前线程正在进行的监控运行（没有就返回 None）。"""
    return getattr(_LOCAL, "run", None)


def stack() -> List["Run"]:
    return list(getattr(_LOCAL, "stack", []) or [])


def new_run_id(task: str) -> str:
    return "%s-%s-%s" % (task.replace("_", "")[:8], dt.datetime.now().strftime("%H%M%S"),
                         uuid.uuid4().hex[:6])


def _safe(label: str, fn: Callable[..., Any], *args: Any,
          on_retry: Optional[Callable[[], None]] = None, attempts: int = 2, **kwargs: Any):
    """执行一次监控写入；任何异常都只记 stderr，不影响业务。

    采集与监控用的是同一个 SQLite 文件，写锁偶尔会撞上（WAL 下 ``database is locked``）。
    这里对锁冲突做**有界**重试（最多 ``attempts`` 次，每次最多等 ``MONITOR_BUSY_MS``），
    并在重试前回滚，避免半截事务被重复写入。

    为什么不等到拿到锁为止：监控是旁路，绝不能为了补一条监控记录把采集主流程拖慢。
    等不到就放弃这一条（stderr 留一行），业务数据一条都不会少。
    """
    last: Optional[Exception] = None
    for attempt in range(max(1, attempts)):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            last = exc
            if not any(marker in str(exc).lower() for marker in _RETRYABLE):
                break
            if on_retry is not None:
                try:
                    on_retry()
                except Exception:  # noqa: BLE001
                    pass
            if attempt < attempts - 1:
                time.sleep(0.15 * (attempt + 1))
        except Exception as exc:  # noqa: BLE001 - 监控是旁路，绝不能影响主流程
            print("[monitor] %s failed: %s: %s" % (label, type(exc).__name__, exc))
            if os.environ.get("MONITOR_DEBUG"):
                traceback.print_exc()
            return None
    print("[monitor] %s skipped（数据库写锁被占用，放弃这条监控记录）: %s" % (label, last))
    if os.environ.get("MONITOR_DEBUG"):
        traceback.print_exc()
    return DROPPED


# ---------------------------------------------------------------- 阶段

class Stage:
    """一个阶段的计时与结果容器（上下文管理器）。"""

    def __init__(self, run: "Run", name: str, status: str = "ok",
                 result: Optional[Dict[str, Any]] = None):
        self.run = run
        self.name = name
        self.status = status
        self.result: Dict[str, Any] = dict(result or {})
        self.error: Optional[str] = None
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.duration_ms: Optional[int] = None
        self._t0: Optional[dt.datetime] = None

    def __enter__(self) -> "Stage":
        self._t0 = store.now()
        self.started_at = self._t0.isoformat(timespec="seconds")
        self.run._write("stage.start", self.run._write_stage, self, "running")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.finished_at = store.now().isoformat(timespec="seconds")
        if self._t0 is not None:
            self.duration_ms = int((store.now() - self._t0).total_seconds() * 1000)
        if exc is not None:
            self.status = "failed"
            self.error = redact.redact_text("%s: %s" % (exc_type.__name__, exc))
        self.run._write("stage.finish", self.run._write_stage, self, self.status)
        return False  # 绝不吞异常

    # 链式配置
    def note(self, text: str, **extra: Any) -> "Stage":
        self.result["note"] = text
        self.result.update(extra)
        return self

    def mark(self, status: str) -> "Stage":
        self.status = status
        return self


# ---------------------------------------------------------------- 运行

class Run:
    """一次真实任务运行。任务侧只调用这里的几个方法，不需要关心 SQL。"""

    def __init__(self, task: str, trigger: str = "unknown", run_id: Optional[str] = None,
                 stats: Optional[Dict[str, Any]] = None, origin: str = "live"):
        if task not in store.TASKS:
            raise ValueError("unknown monitor task: %s" % task)
        self.task = task
        self.trigger = trigger or "unknown"
        self.run_id = run_id or new_run_id(task)
        self.stats: Dict[str, Any] = dict(stats or {})
        self.origin = origin
        self.status = "running"
        self.error: Optional[str] = None
        self.counts: Dict[str, Any] = {
            "sources_attempted": None, "items_fetched": None, "items_new": None,
            "items_updated": None, "items_duplicate": None, "items_filtered": None,
        }
        self.errors: List[Dict[str, Any]] = []
        self.conn: Optional[sqlite3.Connection] = None
        self._t0: Optional[dt.datetime] = None
        self.started_at: Optional[str] = None
        self._item_rows = 0
        self._items_truncated = False
        self._degraded = False
        self._closed = False

    # ---- 生命周期
    def start(self) -> "Run":
        self._write("run.start", self._open)
        return self

    def _rollback(self) -> None:
        if self.conn is not None:
            try:
                self.conn.rollback()
            except Exception:  # noqa: BLE001
                pass

    def _write(self, label: str, fn: Callable[..., Any], *args: Any):
        if self._degraded:
            return None
        result = _safe(label, fn, *args, on_retry=self._rollback)
        if result is DROPPED:
            # 写锁被别人占着：这一轮剩下的监控写入直接跳过，
            # 只把最多一次「等待 + 放弃」的开销留给业务路径。
            self._degraded = True
        return None if result is DROPPED else result

    def _open(self) -> None:
        store.ensure()
        self.conn = monitor_connect()
        self._t0 = store.now()
        self.started_at = self._t0.isoformat(timespec="seconds")
        store.start_run(self.conn, {
            "run_id": self.run_id, "task": self.task, "trigger": self.trigger,
            "started_at": self.started_at, "status": "running", "stats": self.stats,
            "origin": self.origin, "error_count": 0, "errors": [],
            "created_at": self.started_at,
        })
        self.conn.commit()
        # 线程内可能嵌套运行（例如新闻采集里再记一次地理采集）：用栈保存，收尾时恢复
        runs = list(getattr(_LOCAL, "stack", []) or [])
        runs.append(self)
        _LOCAL.stack = runs
        _LOCAL.run = self

    def __enter__(self) -> "Run":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self.error = redact.redact_text("%s: %s\n%s" % (
                exc_type.__name__, exc, traceback.format_exc(limit=2)))
            self.add_error(self.error, error_code=store.classify_error(str(exc)))
        self.finish()
        return False

    def finish(self, status: Optional[str] = None, error: Optional[str] = None,
               stats: Optional[Dict[str, Any]] = None, **counts: Any) -> None:
        if self._closed:
            return
        self._closed = True
        if status:
            self.status = status
        elif self.status == "running":
            self.status = "failed" if self.error else "ok"
        if error:
            self.error = redact.redact_text(error)
        merged = dict(self.stats)
        merged.update(stats or {})
        if self._items_truncated:
            merged["items_trace_truncated"] = True
            merged["items_trace_limit"] = MAX_ITEMS_PER_RUN
        merged["counts_unknown"] = [key for key, value in self.counts.items() if value is None]
        self._write("run.finish", self._finish, merged, counts)

    def _finish(self, merged: Dict[str, Any], counts: Dict[str, Any]) -> None:
        if self.conn is None:
            return
        finished = store.now().isoformat(timespec="seconds")
        duration = int((store.now() - self._t0).total_seconds() * 1000) if self._t0 else None
        self.counts.update({k: v for k, v in counts.items() if k in self.counts})
        store.update_run(
            self.conn, self.run_id, status=self.status, finished_at=finished,
            duration_ms=duration, errors=self.errors, stats=merged, error_count=len(self.errors),
            **self.counts)
        self.conn.commit()
        self.conn.close()
        self.conn = None
        runs = [item for item in (getattr(_LOCAL, "stack", []) or []) if item is not self]
        _LOCAL.stack = runs
        _LOCAL.run = runs[-1] if runs else None

    # ---- 阶段
    def phase(self, name: str, status: str = "ok",
              result: Optional[Dict[str, Any]] = None) -> Stage:
        return Stage(self, name, status, result)

    def stage(self, name: str, status: str, result: Optional[Dict[str, Any]] = None,
              error: Optional[str] = None, duration_ms: Optional[int] = None,
              started_at: Optional[str] = None, finished_at: Optional[str] = None,
              note: Optional[str] = None) -> None:
        """不经过上下文管理器、直接写一个阶段（例如同一阶段内多个来源合并计时）。"""
        stage = Stage(self, name, status, result)
        stage.duration_ms = duration_ms
        stage.started_at = started_at or self.started_at
        stage.finished_at = finished_at or store.now_iso()
        stage.error = redact.redact_text(error) if error else None
        if note:
            stage.result.setdefault("note", note)
        self._write("stage.write", self._write_stage, stage, status)

    def _write_stage(self, stage: Stage, status: Optional[str] = None) -> None:
        if self.conn is None:
            return
        plan = store.TASK_STAGES.get(self.task, [])
        seq = (plan.index(stage.name) + 1) if stage.name in plan else len(plan) + 1
        store.upsert_stage(self.conn, {
            "run_id": self.run_id, "name": stage.name, "seq": seq,
            "status": status or stage.status, "started_at": stage.started_at,
            "finished_at": stage.finished_at, "duration_ms": stage.duration_ms,
            "result": stage.result, "error": stage.error,
        })
        self.conn.commit()

    # ---- 计数与错误
    def set_counts(self, **counts: Any) -> None:
        """设置绝对值（None 表示“本次没有记录到”，不会覆盖已有值）。"""
        for key, value in counts.items():
            if key in self.counts and value is not None:
                self.counts[key] = int(value)
        self._write("run.counts", self._write_counts)

    def add_counts(self, **counts: Any) -> None:
        """累加增量（同一阶段多个来源各自汇总时用）。"""
        for key, value in counts.items():
            if key in self.counts and value is not None:
                existing = self.counts.get(key)
                self.counts[key] = int(value) if existing is None else int(existing) + int(value)
        self._write("run.counts", self._write_counts)

    def add_error(self, message: Any, *, source_id: Optional[str] = None,
                  error_code: Optional[str] = None, stage: Optional[str] = None,
                  details: Optional[Dict[str, Any]] = None) -> None:
        text = redact.redact_text(message or "")
        if not text:
            return
        self.errors.append({"message": text[:1000], "at": store.now_iso(),
                            "source_id": source_id,
                            "error_code": error_code or store.classify_error(text),
                            "stage": stage})
        self._write("run.error", self._write_errors)
        self.log("ERROR", text, source_id=source_id, stage=stage,
                 error_code=error_code or store.classify_error(text), details=details)

    def _write_counts(self) -> None:
        if self.conn is None:
            return
        store.update_run(self.conn, self.run_id, **self.counts)
        self.conn.commit()

    def _write_errors(self) -> None:
        if self.conn is None:
            return
        store.update_run(self.conn, self.run_id, errors=self.errors,
                         error_count=len(self.errors))
        self.conn.commit()

    # ---- 日志
    def log(self, level: str, message: Any, *, stage: Optional[str] = None,
            source_id: Optional[str] = None, error_code: Optional[str] = None,
            details: Optional[Dict[str, Any]] = None) -> None:
        self._write("run.log", self._write_log, level, message, stage, source_id, error_code, details)

    def _write_log(self, level: str, message: Any, stage: Optional[str],
                   source_id: Optional[str], error_code: Optional[str],
                   details: Optional[Dict[str, Any]]) -> None:
        if self.conn is None:
            return
        store.insert_logs(self.conn, [{
            "ts": store.now_iso(), "level": level, "task": self.task, "run_id": self.run_id,
            "source_id": source_id, "stage": stage, "error_code": error_code,
            "message": message, "details": details,
        }])
        self.conn.commit()

    # ---- 来源结果
    def source_result(self, source_id: str, status: str, **fields: Any) -> None:
        """记录一个来源在本轮的真实结果（成功/失败/0 条/跳过都在这里）。"""
        self._write("source_result", self._write_source_result, source_id, status, fields)

    def _write_source_result(self, source_id: str, status: str,
                             fields: Dict[str, Any]) -> None:
        if self.conn is None:
            return
        row = dict(fields)
        row.update({"run_id": self.run_id, "source_id": str(source_id), "status": status})
        started = row.pop("started_at", None) or getattr(self, "started_at", None)
        finished = row.pop("finished_at", None) or store.now_iso()
        row["started_at"] = started
        row["finished_at"] = finished
        if row.get("error") and not row.get("error_code"):
            row["error_code"] = store.classify_error(row["error"], row.get("http_status"))
        store.upsert_source_result(self.conn, row)
        self.conn.commit()

    # ---- 逐条处理结果
    def items(self, rows: List[Dict[str, Any]], source_id: Optional[str] = None) -> None:
        self._write("items", self._write_items, rows, source_id)

    def _write_items(self, rows: List[Dict[str, Any]], source_id: Optional[str]) -> None:
        if self.conn is None or not rows:
            return
        room = MAX_ITEMS_PER_RUN - self._item_rows
        if room <= 0:
            self._items_truncated = True
            return
        payload = []
        for row in rows[:room]:
            payload.append({**row, "run_id": self.run_id,
                            "source_id": row.get("source_id") or source_id})
        self._item_rows += store.insert_item_actions(self.conn, payload)
        if len(rows) > room:
            self._items_truncated = True
        self.conn.commit()


def run(task: str, trigger: str = "unknown", run_id: Optional[str] = None,
        stats: Optional[Dict[str, Any]] = None) -> Run:
    """开始一次监控运行（返回未启动的对象，交给 ``with`` 或 ``.start()``）。"""
    return Run(task, trigger, run_id, stats)


def log_event(level: str, message: Any, *, task: Optional[str] = None,
              run_id: Optional[str] = None, source_id: Optional[str] = None,
              stage: Optional[str] = None, error_code: Optional[str] = None,
              details: Optional[Dict[str, Any]] = None) -> None:
    """独立写一条结构化日志（不属于任何运行，例如推送跳过、调度异常）。

    写锁被占用时同样放弃（并对后续独立日志做冷却），绝不在业务路径上长时间等待。
    """
    global _LOG_COOLDOWN_UNTIL
    if time.time() < _LOG_COOLDOWN_UNTIL:
        return

    def _write() -> None:
        store.ensure()
        conn = monitor_connect()
        try:
            store.insert_logs(conn, [{
                "ts": store.now_iso(), "level": level, "task": task, "run_id": run_id,
                "source_id": source_id, "stage": stage, "error_code": error_code,
                "message": message, "details": details,
            }])
            conn.commit()
        finally:
            conn.close()

    if _safe("log_event", _write) is DROPPED:
        _LOG_COOLDOWN_UNTIL = time.time() + 30