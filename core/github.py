"""GitHub data collection: Trending HTML + REST API, with graceful degradation."""
from __future__ import annotations

import base64
import datetime as dt
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from . import config, db

UA_HEADERS = {"User-Agent": config.USER_AGENT, "Accept-Encoding": "identity"}
RATE_REMAINING: Optional[int] = None


def _headers(api: bool = False) -> Dict[str, str]:
    h = dict(UA_HEADERS)
    if api:
        h["Accept"] = "application/vnd.github+json"
        h["X-GitHub-Api-Version"] = "2022-11-28"
        if config.GITHUB_TOKEN:
            h["Authorization"] = "Bearer " + config.GITHUB_TOKEN
    return h


def http_get(url: str, api: bool = False, timeout: Optional[float] = None) -> Optional[bytes]:
    global RATE_REMAINING
    last: Optional[Exception] = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=_headers(api))
            with urllib.request.urlopen(req, timeout=timeout or config.HTTP_TIMEOUT) as resp:
                if api:
                    raw = resp.headers.get("X-RateLimit-Remaining")
                    if raw is not None:
                        try:
                            RATE_REMAINING = int(raw)
                        except ValueError:
                            pass
                return resp.read()
        except Exception as exc:  # noqa: BLE001 - network errors are expected
            last = exc
            time.sleep(0.8 + attempt * 1.2)
    print("[github] GET failed %s: %s" % (url, last))
    return None


def _text(fragment: str) -> str:
    fragment = re.sub(r"<svg.*?</svg>", " ", fragment, flags=re.S)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(fragment)).strip()


def _int(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else 0


def parse_trending(markup: str, since: str) -> List[Dict[str, Any]]:
    articles = re.findall(r'<article class="Box-row">(.*?)</article>', markup, re.S)
    out: List[Dict[str, Any]] = []
    for idx, block in enumerate(articles):
        h2 = re.search(r"<h2[^>]*>(.*?)</h2>", block, re.S)
        if not h2:
            continue
        link = re.search(r'href="/([^/"]+)/([^/"]+)"', h2.group(1))
        if not link:
            continue
        owner, name = link.group(1), link.group(2)
        desc_m = re.search(r'<p class="col-9[^"]*"[^>]*>(.*?)</p>', block, re.S)
        lang_m = re.search(r'itemprop="programmingLanguage">(.*?)</span>', block, re.S)
        star_m = re.search(r'href="[^"]*/stargazers"[^>]*>(.*?)</a>', block, re.S)
        fork_m = re.search(r'href="[^"]*/forks"[^>]*>(.*?)</a>', block, re.S)
        today_m = re.search(r'class="d-inline-block float-sm-right"[^>]*>(.*?)</span>', block, re.S)
        today_text = _text(today_m.group(1)) if today_m else ""
        item: Dict[str, Any] = {
            "full_name": "%s/%s" % (owner, name),
            "owner": owner,
            "name": name,
            "description": _text(desc_m.group(1)) if desc_m else "",
            "url": "https://github.com/%s/%s" % (owner, name),
            "language": _text(lang_m.group(1)) if lang_m else "",
            "stars": _int(_text(star_m.group(1))) if star_m else 0,
            "forks": _int(_text(fork_m.group(1))) if fork_m else 0,
            "avatar": "https://github.com/%s.png?size=96" % owner,
            "topics": [],
            "sources": ["daily" if since == "daily" else "weekly"],
            "daily_rank": idx + 1 if since == "daily" else None,
            "weekly_rank": idx + 1 if since == "weekly" else None,
        }
        if since == "daily":
            m = re.search(r"([\d,]+)\s+stars?\s+today", today_text)
            item["stars_today"] = _int(m.group(1)) if m else 0
        else:
            m = re.search(r"([\d,]+)\s+stars?\s+this week", today_text)
            item["stars_week"] = _int(m.group(1)) if m else 0
        out.append(item)
    return out


def fetch_trending(since: str) -> List[Dict[str, Any]]:
    raw = http_get("https://github.com/trending?since=%s" % since)
    if not raw:
        return []
    return parse_trending(raw.decode("utf-8", "replace"), since)


def _search(query: str, per_page: int = 30) -> List[Dict[str, Any]]:
    url = (
        "https://api.github.com/search/repositories?q=%s&sort=stars&order=desc&per_page=%d"
        % (urllib.parse.quote(query), per_page)
    )
    raw = http_get(url, api=True)
    if not raw:
        return []
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return []
    items = []
    for it in payload.get("items", []):
        owner = (it.get("owner") or {}).get("login") or it["full_name"].split("/")[0]
        lic = it.get("license") or {}
        items.append(
            {
                "full_name": it["full_name"],
                "owner": owner,
                "name": it["name"],
                "description": it.get("description") or "",
                "url": it["html_url"],
                "language": it.get("language") or "",
                "stars": it.get("stargazers_count") or 0,
                "forks": it.get("forks_count") or 0,
                "avatar": (it.get("owner") or {}).get("avatar_url")
                or "https://github.com/%s.png?size=96" % owner,
                "topics": it.get("topics") or [],
                "homepage": it.get("homepage") or "",
                "license": lic.get("spdx_id") or lic.get("name") or "",
                "created_at": it.get("created_at"),
                "pushed_at": it.get("pushed_at"),
                "sources": ["new"],
                "meta_at": db.now_iso(),
            }
        )
    return items


def search_recent(days: int, per_page: int = 30) -> List[Dict[str, Any]]:
    created = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    return _search("created:>%s stars:>50" % created, per_page=per_page)


def enrich(repo: Dict[str, Any], conn, max_age_hours: int = 24) -> Dict[str, Any]:
    """Add topics / created_at / license via the REST API. Uses the local cache."""
    cached = db.get_repo(conn, repo["full_name"])
    if cached and cached.get("meta_at") and cached.get("created_at"):
        try:
            stamped = dt.datetime.fromisoformat(cached["meta_at"])
            if (dt.datetime.now().astimezone() - stamped).total_seconds() < max_age_hours * 3600:
                merged = dict(repo)
                for key in ("topics", "homepage", "license", "created_at", "pushed_at", "avatar"):
                    if cached.get(key):
                        merged[key] = cached[key]
                merged["meta_at"] = cached["meta_at"]
                return merged
        except (ValueError, TypeError):
            pass

    if RATE_REMAINING is not None and RATE_REMAINING < 2:
        return repo
    raw = http_get("https://api.github.com/repos/" + repo["full_name"], api=True)
    if not raw:
        return repo
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return repo
    if payload.get("message"):
        return repo
    lic = payload.get("license") or {}
    merged = dict(repo)
    merged.update(
        {
            "description": payload.get("description") or repo.get("description") or "",
            "stars": payload.get("stargazers_count") or repo.get("stars") or 0,
            "forks": payload.get("forks_count") or repo.get("forks") or 0,
            "language": payload.get("language") or repo.get("language") or "",
            "topics": payload.get("topics") or [],
            "homepage": payload.get("homepage") or "",
            "license": lic.get("spdx_id") or lic.get("name") or "",
            "created_at": payload.get("created_at"),
            "pushed_at": payload.get("pushed_at"),
            "avatar": (payload.get("owner") or {}).get("avatar_url") or repo.get("avatar"),
            "meta_at": db.now_iso(),
        }
    )
    db.save_repo(conn, merged)
    return merged


def fetch_readme(full_name: str, limit: int = 70000) -> str:
    """README via the API (raw.githubusercontent is unreliable on some networks)."""
    if RATE_REMAINING is not None and RATE_REMAINING < 4:
        return ""
    raw = http_get("https://api.github.com/repos/%s/readme" % full_name, api=True, timeout=15)
    if not raw:
        return ""
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
        content = payload.get("content") or ""
        if payload.get("encoding") == "base64":
            data = base64.b64decode(content)
        else:
            data = content.encode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""
    return data[:limit].decode("utf-8", "replace")


def readme_excerpt(markdown: str) -> str:
    """Pick the first substantive prose paragraph out of a README."""
    if not markdown:
        return ""
    text = re.sub(r"<!--.*?-->", " ", markdown, flags=re.S)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)", " ", text)
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "|", ">", "---", "===")):
            continue
        if line.startswith("[![" ) or "badge" in line.lower() or line.count("](") > 2:
            continue
        if re.fullmatch(r"[\W_]+", line):
            continue
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"[*_`>#]", "", line).strip()
        if len(line) < 45:
            continue
        lines.append(line)
        if sum(len(x) for x in lines) > 320:
            break
    return " ".join(lines)[:320].strip()


