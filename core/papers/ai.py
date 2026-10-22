"""论文中文概况生成。

规则：
* 只基于标题和原始摘要，不假装读过全文；
* 摘要没给的信息必须写"摘要未提供足够信息，无法判断"，不得补写；
* 结果与原始摘要分开保存，前端按键名渲染，不会和原文混淆；
* 每篇论文独立生成，不使用整期综合摘要。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .. import ai_client

PROMPT_VERSION = 1

SYSTEM = (
    "你是一名科研助理，负责把论文摘要讲清楚。"
    "只依据用户给出的标题与摘要作答，严禁补充摘要里没有的实验结果、数值、结论或方法。"
    "摘要没有提到的内容，写「摘要未提供足够信息，无法判断」。"
    "输出严格 JSON，中文，简洁。"
)

USER = """论文标题：{title}
来源：{source}
期刊/分类：{venue}
发表日期：{published}

原始摘要：
{abstract}

请输出 JSON：
{{
  "research": "研究什么问题（不超过 50 字）",
  "method": "用了什么方法或技术路线（不超过 60 字，摘要没写就写无法判断）",
  "findings": "摘要明确提到的结果或贡献（不超过 60 字，摘要没写就写无法判断）",
  "significance": "对学科或应用可能的意义（不超过 50 字）",
  "limits": "当前摘要没有提供哪些信息（不超过 50 字）"
}}"""

FIELD_LABELS = [
    ("research", "研究内容"),
    ("method", "核心方法"),
    ("findings", "主要发现"),
    ("significance", "研究意义"),
    ("limits", "信息局限"),
]


def _clean(value: Any, limit: int = 160) -> str:
    text = str(value or "").strip().replace("\n", " ")
    return text[:limit]


def build_prompt(paper: Dict[str, Any]) -> list:
    abstract = (paper.get("abstract") or "").strip()
    if len(abstract) > 3000:
        abstract = abstract[:3000] + "…"
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER.format(
            title=(paper.get("title") or "")[:300],
            source=paper.get("source") or "",
            venue=(paper.get("venue") or "")[:120],
            published=paper.get("published_at") or "",
            abstract=abstract,
        )},
    ]


def normalize(data: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(data, dict):
        return None
    out = {}
    for key, _label in FIELD_LABELS:
        value = _clean(data.get(key), 200)
        if value:
            out[key] = value
    if "research" not in out:
        return None
    return out


def analyze(paper: Dict[str, Any], settings: Dict[str, Any]) -> tuple:
    """返回 (概况 dict 或 None, 错误信息, 模型名)。不抛异常。"""
    ok, reason, llm = ai_client.resolve(settings)
    if not ok:
        return None, reason, ""
    if not (paper.get("abstract") or "").strip():
        return None, "论文没有摘要，无法生成概况", llm.get("model", "")

    text, error = ai_client.chat_result(build_prompt(paper), settings, json_mode=True)
    if not text:
        return None, (error or {}).get("message", "模型没有返回内容"), llm.get("model", "")
    data = ai_client.extract_json(text)
    if data is None:
        retry = build_prompt(paper) + [
            {"role": "assistant", "content": text[:600]},
            {"role": "user", "content": "上面的输出不是合法 JSON，请只输出一个完整 JSON 对象。"},
        ]
        text, error = ai_client.chat_result(retry, settings, json_mode=True)
        data = ai_client.extract_json(text) if text else None
    normalized = normalize(data)
    if not normalized:
        return None, "模型返回的内容缺少必要字段", llm.get("model", "")
    return normalized, "", llm.get("model", "")


def render_text(overview: Optional[Dict[str, Any]]) -> str:
    """给推送/终端用的纯文本版本。"""
    if not overview:
        return ""
    lines = []
    for key, label in FIELD_LABELS:
        value = overview.get(key)
        if value:
            lines.append("%s：%s" % (label, value))
    return "\n".join(lines)
