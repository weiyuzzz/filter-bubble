"""RSS / Atom adapter shared by every feed-based source."""
from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from ..fetch import clean_text, get, strip_tags

_ISO_HINT = re.compile(r"\d{4}-\d{2}-\d{2}")


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _parse_date(raw: str) -> str:
    raw = clean_text(raw, 80)
    if not raw:
        return ""
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.isoformat(timespec="seconds")
    except (TypeError, ValueError):
        pass
    if _ISO_HINT.match(raw):
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.isoformat(timespec="seconds")
        except ValueError:
            pass
    return ""


def _text_of(node: Optional[ET.Element]) -> str:
    if node is None:
        return ""
    return (node.text or "").strip()


def parse_feed(xml_text: str) -> List[Dict[str, str]]:
    """Namespace-agnostic RSS 2.0 / RSS 1.0 (RDF) / Atom parsing."""
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        # Some Chinese feeds ship stray control characters; retry after stripping them.
        cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml_text)
        root = ET.fromstring(cleaned)

    entries: List[ET.Element] = []
    for node in root.iter():
        if _localname(node.tag) in ("item", "entry"):
            entries.append(node)

    out: List[Dict[str, str]] = []
    for entry in entries:
        fields: Dict[str, str] = {}
        for child in list(entry):
            name = _localname(child.tag)
            if name in ("title", "link", "pubdate", "published", "updated", "description",
                        "summary", "content", "date", "creator", "author", "guid", "id"):
                if name == "link" and not fields.get("link"):
                    href = child.attrib.get("href")
                    fields["link"] = href or _text_of(child)
                    continue
                if name not in fields:
                    fields[name] = _text_of(child) or "".join(child.itertext()).strip()
        url = fields.get("link") or fields.get("guid") or fields.get("id") or ""
        title = fields.get("title", "")
        if not title or not url:
            continue
        published = (_parse_date(fields.get("pubdate", "")) or _parse_date(fields.get("published", ""))
                     or _parse_date(fields.get("updated", "")) or _parse_date(fields.get("date", "")))
        summary_raw = fields.get("description") or fields.get("summary") or fields.get("content") or ""
        out.append({
            "title": clean_text(title, 300),
            "url": url.strip(),
            "published_at": published,
            "summary": strip_tags(summary_raw, 600),
            "author": clean_text(fields.get("creator") or fields.get("author") or "", 120),
        })
    return out


def content_hash(title: str) -> str:
    normalized = re.sub(r"[\s\W_]+", "", (title or "").lower())
    return hashlib.sha1(normalized.encode("utf-8", "ignore")).hexdigest()


def fetch(spec: Dict[str, Any], make_item) -> List[Dict[str, Any]]:
    result = get(spec["url"], headers=spec.get("headers") or {},
                 rate_key=spec["source_id"], min_interval=float(spec.get("min_interval") or 0))
    if not result.ok:
        raise RuntimeError(result.error or "fetch failed")
    entries = parse_feed(result.text)
    if not entries:
        raise RuntimeError("feed parsed but contained no items")
    limit = int(spec.get("max_items") or 40)
    items: List[Dict[str, Any]] = []
    for entry in entries[:limit]:
        items.append(make_item(
            spec,
            title=entry["title"],
            url=entry["url"],
            published_at=entry["published_at"],
            author_name=entry.get("author", ""),
            raw_content=entry.get("summary", ""),
            source_type="news",
        ))
    return items
