"""大模型 token 用量统计与额度控制。

为什么要本地记账：
* 免费/低价接口的额度是按 token 算的，服务商只在失败时才告诉你超了；
* 本地先记账就能在「还没超」的时候切到下一个接口，避免任务中途失败；
* 所有接口都用完（或触发了套餐限额）就暂停 AI，窗口结束后自动恢复并重新计数。

支持三种额度模式（每个接口各自设置，见 ai_settings.norm_quota）：
* ``daily``  —— 自然日清零（默认，适合「每天 N token」的服务商）；
* ``window`` —— 每 N 小时一个窗口（套餐常见，例如 5 小时刷新一次）；
* ``total``  —— 总量（累计抵扣，例如「买断 1 亿 token 的包」，可选 reset_days 周期）。

存储：``data/ai_usage.json``（运行时状态，不进仓库）
结构：
{
  "version": 2,
  "date": "2026-10-26",
  "slots": {"primary": {"prompt": 0, "completion": 0, "calls": 0}},     # 当前窗口
  "totals": {"primary": {"prompt": 0, "completion": 0, "calls": 0}},    # 累计
  "windows": {"primary": "2026-10-26T00:00:00+08:00"},
  "paused_until": "2026-10-27T05:00:00+08:00",
  "history": [{"date": "2026-10-25", "primary": 1200, "fallback": 0,
               "calls": 3, "tokens": {"primary": 1200}}]
}
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import DATA_DIR

USAGE_PATH = Path(os.environ.get("AI_USAGE", str(DATA_DIR / "ai_usage.json")))
SLOTS = ("primary", "fallback")  # 兼容旧代码的默认槽位名
HISTORY_DAYS = 30

_LOCK = threading.RLock()
_QUOTA_DEFAULT = {"mode": "daily", "limit": 0, "window_hours": 24.0, "reset_days": 0}


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _today() -> str:
    return _now().date().isoformat()


def _midnight(day: Optional[dt.date] = None) -> dt.datetime:
    day = day or _now().date()
    return dt.datetime(day.year, day.month, day.day).astimezone()


def _parse_time(value: Any) -> Optional[dt.datetime]:
    """容忍 date-only / 带偏移 / 无偏移（按本地时区）三种写法。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if len(text) == 10:  # 2026-10-27
            return dt.datetime.fromisoformat(text + "T23:59:59").astimezone()
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.astimezone()
    except ValueError:
        return None


def _empty_slot() -> Dict[str, int]:
    return {"prompt": 0, "completion": 0, "calls": 0}


def _empty_slots() -> Dict[str, Dict[str, int]]:
    return {slot: _empty_slot() for slot in SLOTS}


def _read() -> Dict[str, Any]:
    try:
        if USAGE_PATH.exists():
            data = json.loads(USAGE_PATH.read_text(encoding="utf-8-sig") or "{}")
            if isinstance(data, dict):
                return data
    except Exception as exc:  # noqa: BLE001
        print("[ai] usage read failed:", exc)
    return {}


def _write(data: Dict[str, Any]) -> None:
    try:
        USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {key: value for key, value in data.items() if not key.startswith("_")}
        tmp = USAGE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, USAGE_PATH)
    except Exception as exc:  # noqa: BLE001
        print("[ai] usage write failed:", exc)


def _total(slot_data: Optional[Dict[str, Any]]) -> int:
    if not slot_data:
        return 0
    return int(slot_data.get("prompt") or 0) + int(slot_data.get("completion") or 0)


def _as_quota(value: Any) -> Dict[str, Any]:
    """limits 可以是 int（旧的每日上限）也可以是 quota 字典。"""
    if isinstance(value, dict):
        mode = str(value.get("mode") or "daily").lower()
        if mode not in ("daily", "window", "total"):
            mode = "daily"
        try:
            limit = max(0, int(value.get("limit") or 0))
        except (TypeError, ValueError):
            limit = 0
        try:
            window = float(value.get("window_hours") or 24)
        except (TypeError, ValueError):
            window = 24.0
        try:
            reset_days = max(0, int(value.get("reset_days") or 0))
        except (TypeError, ValueError):
            reset_days = 0
        return {"mode": mode, "limit": limit, "window_hours": window or 24.0,
                "reset_days": reset_days}
    try:
        limit = max(0, int(value or 0))
    except (TypeError, ValueError):
        limit = 0
    return {"mode": "daily", "limit": limit, "window_hours": 24.0, "reset_days": 0}


