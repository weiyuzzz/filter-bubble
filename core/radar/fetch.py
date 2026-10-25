"""HTTP access for radar sources: per-source timeout, bounded retries, polite rate limiting.

Everything fetched here is untrusted input. ``clean_text`` is the single place where
external text is normalised before it reaches the database or the UI.
"""
from __future__ import annotations

import gzip
import html
import json
import re
import threading
import time
import urllib.error
import urllib.request
import zlib
from typing import Any, Dict, Optional, Tuple

from . import config as radar_config

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 FilterBubbleRadar/1.0")

_LOCK = threading.Lock()
_LAST_CALL: Dict[str, float] = {}

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u2028\u2029\ufeff]")
_SPACE = re.compile(r"[ \t\u00a0]+")


def clean_text(value: Any, limit: int = 500) -> str:
    """Neutralise untrusted external text: no control chars, no runaway length."""
    if value is None:
        return ""
    text = str(value)
    text = _CONTROL.sub(" ", text)
    text = html.unescape(text)
    text = _SPACE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def strip_tags(value: str, limit: int = 500) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", value or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return clean_text(text, limit)


def _decode(body: bytes, content_type: str) -> str:
    charset = ""
    match = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    if match:
        charset = match.group(1).lower()
    candidates = [charset] if charset else []
    candidates += ["utf-8", "gb18030", "latin-1"]
    for enc in candidates:
        if not enc:
            continue
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")


def _decompress(body: bytes, encoding: str) -> bytes:
    encoding = (encoding or "").lower()
    try:
        if "gzip" in encoding:
            return gzip.decompress(body)
        if "deflate" in encoding:
            return zlib.decompress(body, -zlib.MAX_WBITS)
    except Exception:  # noqa: BLE001 - a broken body is just an empty body
        return body
    return body


def _respect_rate(key: str, min_interval: float) -> None:
    if not key or min_interval <= 0:
        return
    with _LOCK:
        last = _LAST_CALL.get(key, 0.0)
        wait = min_interval - (time.time() - last)
        if wait > 0:
            time.sleep(min(6.0, wait))
        _LAST_CALL[key] = time.time()


class FetchResult:
    __slots__ = ("ok", "status", "text", "error", "latency_ms", "url")

    def __init__(self, ok: bool, status: int, text: str = "", error: str = "",
                 latency_ms: int = 0, url: str = ""):
        self.ok = ok
        self.status = status
        self.text = text
        self.error = error
        self.latency_ms = latency_ms
        self.url = url

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "status": self.status, "error": self.error,
                "latency_ms": self.latency_ms, "url": self.url, "bytes": len(self.text)}


def _perform(method: str, url: str, body: Optional[bytes] = None,
             headers: Optional[Dict[str, str]] = None, timeout: Optional[float] = None,
             retries: Optional[int] = None, rate_key: str = "",
             min_interval: float = 0.0) -> FetchResult:
    """One request with at most ``retries + 1`` attempts and exponential backoff.

    Error strings never carry the response body. Some upstreams return credentials in
    their error bodies (AnySearch's 402 auto-registration returns username/password/
    api_key), and these errors end up in logs, so only the status is kept.
    """
    cfg = radar_config.cached()
    limits = cfg.get("limits") or {}
    timeout = float(timeout or limits.get("source_timeout_seconds", 12))
    retries = int(limits.get("source_retries", 1) if retries is None else retries)

    _respect_rate(rate_key or url, min_interval)

    merged = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }
    merged.update(headers or {})

    started = time.time()
    last_error = ""
    status = 0
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=merged, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                raw = _decompress(raw, resp.headers.get("Content-Encoding", ""))
                text = _decode(raw, resp.headers.get("Content-Type", ""))
                return FetchResult(True, resp.status, text, "", int((time.time() - started) * 1000), url)
        except urllib.error.HTTPError as exc:
            status = exc.code
            last_error = "HTTP %s" % exc.code
            # 4xx (except 429) will not improve with a retry
            if 400 <= exc.code < 500 and exc.code != 429:
                break
        except Exception as exc:  # noqa: BLE001 - network failures are expected
            last_error = "%s: %s" % (type(exc).__name__, exc)
        if attempt < retries:
            time.sleep(min(4.0, 0.8 * (2 ** attempt)))

    return FetchResult(False, status, "", last_error[:300], int((time.time() - started) * 1000), url)


def get(url: str, headers: Optional[Dict[str, str]] = None, timeout: Optional[float] = None,
        retries: Optional[int] = None, rate_key: str = "", min_interval: float = 0.0) -> FetchResult:
    """One GET with at most ``retries + 1`` attempts and exponential backoff."""
    return _perform("GET", url, None, headers, timeout, retries, rate_key, min_interval)


def post_json(url: str, payload: Any, headers: Optional[Dict[str, str]] = None,
              timeout: Optional[float] = None, retries: Optional[int] = None,
              rate_key: str = "", min_interval: float = 0.0) -> Tuple[Optional[Any], FetchResult]:
    """POST a JSON body and parse a JSON response, with the same retry/rate policy."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    merged = {"Content-Type": "application/json", "Accept": "application/json"}
    merged.update(headers or {})
    result = _perform("POST", url, body, merged, timeout, retries, rate_key, min_interval)
    if not result.ok:
        return None, result
    try:
        return json.loads(result.text), result
    except ValueError as exc:
        result.ok = False
        result.error = "invalid JSON: %s" % exc
        return None, result


def get_json(url: str, **kwargs) -> Tuple[Optional[Any], FetchResult]:
    result = get(url, **kwargs)
    if not result.ok:
        return None, result
    try:
        return json.loads(result.text), result
    except ValueError as exc:
        result.ok = False
        result.error = "invalid JSON: %s" % exc
        return None, result
