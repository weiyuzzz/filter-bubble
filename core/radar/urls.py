"""URL 标准化：去重前先剥掉动态参数。

同一个链接经常带着不同的追踪/排名参数出现：

* 微博热搜的 ``?q=xxx&t=31&band_rank=1&Refer=top``，band_rank 每次抓取都可能变；
* 各类分享链接的 ``utm_source`` / ``spm`` / ``from``；
* 时间戳参数（``_t``、``t``）让每次的 URL 都不同。

如果直接对原始 URL 求哈希，同一条新闻每轮都会算成新条目，既浪费存储也让
"覆盖了多少材料"的统计失真。这里先归一化再哈希。
"""
from __future__ import annotations

import urllib.parse
from typing import Dict, Set

# 通用追踪参数（大小写不敏感）
TRACKING_PARAMS: Set[str] = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "spm", "from", "ref", "referer", "referrer",
    "channel", "source_type", "share_token", "share_id", "share_from",
    # 今日头条热榜：log_pb 里塞了一个每次抓取都变的 impr_id 和展示位信息
    "log_pb", "impr_id", "hot_board_impr_id", "style_id",
}

# 平台特定参数：这些是"位置/来源"而非内容标识
PLATFORM_PARAMS: Dict[str, Set[str]] = {
    # 微博：t=时间范围，band_rank=当前排名，Refer=来源位
    "weibo_hot": {"band_rank", "refer", "t", "display_time"},
    # 贴吧/知乎热榜附带的位置参数
    "tieba": {"pn", "ie"},
    "zhihu_hot": {"utm_psn"},
}

# 这些参数是内容标识，必须保留
KEEP_PARAMS: Set[str] = {"id", "q", "wd", "keyword", "kw", "k", "page", "p"}


def normalize(url: str, source_id: str = "") -> str:
    """返回用于去重比对的 URL；无法解析时原样返回。"""
    if not url:
        return url
    if url.startswith(("radar://", "data:", "javascript:")):
        return url
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    if parsed.scheme not in ("http", "https"):
        return url

    drop = {p.lower() for p in TRACKING_PARAMS}
    drop |= {p.lower() for p in PLATFORM_PARAMS.get(source_id, set())}

    kept = []
    for key, values in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items():
        lowered = key.lower()
        if lowered in KEEP_PARAMS:
            kept.append((key, values))
            continue
        if lowered in drop:
            continue
        if lowered.startswith("utm_"):
            continue
        kept.append((key, values))

    query = urllib.parse.urlencode(
        [(key, value) for key, values in sorted(kept) for value in values],
        doseq=True,
    )

    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, query, ""))


def same(a: str, b: str, source_id: str = "") -> bool:
    return normalize(a, source_id) == normalize(b, source_id)
