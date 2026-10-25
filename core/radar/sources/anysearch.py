"""AnySearch 适配器：统一搜索引擎（网页/学术/代码/金融/安全）。

它和 newsnow 的区别，也是接它的理由：
* newsnow 给的是各平台的**热榜排名**（标题 + 链接），没有正文；
* AnySearch 给的是**按查询召回的结果**（标题 + 链接 + 摘要 + 清洗后内容），
  并且有 ``/v1/extract`` 能把任意公开链接抓成正文。

因此它有两种用法：
1. ``fetch()`` —— 作为雷达的**发现源**（spec 里带 ``query``），和 RSS 源平级；
2. ``search()`` / ``extract()`` —— 供调查阶段做**外部取证**（见 ``investigate.gather_evidence``）。

约束（都是实测确认的，不是猜的）：
* 单次 ``max_results`` 上限 10；
* 限流窗口 20 次（响应头 ``X-Ratelimit-Limit``），超额 429；额度用尽 402；
* 未配 key 时走匿名通道，也能用，但额度更低；
* 402 的响应体里可能带 ``username`` / ``password`` / ``api_key``（匿名自动注册流程），
  所以**错误里只保留状态码**，绝不把响应体写进日志（见 ``fetch._perform``）；
* 返回的一切文本都是不可信输入，统一过 ``clean_text``，长度按字段截断。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from ..fetch import FetchResult, clean_text, post_json

DEFAULT_API = "https://api.anysearch.com"

# 来源类型：既不是"热榜"也不是"媒体"，单独一类，便于在报告里区分
# （不在 score.NEWS_TYPES 里 —— 搜索引擎材料不能算"媒体报道"）
SOURCE_TYPE = "search"

PLATFORM = "AnySearch"

# 搜索引擎一次最多只给 10 条，多要无效
MAX_RESULTS_CAP = 10

# 正文抓取上限：extract 实测能返回 3 万字以上，入库前必须截断
CONTENT_LIMIT = 4000

# 402 的响应体可能含自动生成的账号密码，这里只暴露状态，不暴露 body
_QUOTA_HINT = {
    400: "请求体不合法（extract 只接受 {\"url\": \"...\"} 这一个字段）",
    401: "API Key 无效（key 与接口地址不匹配）",
    402: "额度用尽（免费额度按窗口计）",
    403: "Key 已过期 / 账号被停用",
    415: "Content-Type 必须是 application/json",
    422: "该站点抓不到正文（付费墙 / 反爬 / 需要 JS 渲染）",
    429: "触发限流（窗口 20 次）",
    502: "上游搜索服务不可用",
}


def _base(spec_or_cfg: Dict[str, Any]) -> str:
    section = spec_or_cfg.get("anysearch") or {}
    base = str(section.get("api_base") or spec_or_cfg.get("api_base") or DEFAULT_API).rstrip("/")
    return base or DEFAULT_API


def api_key(cfg: Dict[str, Any]) -> str:
    """从环境变量取 key（和 openalex_api_key_env 同一套做法，key 不进仓库）。"""
    section = cfg.get("anysearch") or {}
    env_name = str(section.get("api_key_env") or "ANYSEARCH_API_KEY").strip()
    if not env_name:
        return ""
    return (os.environ.get(env_name) or "").strip()


def _headers(key: str) -> Dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    return headers


def _raise_for(result: FetchResult) -> None:
    hint = _QUOTA_HINT.get(result.status, "")
    detail = ("HTTP %s" % result.status) if result.status else (result.error or "请求失败")
    raise RuntimeError("AnySearch 请求失败：%s%s" % (detail, ("（%s）" % hint) if hint else ""))


def _results(payload: Any) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        raise RuntimeError("AnySearch 返回的不是 JSON 对象")
    if payload.get("code") not in (0, None):
        raise RuntimeError("AnySearch 返回错误：code=%s" % payload.get("code"))
    data = payload.get("data") or {}
    rows = data.get("results")
    return [r for r in (rows or []) if isinstance(r, dict)]


def search(query: str, cfg: Dict[str, Any], max_results: int = 10,
           tag: str = "", zone: str = "", language: str = "",
           timeout: Optional[float] = None) -> List[Dict[str, Any]]:
    """调 ``/v1/search``，返回 [{title, url, snippet, content, request_id}]。"""
    query = (query or "").strip()
    if not query:
        return []

    section = cfg.get("anysearch") or {}
    tag = tag or str(section.get("tag") or "")
    zone = zone or str(section.get("zone") or "")
    language = language or str(section.get("language") or "")
    payload: Dict[str, Any] = {"query": query, "max_results": max(1, min(int(max_results), MAX_RESULTS_CAP))}
    if tag:
        payload["tag"] = tag
    if zone:
        payload["zone"] = zone
    if language:
        payload["language"] = language

    result, res = post_json(
        _base(cfg) + "/v1/search", payload, headers=_headers(api_key(cfg)),
        timeout=float(timeout or section.get("timeout_seconds") or 20),
        retries=int(section.get("retries") or 1),
        rate_key="anysearch", min_interval=float(section.get("min_interval_seconds") or 1.0),
    )
    if not res.ok:
        _raise_for(res)

    items: List[Dict[str, Any]] = []
    for row in _results(result):
        url = clean_text(row.get("url"), 1000)
        title = clean_text(row.get("title"), 300)
        if not title and not url:
            continue
        items.append({
            "title": title,
            "url": url,
            "snippet": clean_text(row.get("snippet"), 500),
            "content": clean_text(row.get("content"), CONTENT_LIMIT),
        })
    return items


def extract(url: str, cfg: Dict[str, Any], timeout: Optional[float] = None) -> str:
    """调 ``/v1/extract`` 抓正文。

    请求体必须**严格**只有一个 ``url`` 字段：多传字段（例如 format）会被判 400，
    这是实测踩过的坑，不要再加参数。
    """
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return ""
    section = cfg.get("anysearch") or {}
    result, res = post_json(
        _base(cfg) + "/v1/extract", {"url": url}, headers=_headers(api_key(cfg)),
        timeout=float(timeout or section.get("extract_timeout_seconds") or 30),
        retries=0,  # 抓正文很贵（占额度），失败不重试
        rate_key="anysearch", min_interval=float(section.get("min_interval_seconds") or 1.0),
    )
    if not res.ok:
        # 抓不到正文很常见（付费墙 / 反爬），422 之类不当成失败，调用方用摘要即可
        print("[anysearch] 抓正文失败 HTTP %s（%s）：%s"
              % (res.status, _QUOTA_HINT.get(res.status, "未知原因"), url[:80]))
        return ""
    if not isinstance(result, dict):
        return ""
    data = result.get("data") or {}
    return clean_text(data.get("content"), CONTENT_LIMIT)


def fetch(spec: Dict[str, Any], make_item) -> List[Dict[str, Any]]:
    """雷达发现源适配器：一个 spec = 一条固定查询。

    只做搜索，不做 extract —— 每条结果都抓正文的话，10 条就吃掉半个限流窗口。
    """
    cfg = {"anysearch": spec.get("_anysearch_cfg") or {}}
    query = str(spec.get("query") or "").strip()
    if not query:
        raise RuntimeError("AnySearch 源缺少 query 配置")

    rows = search(
        query, cfg,
        max_results=int(spec.get("max_results") or spec.get("max_items") or 10),
        tag=str(spec.get("tag") or ""),
        zone=str(spec.get("zone") or ""),
        language=str(spec.get("language") or ""),
        timeout=float(spec.get("timeout") or 20),
    )
    if not rows:
        raise RuntimeError("AnySearch 没有返回结果：%s" % query)

    max_items = int(spec.get("max_items") or 10)
    items: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows[:max_items], start=1):
        title = row.get("title") or row["url"]
        if not title:
            continue
        body = row.get("content") or row.get("snippet") or ""
        items.append(make_item(
            spec,
            title=title,
            url=row.get("url", ""),
            # 搜索引擎给的正文是片段，不是原文，标注清楚免得被当成媒体报道
            raw_content="%s（AnySearch 搜索结果：%s）" % (body, query),
            engagement={"rank": idx},
            source_type=SOURCE_TYPE,
            language=spec.get("language", ""),
        ))
    if not items:
        raise RuntimeError("AnySearch 结果全部缺少标题")
    return items