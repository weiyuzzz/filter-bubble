"""采集监控模块：把真实的后台运行、来源、条目与日志暴露成只读接口。

* ``store``      —— 监控表、既有真实历史记录的导入、全部查询与统计口径
* ``instrument`` —— 业务流水线上的埋点（运行 / 阶段 / 来源 / 条目 / 日志）
* ``api``        —— ``/api/monitor/*`` 只读接口
* ``redact``     —— 密钥与敏感信息脱敏、外链协议校验

监控是**旁路**：任何埋点或接口异常都不会影响采集、聚类、日报、推送本身。
"""
from __future__ import annotations

from . import api, instrument, redact, store  # noqa: F401

#: 便捷别名：``from core import monitor`` 之后直接用 ``monitor.run(...)``
run = instrument.run
current = instrument.current
log_event = instrument.log_event
redact_text = redact.redact_text

__all__ = ["api", "instrument", "redact", "store", "run", "current",
           "log_event", "redact_text"]