"""全局密钥存储（大模型 API Key）。

密钥按「服务商 / 接口」隔离，而不是按槽位：
* ``keys`` 字典以 *key_ref* 为键，例如 ``deepseek`` / ``qianfan`` / ``primary``；
* 每个接口（slot）通过 ``key_ref`` 指向自己那把 key，换服务商时地址与 key 一起换，
  不会再出现「地址换成 DeepSeek，key 还是 OpenRouter 的」这种 401；
* 旧版本只有 ``llm_api_key`` / ``llm_api_key_fallback`` 两个字段，读取时自动映射成
  ``primary`` / ``fallback`` 两个 ref，写入后统一收敛到 ``keys``。

* 只写 ``data/ai_secrets.json``，该文件已在 .gitignore 里；
* 接口只返回「是否已配置」与掩码，绝不回传明文；
* 环境变量优先，方便用 .env / 系统环境变量管理；命名规则：
  ``primary`` → ``RADAR_LLM_API_KEY``，``fallback`` → ``RADAR_LLM_API_KEY_FALLBACK``，
  其它 ref → ``RADAR_LLM_API_KEY_<REF>``（非字母数字转下划线后大写）。
"""
from __future__ import annotations

import json
import os
import re
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


def normalize_ref(ref: Optional[str]) -> str:
    """把任意 key_ref 归一化成文件名友好的小写短串。"""
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(ref or "").strip().lower()).strip("-._")
    return text or "primary"


def env_name_for(ref: Optional[str]) -> str:
    """某个 key_ref 对应的环境变量名（primary/fallback 沿用旧名字）。"""
    ref = normalize_ref(ref)
    if ref == "primary":
        return ENV_NAME
    if ref == "fallback":
        return FALLBACK_ENV_NAME
    return "RADAR_LLM_API_KEY_" + re.sub(r"[^A-Z0-9]+", "_", ref.upper()).strip("_")


def _read() -> Dict[str, Any]:
    for path in (SECRETS_PATH, LEGACY_PATH):
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
                if isinstance(data, dict):
                    return _migrate(data)
        except Exception as exc:  # noqa: BLE001
            print("[ai] secrets read failed:", exc)
    return {}


def _migrate(data: Dict[str, Any]) -> Dict[str, Any]:
    """旧字段 → keys 字典（只在内存里迁移，下次写入才落盘）。"""
    keys = data.get("keys")
    keys = dict(keys) if isinstance(keys, dict) else {}
    for ref, field in _FIELD.items():
        value = str(data.get(field) or "").strip()
        if value and not keys.get(ref):
            keys[ref] = value
    data["keys"] = {normalize_ref(k): str(v or "").strip()
                    for k, v in keys.items() if str(v or "").strip()}
    return data


def _write(data: Dict[str, Any]) -> None:
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"keys": {normalize_ref(k): str(v or "").strip()
                        for k, v in (data.get("keys") or {}).items() if str(v or "").strip()}}
    SECRETS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
    try:
        SECRETS_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except Exception:  # noqa: BLE001
        pass


def get_api_key(env_name: Optional[str] = None, ref: str = "primary") -> Tuple[str, str]:
    """返回 (key, 来源)。环境变量优先，其次本地文件。"""
    ref = normalize_ref(ref)
    env_name = str(env_name or env_name_for(ref)).strip()
    if env_name:
        from_env = (os.environ.get(env_name) or "").strip()
        if from_env:
            return from_env, "env:" + env_name
    stored = str((_read().get("keys") or {}).get(ref) or "").strip()
    if stored:
        return stored, "file:%s#%s" % (SECRETS_PATH.name, ref)
    return "", ""


def has_api_key(ref: str = "primary") -> bool:
    return bool(get_api_key(None, ref)[0])


def set_api_key(value: Optional[str], ref: str = "primary") -> bool:
    """写入/清除某个 ref 的 key。返回是否发生了变化。"""
    ref = normalize_ref(ref)
    data = _read()
    keys = dict(data.get("keys") or {})
    value = (value or "").strip()
    if not value:
        changed = keys.pop(ref, None) is not None
        if changed:
            _write({"keys": keys})
        return changed
    if keys.get(ref) == value:
        return False
    keys[ref] = value
    _write({"keys": keys})
    return True


def clear_api_key(ref: str = "primary") -> bool:
    return set_api_key(None, ref)


def mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return "%s…%s" % (key[:4], key[-4:])


def public_state(env_name: Optional[str] = None, ref: str = "primary") -> Dict[str, Any]:
    ref = normalize_ref(ref)
    key, source = get_api_key(env_name, ref)
    return {
        "configured": bool(key),
        "masked": mask(key),
        "source": source,
        "env_name": str(env_name or env_name_for(ref)),
        "ref": ref,
        "slot": ref,  # 兼容旧字段名
    }


def refs() -> list:
    """当前已存过 key 的所有 ref。"""
    return sorted((_read().get("keys") or {}).keys())