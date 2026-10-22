"""Event origin tracing, claim decomposition and multi-source verification.

Everything here is derived from material actually collected by the adapters. No claim,
quote or link is invented: if the pool has nothing, the report says so.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import store
from . import score
from .cluster import tokens

STATUS_LABEL = {
    "multi_support": "多项独立证据支持",
    "direct_support": "有直接证据支持",
    "partial": "部分支持",
    "disputed": "争议中",
    "insufficient": "证据不足",
    "contradicted": "与现有证据矛盾",
    "corrected": "已被可靠材料更正或推翻",
}

REFUTE_MARKERS = ("否认", "反驳", "辟谣", "不实", "谣言", "质疑", "撤回", "更正", "澄清",
                  "错误", "误导", "断章取义", "fake", "denied", "debunk", "retracted",
                  "correction", "misleading", "false")
CORRECTION_MARKERS = ("更正", "撤回", "道歉", "澄清", "retraction", "correction", "apology")
ASSERTION_MARKERS = ("宣布", "表示", "声称", "称", "证实", "披露", "发布", "实现", "突破",
                     "首次", "领先", "量产", "达到", "完成", "计划", "认为", "预计",
                     "announced", "said", "claims", "reported", "unveiled", "achieved")
SPECULATION_MARKERS = ("可能", "或许", "据传", "消息人士", "传闻", "或将", "疑似", "猜测",
                       "rumor", "reportedly", "may", "could", "allegedly")
ALLEGATION_MARKERS = ("指控", "举报", "涉嫌", "accused", "alleged", "lawsuit", "sued")

_SENTENCE = re.compile(r"(?<=[。！？!?；;\n])\s*")
_NUMBER = re.compile(r"(\d+(?:\.\d+)?)\s*(nm|μm|um|mm|cm|ghz|mhz|tops|flops|gb|tb|w|kw|%|倍|亿|万|台|颗|片|年|纳米|纳米制程)", re.I)


def _domain(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url or "").netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _sentences(text: str, limit: int = 6) -> List[str]:
    parts = [p.strip() for p in _SENTENCE.split(text or "") if p.strip()]
    return [p[:220] for p in parts[:limit]]


def _source_role(item: Dict[str, Any]) -> Dict[str, Any]:
    source_type = item.get("source_type", "news")
    first_hand = source_type in ("social_post", "paper", "official", "press_release")
    return {
        "url": item.get("url", ""),
        "platform": item.get("platform", ""),
        "source_id": item.get("source_id", ""),
        "source_type": source_type,
        "title": item.get("title", ""),
        "published_at": item.get("published_at", ""),
        "collected_at": item.get("collected_at", ""),
        "language": item.get("language", ""),
        "first_hand": first_hand,
        "independent_group": _domain(item.get("url", "")) or item.get("platform", ""),
        "quote": (item.get("raw_content") or item.get("title") or "")[:240],
    }


_TERM_SPLIT = re.compile(r"[\s,，、;；/|·\-—_]+")


def query_terms(text: str) -> List[str]:
    """Split free text into search terms that survive CJK word boundaries."""
    parts = [p.strip().lower() for p in _TERM_SPLIT.split(text or "")]
    terms = [p for p in parts if len(p) >= 2]
    if not terms:
        whole = (text or "").strip().lower()
        terms = [whole] if len(whole) >= 2 else []
    return terms


def search_pool(conn, text: str, days: int = 7, limit: int = 60) -> List[Dict[str, Any]]:
    """Search already-collected material. Never goes to the network.

    Ranking is the best of two signals: substring containment of the query terms
    (robust for Chinese) and token-set similarity (robust for English). The strict
    threshold runs first; if it finds too little we relax it so a free-text query
    still returns its best available material.
    """
    since = (dt.datetime.now().astimezone() - dt.timedelta(days=days)).isoformat(timespec="seconds")
    pool = store.recent_items(conn, since, limit=4000)
    wanted = tokens(text)
    terms = query_terms(text)
    if not wanted and not terms:
        return []

    def _scan(threshold: float) -> List[Tuple[float, Dict[str, Any]]]:
        found = []
        for item in pool:
            title = (item.get("title") or "").lower()
            term_score = 0.0
            if terms:
                term_score = sum(1 for term in terms if term in title) / float(len(terms))
            bigram_score = (len(wanted & tokens(title)) / float(len(wanted))) if wanted else 0.0
            score_value = max(term_score, bigram_score)
            if score_value >= threshold:
                found.append((score_value, item))
        return found

    scored = _scan(0.30)
    if len(scored) < 3:
        seen = {item.get("url_hash") for _, item in scored}
        for sim, item in _scan(0.15):
            if item.get("url_hash") not in seen:
                scored.append((sim, item))
                seen.add(item.get("url_hash"))

    scored.sort(key=lambda pair: (-pair[0], pair[1].get("published_at") or ""))
    return [item for _, item in scored[:limit]]


def gather_evidence(conn, event: Dict[str, Any], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    max_evidence = int(((cfg.get("investigation") or {}).get("max_evidence")) or 60)
    items = store.items_for_event(conn, event["id"])
    seen = {i.get("url_hash") for i in items}
    for extra in search_pool(conn, event.get("title", ""), days=7, limit=max_evidence):
        if extra.get("url_hash") in seen:
            continue
        items.append(extra)
        seen.add(extra.get("url_hash"))
        if len(items) >= max_evidence:
            break
    return items


def build_origin(evidence: List[Dict[str, Any]]) -> Dict[str, Any]:
    dated = [e for e in evidence if e.get("published_at")]
    dated.sort(key=lambda e: e["published_at"])
    timeline = []
    previous_domains: List[str] = []
    for item in dated[:20]:
        role = _source_role(item)
        host = role["independent_group"]
        quoted = any(d and d != host and d in (item.get("raw_content") or "")
                     for d in previous_domains)
        timeline.append({
            **role,
            "quotes_previous": quoted,
            "note": ("内容中出现了更早来源的域名，可能是转述" if quoted else ""),
        })
        if host and host not in previous_domains:
            previous_domains.append(host)

    earliest = timeline[0] if timeline else None
    undated = [e for e in evidence if not e.get("published_at")]
    return {
        "earliest": earliest,
        "note": ("这是当前可获取材料中最早的公开来源，不等于事件真正起源" if earliest
                 else "尚未找到最早来源：所有材料都缺少可核对的时间或发布时间"),
        "timeline": timeline,
        "undated_count": len(undated),
        "traceable_scope": ("可追溯到 %s 起 %d 条有时间的材料" % (earliest["published_at"][:16], len(timeline))
                            if earliest else "无法建立时间线"),
    }


def _relation_for(claim_text: str, item: Dict[str, Any]) -> str:
    blob = ((item.get("title") or "") + " " + (item.get("raw_content") or "")).lower()
    if any(marker in blob for marker in CORRECTION_MARKERS):
        return "correction"
    if any(marker in blob for marker in REFUTE_MARKERS):
        return "refute"
    overlap = len(tokens(claim_text) & tokens(item.get("title", "")))
    if overlap >= 2:
        return "support"
    return "context"


def _claim_status(relations: List[str], independent_groups: set, first_hand: bool) -> Tuple[str, str]:
    support = relations.count("support")
    refute = relations.count("refute")
    correction = relations.count("correction")
    if correction and refute:
        return "corrected", "存在更正/撤回类材料，原说法需要下调"
    if support and refute:
        return "disputed", "同时存在支持与反驳材料，尚不能判定"
    if support >= 3 and len(independent_groups) >= 3:
        return "multi_support", "%d 条独立来源（%d 个域名）内容一致" % (support, len(independent_groups))
    if support and first_hand:
        return "direct_support", "有一手材料直接支持"
    if support:
        return "partial", "有 %d 条材料支持，但独立来源不足 3 个" % support
    if refute:
        return "contradicted", "现有材料以反驳/质疑为主"
    return "insufficient", "暂无可用于支持或反驳的直接材料"


def extract_claims(event: Dict[str, Any], evidence: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Decompose into separately checkable claims instead of judging a whole article."""
    claims: List[Dict[str, Any]] = []
    core = (event.get("title") or "").strip()
    if core:
        claims.append({"text": core, "claimant": "事件标题（多方转述）",
                       "stated_at": event.get("first_seen_at", ""), "core": True})
    seen = set()
    for item in evidence:
        blob = item.get("raw_content") or ""
        for sentence in _sentences(blob, limit=3):
            if len(sentence) < 12 or sentence in seen:
                continue
            low = sentence.lower()
            if any(m in low for m in ASSERTION_MARKERS) or _NUMBER.search(sentence):
                seen.add(sentence)
                claims.append({
                    "text": sentence,
                    "claimant": item.get("author_name") or item.get("platform", ""),
                    "stated_at": item.get("published_at") or item.get("collected_at", ""),
                    "source_item": item,
                    "core": False,
                })
            if len(claims) >= 8:
                break
        if len(claims) >= 8:
            break
    return claims[:8]


