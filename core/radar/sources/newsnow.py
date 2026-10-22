"""newsnow 聚合 API 适配器。

为什么需要它：抖音、知乎、贴吧这些平台没有可直连的公开接口（需要登录态或
签名），直连适配器只能标成"未接入"。newsnow 把各平台的公开热榜聚合成一个
JSON 接口，用同一个适配器就能覆盖。

代价与约束：
* 数据来自第三方服务，可用性和准确性不由我们控制 → 单独计入熔断，失败不影响直连源；
* 只返回标题与链接，没有正文 → 这些源只作为"热点发现"，不参与深度调查；
* 必须做域名校验（``expected_domain``）：第三方返回的链接如果指向别的域名，
  说明数据被篡改或不匹配，直接丢弃而不是入库。
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Any, Dict, List

from ..fetch import clean_text, get

DEFAULT_API = "https://newsnow.busiyi.world/api/s"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

ALLOWED_STATUS = ("success", "cache")


def _host(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    if parsed.scheme != "https":
        return ""
    return (parsed.hostname or "").lower()


def domain_ok(url: str, expected: str) -> bool:
    """链接必须是 https 且域名匹配（允许子域名）。空 URL 视为通过。"""
    if not expected or not url:
        return True
    host = _host(url)
    if not host:
        return False
    expected = expected.lower().strip()
    return host == expected or host.endswith("." + expected)


def fetch(spec: Dict[str, Any], make_item) -> List[Dict[str, Any]]:
    api_id = str(spec.get("api_id") or spec["source_id"])
    api = str(spec.get("api_url") or DEFAULT_API).rstrip("/")
    url = "%s?id=%s&latest" % (api, urllib.parse.quote(api_id))

    result = get(url, headers=HEADERS, timeout=float(spec.get("timeout") or 15),
                 rate_key="newsnow", min_interval=1.2)
    if not result.ok:
        raise RuntimeError("newsnow 请求失败：%s" % (result.error or result.status))

    try:
        payload = json.loads(result.text)
    except ValueError:
        raise RuntimeError("newsnow 返回的不是 JSON")

    status = payload.get("status")
    if status not in ALLOWED_STATUS:
        raise RuntimeError("newsnow 返回状态异常：%s" % status)

    raw_items = payload.get("items") or []
    if not isinstance(raw_items, list) or not raw_items:
        raise RuntimeError("newsnow 返回空列表")

    expected = str(spec.get("expected_domain") or "")
    verify = bool(spec.get("verify_domain", True))
    max_items = int(spec.get("max_items") or 30)

    items: List[Dict[str, Any]] = []
    rejected = 0
    for idx, raw in enumerate(raw_items[:max_items], start=1):
        if not isinstance(raw, dict):
            continue
        title = clean_text(raw.get("title"), 300)
        if not title:
            continue

        link = ""
        for key in ("url", "mobileUrl"):
            candidate = str(raw.get(key) or "").strip()
            if candidate and (not verify or not expected or domain_ok(candidate, expected)):
                link = candidate
                break
        if not link:
            rejected += 1
            continue

        items.append(make_item(
            spec,
            title=title,
            url=link,
            raw_content="%s热榜第 %d 位（经 newsnow 聚合）" % (spec.get("platform") or "", idx),
            engagement={"rank": idx},
            source_type=spec.get("source_type", "social_trend"),
            language=spec.get("language", "zh"),
        ))

    if rejected and not items:
        raise RuntimeError("域名校验失败：%d 条链接与 %s 不匹配，已全部丢弃" % (rejected, expected))
    if rejected:
        print("[newsnow] %s 丢弃 %d 条域名不匹配的链接" % (spec.get("source_id"), rejected))
    if not items:
        raise RuntimeError("newsnow 没有可用条目")
    return items
