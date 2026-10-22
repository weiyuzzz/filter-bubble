"""全局 AI 设置：读写项目根目录的 app_settings.json。

设计原则：
* 一份配置，两个模块共用（GitHub 项目解读、热点摘要、以后新增的功能）；
* 旧的 social_radar.json → llm 仍然会被读取（向后兼容），但新写入一律进 app_settings.json；
* 密钥不在这里，见 core/ai_secrets.py。
"""
from __future__ import annotations

import json
import os
import pathlib
from typing import Any, Dict

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = pathlib.Path(os.environ.get("AI_CONFIG", str(BASE_DIR / "app_settings.json")))
LEGACY_PATH = pathlib.Path(os.environ.get("RADAR_CONFIG", str(BASE_DIR / "social_radar.json")))

DEFAULTS: Dict[str, Any] = {
    "version": 1,
    "llm": {
        "enabled": False,
        "provider": "openai_compatible",
        "base_url": "",
        "model": "",
        "api_key_env": "RADAR_LLM_API_KEY",
        "timeout_seconds": 60,
        "max_attempts": 2,
        "temperature": 0.2,
        "max_tokens": 1500,
        # 备用接口：主接口额度用尽时自动切过来，第二天自动切回
        "fallback": {
            "enabled": False,
            "provider": "openai_compatible",
            "base_url": "",
            "model": "",
            "api_key_env": "RADAR_LLM_API_KEY_FALLBACK",
        },
        # token 限额：本地记账，超了自动切下一个接口；两个都超了暂停 window_hours 小时
        # 0 表示不限
        "token_limits": {"primary": 500000, "fallback": 500000, "window_hours": 24},
    },
    "ai": {
        # GitHub 项目 AI 解读：每天为多少个项目生成（其余按需在页面里生成）
        "briefs_per_run": 12,
        # 并发请求数，免费模型建议 1–2，避免触发上游限流
        "concurrency": 1,
        # 是否在生成日报时自动调用模型
        "enable_briefs": True,
    },
}


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _read(path: pathlib.Path) -> Dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
            if isinstance(data, dict):
                return data
    except Exception as exc:  # noqa: BLE001
        print("[ai] settings read failed (%s): %s" % (path, exc))
    return {}


def load() -> Dict[str, Any]:
    cfg = _merge(DEFAULTS, _read(CONFIG_PATH))
    if not CONFIG_PATH.exists():
        # 第一次运行：把旧的雷达配置里的 llm 迁移过来
        legacy = (_read(LEGACY_PATH).get("llm") or {})
        if legacy:
            cleaned = {k: v for k, v in legacy.items() if k in DEFAULTS["llm"]}
            if cleaned:
                cfg["llm"] = _merge(cfg["llm"], cleaned)
    return cfg


def save(updates: Dict[str, Any]) -> Dict[str, Any]:
    """只写白名单字段，保持文件可读（两空格缩进，分段）。"""
    current = _read(CONFIG_PATH)
    if not isinstance(current, dict):
        current = {}

    llm = current.setdefault("llm", {})
    incoming_llm = updates.get("llm") or {}
    for key in ("enabled", "provider", "base_url", "model", "api_key_env",
                "timeout_seconds", "max_attempts", "temperature", "max_tokens"):
        if key in incoming_llm:
            llm[key] = incoming_llm[key]

    # 备用接口是嵌套字典，单独合并，避免被上面的白名单忽略
    incoming_fb = incoming_llm.get("fallback")
    if isinstance(incoming_fb, dict) and incoming_fb:
        fallback = current.setdefault("llm", {}).setdefault("fallback", {})
        for key in ("enabled", "provider", "base_url", "model", "api_key_env"):
            if key in incoming_fb:
                fallback[key] = incoming_fb[key]

    incoming_limits = incoming_llm.get("token_limits")
    if isinstance(incoming_limits, dict) and incoming_limits:
        limits = current.setdefault("llm", {}).setdefault("token_limits", {})
        for key in ("primary", "fallback", "window_hours"):
            if key in incoming_limits:
                limits[key] = incoming_limits[key]

    ai = current.setdefault("ai", {})
    incoming_ai = updates.get("ai") or {}
    for key in ("briefs_per_run", "concurrency", "enable_briefs"):
        if key in incoming_ai:
            ai[key] = incoming_ai[key]

    current.setdefault("version", 1)
    CONFIG_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    return {"llm": llm, "ai": ai}


def public() -> Dict[str, Any]:
    return load()
