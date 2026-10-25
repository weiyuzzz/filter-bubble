"""项目速读的回退版本。

速读现在以**大模型解读**为主（见 ``ai_brief``）。以前这里用「语言 + topics + 分类」
套模板编出「适合谁 / 怎么用 / 解决什么问题 / 典型场景 / 上手流程」，读起来像是针对
这个项目写的，其实和项目本身没关系。所以模板整体删掉：

* 有大模型 → ``ai_brief`` 用项目的简介、语言、topics、README 摘要生成解读；
* 没有大模型（未启用 / 调用失败 / 额度用尽）→ 这里只回退到 **GitHub 官方简介**，
  不再编造解读，界面上也会标明「GitHub 简介」。

``source`` 字段就是给界面的标识：``ai`` = 已由大模型解读，``github`` = 回退到简介。
"""
from __future__ import annotations

from typing import Any, Dict, List

SOURCE_AI = "ai"
SOURCE_GITHUB = "github"

NO_DESCRIPTION = "这个项目没有填写简介，也没有可用的 AI 解读。"


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip().replace("\n", " ")[:limit]


def build(item: Dict[str, Any], categories: List[str]) -> Dict[str, Any]:
    """回退版速读：只用 GitHub 简介，不做任何推断。"""
    description = _text(item.get("summary_zh") or item.get("description"), 400)
    return {
        "source": SOURCE_GITHUB,
        "what": description or NO_DESCRIPTION,
        "detail": "",
        "highlights": [],
        "limits": "",
        "category": (categories or ["技术项目"])[0],
        "note": "未使用大模型，这里直接显示 GitHub 上的项目简介。",
    }