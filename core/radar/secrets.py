"""本地密钥存储。

大模型的 API key 不写进 social_radar.json（那会被提交到 git），而是放在
``data/radar_secrets.json``，并且：

* 只接受来自本机 Web 界面的写入；
* 任何接口都不会把它读回来，只返回「是否已配置」和掩码；
* 环境变量仍然优先，方便用 .env 或系统环境管理密钥。
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..config import DATA_DIR

SECRETS_PATH = Path(os.environ.get("RADAR_SECRETS", str(DATA_DIR / "radar_secrets.json")))
ENV_NAME = "RADAR_LLM_API_KEY"


def _read() -> Dict[str, Any]:
    try:
        if SECRETS_PATH.exists():
            data = json.loads(SECRETS_PATH.read_text(encoding="utf-8-sig") or "{}")
            if isinstance(data, dict):
                return data
    except Exception as exc:  # noqa: BLE001
        print("[radar] secrets read failed:", exc)
    return {}


def _write(data: Dict[str, Any]) -> None:
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SECRETS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:  # 尽量收紧权限（Windows 上可能不生效，失败不影响功能）
        SECRETS_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except Exception:  # noqa: BLE001
        pass


def get_api_key(env_name: str = ENV_NAME) -> Tuple[str, str]:
    """返回 (key, 来源)。环境变量优先，其次本地密钥文件。"""
    from_env = (os.environ.get(env_name) or "").strip()
    if from_env:
        return from_env, "env:" + env_name
    stored = str(_read().get("llm_api_key") or "").strip()
    if stored:
        return stored, "file:" + SECRETS_PATH.name
    return "", ""


def set_api_key(value: Optional[str]) -> bool:
    """写入或清除本地密钥。返回是否发生了变更。"""
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
    return {
        "configured": bool(key),
        "masked": mask(key),
        "source": source,
        "env_name": env_name,
    }