def _classify_claim(claim: Dict[str, Any], evidence: List[Dict[str, Any]],
                    now: str) -> Dict[str, Any]:
    relations = []
    groups = set()
    first_hand = False
    evidence_rows = []
    for item in evidence:
        relation = _relation_for(claim["text"], item)
        role = _source_role(item)
        if relation in ("support", "refute", "correction"):
            relations.append(relation)
            if role["independent_group"]:
                groups.add(role["independent_group"])
            first_hand = first_hand or role["first_hand"]
        if relation != "context":
            evidence_rows.append({**role, "relation": relation})
    status, rationale = _claim_status(relations, groups, first_hand)

    blob = claim["text"].lower()
    if any(m in blob for m in SPECULATION_MARKERS):
        rationale += "；表述含推测/传闻语气"
    if any(m in blob for m in ALLEGATION_MARKERS):
        rationale += "；属于指控/单方说法，不能当作已证实事实"

    return {
        "text": claim["text"],
        "claimant": claim.get("claimant", ""),
        "stated_at": claim.get("stated_at", ""),
        "status": status,
        "status_label": STATUS_LABEL.get(status, status),
        "confidence": "high" if status in ("multi_support", "direct_support") else (
            "medium" if status in ("partial", "disputed") else "low"),
        "rationale": rationale,
        "is_core": bool(claim.get("core")),
        "evidence": evidence_rows[:8],
        "independent_groups": len(groups),
    }


