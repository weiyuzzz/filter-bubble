"""全局密钥存储（大模型 API Key）。

支持两个槽位：
* ``primary``：主接口的 Key；
* ``fallback``：备用接口的 Key（主接口额度用尽时自动切换）。

* 只写 ``data/ai_secrets.json``，该文件已在 .gitignore 里；
* 接口只返回「是否已配置」与掩码，绝不回传明文；
* 环境变量优先，方便用 .env / 系统环境变量管理。
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .config import DATA_DIR

SECRETS_PATH = Path(os.environ.get("AI_SECRETS", str(DATA_DIR / "ai_secrets.json")))
# 兼容旧路径（早期版本用的是 radar_secrets.json）
LEGACY_PATH = Path(os.environ.get("RADAR_SECRETS", str(DATA_DIR / "radar_secrets.json")))
ENV_NAME = "RADAR_LLM_API_KEY"
FALLBACK_ENV_NAME = "RADAR_LLM_API_KEY_FALLBACK"

SLOTS = ("primary", "fallback")
_FIELD = {"primary": "llm_api_key", "fallback": "llm_api_key_fallback"}


def _read() -> Dict[str, Any]:
    for path in (SECRETS_PATH, LEGACY_PATH):
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
                if isinstance(data, dict):
                    return data
        except Exception as exc:  # noqa: BLE001
            print("[ai] secrets read failed:", exc)
    return {}


def _write(data: Dict[str, Any]) -> None:
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SECRETS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        SECRETS_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except Exception:  # noqa: BLE001
        pass


def _slot(value: str) -> str:
    return value if value in SLOTS else "primary"


def get_api_key(env_name: str = ENV_NAME, slot: str = "primary") -> Tuple[str, str]:
    slot = _slot(slot)
    from_env = (os.environ.get(env_name) or "").strip()
    if from_env:
        return from_env, "env:" + env_name
    stored = str(_read().get(_FIELD[slot]) or "").strip()
    if stored:
        return stored, "file:" + SECRETS_PATH.name
    # 主接口兼容最早期只有 llm_api_key 的情况（字段名相同，无需额外处理）
    return "", ""


def set_api_key(value: Optional[str], slot: str = "primary") -> bool:
    slot = _slot(slot)
    field = _FIELD[slot]
    data = _read()
    value = (value or "").strip()
    if not value:
        if field in data:
            data.pop(field, None)
            _write(data)
            return True
        return False
    if data.get(field) == value:
        return False
    data[field] = value
    _write(data)
    return True


def mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return "%s…%s" % (key[:4], key[-4:])


def public_state(env_name: str = ENV_NAME, slot: str = "primary") -> Dict[str, Any]:
    key, source = get_api_key(env_name, slot)
    return {"configured": bool(key), "masked": mask(key), "source": source,
            "env_name": env_name, "slot": _slot(slot)}
