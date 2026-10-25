"""地理识别：按固定优先级把标题/来源信息解析成可追溯的位置。

优先级（与设计文档一致）：
1. ``source_coordinates`` —— 来源直接给出的、经过坐标校验的事件坐标 → ``high``；
2. ``source_country`` —— 来源明确声明的事件发生国家（当前没有可用来源，保留入口）；
3. ``gazetteer_match`` —— 通过地名库 + 上下文规则识别到城市/地区 → ``medium``；
4. ``country_centroid`` —— 只能确认国家、没有具体坐标 → 国家质心锚点 → ``low``；
5. 无法明确判断 → ``None``，**不生成任何标记**。

硬规则：
* **绝不**因为标题里出现某个国家就认定事件发生在该国；
* 标题里出现 ≥2 个国家/地区时判为歧义，直接返回未知（例如
  「美国警告中国某项政策可能影响日本」不得锚定到美/中/日任何一处）；
* 置信度只由**可解释证据**决定，不因"多个平台报道同一事件"而提高
  （多平台往往是转载同一条消息，不构成独立的位置证据）。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from . import countries, gazetteer, validate

HIGH = "high"
MEDIUM = "medium"
LOW = "low"
NONE = "none"

METHOD_SOURCE_COORDS = "source_coordinates"
METHOD_SOURCE_COUNTRY = "source_country"
METHOD_GAZETTEER = "gazetteer_match"
METHOD_CENTROID = "country_centroid"
METHOD_NONE = "none"

MAX_ITEM_TITLES = 8

# 跨国缩写：出现这些词说明事件至少涉及两个国家，"国家级"证据因此失效。
# 注意：**不因为它们就直接判未知** —— 如果标题里还有具体城市（如"俄乌…基辅遭袭"），
# 城市级证据仍然保留，只有"仅剩国家级证据"时才降级为未知。
CROSS_BORDER_CONTRACTIONS = (
    "中美", "中俄", "中日", "中欧", "中英", "中法", "中德", "中韩", "中澳", "中加",
    "中印", "中越", "中菲", "美俄", "美日", "美欧", "美以", "美伊", "美朝", "美印",
    "美台", "俄乌", "日韩", "朝韩", "巴以", "印巴", "以伊", "以黎", "欧美", "加沙",
)

CITY_KINDS = ("city", "region")

# 知名"行为体"→所属国家。用途**只有一个**：判断这是不是跨国报道。
#   * 它本身**从不用来定位**（不会因为提到"特斯拉"就把事件放到美国）；
#   * 只有当它与地名匹配出的国家**冲突**时，才把"仅国家级"的结论降级为未知；
#   * 如果地名里还有具体城市（"特斯拉上海工厂"），城市级证据仍然保留。
ACTORS = {
    "习近平": "CN", "王沪宁": "CN", "李强": "CN", "中方": "CN",
    "商务部": "CN", "外交部": "CN", "驻华": "CN", "访华": "CN", "对华": "CN",
    "在华": "CN", "涉华": "CN", "上证": "CN", "A股": "CN",
    "特朗普": "US", "拜登": "US", "马斯克": "US", "馬斯克": "US", "美方": "US",
    "美联储": "US", "特斯拉": "US", "英伟达": "US", "微软": "US", "谷歌": "US",
    "亚马逊": "US", "OpenAI": "US", "苹果公司": "US",
    "普京": "RU", "拉夫罗夫": "RU", "俄方": "RU", "俄軍": "RU", "俄军": "RU",
    "泽连斯基": "UA", "马克龙": "FR", "默茨": "DE", "内塔尼亚胡": "IL",
    "莫迪": "IN", "金正恩": "KP", "台积电": "TW", "三星": "KR", "丰田": "JP",
    "华为": "CN", "字节跳动": "CN", "比亚迪": "CN", "奇瑞": "CN",
}


def _actor_conflict(hits: List[Dict[str, Any]], text: str):
    """跨国行为体与地名国家冲突时，丢弃"国家级"命中，只保留城市/地区级。"""
    if not hits:
        return hits, ""
    resolved = {hit.get("iso2") for hit in hits if hit.get("iso2")}
    if not resolved:
        return hits, ""
    for name, iso2 in ACTORS.items():
        if name in (text or "") and iso2 not in resolved:
            specific = [hit for hit in hits if hit.get("kind") in CITY_KINDS]
            return specific, name
    return hits, ""


def _split_contraction(hits: List[Dict[str, Any]], text: str):
    """跨国语境下丢弃"国家级"命中，保留城市/地区级命中。"""
    contraction = next((token for token in CROSS_BORDER_CONTRACTIONS if token in (text or "")), "")
    if not contraction:
        return hits, ""
    specific = [hit for hit in hits if hit.get("kind") in CITY_KINDS]
    return specific, contraction


def _drop_team_references(text: str, hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """体育语境下，只保留有"确实发生在这里"线索的地名。

    不算"一刀切禁止"：真实赛事（"2026天津马拉松开跑"）因为带"开跑/举办/现场"等
    在地线索会被保留；球队名（"辽宁铁人""北京男篮"）没有线索，会被丢弃。
    """
    if not hits or not gazetteer.is_sports_context(text):
        return hits
    return [hit for hit in hits if gazetteer.has_locative_cue(text, hit.get("span"))]


def _blank(reason: str, candidates: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {
        "latitude": None, "longitude": None, "country_code": None, "label": "",
        "confidence": NONE, "method": METHOD_NONE, "source": "",
        "candidates": candidates or [], "ambiguous": False, "reason": reason,
    }


def _places(text: str) -> List[Dict[str, Any]]:
    """一次性做最长优先匹配：地名库（城市/地区）与国家名/别名混合排序。

    必须**先长后短**：否则 "印度" 会抢在 "印度尼西亚" 前面命中，把事件锚到错误国家。
    """
    raw = text or ""
    if not raw:
        return []
    masked = gazetteer._masked(raw)
    taken = [False] * len(raw)

    candidates: List[Dict[str, Any]] = []
    for entry in gazetteer.ENTRIES:
        candidates.append({"term": entry["zh"], "lang": "zh", "kind": entry["kind"],
                           "iso2": entry["iso2"], "lat": entry["lat"], "lon": entry["lon"],
                           "label": entry["label"]})
        if entry["en"]:
            candidates.append({"term": entry["en"], "lang": "en", "kind": entry["kind"],
                               "iso2": entry["iso2"], "lat": entry["lat"], "lon": entry["lon"],
                               "label": entry["label"]})
    for record in countries.terms():
        candidates.append({"term": record["term"], "lang": record["lang"], "kind": "country",
                           "iso2": record["iso2"], "lat": record["lat"], "lon": record["lon"],
                           "label": record["label"]})
    candidates.sort(key=lambda item: len(str(item["term"])), reverse=True)

    hits: List[Dict[str, Any]] = []
    lowered = masked.lower()
    for candidate in candidates:
        term = str(candidate["term"])
        if not term:
            continue
        if candidate["lang"] == "zh":
            haystack, source_text = masked, raw
            start = haystack.find(term)
            while start != -1:
                end = start + len(term)
                if not any(taken[start:end]) and not gazetteer._is_organisation(raw, end):
                    for index in range(start, end):
                        taken[index] = True
                    hits.append({**candidate, "term": source_text[start:end],
                                 "span": [start, end]})
                    break
                start = haystack.find(term, start + 1)
        else:
            import re as _re
            # 必须先把词本身转小写：国家名的英文原名带大写（Ukraine），
            # 而匹配是在小写文本上做的，不转就永远匹配不上。
            needle = term.lower()
            pattern = _re.compile(r"(?<![a-z])" + _re.escape(needle) + r"(?![a-z])")
            for match in pattern.finditer(lowered):
                start, end = match.start(), match.end()
                if any(taken[start:end]):
                    continue
                if gazetteer._is_organisation(raw, end):
                    continue
                for index in range(start, end):
                    taken[index] = True
                hits.append({**candidate, "term": raw[start:end], "span": [start, end]})
                break

    hits.sort(key=lambda h: h["span"][0])
    return hits


def _candidate_view(hits: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"term": h["term"], "kind": h["kind"], "iso2": h.get("iso2"),
             "label": h.get("label"), "span": h.get("span")} for h in hits]


def _from_hits(hits: List[Dict[str, Any]], source: str) -> Dict[str, Any]:
    """把地名候选收敛成一个位置；有歧义就如实返回未知。"""
    if not hits:
        return _blank("no_place_signal")

    iso2_set = {h.get("iso2") for h in hits if h.get("iso2")}
    candidates = _candidate_view(hits)

    if len(iso2_set) > 1:
        # 跨国歧义：不猜，也不做虚假的地理质心
        result = _blank("multiple_countries_in_text", candidates)
        result["ambiguous"] = True
        return result

    iso2 = next(iter(iso2_set)) if iso2_set else None
    city_hits = [h for h in hits if h["kind"] == gazetteer.CITY]
    region_hits = [h for h in hits if h["kind"] == gazetteer.REGION]
    country_hits = [h for h in hits if h["kind"] == "country"]

    distinct_cities = {h["label"] for h in city_hits}
    if len(distinct_cities) > 1:
        # 同一国家内出现多个城市（"北京和上海"）：只能给国家级近似
        centroid = countries.centroid(iso2) if iso2 else None
        if not centroid:
            result = _blank("multiple_cities_same_country", candidates)
            result["ambiguous"] = True
            return result
        point = validate.parse_pair(centroid["lat"], centroid["lon"])
        if not point:
            return _blank("centroid_invalid", candidates)
        result = _blank("multiple_cities_same_country", candidates)
        result.update({"latitude": point["lat"], "longitude": point["lon"],
                       "country_code": iso2, "label": countries.name(iso2),
                       "confidence": LOW, "method": METHOD_CENTROID, "source": source})
        result["ambiguous"] = True
        return result

    if city_hits:
        hit = city_hits[0]
        point = validate.parse_pair(hit["lat"], hit["lon"])
        if point:
            return {"latitude": point["lat"], "longitude": point["lon"], "country_code": iso2,
                    "label": hit["label"], "confidence": MEDIUM, "method": METHOD_GAZETTEER,
                    "source": "%s:gazetteer:%s" % (source, hit["term"]),
                    "candidates": candidates, "ambiguous": False,
                    "reason": "city_match"}

    if region_hits:
        hit = region_hits[0]
        point = validate.parse_pair(hit["lat"], hit["lon"])
        if point:
            return {"latitude": point["lat"], "longitude": point["lon"], "country_code": iso2,
                    "label": hit["label"], "confidence": MEDIUM, "method": METHOD_GAZETTEER,
                    "source": "%s:gazetteer:%s" % (source, hit["term"]),
                    "candidates": candidates, "ambiguous": False,
                    "reason": "region_match"}

    if country_hits and iso2:
        # 只能确认国家：用国家质心做**低精度锚点**，并明确标注
        centroid = countries.centroid(iso2)
        if centroid:
            point = validate.parse_pair(centroid["lat"], centroid["lon"])
            if point:
                return {"latitude": point["lat"], "longitude": point["lon"], "country_code": iso2,
                        "label": countries.name(iso2), "confidence": LOW,
                        "method": METHOD_CENTROID,
                        "source": "%s:country_centroid:%s" % (source, iso2),
                        "candidates": candidates, "ambiguous": False,
                        "reason": "country_only"}

    return _blank("no_usable_coordinates", candidates)


def resolve_title(title: str) -> Dict[str, Any]:
    """仅用标题解析位置（最保守的一档）。"""
    text = title or ""
    hits = _places(text)
    hits, contraction = _split_contraction(hits, text)
    hits = _drop_team_references(text, hits)
    hits, actor = _actor_conflict(hits, text)
    if actor and not hits:
        result = _blank("cross_border_actor", [{"term": actor, "kind": "actor"}])
        result["ambiguous"] = True
        return result
    if contraction and not hits:
        result = _blank("cross_border_contraction", [{"term": contraction, "kind": "contraction"}])
        result["ambiguous"] = True
        return result
    return _from_hits(hits, "title")


def resolve_event(title: str, item_titles: Optional[Iterable[str]] = None,
                  source_coords: Optional[Dict[str, Any]] = None,
                  source_country: Optional[str] = None,
                  source_name: str = "") -> Dict[str, Any]:
    """事件级解析：先看来源坐标，再看来源国家，再看标题，最后才看成员条目标题。"""
    point = validate.parse_pair((source_coords or {}).get("lat"),
                                (source_coords or {}).get("lon"))
    if point:
        iso2 = countries.normalise(source_country or "") or None
        return {"latitude": point["lat"], "longitude": point["lon"], "country_code": iso2,
                "label": source_name or "来源坐标", "confidence": HIGH,
                "method": METHOD_SOURCE_COORDS,
                "source": "%s:coordinates" % (source_name or "source"),
                "candidates": [], "ambiguous": False, "reason": "source_coordinates"}

    if source_country:
        iso2 = countries.normalise(source_country)
        if iso2:
            centroid = countries.centroid(iso2)
            point = validate.parse_pair((centroid or {}).get("lat"), (centroid or {}).get("lon"))
            if point:
                return {"latitude": point["lat"], "longitude": point["lon"], "country_code": iso2,
                        "label": countries.name(iso2), "confidence": MEDIUM,
                        "method": METHOD_SOURCE_COUNTRY,
                        "source": "%s:declared_country:%s" % (source_name or "source", iso2),
                        "candidates": [], "ambiguous": False, "reason": "source_declared_country"}

    result = resolve_title(title)
    if result["confidence"] != NONE:
        return result
    # 标题本身已经判定为歧义（多国/跨国）时，**不要**再用成员条目去"救"，
    # 否则等于用更多文本把歧义强行收敛成一个错误的国家。
    if result.get("ambiguous"):
        return result

    # 标题给不出答案时，才看成员条目标题；仍然要求"单一国家"，否则保持未知
    pooled: List[Dict[str, Any]] = []
    for item_title in list(item_titles or [])[:MAX_ITEM_TITLES]:
        item_hits = _places(item_title or "")
        item_hits, _contraction = _split_contraction(item_hits, item_title or "")
        pooled.extend(_drop_team_references(item_title or "", item_hits))
    if pooled:
        pooled_result = _from_hits(pooled, "items")
        if pooled_result["confidence"] != NONE:
            pooled_result["reason"] = "item_titles:%s" % pooled_result.get("reason", "")
            return pooled_result
        if pooled_result.get("ambiguous"):
            return pooled_result

    return result if result["candidates"] else _blank("no_place_signal")


def confidence_rank(confidence: str) -> int:
    return {HIGH: 3, MEDIUM: 2, LOW: 1}.get(confidence, 0)


def metadata() -> Dict[str, Any]:
    return {
        "levels": {HIGH: "来源给出并经校验的事件坐标",
                   MEDIUM: "地名库匹配到城市/地区，或来源声明的国家",
                   LOW: "仅能确认国家，使用国家质心近似",
                   NONE: "无法可靠定位，不生成标记"},
        "methods": [METHOD_SOURCE_COORDS, METHOD_SOURCE_COUNTRY, METHOD_GAZETTEER,
                    METHOD_CENTROID, METHOD_NONE],
        "rule": "标题出现 ≥2 个国家/地区时判为歧义并返回未知；不因多平台转载提高置信度。",
    }