def detect_corrections(evidence: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for item in evidence:
        blob = ((item.get("title") or "") + " " + (item.get("raw_content") or "")).lower()
        if any(marker in blob for marker in CORRECTION_MARKERS):
            role = _source_role(item)
            out.append({"title": item.get("title", ""), "url": role["url"],
                        "platform": role["platform"], "published_at": role["published_at"]})
    return out[:5]


def technical_metrics(evidence: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    found = []
    for item in evidence:
        blob = (item.get("title") or "") + " " + (item.get("raw_content") or "")
        for match in _NUMBER.finditer(blob):
            found.append({
                "value": match.group(1),
                "unit": match.group(2),
                "context": blob[max(0, match.start() - 40):match.end() + 40].replace("\n", " "),
                "url": item.get("url", ""),
                "platform": item.get("platform", ""),
            })
    # de-duplicate identical value+unit pairs
    seen = set()
    unique = []
    for row in found:
        key = (row["value"], row["unit"].lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique[:12]


COMPARABILITY_RULES = [
    "不同测试条件（制程、负载、精度、数据集）下的指标不能直接比较",
    "实验室结果不等于量产结果",
    "论文发表不等于技术领先的充分证明",
    "产品宣传参数不等于第三方独立测试",
    "国外媒体的正面评价不构成技术真实性的最终证据",
    "国外没有报道不能证明技术成果不存在",
    "某来源提出质疑也不能抹掉已有的客观实验数据",
]


def tech_report(event: Dict[str, Any], evidence: List[Dict[str, Any]],
                claims: List[Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, Any]:
    domains = {(_domain(i.get("url", "")) or i.get("platform", "")) for i in evidence}
    zh = [i for i in evidence if (i.get("language") or "").startswith("zh")]
    intl = [i for i in evidence if (i.get("language") or "").startswith("en")]
    metrics = technical_metrics(evidence)

    domestic_outlets = sorted({i.get("platform", "") for i in zh})
    intl_outlets = sorted({i.get("platform", "") for i in intl})

    if not metrics:
        comparability = "公开材料中没有给出可提取的技术指标，无法做指标级比较"
    elif len(metrics) < 2:
        comparability = "仅有 1 项可提取指标，样本不足，无法与其他方案公平比较"
    else:
        comparability = "已提取 %d 项指标，但材料未说明测试条件；不能与不同条件下的产品直接比较" % len(metrics)

    return {
        "kind": "tech",
        "sections": {
            "event_summary": event.get("title", ""),
            "core_claims": [c["text"] for c in claims if c.get("is_core")] or [event.get("title", "")],
            "raw_material": [{"title": i.get("title"), "url": i.get("url"),
                              "platform": i.get("platform"), "published_at": i.get("published_at")}
                             for i in evidence[:6]],
            "domestic_coverage": {"count": len(zh), "outlets": domestic_outlets[:10]},
            "intl_coverage": {"count": len(intl), "outlets": intl_outlets[:10],
                              "note": ("尚无英文/国际媒体报道，不能据此判定技术不存在"
                                       if not intl else "")},
            "metrics": metrics,
            "comparability": comparability,
            "comparability_rules": COMPARABILITY_RULES,
            "supporting_evidence": [{"text": c["text"], "status": c["status_label"],
                                     "evidence": c["evidence"][:3]}
                                    for c in claims if c["status"] in ("multi_support", "direct_support", "partial")],
            "limiting_evidence": [{"text": c["text"], "status": c["status_label"],
                                   "evidence": c["evidence"][:3]}
                                  for c in claims if c["status"] in ("disputed", "contradicted", "corrected")],
            "maturity": _maturity_hint(event, evidence),
            "confirmed": [c["text"] for c in claims if c["status"] in ("multi_support", "direct_support")],
            "disputed": [c["text"] for c in claims if c["status"] == "disputed"],
            "gaps": [g for g in [
                "缺少独立第三方测试" if not any("test" in (i.get("title") or "").lower() for i in evidence) else "",
                "缺少原始论文/技术报告" if not any(i.get("source_type") == "paper" for i in evidence) else "",
                "尚无国际同行评估" if not intl else "",
            ] if g],
            "independent_domains": len(domains),
            "sources": [{"title": i.get("title"), "url": i.get("url"), "platform": i.get("platform"),
                         "published_at": i.get("published_at")} for i in evidence[:10]],
        },
    }


def _maturity_hint(event: Dict[str, Any], evidence: List[Dict[str, Any]]) -> str:
    blob = " ".join((i.get("title") or "") + " " + (i.get("raw_content") or "") for i in evidence).lower()
    if any(w in blob for w in ("量产", "上市", "发货", "mass production", "shipping")):
        return "材料提到量产/上市，但仍需第三方出货或实测数据支持"
    if any(w in blob for w in ("样机", "原型", "prototype", "demo", "演示")):
        return "材料显示为样机/演示阶段，距量产尚有距离"
    if any(w in blob for w in ("论文", "预印本", "paper", "preprint")):
        return "材料以论文/预印本为主，属于研究阶段"
    return "材料不足以判断技术成熟度"


def _method_note(cfg: Dict[str, Any]) -> str:
    llm = cfg.get("llm") or {}
    if llm.get("enabled"):
        return "分析由已配置模型与本地证据库共同生成；所有结论均关联原始来源。"
    return ("分析由本地规则引擎基于已采集材料生成（未启用任何外部模型），"
            "每条结论都指回具体来源；系统不会补写缺失的事实。")


def run(conn, event_id: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    event = store.event_by_id(conn, event_id)
    if not event:
        raise ValueError("event not found")
    evidence = gather_evidence(conn, event, cfg)
    origin = build_origin(evidence)
    raw_claims = extract_claims(event, evidence)
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")

    claims = [_classify_claim(c, evidence, now) for c in raw_claims]

    # persist (regenerated per run; ids are internal)
    rows = [{
        "text": c["text"],
        "claimant": c["claimant"],
        "stated_at": c["stated_at"],
        "status": c["status"],
        "confidence": c["confidence"],
        "rationale": c["rationale"],
        "created_at": now,
        "updated_at": now,
    } for c in claims]
    claim_ids = store.replace_claims(conn, event_id, rows)

    for row_claim, claim_id in zip(claims, claim_ids):
        for cited in row_claim.get("evidence", []):
            evidence_id = store.add_evidence(conn, {
                "event_id": event_id,
                "claim_id": claim_id,
                "url": cited.get("url", ""),
                "source_id": cited.get("source_id", ""),
                "platform": cited.get("platform", ""),
                "source_type": cited.get("source_type", "news"),
                "quote": cited.get("quote", ""),
                "published_at": cited.get("published_at", ""),
                "collected_at": cited.get("collected_at", now),
                "language": cited.get("language", ""),
                "first_hand": cited.get("first_hand"),
                "independent_group": cited.get("independent_group", ""),
            })
            store.link_evidence(conn, claim_id, evidence_id, cited.get("relation", "context"),
                                "independent" if cited.get("independent_group") else "unknown",
                                "" if cited.get("relation") == "support" else "需结合上下文判断")

    state = "tech" if event.get("tech_flag") else "done"
    store.set_investigation_state(conn, event_id, state)
    conn.commit()

    corrections = detect_corrections(evidence)
    confirmed = [c for c in claims if c["status"] in ("multi_support", "direct_support")]
    disputed = [c for c in claims if c["status"] in ("disputed", "contradicted")]
    unverified = [c for c in claims if c["status"] in ("insufficient", "partial")]

    payload = {
        "kind": "tech" if event.get("tech_flag") else "event",
        "event": {
            "id": event["id"], "title": event["title"], "date": event["date"],
            "heat": event.get("heat"), "importance": event.get("importance"),
            "confidence": event.get("confidence"), "category": event.get("category"),
            "tech_flag": event.get("tech_flag"),
        },
        "origin": origin,
        "claims": claims,
        "corrections": corrections,
        "confirmed": [c["text"] for c in confirmed],
        "disputed": [c["text"] for c in disputed],
        "unverified": [c["text"] for c in unverified],
        "evidence_count": len(evidence),
        "independent_domains": len({_domain(i.get("url", "")) for i in evidence if i.get("url")}),
        "gaps": [
            g for g in [
                "" if origin["earliest"] else "尚未找到最早公开来源",
                "" if corrections else "未发现公开更正或撤回材料",
                "" if any(i.get("source_type") in score.NEWS_TYPES for i in evidence) else "仅有热榜/社交材料，缺少媒体报道",
                "" if len({_domain(i.get("url", "")) for i in evidence}) >= 3 else "独立来源不足 3 个，结论应谨慎",
            ] if g
        ],
        "method": _method_note(cfg),
        "generated_at": now,
        "status_legend": STATUS_LABEL,
    }
    if event.get("tech_flag"):
        payload["tech"] = tech_report(event, evidence, claims, cfg)

    store.save_report(conn, "%s|%s" % (event["date"], event["id"]), "investigation", payload, now)
    conn.commit()
    return payload


def run_query(conn, query: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Investigate free text: an event name, a person, a company or a URL."""
    query = (query or "").strip()
    if not query:
        raise ValueError("empty query")

    # a pasted URL is turned into a direct evidence seed
    seed = None
    if re.match(r"^https?://", query):
        for item in store.recent_items(conn, "1970-01-01T00:00:00+00:00", limit=8000):
            if item.get("url", "").startswith(query[:120]):
                seed = item
                break
        text = seed.get("title", "") if seed else query
    else:
        text = query

    matches = search_pool(conn, text, days=14, limit=40)
    if seed and seed not in matches:
        matches.insert(0, seed)

    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    event_id = "qry-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:14]
    synthetic = {
        "id": event_id,
        "date": dt.date.today().isoformat(),
        "title": (seed or {}).get("title") or text[:120],
        "first_seen_at": now,
        "heat": 0,
        "importance": 0,
        "confidence": "unverified",
        "category": "",
        "tech_flag": any(k in text.lower() for k in (cfg.get("investigation") or {}).get("tech_keywords") or []),
        "investigation_state": "query",
    }
    if matches:
        store.ensure_source(conn, {"source_id": "query", "platform": "查询", "name": "查询",
                                   "kind": "query", "url": "", "enabled": True})
        # materialise the synthetic event so evidence rows have a home
        store.upsert_event(conn, {
            **synthetic,
            "summary": "由关键词调查生成的临时事件",
            "keywords": sorted(tokens(text))[:10],
            "item_count": len(matches),
            "platform_count": len({i.get("platform") for i in matches}),
            "source_count": len({i.get("source_id") for i in matches}),
            "last_updated_at": now,
            "origin_item_id": matches[0].get("id"),
            "metrics": {"lifecycle": "query"},
        })
        store.assign_event(conn, [i["id"] for i in matches if i.get("id")], event_id)
        conn.commit()
        return run(conn, event_id, cfg)

    payload = {
        "kind": "event",
        "event": {"id": event_id, "title": text[:120], "tech_flag": synthetic["tech_flag"]},
        "origin": {"earliest": None, "timeline": [],
                   "note": "尚未找到最早来源：本地证据库中没有与该查询匹配的材料",
                   "traceable_scope": "无可追溯材料", "undated_count": 0},
        "claims": [],
        "corrections": [],
        "confirmed": [], "disputed": [], "unverified": [],
        "evidence_count": 0,
        "independent_domains": 0,
        "gaps": ["本地证据库中没有匹配材料；请先运行一次采集，或换用更具体的关键词"],
        "method": _method_note(cfg),
        "generated_at": now,
        "status_legend": STATUS_LABEL,
    }
    store.save_report(conn, "%s|%s" % (synthetic["date"], event_id), "investigation", payload, now)
    conn.commit()
    return payload