def _rollover(data: Dict[str, Any], quotas: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], bool]:
    """按各槽位的额度模式重置计数窗口；跨天归档 history；解除到期暂停。"""
    changed = False
    today = _today()
    data.setdefault("version", 2)
    data.setdefault("slots", _empty_slots())
    data.setdefault("totals", {})
    data.setdefault("windows", {})
    data.setdefault("history", [])

    quotas = {slot: _as_quota(value) for slot, value in (quotas or {}).items()}
    stored_date = str(data.get("date") or "")
    day_changed = stored_date != today

    if day_changed and stored_date:
        slots = data.get("slots") or {}
        tokens = {slot_id: _total(entry) for slot_id, entry in slots.items()}
        entry: Dict[str, Any] = {
            "date": stored_date,
            "calls": sum(int((item or {}).get("calls", 0)) for item in slots.values()),
            "tokens": tokens,
        }
        entry.update(tokens)  # 兼容旧图表：直接按槽位名取
        data["history"].append(entry)
        data["history"] = data["history"][-HISTORY_DAYS:]
        changed = True
    if day_changed:
        data["date"] = today
        changed = True

    # 保证出现过的槽位结构完整
    for slot_id, entry in list(data["slots"].items()):
        if not isinstance(entry, dict):
            data["slots"][slot_id] = _empty_slot()
            changed = True
            continue
        for key in ("prompt", "completion", "calls"):
            value = int(entry.get(key) or 0)
            if entry.get(key) != value:
                entry[key] = value
                changed = True

    # 各槽位计数窗口（含仅在 quotas 里出现、还没用过的新槽位）
    known_slots = list(data["slots"].keys())
    for slot_id in quotas:
        if slot_id not in data["slots"]:
            known_slots.append(slot_id)
    for slot_id in known_slots:
        entry = data["slots"].setdefault(slot_id, _empty_slot())
        if not isinstance(entry, dict):
            data["slots"][slot_id] = _empty_slot()
            changed = True
        quota = quotas.get(slot_id, _QUOTA_DEFAULT)
        mode = quota["mode"]
        started = _parse_time(data["windows"].get(slot_id))
        if mode == "total":
            continue
        if mode == "window":
            if started is None:
                data["windows"][slot_id] = _now().isoformat(timespec="seconds")
                changed = True
                continue
            if _now() - started >= dt.timedelta(hours=float(quota["window_hours"])):
                data["slots"][slot_id] = _empty_slot()
                data["windows"][slot_id] = _now().isoformat(timespec="seconds")
                changed = True
        else:  # daily
            if day_changed or (started is not None and started.date() < _now().date()):
                data["slots"][slot_id] = _empty_slot()
                data["windows"][slot_id] = _midnight().isoformat(timespec="seconds")
                changed = True
            elif started is None:
                # 老数据没有窗口元信息：只补上，不动已有计数
                data["windows"][slot_id] = _midnight().isoformat(timespec="seconds")
                changed = True

    # 累计总量（total 模式）的周期重置
    for slot_id, entry in list(data["totals"].items()):
        quota = quotas.get(slot_id)
        if not quota or quota["mode"] != "total" or not quota["reset_days"]:
            continue
        started = _parse_time(data.get("totals_started_at", {}).get(slot_id))
        if started and _now() - started >= dt.timedelta(days=float(quota["reset_days"])):
            data["totals"][slot_id] = _empty_slot()
            data.setdefault("totals_started_at", {})[slot_id] = _now().isoformat(timespec="seconds")
            changed = True

    # 暂停到期 → 清标记并重新计数（24 小时后自动解除限制重新计算）
    paused_until = str(data.get("paused_until") or "")
    if paused_until:
        until = _parse_time(paused_until)
        if until is None or _now() >= until:
            data["paused_until"] = ""
            for slot_id in list(data["slots"].keys()):
                data["slots"][slot_id] = _empty_slot()
            changed = True
    return data, changed


