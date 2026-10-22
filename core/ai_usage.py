"""大模型 token 用量统计与限额控制。

为什么要本地记账：
* 免费/低价接口的额度是按 token 算的，服务商只在失败时才告诉你超了；
* 本地先记账就能在「还没超」的时候切到备用接口，避免任务中途失败；
* 两个接口都超了就暂停 AI，24 小时后自动恢复并重新计数。

存储：``data/ai_usage.json``（运行时状态，不进仓库）
结构：
{
  "date": "2026-10-23",
  "slots": {"primary": {"prompt": 0, "completion": 0, "calls": 0},
             "fallback": {...}},
  "paused_until": "2026-10-24T05:00:00+08:00",
  "history": [{"date": "2026-10-22", "primary": 1200, "fallback": 0, "calls": 3}]
}
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DATA_DIR

USAGE_PATH = Path(os.environ.get("AI_USAGE", str(DATA_DIR / "ai_usage.json")))
SLOTS = ("primary", "fallback")
HISTORY_DAYS = 30


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _today() -> str:
    return _now().date().isoformat()


def _empty_slots() -> Dict[str, Dict[str, int]]:
    return {slot: {"prompt": 0, "completion": 0, "calls": 0} for slot in SLOTS}


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
        USAGE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print("[ai] usage write failed:", exc)


def _rollover(data: Dict[str, Any]) -> Dict[str, Any]:
    """跨天：把昨天的统计压进 history，然后清零。暂停到期也在这里解除。"""
    today = _today()
    data.setdefault("slots", _empty_slots())
    data.setdefault("history", [])

    stored_date = data.get("date") or ""
    if stored_date != today:
        if stored_date:
            slots = data.get("slots") or {}
            data["history"].append({
                "date": stored_date,
                "primary": _total(slots.get("primary")),
                "fallback": _total(slots.get("fallback")),
                "calls": int((slots.get("primary") or {}).get("calls", 0))
                         + int((slots.get("fallback") or {}).get("calls", 0)),
            })
            data["history"] = data["history"][-HISTORY_DAYS:]
        data["date"] = today
        data["slots"] = _empty_slots()

    for slot in SLOTS:
        data["slots"].setdefault(slot, {"prompt": 0, "completion": 0, "calls": 0})
        for key in ("prompt", "completion", "calls"):
            data["slots"][slot][key] = int(data["slots"][slot].get(key) or 0)

    # 暂停到期 → 清标记并重新计数（用户要求 24 小时后解除限制重新计算）
    paused_until = data.get("paused_until") or ""
    if paused_until:
        try:
            until = dt.datetime.fromisoformat(paused_until)
            if _now() >= until:
                data["paused_until"] = ""
                data["slots"] = _empty_slots()
        except ValueError:
            data["paused_until"] = ""
    return data


def _total(slot_data: Optional[Dict[str, Any]]) -> int:
    if not slot_data:
        return 0
    return int(slot_data.get("prompt") or 0) + int(slot_data.get("completion") or 0)


def load() -> Dict[str, Any]:
    data = _rollover(_read())
    _write(data)
    return data


def record(slot: str, prompt_tokens: int = 0, completion_tokens: int = 0, calls: int = 1) -> Dict[str, Any]:
    slot = slot if slot in SLOTS else "primary"
    data = load()
    entry = data["slots"][slot]
    entry["prompt"] += max(0, int(prompt_tokens))
    entry["completion"] += max(0, int(completion_tokens))
    entry["calls"] += max(0, int(calls))
    _write(data)
    return data


def used(slot: str) -> int:
    data = load()
    return _total((data.get("slots") or {}).get(slot))


def remaining(slot: str, limit: int) -> Optional[int]:
    """剩余额度；limit<=0 表示不限，返回 None。"""
    if int(limit or 0) <= 0:
        return None
    return max(0, int(limit) - used(slot))


def exhausted(slot: str, limit: int) -> bool:
    left = remaining(slot, limit)
    return left is not None and left <= 0


def paused() -> bool:
    data = load()
    until = data.get("paused_until") or ""
    if not until:
        return False
    try:
        return _now() < dt.datetime.fromisoformat(until)
    except ValueError:
        return False


def pause_until() -> str:
    data = load()
    until = data.get("paused_until") or ""
    try:
        if until and _now() < dt.datetime.fromisoformat(until):
            return until
    except ValueError:
        pass
    return ""


def pause(hours: float = 24) -> str:
    until = (_now() + dt.timedelta(hours=float(hours or 24))).isoformat(timespec="seconds")
    data = load()
    data["paused_until"] = until
    _write(data)
    print("[ai] 主备接口额度都用尽，AI 暂停到 %s" % until)
    return until


def resume() -> None:
    data = load()
    data["paused_until"] = ""
    data["slots"] = _empty_slots()
    _write(data)


def summary(limits: Dict[str, int], window_hours: float = 24) -> Dict[str, Any]:
    """给界面用的完整视图：各槽用量、剩余、暂停状态、历史。"""
    data = load()
    slots_out: Dict[str, Dict[str, Any]] = {}
    for slot in SLOTS:
        entry = (data.get("slots") or {}).get(slot) or {}
        total = _total(entry)
        limit = int(limits.get(slot) or 0)
        slots_out[slot] = {
            "prompt": int(entry.get("prompt") or 0),
            "completion": int(entry.get("completion") or 0),
            "calls": int(entry.get("calls") or 0),
            "tokens": total,
            "limit": limit,
            "remaining": None if limit <= 0 else max(0, limit - total),
            "ratio": round(total / limit, 4) if limit > 0 else None,
        }
    return {
        "date": data.get("date") or _today(),
        "window_hours": float(window_hours or 24),
        "slots": slots_out,
        "paused": paused(),
        "paused_until": pause_until(),
        "history": (data.get("history") or [])[-HISTORY_DAYS:],
    }
