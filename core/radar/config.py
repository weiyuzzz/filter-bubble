"""Radar configuration: JSON file + environment overrides.

Kept separate from ``core.config`` on purpose — the GitHub digest reads that module
at import time, and the radar must never be able to change those values.
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
from typing import Any, Dict

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = pathlib.Path(os.environ.get("RADAR_CONFIG", str(BASE_DIR / "social_radar.json")))

# Defaults are the documented baseline. A missing/partial config file still runs.
DEFAULTS: Dict[str, Any] = {
    "version": 1,
    "enabled": True,
    "timezone": "Asia/Shanghai",
    "schedule": {
        "daily_time": "09:00",
        "retry_after_minutes": 10,
        "max_retries": 3,
    },
    "limits": {
        "max_candidates": 150,
        "max_events": 60,
        "max_report_events": 12,
        "source_timeout_seconds": 12,
        "source_retries": 1,
        "min_minutes_between_collects": 20,
        "min_minutes_between_reports": 60,
    },
    "scoring": {
        "velocity": 0.30,
        "cross_platform": 0.20,
        "engagement": 0.20,
        "verification": 0.20,
        "interest": 0.10,
    },
    "report": {
        "length": "standard",
        "top_events": 12,
        "section_limits": {"zh_web": 8, "intl": 8, "tech": 8, "investigations": 5},
    },
    "push": {
        "enabled": False,
        "channel": "radar-wechat",
        "items": 5,
        "alert_threshold": 82,
        "max_attempts": 3,
    },
    "interests": {
        "topics": [
            "AI 与大模型", "芯片与半导体", "开源软件", "国际科技公司",
            "游戏与消费电子", "国际新闻", "中国科技发展与科研成果",
            "产业竞争与经济事件",
        ],
        "keywords": [],
        "synonyms": {},
        "exclude": [],
        "languages": ["zh", "en"],
    },
    "investigation": {
        "min_heat": 70,
        "max_evidence": 60,
        "tech_auto_check": True,
        "tech_keywords": [
            "芯片", "半导体", "光刻", "大模型", "人工智能", "量子", "可控核聚变",
            "超导", "6G", "卫星", "航空发动机", "新材料", "量产", "首次实现",
            "国际领先", "突破",
        ],
    },
    "retention": {"days": 90},
    "llm": {"enabled": False, "provider": "", "note": "默认关闭：调查结论由本地规则与已采集证据生成"},
    "sources": {},
}

_ENV_OVERRIDES = {
    "RADAR_ENABLED": ("enabled", lambda v: v not in ("0", "false", "False", "")),
    "RADAR_DAILY_TIME": ("schedule.daily_time", str),
    "RADAR_PUSH_ENABLED": ("push.enabled", lambda v: v not in ("0", "false", "False", "")),
    "RADAR_PUSH_ITEMS": ("push.items", int),
    "RADAR_MAX_REPORT_EVENTS": ("report.top_events", int),
}


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _set_path(data: Dict[str, Any], dotted: str, value: Any) -> None:
    node = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def load() -> Dict[str, Any]:
    cfg = copy.deepcopy(DEFAULTS)
    try:
        if CONFIG_PATH.exists():
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8") or "{}")
            if isinstance(raw, dict):
                cfg = _merge(cfg, raw)
            else:
                print("[radar] config file is not a JSON object; using defaults")
    except Exception as exc:  # noqa: BLE001 - a broken config must not stop the app
        print("[radar] config load failed (%s): %s" % (CONFIG_PATH, exc))

    for env_key, (dotted, caster) in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_key)
        if raw is None or raw == "":
            continue
        try:
            _set_path(cfg, dotted, caster(raw))
        except Exception:  # noqa: BLE001
            print("[radar] ignoring invalid %s=%r" % (env_key, raw))
    return cfg


def cached() -> Dict[str, Any]:
    """Reloaded on every call: edits to social_radar.json take effect without a restart."""
    return load()
