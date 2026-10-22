"""全局密钥存储（大模型 API Key）。

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


def get_api_key(env_name: str = ENV_NAME) -> Tuple[str, str]:
    from_env = (os.environ.get(env_name) or "").strip()
    if from_env:
        return from_env, "env:" + env_name
    stored = str(_read().get("llm_api_key") or "").strip()
    if stored:
        return stored, "file:" + SECRETS_PATH.name
    return "", ""


def set_api_key(value: Optional[str]) -> bool:
    data = _read()
    value = (value or "").strip()
    if not value:
        if "llm_api_key" in data:
            data.pop("llm_api_key", None)
            _write(data)
            return True
        return False
    if data.get("llm_api_key") == value:
        return False
    data["llm_api_key"] = value
    _write(data)
    return True


def mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return "%s…%s" % (key[:4], key[-4:])


def public_state(env_name: str = ENV_NAME) -> Dict[str, Any]:
    key, source = get_api_key(env_name)
    return {"configured": bool(key), "masked": mask(key), "source": source, "env_name": env_name}