def load(quotas: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    with _LOCK:
        data, changed = _rollover(_read(), quotas)
        if changed:
            _write(data)
        return data


def _ensure(data: Dict[str, Any], slot: str) -> Dict[str, Any]:
    entry = data["slots"].get(slot)
    if not isinstance(entry, dict):
        entry = _empty_slot()
        data["slots"][slot] = entry
    return entry


def record(slot: str, prompt_tokens: int = 0, completion_tokens: int = 0, calls: int = 1,
           quota: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """记一笔用量。quota 用来决定窗口重置规则（不传按自然日）。"""
    slot = str(slot or "primary")
    with _LOCK:
        data, _changed = _rollover(_read(), {slot: quota} if quota else None)
        entry = _ensure(data, slot)
        entry["prompt"] += max(0, int(prompt_tokens))
        entry["completion"] += max(0, int(completion_tokens))
        entry["calls"] += max(0, int(calls))
        total_entry = data.setdefault("totals", {}).setdefault(slot, _empty_slot())
        if not isinstance(total_entry, dict):
            total_entry = data["totals"][slot] = _empty_slot()
        total_entry["prompt"] += max(0, int(prompt_tokens))
        total_entry["completion"] += max(0, int(completion_tokens))
        total_entry["calls"] += max(0, int(calls))
        data.setdefault("totals_started_at", {}).setdefault(
            slot, _now().isoformat(timespec="seconds"))
        _write(data)
        return data


def used(slot: str) -> int:
    """当前窗口用量（默认自然日 / 套餐窗口）。"""
    data = load()
    return _total((data.get("slots") or {}).get(slot))


def used_total(slot: str) -> int:
    """累计用量（总量型额度看这个）。"""
    data = load()
    return _total((data.get("totals") or {}).get(slot))


def remaining(slot: str, limit: int) -> Optional[int]:
    """剩余额度；limit<=0 表示不限，返回 None。"""
    if int(limit or 0) <= 0:
        return None
    return max(0, int(limit) - used(slot))


def exhausted(slot: str, limit: int) -> bool:
    left = remaining(slot, limit)
    return left is not None and left <= 0


def reset_at(slot: str, quota: Any, data: Optional[Dict[str, Any]] = None) -> str:
    """这个槽位的额度什么时候回满（ISO 字符串，空表示不会自动回）。"""
    quota = _as_quota(quota)
    data = data if data is not None else load({slot: quota})
    if int(quota["limit"]) <= 0:
        return ""
    mode = quota["mode"]
    if mode == "daily":
        return (_midnight() + dt.timedelta(days=1)).isoformat(timespec="seconds")
    if mode == "window":
        started = _parse_time((data.get("windows") or {}).get(slot)) or _now()
        due = started + dt.timedelta(hours=float(quota["window_hours"]))
        return due.isoformat(timespec="seconds")
    if mode == "total" and quota["reset_days"]:
        started = _parse_time((data.get("totals_started_at") or {}).get(slot)) or _now()
        due = started + dt.timedelta(days=float(quota["reset_days"]))
        return due.isoformat(timespec="seconds")
    return ""


def quota_view(slot: str, quota: Any, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """给界面用的额度视图：模式、已用、剩余、比例、何时回满。"""
    quota = _as_quota(quota)
    data = data if data is not None else load({slot: quota})
    entry = (data.get("slots") or {}).get(slot) or {}
    total_entry = (data.get("totals") or {}).get(slot) or {}
    mode = quota["mode"]
    used_tokens = _total(total_entry) if mode == "total" else _total(entry)
    limit = int(quota["limit"])
    return {
        "mode": mode,
        "prompt": int(entry.get("prompt") or 0),
        "completion": int(entry.get("completion") or 0),
        "calls": int(entry.get("calls") or 0),
        "tokens": used_tokens,
        "window_tokens": _total(entry),
        "total_tokens": _total(total_entry),
        "limit": limit,
        "remaining": None if limit <= 0 else max(0, limit - used_tokens),
        "ratio": round(used_tokens / limit, 4) if limit > 0 else None,
        "window_hours": float(quota["window_hours"]),
        "reset_days": int(quota["reset_days"]),
        "reset_at": reset_at(slot, quota, data),
        "exhausted": limit > 0 and used_tokens >= limit,
    }


def exhausted_quota(slot: str, quota: Any, data: Optional[Dict[str, Any]] = None) -> bool:
    quota = _as_quota(quota)
    if int(quota["limit"]) <= 0:
        return False
    view = quota_view(slot, quota, data)
    return bool(view["exhausted"])


def paused() -> bool:
    data = load()
    until = _parse_time(data.get("paused_until"))
    return bool(until and _now() < until)


def pause_until() -> str:
    data = load()
    until = _parse_time(data.get("paused_until"))
    if until and _now() < until:
        return str(data.get("paused_until"))
    return ""


def pause(hours: float = 24) -> str:
    until = (_now() + dt.timedelta(hours=float(hours or 24))).isoformat(timespec="seconds")
    with _LOCK:
        data, _changed = _rollover(_read())
        data["paused_until"] = until
        _write(data)
    print("[ai] 所有接口额度都用尽，AI 暂停到 %s" % until)
    return until


def resume() -> None:
    with _LOCK:
        data, _changed = _rollover(_read())
        data["paused_until"] = ""
        for slot in list(data["slots"].keys()):
            data["slots"][slot] = _empty_slot()
            data["windows"][slot] = _now().isoformat(timespec="seconds")
        _write(data)


def summary(quotas: Optional[Dict[str, Any]] = None, window_hours: float = 24) -> Dict[str, Any]:
    """给界面用的完整视图：各槽用量、剩余、暂停状态、历史。

    quotas 可以是 ``{"primary": 1000}`` 这样的旧格式，也可以是 quota 字典。
    """
    quotas = {slot: _as_quota(value) for slot, value in (quotas or {}).items()}
    data = load(quotas)
    slots_out: Dict[str, Dict[str, Any]] = {}
    for slot in list(data.get("slots", {}).keys()) + [s for s in quotas if s not in data.get("slots", {})]:
        view = quota_view(slot, quotas.get(slot, _QUOTA_DEFAULT), data)
        slots_out[slot] = view
    return {
        "version": 2,
        "date": data.get("date") or _today(),
        "window_hours": float(window_hours or 24),
        "slots": slots_out,
        "paused": paused(),
        "paused_until": pause_until(),
        "history": (data.get("history") or [])[-HISTORY_DAYS:],
    }