"""地理能力模块（阶段三）。

职责边界：
* **离线**：不在请求路径上调用任何在线地理编码服务；
* **可追溯**：每个定位结果都带 ``geo_method`` / ``geo_source`` / ``geo_confidence``，
  能回溯到具体的原始条目或来源响应；
* **不臆测**：无法可靠判断时返回 ``None``，绝不因为"新闻提到了某个国家"就把事件
  认定发生在该国，也不做跨国事件的虚假地理质心。
"""
from __future__ import annotations

__all__ = ["countries", "gazetteer", "geocode", "project", "validate"]