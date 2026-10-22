"""关键词语法：分组、必须词、排除词、正则、别名、每组上限。

规则文本每行一条，写法和常见热榜工具一致：

```
[AI 与大模型]
+AI
+大模型
/\\b(llm|gpt|claude)\\b/ => 大模型
!抽奖
!广告
@5
```

* `[组名]` 开一个新组；组内规则只作用于该组
* `+词`  必须命中（一个组里出现多个 `+` 时，命中任意一个即可进入该组）
* `!词`  命中即排除（优先级最高）
* `/正则/` 用正则匹配，可带 `=> 别名`
* `@数字` 该组最多保留多少条（0 表示不限）
* 普通行 = 命中即纳入该组

没有 `[组名]` 的规则进入默认组。解析失败的正则会被忽略并打印警告，不会让整份配置失效。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

RE_GROUP = re.compile(r"^\[(.+?)\]$")
RE_LIMIT = re.compile(r"^@(\d+)$")
RE_REGEX = re.compile(r"^/(.+)/[a-z]*$")
RE_ALIAS = re.compile(r"\s*=>\s*(.+)$")


def _parse_token(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip()
    if not text:
        return None

    must = False
    exclude = False
    if text.startswith("+") and len(text) > 1:
        must = True
        text = text[1:].strip()
    elif text.startswith("!") and len(text) > 1:
        exclude = True
        text = text[1:].strip()

    alias = ""
    match = RE_ALIAS.search(text)
    if match:
        alias = match.group(1).strip()
        text = RE_ALIAS.sub("", text).strip()

    regex = RE_REGEX.match(text)
    if regex:
        pattern = regex.group(1)
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            print("[keywords] 正则无效，已忽略：/%s/ (%s)" % (pattern, exc))
            return None
        return {"kind": "regex", "value": pattern, "pattern": compiled,
                "must": must, "exclude": exclude, "alias": alias}

    if not text:
        return None
    return {"kind": "plain", "value": text.lower(), "pattern": None,
            "must": must, "exclude": exclude, "alias": alias}


def _new_group(name: str) -> Dict[str, Any]:
    return {"name": name, "any": [], "must": [], "exclude": [], "limit": 0}


def parse(lines: List[str]) -> List[Dict[str, Any]]:
    """把规则行解析成组列表。空输入返回空列表。"""
    groups: List[Dict[str, Any]] = []
    current = _new_group("默认")

    for raw in lines or []:
        line = str(raw or "").strip()
        if not line or line.startswith("#"):
            continue

        group_match = RE_GROUP.match(line)
        if group_match:
            if current["any"] or current["must"] or current["exclude"] or current["limit"]:
                groups.append(current)
            current = _new_group(group_match.group(1).strip())
            continue

        limit_match = RE_LIMIT.match(line)
        if limit_match:
            current["limit"] = int(limit_match.group(1))
            continue

        token = _parse_token(line)
        if not token:
            continue
        if token["exclude"]:
            current["exclude"].append(token)
        elif token["must"]:
            current["must"].append(token)
        else:
            current["any"].append(token)

    if current["any"] or current["must"] or current["exclude"] or current["limit"]:
        groups.append(current)
    return groups


def _hit(token: Dict[str, Any], text: str) -> bool:
    if token["kind"] == "regex":
        return bool(token["pattern"].search(text))
    return token["value"] in text


def match(groups: List[Dict[str, Any]], text: str) -> List[Dict[str, Any]]:
    """返回命中的组（排除词优先）。"""
    text = (text or "").lower()
    hit_groups: List[Dict[str, Any]] = []
    for group in groups:
        if any(_hit(token, text) for token in group["exclude"]):
            continue
        if group["must"] and not any(_hit(token, text) for token in group["must"]):
            continue
        if group["any"] and not any(_hit(token, text) for token in group["any"]):
            # 只有 must 的组也算命中（上面已处理）
            if group["must"]:
                hit_groups.append(group)
            continue
        hit_groups.append(group)
    return hit_groups


def excluded(groups: List[Dict[str, Any]], text: str) -> bool:
    """是否命中任意组的排除词。"""
    text = (text or "").lower()
    return any(_hit(token, text) for group in groups for token in group["exclude"])
