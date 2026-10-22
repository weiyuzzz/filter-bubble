"""兼容层：大模型客户端已移到全局 ``core.ai_client``。

保留这个模块是为了不破坏既有调用（``from . import llm``），行为完全一致。
"""
from __future__ import annotations

from ..ai_client import (  # noqa: F401
    ERROR_LOG,
    PROVIDERS,
    STATS,
    chat,
    chat_result,
    read_errors,
    resolve,
    status,
    test_connection,
)

__all__ = [
    "ERROR_LOG", "PROVIDERS", "STATS", "chat", "chat_result", "read_errors",
    "resolve", "status", "test_connection",
]