def search_users(query: str, per_page: int = 12) -> List[Dict[str, Any]]:
    """按关键词搜 GitHub 用户/组织（关注列表用，不再局限于已有列表）。

    ``/search/users`` 只返回 login / avatar / type，没有 bio、粉丝数；
    补那些人资料要每个用户一次请求，成本高且吃 API 额度，所以这里只返回
    搜索结果自带字段，够用来「加关注」。
    """
    query = (query or "").strip()
    if not query:
        return []
    if RATE_REMAINING is not None and RATE_REMAINING < 2:
        return []
    url = (
        "https://api.github.com/search/users?q=%s&per_page=%d"
        % (urllib.parse.quote(query), max(1, min(int(per_page), 20)))
    )
    raw = http_get(url, api=True)
    if not raw:
        return []
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return []
    out: List[Dict[str, Any]] = []
    for item in payload.get("items") or []:
        login = str(item.get("login") or "").strip()
        if not login:
            continue
        out.append({
            "login": login,
            "type": "Organization" if item.get("type") == "Organization" else "User",
            "avatar": item.get("avatar_url") or "https://github.com/%s.png?size=96" % login,
            "url": item.get("html_url") or "https://github.com/%s" % login,
        })
    return out


def fetch_owner_repos(owner: str, per_page: int = 10) -> List[Dict[str, Any]]:
    """Newest public repositories for a publisher (used by the follow feed)."""
    if RATE_REMAINING is not None and RATE_REMAINING < 2:
        return []
    url = (
        "https://api.github.com/users/%s/repos?type=owner&sort=created&direction=desc&per_page=%d"
        % (urllib.parse.quote(owner), per_page)
    )
    raw = http_get(url, api=True)
    if not raw:
        return []
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return []
    return data if isinstance(data, list) else []
