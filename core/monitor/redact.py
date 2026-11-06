"""脱敏：监控页面能看到的所有文本（标题、URL、错误、日志、details_json）都可能含密钥。

上游把 api_key 塞进错误正文、把 sendkey 塞进 URL 路径（Server酱）、把 token 塞进查询串，
这些都会顺着错误信息流到监控界面上。所以监控层有两条硬约束：

1. **写库前**就脱敏（``redact_text`` / ``redact_obj``）—— 监控库不会存未脱敏的密钥；
2. **出接口前**再脱敏一次（``api`` 层对 message/details 都过一遍）—— 老数据或未来新增
   写入路径漏了，也不会把密钥发给浏览器。

脱敏只做减法，不做任何猜测性改写：要么把敏感片段替换成固定的占位符，要么整段丢弃。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MASK = "***"

# 键名里出现这些词 → 对应值一律脱敏（用于 dict / JSON 文本）
SENSITIVE_KEY = re.compile(
    r"(?i)(api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|auth[_-]?token|token|"
    r"secret|password|passwd|pwd|send[_-]?key|sendkey|authorization|cookie|session|"
    r"credential|private[_-]?key|client[_-]?secret|bearer)"
)

# URL 查询串里出现这些参数名 → 整个参数删掉（不是替换值，避免拼出半截链接）
SENSITIVE_QUERY = frozenset({
    "key", "api_key", "apikey", "api-key", "token", "access_token", "auth", "authorization",
    "secret", "client_secret", "password", "passwd", "pwd", "sign", "signature", "sig",
    "sendkey", "send_key", "code", "session", "cookie", "credential",
})

_TEXT_RULES = [
    # (正则, 替换模板) —— 模板用 \1/\3 保留上下文，只把密钥本身换成占位符
    # Server酱 / Server酱³ 的 SendKey（SCT.../sctp...），也可能出现在 URL 路径里
    (re.compile(r"(?i)\bsctp?[A-Za-z0-9]{8,}\b"), MASK),
    # pHash / OpenAI 风格 sk-...
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"), MASK),
    # Bearer <token>
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"), MASK),
    # AWS access key id
    (re.compile(r"\bAKIA[0-9A-Z]{12,}\b"), MASK),
    # sctapi.ftqq.com/<sendkey>.send —— Server酱把 key 放在路径里
    (re.compile(r"(?i)(sctapi\.ftqq\.com/)[A-Za-z0-9_\-]{6,}(\.send)"), r"\1" + MASK + r"\2"),
    # URL 里的 userinfo：https://user:pass@host/
    (re.compile(r"(?i)(://[^/\s:@]{1,64}:)[^/\s:@]{2,64}(@)"), r"\1" + MASK + r"\2"),
]

# "api_key": "xxxx" / api_key=xxxx / 'token': 'xxxx'
_TEXT_KEY_VALUE = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|auth[_-]?token|token|"
    r"secret|password|passwd|pwd|send[_-]?key|sendkey|authorization|client[_-]?secret)"
    r"[\"']?\s*[:=]\s*[\"']?)([^\"',\s}{]{4,})"
)

# 查询串里的敏感参数：?key=xxx&token=yyy
_TEXT_QUERY = re.compile(
    r"(?i)([?&](?:key|api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|token|secret|"
    r"password|passwd|pwd|sign|signature|sig|send[_-]?key|sendkey|session|cookie|"
    r"credential|code)=)([^&\s\"'<>]+)"
)


def redact_text(value: Any) -> str:
    """脱敏一段自由文本。返回新字符串，输入不会被修改。"""
    text = "" if value is None else str(value)
    for rule, replacement in _TEXT_RULES:
        text = rule.sub(replacement, text)
    text = _TEXT_KEY_VALUE.sub(lambda m: m.group(1) + MASK, text)
    text = _TEXT_QUERY.sub(lambda m: m.group(1) + MASK, text)
    return text


def redact_key(key: Any) -> bool:
    return bool(SENSITIVE_KEY.search(str(key or "")))


def redact_obj(value: Any, depth: int = 0) -> Any:
    """递归脱敏结构化数据（dict/list/标量）。对象本身不被修改。"""
    if depth > 6:
        return MASK
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 200:
                break
            name = str(key)
            out[name] = MASK if redact_key(name) else redact_obj(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_obj(item, depth + 1) for item in value[:200]]
    if isinstance(value, str):
        return redact_text(value)
    return value


def strip_url_secrets(url: Any) -> str:
    """去掉 URL 里的敏感查询参数与 userinfo；用于任何准备渲染成链接的地址。"""
    text = "" if url is None else str(url).strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return redact_text(text)
    if not parts.scheme or not parts.netloc:
        return redact_text(text)
    netloc = parts.netloc.rsplit("@", 1)[-1] if "@" in parts.netloc else parts.netloc
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in SENSITIVE_QUERY]
    return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), ""))


def safe_link(url: Any) -> str:
    """只有 http/https 才允许作为外链渲染；其余（javascript:/data:/radar://）返回空串。"""
    text = strip_url_secrets(url)
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https"):
        return ""
    if not parts.netloc:
        return ""
    return text