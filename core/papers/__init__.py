"""学术论文模块：采集公开论文元数据、按学科分类、生成中文 AI 概况。

与雷达模块共用 HTTP、AI 客户端、SQLite 和调度线程机制，但数据表独立（``paper_*``）。
"""
from __future__ import annotations

from . import ai, api, classify, probe, service, sources, store  # noqa: F401

__all__ = ["ai", "api", "classify", "probe", "service", "sources", "store"]
