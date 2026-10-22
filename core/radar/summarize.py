"""事件摘要：优先调用可选大模型，失败或未配置时回退到本地抽取式摘要。

原则不变：**只使用已采集材料的原文片段**，不编造事实、不补写没有出现的数字。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from . import llm, store
from .cluster import tokens

MAX_SUMMARY = 400
# 只摘带单位的数字（7.0级 / 400公里 / 30%），避免把「3比0」「4连冠」这类比分当成指标
_NUMBER_UNIT = re.compile(
    r"(\d+(?:\.\d+)?)\s*(%|％|倍|万|亿|吨|元|美元|人|年|月|日|小时|分钟|秒|"
    r"级|度|米|千米|公里|纳米|nm|um|μm|mm|cm|GHz|MHz|TOPS|TFLOPS|GB|TB|W|kW|颗|台|片)"
)
_PLAIN_LONG_NUMBER = re.compile(r"\b(\d{3,})\b")


def _content_hash(event: Dict[str, Any], items: List[Dict[str, Any]]) -> str:
    blob = "\n".join([event.get("title", "")] + [i.get("title", "") for i in items[:8]])
    return hashlib.sha1(blob.encode("utf-8", "ignore")).hexdigest()[:16]


def _distinct_angles(items: List[Dict[str, Any]], limit: int = 3) -> List[Dict[str, Any]]:
    """挑出信息量不同、措辞不同的来源标题（相似度低的优先）。"""
    chosen: List[Dict[str, Any]] = []
    chosen_tokens: List[set] = []
    for item in items:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        token_set = tokens(title)
        if not token_set:
            continue
        if any(
            len(token_set & other) / float(max(1, len(token_set | other))) > 0.6
            for other in chosen_tokens
        ):
            continue
        chosen.append(item)
        chosen_tokens.append(token_set)
        if len(chosen) >= limit:
            break
    return chosen


def extractive(event: Dict[str, Any], items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """本地抽取式摘要：直接引用已采集的标题，不生成任何新事实。"""
    angles = _distinct_angles(items, limit=3)
    if not angles:
        return {"headline": event.get("title", ""), "summary": "", "points": []}

    parts = []
    for item in angles:
        platform = (item.get("platform") or "").strip()
        title = (item.get("title") or "").strip().rstrip("。")
        parts.append("据%s：%s" % (platform, title) if platform else title)

    summary = "；".join(parts)
    if len(summary) > MAX_SUMMARY:
        summary = summary[:MAX_SUMMARY].rstrip() + "…"

    numbers: List[str] = []
    for item in items[:8]:
        text = item.get("title") or ""
        for value, unit in _NUMBER_UNIT.findall(text):
            token = "%s%s" % (value, unit)
            if token not in numbers and len(numbers) < 4:
                numbers.append(token)
        for value in _PLAIN_LONG_NUMBER.findall(text):
            if value not in numbers and len(numbers) < 4:
                numbers.append(value)
    return {"headline": event.get("title", ""), "summary": summary, "points": numbers}


SYSTEM_PROMPT = (
    "你是一个中文新闻编辑助手。你只能使用用户给出的标题与来源，"
    "严禁补充任何未出现的事实、数字、引语或推断。"
    "如果信息不足以成句，就只做标题整合。输出严格 JSON。"
)

USER_TEMPLATE = """事件标题：{title}
今日热度：{heat}，涉及平台：{platforms}，相关材料 {count} 条。

可用材料（只能引用这些）：
{materials}

请输出 JSON：
{{"headline": "不超过 30 字的中文标题（可以整合但不得新增事实）",
  "summary": "不超过 120 字的中文摘要，只能转述上面的材料，必要时写明来源媒体",
  "points": ["从材料中摘出的关键数字或专有名词，最多 4 个，没有就给空数组"]}}"""


def _prompt(event: Dict[str, Any], items: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    materials = "\n".join(
        "- [%s] %s" % (i.get("platform") or "未知来源", (i.get("title") or "")[:120])
        for i in items[:8]
    )
    metrics = event.get("metrics") or {}
    if isinstance(metrics, str):  # 直接来自 SQL 行时是 JSON 字符串
        try:
            metrics = json.loads(metrics)
        except ValueError:
            metrics = {}
    user = USER_TEMPLATE.format(
        title=(event.get("title") or "")[:120],
        heat=event.get("heat"),
        platforms="、".join((metrics.get("platforms") or [])[:5]) or "未知",
        count=len(items),
        materials=materials or "（无）",
    )
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user}]


def _parse_llm(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    from ..ai_client import extract_json

    data = extract_json(raw)
    if data is None:
        return None
    if not isinstance(data, dict):
        return None
    summary = str(data.get("summary") or "").strip()
    headline = str(data.get("headline") or "").strip()
    points = data.get("points") if isinstance(data.get("points"), list) else []
    points = [str(p)[:40] for p in points[:4] if str(p).strip()]
    if not summary and not headline:
        return None
    if len(summary) > 800:  # 模型跑飞时不采用
        return None
    return {"headline": headline[:60], "summary": summary[:MAX_SUMMARY], "points": points}


def summarize_event(conn, event: Dict[str, Any], items: List[Dict[str, Any]],
                    cfg: Dict[str, Any], use_cache: bool = True) -> Dict[str, Any]:
    """返回 {headline, summary, points, provider, model, cached}。"""
    llm_cfg = cfg.get("llm") or {}
    ok, _reason, resolved = llm.resolve(cfg)
    provider = resolved.get("provider", "") if ok else "extractive"
    model = resolved.get("model", "") if ok else ""
    digest = _content_hash(event, items)
    cache_key = "%s|%s|%s|%s" % (event.get("id", ""), provider, model, digest)

    if use_cache:
        cached = store.get_summary(conn, cache_key)
        if cached:
            return {"headline": cached.get("headline") or event.get("title", ""),
                    "summary": cached.get("summary", ""),
                    "points": cached.get("points") or [],
                    "provider": cached.get("provider", ""),
                    "model": cached.get("model", ""),
                    "cached": True}

    result: Optional[Dict[str, Any]] = None
    used_provider = "extractive"
    used_model = ""
    if ok:
        raw, _err, used_info = llm.chat_result_ex(_prompt(event, items), cfg, json_mode=True)
        parsed = _parse_llm(raw)
        if parsed:
            result = parsed
            # 主备切换后记为实际服务的接口
            used_provider = used_info.get("provider") or provider
            used_model = used_info.get("model") or model

    if result is None:
        result = extractive(event, items)

    payload = {
        "event_id": event.get("id", ""),
        "provider": used_provider,
        "model": used_model,
        "content_hash": digest,
        "headline": result.get("headline") or event.get("title", ""),
        "summary": result.get("summary", ""),
        "points": result.get("points") or [],
    }
    try:
        store.save_summary(conn, cache_key, payload,
                           event.get("last_updated_at") or "")
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - 缓存失败不影响报告
        print("[radar] summary cache write failed:", exc)

    return {**payload, "cached": False}


def backend_status(cfg: Dict[str, Any]) -> Dict[str, Any]:
    info = llm.status(cfg)
    info["fallback"] = "本地抽取式摘要（直接引用已采集标题）"
    return info
