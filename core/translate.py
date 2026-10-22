"""Best-effort English → Chinese translation with a local cache and daily budget.

Uses MyMemory's public endpoint (no key).  When it is unreachable or out of
quota the digest simply keeps the original English copy — never an error.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.parse
import urllib.request
from datetime import date
from typing import Iterable, List, Optional

from . import config, db


class Translator:
    def __init__(self, conn):
        self.conn = conn
        self.budget = config.TRANSLATE_DAILY_CHARS if config.TRANSLATE else 0
        self.used = 0
        self.stopped = False
        self.today = date.today().isoformat()
        if db.get_meta(conn, "translate_date") != self.today:
            db.set_meta(conn, "translate_date", self.today)
            db.set_meta(conn, "translate_chars", 0)
            conn.commit()
        try:
            self.used = int(db.get_meta(conn, "translate_chars", "0") or 0)
        except ValueError:
            self.used = 0

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.used)

    def _cache_key(self, text: str) -> str:
        return hashlib.sha1(("mymemory-v1:" + text).encode("utf-8")).hexdigest()

    def _cached(self, text: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT zh FROM translations WHERE key=?", (self._cache_key(text),)
        ).fetchone()
        return row["zh"] if row else None

    def translate(self, text: str) -> Optional[str]:
        text = (text or "").strip()
        if not text or self.stopped or self.budget <= 0:
            return None
        if re.search(r"[\u4e00-\u9fff]", text):
            return text
        cached = self._cached(text)
        if cached:
            return cached
        cost = len(text)
        if cost > self.remaining or cost > 480:
            if cost > 480:
                text = text[:470].rsplit(" ", 1)[0] + "…"
                cost = len(text)
            if cost > self.remaining:
                self.stopped = True
                return None
        url = (
            "https://api.mymemory.translated.net/get?q=%s&langpair=%s"
            % (urllib.parse.quote(text), urllib.parse.quote("en|zh-CN"))
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
            with urllib.request.urlopen(req, timeout=12) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            print("[translate] failed:", exc)
            return None
        if int(payload.get("responseStatus") or 0) != 200:
            print("[translate] status:", payload.get("responseStatus"), payload.get("responseDetails"))
            self.stopped = True
            return None
        zh = html.unescape((payload.get("responseData") or {}).get("translatedText") or "").strip()
        if not zh or not re.search(r"[\u4e00-\u9fff]", zh):
            return None
        zh = re.sub(r"\s+([，。！？；：])", r"\1", zh)
        self.conn.execute(
            "INSERT OR REPLACE INTO translations(key, zh, created_at) VALUES(?,?,?)",
            (self._cache_key(text), zh, db.now_iso()),
        )
        self.used += cost
        db.set_meta(self.conn, "translate_chars", self.used)
        self.conn.commit()
        return zh

    def translate_many(self, texts: Iterable[str]) -> List[Optional[str]]:
        out: List[Optional[str]] = []
        for text in texts:
            out.append(self.translate(text))
        return out
