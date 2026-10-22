"""论文数据源适配器。

每个适配器独立：一个失败不影响其他来源。全部只读公开接口，不绕过登录/付费墙。

已实测（2026-10-23，本机）：
* Crossref（`has-abstract:true` + 相关度排序）：标题/摘要/日期/DOI 齐全，跨学科可用
* PubMed E-utilities：标题/摘要/日期/PMID 齐全
* Europe PMC：标题/摘要（部分为空）/日期/DOI
* bioRxiv / medRxiv：预印本，摘要齐全
* HuggingFace daily papers：AI 论文，摘要齐全
* OpenAlex：需要 API Key；无 Key 时共享 IP 预算容易 429
* arXiv RSS：本机 TLS 证书链缺少中间证书（Certainly CA），无法通过校验，暂不接入
"""
from __future__ import annotations

import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from typing import Any, Callable, Dict, List

from ..radar.fetch import clean_text, get

UA = "FilterBubble/1.0 (paper module; +https://github.com/weiyuzzz/filter-bubble)"


def _headers() -> Dict[str, str]:
    return {"User-Agent": UA, "Accept": "application/json, application/xml, text/xml, */*"}


def _strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", text).strip()


_MONTHS = {name: idx for idx, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def iso_date(value: str) -> str:
    """把各来源的日期统一成 YYYY-MM-DD（无法识别时原样返回）。"""
    text = (value or "").strip()
    if not text:
        return ""
    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        return "%04d-%02d-%02d" % (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    match = re.match(r"^(\d{4})-([A-Za-z]{3})-(\d{1,2})", text)
    if match:
        month = _MONTHS.get(match.group(2).lower())
        if month:
            return "%04d-%02d-%02d" % (int(match.group(1)), month, int(match.group(3)))
    match = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})", text)
    if match:
        return "%04d-%02d-%02d" % (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    match = re.match(r"^(\d{4})-([A-Za-z]{3})$", text)
    if match:
        month = _MONTHS.get(match.group(2).lower())
        if month:
            return "%04d-%02d-01" % (int(match.group(1)), month)
    match = re.match(r"^(\d{4})$", text)
    if match:
        return "%s-01-01" % match.group(1)
    return text[:10]


def _item(source: str, source_id: str, title: str, abstract: str, url: str, published: str,
          **extra: Any) -> Dict[str, Any]:
    return {
        "source": source,
        "source_id": source_id,
        "title": clean_text(title, 400),
        "abstract": clean_text(abstract, 4000),
        "url": url,
        "published_at": iso_date(published),
        "doi": extra.get("doi", ""),
        "pmid": extra.get("pmid", ""),
        "arxiv_id": extra.get("arxiv_id", ""),
        "venue": clean_text(extra.get("venue", ""), 200),
        "subjects": extra.get("subjects") or [],
        "raw": extra.get("raw") or {},
    }


# ---------------- Crossref ----------------

CROSSREF_QUERY = "https://api.crossref.org/works"

# 按学科检索 Crossref：单条 OR 查询会让结果被最大的学科淹没，所以分学科各查一次
CROSSREF_TOPICS = [
    ("cs", "machine learning OR artificial intelligence OR computer vision OR software engineering"),
    ("physics", "quantum mechanics OR condensed matter OR astrophysics OR particle physics"),
    ("math", "mathematics theorem OR number theory OR algebra OR topology OR combinatorics"),
    ("engineering", "signal processing OR semiconductor OR antenna OR power systems OR robotics"),
    ("finance", "asset pricing OR econometrics OR monetary policy OR labor market"),
    ("biology", "gene expression OR cell biology OR genomics OR ecology OR evolution"),
]


def _crossref_page(topic: str, rows: int, since_days: int, mailto: str) -> List[Dict[str, Any]]:
    since = (date.today() - timedelta(days=max(1, since_days))).isoformat()
    params = {
        "filter": "has-abstract:true,from-pub-date:%s" % since,
        "rows": str(max(1, min(rows, 100))),
        "sort": "relevance",
        "order": "desc",
    }
    if topic:
        params["query.bibliographic"] = topic
    if mailto:
        params["mailto"] = mailto
    url = "%s?%s" % (CROSSREF_QUERY, urllib.parse.urlencode(params))

    result = get(url, headers=_headers(), timeout=30, rate_key="papers:crossref", min_interval=1.0)
    if not result.ok:
        raise RuntimeError("Crossref 请求失败：%s" % (result.error or result.status))
    payload = json.loads(result.text)
    items = (payload.get("message") or {}).get("items") or []

    out: List[Dict[str, Any]] = []
    for item in items:
        title = " ".join(item.get("title") or [])
        abstract = _strip_html(item.get("abstract") or "")
        if not title or not abstract:
            continue
        parts = ((item.get("published") or item.get("issued") or {}).get("date-parts") or [[]])[0]
        published = "-".join("%02d" % int(x) if idx else str(x) for idx, x in enumerate(parts)) if parts else ""
        out.append(_item(
            "crossref", item.get("DOI") or "", title, abstract,
            item.get("URL") or ("https://doi.org/%s" % item.get("DOI", "")),
            published, doi=item.get("DOI", ""),
            venue=" ".join(item.get("container-title") or []),
            subjects=item.get("subject") or [],
        ))
    return out


def crossref_fetch(topic: Any, rows: int, since_days: int,
                   mailto: str = "", per_topic: int = 8) -> List[Dict[str, Any]]:
    """topic 可以是字符串或 [(学科, 查询), ...]；后者按学科分别检索并合并。"""
    if isinstance(topic, (list, tuple)):
        topics = [t for t in topic if t]
        merged: List[Dict[str, Any]] = []
        seen = set()
        for _key, query in topics:
            try:
                page = _crossref_page(query, per_topic, since_days, mailto)
            except Exception as exc:  # noqa: BLE001 - 单学科失败不拖垮整体
                print("[papers] crossref topic failed:", exc)
                continue
            for item in page:
                if item["source_id"] in seen:
                    continue
                seen.add(item["source_id"])
                merged.append(item)
        if not merged:
            raise RuntimeError("Crossref 所有学科查询都没有返回可用记录")
        return merged
    return _crossref_page(topic or "", rows, since_days, mailto)


# ---------------- PubMed ----------------

PUBMED = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def pubmed_fetch(rows: int, since_days: int, term: str = "") -> List[Dict[str, Any]]:
    since = (date.today() - timedelta(days=max(1, since_days))).strftime("%Y/%m/%d")
    today = date.today().strftime("%Y/%m/%d")
    query = term or "(medicine[MeSH Terms] OR biomedical[MeSH Terms] OR clinical[Title])"
    search = "%s/esearch.fcgi?db=pubmed&retmax=%d&retmode=json&sort=date&term=%s" % (
        PUBMED, max(1, min(rows, 50)),
        urllib.parse.quote("%s AND %s:%s[dp]" % (query, since, today)))
    result = get(search, headers=_headers(), timeout=25, rate_key="papers:pubmed", min_interval=0.5)
    if not result.ok:
        raise RuntimeError("PubMed esearch 失败：%s" % (result.error or result.status))
    ids = (json.loads(result.text).get("esearchresult") or {}).get("idlist") or []
    if not ids:
        return []

    fetch = "%s/efetch.fcgi?db=pubmed&retmode=xml&id=%s" % (PUBMED, ",".join(ids))
    result = get(fetch, headers=_headers(), timeout=30, rate_key="papers:pubmed", min_interval=0.5)
    if not result.ok:
        raise RuntimeError("PubMed efetch 失败：%s" % (result.error or result.status))
    root = ET.fromstring(result.text)

    out: List[Dict[str, Any]] = []
    for art in root.findall(".//PubmedArticle"):
        pmid = art.findtext(".//PMID") or ""
        title = "".join(art.findtext(".//ArticleTitle") or "").strip()
        parts = [t.text or "" for t in art.findall(".//Abstract/AbstractText")]
        abstract = " ".join(p.strip() for p in parts if p.strip())
        year = art.findtext(".//JournalIssue/PubDate/Year") or ""
        month = art.findtext(".//JournalIssue/PubDate/Month") or ""
        day = art.findtext(".//JournalIssue/PubDate/Day") or ""
        published = "-".join(x for x in (year, month, day) if x)
        if not title:
            continue
        out.append(_item(
            "pubmed", pmid, title, abstract,
            "https://pubmed.ncbi.nlm.nih.gov/%s/" % pmid if pmid else "",
            published, pmid=pmid,
            venue=art.findtext(".//Journal/Title") or "",
        ))
    return out


# ---------------- Europe PMC ----------------

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


def europepmc_fetch(rows: int, since_days: int, query: str = "") -> List[Dict[str, Any]]:
    since = (date.today() - timedelta(days=max(1, since_days))).isoformat()
    q = query or "SRC:MED AND (biomedical OR clinical)"
    url = "%s?query=%s&format=json&pageSize=%d&resultType=core&sort=P_PDATE_D%%20desc" % (
        EPMC, urllib.parse.quote("%s AND (FIRST_PDATE:[%s TO %s])" % (q, since, date.today().isoformat())),
        max(1, min(rows, 50)))
    result = get(url, headers=_headers(), timeout=30, rate_key="papers:epmc", min_interval=1.0)
    if not result.ok:
        raise RuntimeError("Europe PMC 失败：%s" % (result.error or result.status))
    payload = json.loads(result.text)
    records = (payload.get("resultList") or {}).get("result") or []

    out: List[Dict[str, Any]] = []
    for rec in records:
        title = _strip_html(rec.get("title") or "")
        abstract = _strip_html(rec.get("abstractText") or "")
        if not title:
            continue
        doi = rec.get("doi") or ""
        pmid = rec.get("pmid") or ""
        url = ("https://doi.org/%s" % doi) if doi else (
            "https://pubmed.ncbi.nlm.nih.gov/%s/" % pmid if pmid else "")
        out.append(_item(
            "europepmc", pmid or doi or (rec.get("id") or ""), title, abstract, url,
            rec.get("firstPublicationDate") or "",
            doi=doi, pmid=pmid, venue=rec.get("journalTitle") or "",
        ))
    return out


# ---------------- bioRxiv / medRxiv ----------------

def _preprint_fetch(server: str, rows: int, since_days: int) -> List[Dict[str, Any]]:
    start = (date.today() - timedelta(days=max(1, since_days))).isoformat()
    end = date.today().isoformat()
    url = "https://api.biorxiv.org/details/%s/%s/%s" % (server, start, end)
    result = get(url, headers=_headers(), timeout=30, rate_key="papers:" + server, min_interval=1.0)
    if not result.ok:
        raise RuntimeError("%s 请求失败：%s" % (server, result.error or result.status))
    payload = json.loads(result.text)
    records = payload.get("collection") or []

    out: List[Dict[str, Any]] = []
    for rec in records[:max(1, rows)]:
        title = clean_text(rec.get("title") or "", 400)
        abstract = clean_text(rec.get("abstract") or "", 4000)
        doi = rec.get("doi") or ""
        if not title:
            continue
        out.append(_item(
            server, doi, title, abstract,
            ("https://doi.org/%s" % doi) if doi else "",
            rec.get("date") or "", doi=doi,
            venue=rec.get("category") or "",
            subjects=[rec.get("category") or ""],
        ))
    return out


def biorxiv_fetch(rows: int, since_days: int) -> List[Dict[str, Any]]:
    return _preprint_fetch("biorxiv", rows, since_days)


def medrxiv_fetch(rows: int, since_days: int) -> List[Dict[str, Any]]:
    return _preprint_fetch("medrxiv", rows, since_days)


# ---------------- HuggingFace daily papers ----------------

def huggingface_fetch(rows: int, since_days: int) -> List[Dict[str, Any]]:
    url = "https://huggingface.co/api/daily_papers?limit=%d" % max(1, min(rows, 50))
    result = get(url, headers=_headers(), timeout=30, rate_key="papers:hf", min_interval=1.0)
    if not result.ok:
        raise RuntimeError("HuggingFace 失败：%s" % (result.error or result.status))
    payload = json.loads(result.text)
    out: List[Dict[str, Any]] = []
    for entry in payload if isinstance(payload, list) else []:
        paper = entry.get("paper") or entry
        title = clean_text(paper.get("title") or "", 400)
        abstract = clean_text(entry.get("summary") or paper.get("summary") or "", 4000)
        arxiv_id = paper.get("id") or ""
        if not title:
            continue
        published = entry.get("publishedAt") or paper.get("publicationDate") or ""
        out.append(_item(
            "huggingface", arxiv_id or title[:40], title, abstract,
            ("https://arxiv.org/abs/%s" % arxiv_id) if arxiv_id else (paper.get("url") or ""),
            str(published)[:10], arxiv_id=arxiv_id,
            venue="arXiv (HuggingFace daily papers)",
        ))
    return out


# ---------------- OpenAlex（需要 API Key） ----------------

OPENALEX = "https://api.openalex.org/works"


def openalex_fetch(rows: int, since_days: int, topic: str = "", api_key: str = "") -> List[Dict[str, Any]]:
    since = (date.today() - timedelta(days=max(1, since_days))).isoformat()
    params = {"filter": "from_publication_date:%s,has_abstract:true" % since,
              "per-page": str(max(1, min(rows, 100))),
              "sort": "publication_date:desc"}
    if topic:
        params["search"] = topic
    if api_key:
        params["api_key"] = api_key
    url = "%s?%s" % (OPENALEX, urllib.parse.urlencode(params))

    result = get(url, headers=_headers(), timeout=30, rate_key="papers:openalex", min_interval=1.0)
    if not result.ok:
        detail = (result.error or str(result.status))
        if "429" in detail:
            detail += "（免费共享预算耗尽，配置 papers.openalex_api_key 可获得独立额度）"
        raise RuntimeError("OpenAlex 请求失败：%s" % detail)
    payload = json.loads(result.text)

    out: List[Dict[str, Any]] = []
    for work in payload.get("results") or []:
        title = clean_text(work.get("title") or work.get("display_name") or "", 400)
        abstract = _restore_abstract(work.get("abstract_inverted_index"))
        if not title or not abstract:
            continue
        doi = (work.get("doi") or "").replace("https://doi.org/", "")
        link = ((work.get("primary_location") or {}).get("landing_page_url")
                or (work.get("doi") or ""))
        out.append(_item(
            "openalex", work.get("id") or doi, title, abstract, link,
            work.get("publication_date") or "", doi=doi,
            venue=((work.get("primary_location") or {}).get("source") or {}).get("display_name") or "",
            subjects=[(t.get("display_name") or "") for t in (work.get("topics") or [])][:4],
        ))
    return out


def _restore_abstract(inverted: Any) -> str:
    """OpenAlex 用 {词: [位置]} 存摘要，必须还原成正常语序。"""
    if not isinstance(inverted, dict) or not inverted:
        return ""
    pairs = []
    for word, positions in inverted.items():
        if not isinstance(positions, list):
            continue
        for pos in positions:
            try:
                pairs.append((int(pos), word))
            except (TypeError, ValueError):
                continue
    pairs.sort()
    return " ".join(word for _pos, word in pairs)


# ---------------- 注册表 ----------------

ADAPTERS: Dict[str, Callable[..., List[Dict[str, Any]]]] = {
    "crossref": crossref_fetch,
    "pubmed": pubmed_fetch,
    "europepmc": europepmc_fetch,
    "biorxiv": biorxiv_fetch,
    "medrxiv": medrxiv_fetch,
    "huggingface": huggingface_fetch,
    "openalex": openalex_fetch,
}

# 每个来源查什么主题（Crossref / OpenAlex 用）
SOURCE_TOPICS = {
    "crossref": "machine learning OR physics OR economics OR mathematics OR engineering",
}
