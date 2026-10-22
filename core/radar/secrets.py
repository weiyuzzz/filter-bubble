"""兼容层：密钥存储已移到全局 ``core.ai_secrets``。"""
from __future__ import annotations

from ..ai_secrets import (  # noqa: F401
    ENV_NAME,
    LEGACY_PATH,
    SECRETS_PATH,
    get_api_key,
    mask,
    public_state,
    set_api_key,
)

__all__ = ["ENV_NAME", "LEGACY_PATH", "SECRETS_PATH", "get_api_key", "mask",
           "public_state", "set_api_key"]
