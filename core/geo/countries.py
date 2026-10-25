"""国家代码与中英文名称映射（离线）。

数据来自 ``core/geo/data/countries.json``，由 ``scripts/build-geo-countries.py``
从 Natural Earth 10m admin_0 countries 生成一次（公共领域）。
运行时只读本地文件，不联网；文件缺失时退化为内置的最小集合并在结果里标注。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

DATA_PATH = Path(__file__).resolve().parent / "data" / "countries.json"

# 兜底：文件缺失时至少保证常见国家可解析（不用于生产质量评估）
_FALLBACK: List[Dict[str, Any]] = [
    {"iso2": "CN", "iso3": "CHN", "name_en": "China", "name_zh": "中国", "lat": 35.0, "lon": 105.0},
    {"iso2": "US", "iso3": "USA", "name_en": "United States", "name_zh": "美国", "lat": 39.8, "lon": -98.6},
    {"iso2": "JP", "iso3": "JPN", "name_en": "Japan", "name_zh": "日本", "lat": 36.2, "lon": 138.3},
    {"iso2": "GB", "iso3": "GBR", "name_en": "United Kingdom", "name_zh": "英国", "lat": 54.0, "lon": -2.0},
    {"iso2": "RU", "iso3": "RUS", "name_en": "Russia", "name_zh": "俄罗斯", "lat": 61.5, "lon": 100.0},
]

_loaded: Optional[Dict[str, Any]] = None


def _load() -> Dict[str, Any]:
    global _loaded
    if _loaded is not None:
        return _loaded
    payload: Dict[str, Any] = {
        "source": "built-in fallback",
        "license": "Public domain (Natural Earth) / fallback list",
        "generated_at": None,
        "count": len(_FALLBACK),
        "countries": list(_FALLBACK),
    }
    try:
        raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("countries"), list) and raw["countries"]:
            payload = raw
    except (OSError, ValueError):
        pass

    by_iso2: Dict[str, Dict[str, Any]] = {}
    for record in payload["countries"]:
        code = str(record.get("iso2") or "").upper()
        if len(code) != 2:
            continue
        record = dict(record)
        record["iso2"] = code
        by_iso2[code] = record
    payload["by_iso2"] = by_iso2
    _loaded = payload
    return payload


def metadata() -> Dict[str, Any]:
    """数据来源与许可证说明（文档与接口都用它，避免各处硬编码）。"""
    data = _load()
    return {
        "source": data.get("source"),
        "source_url": data.get("source_url"),
        "license": data.get("license"),
        "generated_at": data.get("generated_at"),
        "count": len(data["by_iso2"]),
        "path": str(DATA_PATH),
    }


def all_countries() -> List[Dict[str, Any]]:
    return list(_load()["by_iso2"].values())


def get(iso2: str) -> Optional[Dict[str, Any]]:
    if not iso2:
        return None
    return _load()["by_iso2"].get(str(iso2).strip().upper())


# 新闻常用简称 / 别名。Natural Earth 的 NAME_ZH 对很多国家给的是**正式长名**
# （中国 → "中华人民共和国"），直接拿它匹配标题几乎命中不了，所以这里维护一份简称表，
# 并补上常见别称。只放"明确指代该国家"的词，避免把泛指词当国名。
SHORT_ZH: Dict[str, str] = {
    "CN": "中国", "US": "美国", "JP": "日本", "KR": "韩国", "KP": "朝鲜", "RU": "俄罗斯",
    "UA": "乌克兰", "BY": "白俄罗斯", "GB": "英国", "FR": "法国", "DE": "德国",
    "IT": "意大利", "ES": "西班牙", "PT": "葡萄牙", "NL": "荷兰", "BE": "比利时",
    "CH": "瑞士", "AT": "奥地利", "SE": "瑞典", "NO": "挪威", "FI": "芬兰",
    "DK": "丹麦", "IS": "冰岛", "PL": "波兰", "CZ": "捷克", "SK": "斯洛伐克",
    "HU": "匈牙利", "RO": "罗马尼亚", "BG": "保加利亚", "GR": "希腊", "RS": "塞尔维亚",
    "HR": "克罗地亚", "BA": "波黑", "AL": "阿尔巴尼亚", "XK": "科索沃",
    "TR": "土耳其", "IL": "以色列", "PS": "巴勒斯坦", "SY": "叙利亚", "LB": "黎巴嫩",
    "JO": "约旦", "YE": "也门", "EG": "埃及", "IQ": "伊拉克", "IR": "伊朗",
    "SA": "沙特阿拉伯", "AE": "阿联酋", "QA": "卡塔尔", "KW": "科威特", "OM": "阿曼",
    "AF": "阿富汗", "PK": "巴基斯坦", "IN": "印度", "BD": "孟加拉国", "LK": "斯里兰卡",
    "NP": "尼泊尔", "MM": "缅甸", "TH": "泰国", "VN": "越南", "MY": "马来西亚",
    "SG": "新加坡", "ID": "印度尼西亚", "PH": "菲律宾", "KH": "柬埔寨", "LA": "老挝",
    "MN": "蒙古", "KZ": "哈萨克斯坦", "UZ": "乌兹别克斯坦", "AZ": "阿塞拜疆",
    "GE": "格鲁吉亚", "AM": "亚美尼亚", "CA": "加拿大", "MX": "墨西哥", "BR": "巴西",
    "AR": "阿根廷", "CL": "智利", "PE": "秘鲁", "CO": "哥伦比亚", "VE": "委内瑞拉",
    "CU": "古巴", "AU": "澳大利亚", "NZ": "新西兰", "ZA": "南非", "NG": "尼日利亚",
    "KE": "肯尼亚", "ET": "埃塞俄比亚", "DZ": "阿尔及利亚", "MA": "摩洛哥",
    "LY": "利比亚", "SD": "苏丹", "SO": "索马里", "CD": "刚果民主共和国",
    "CG": "刚果", "TZ": "坦桑尼亚", "GH": "加纳", "ZW": "津巴布韦", "UG": "乌干达",
    "TW": "台湾", "HK": "香港", "MO": "澳门",
}

# 额外的中/英别名（除简称与 Natural Earth 的正式名之外）
EXTRA_ZH: Dict[str, Sequence[str]] = {
    "SA": ["沙特", "沙烏地"], "ID": ["印尼"], "AE": ["阿联酋"],
    "US": ["美利坚", "美方", "美國"],
    "GB": ["联合王国", "不列颠", "英國"],
    "RU": ["俄国", "俄方", "俄羅斯"], "KR": ["南韩", "韓國"], "KP": ["北朝鲜", "北韓"],
    "CZ": ["捷克共和国"], "CD": ["刚果金", "刚果(金)"], "CG": ["刚果布", "刚果(布)"],
    "TW": ["中华民国", "台湾地区", "台灣"],
    "NZ": ["纽西兰", "紐西蘭"], "AU": ["澳洲", "澳大利亞"],
    # 繁体写法：语料里确实存在（如"美國電影學會""烏克蘭"）
    "CN": ["中國"], "FR": ["法國"], "DE": ["德國"], "JP": ["日本"],
    "UA": ["烏克蘭"], "IL": ["以色列"], "IR": ["伊朗"], "IN": ["印度"],
    "ES": ["西班牙"], "IT": ["義大利"], "NL": ["荷蘭"],
    "CA": ["加拿大"], "BR": ["巴西"], "MX": ["墨西哥"], "EG": ["埃及"],
    "TR": ["土耳其"], "TH": ["泰國"], "VN": ["越南"],
    "PH": ["菲律賓"], "MY": ["馬來西亞"], "SG": ["新加坡"], "PK": ["巴基斯坦"],
}

EXTRA_EN: Dict[str, Sequence[str]] = {
    "US": ["united states", "u.s.", "usa", "america"],
    "GB": ["united kingdom", "britain", "uk", "england"],
    "KR": ["south korea", "republic of korea"],
    "KP": ["north korea", "dprk"],
    "RU": ["russia", "russian federation"],
    "CN": ["china", "prc", "mainland china"],
    "TW": ["taiwan", "taiwan region"],
    "AE": ["uae", "emirates"],
    "CZ": ["czechia", "czech republic"],
    "NL": ["holland"],
    "NZ": ["new zealand"],
    "IE": ["ireland", "republic of ireland"],
    "VA": ["vatican"],
    "MM": ["myanmar", "burma"],
    "CD": ["democratic republic of the congo", "dr congo"],
    "CG": ["republic of the congo"],
    "TR": ["turkey", "turkiye"],
    "CI": ["cote d'ivoire", "ivory coast"],
    "SZ": ["eswatini", "swaziland"],
    "MK": ["north macedonia"],
}

_TERMS: Optional[List[Dict[str, Any]]] = None


def terms() -> List[Dict[str, Any]]:
    """所有可用于匹配的国家名/别名，**按长度倒序**（最长优先，避免"印度"抢先命中"印度尼西亚"）。"""
    global _TERMS
    if _TERMS is not None:
        return _TERMS
    table: List[Dict[str, Any]] = []
    seen: set = set()
    for record in all_countries():
        iso2 = record["iso2"]
        candidates = []
        if SHORT_ZH.get(iso2):
            candidates.append((SHORT_ZH[iso2], "zh"))
        if record.get("name_zh"):
            candidates.append((str(record["name_zh"]), "zh"))
        for alias in EXTRA_ZH.get(iso2, ()):
            candidates.append((alias, "zh"))
        if record.get("name_en"):
            candidates.append((str(record["name_en"]), "en"))
        for alias in EXTRA_EN.get(iso2, ()):
            candidates.append((alias, "en"))
        for term, lang in candidates:
            text = str(term).strip()
            if len(text) < 2:
                continue
            key = (text if lang == "zh" else text.lower(), lang)
            if key in seen:
                continue
            seen.add(key)
            table.append({"term": text, "lang": lang, "iso2": iso2,
                          "lat": record.get("lat"), "lon": record.get("lon"),
                          "label": SHORT_ZH.get(iso2) or record.get("name_zh") or text})
    table.sort(key=lambda item: len(item["term"]), reverse=True)
    _TERMS = table
    return _TERMS


def name(iso2: str, prefer_zh: bool = True) -> str:
    code = str(iso2 or "").upper()
    if prefer_zh and SHORT_ZH.get(code):
        return SHORT_ZH[code]
    record = get(code)
    if not record:
        return ""
    if prefer_zh and record.get("name_zh"):
        return str(record["name_zh"])
    return str(record.get("name_en") or record.get("name_zh") or code)


def centroid(iso2: str) -> Optional[Dict[str, float]]:
    """国家代表点。**只用于「仅能确定国家级位置」的低精度锚点**，不是事件坐标。"""
    record = get(iso2)
    if not record:
        return None
    lat, lon = record.get("lat"), record.get("lon")
    try:
        return {"lat": float(lat), "lon": float(lon)}
    except (TypeError, ValueError):
        return None


def normalise(code: str) -> str:
    """把各种写法（cn / CHN / China / 中国 / 美国）收敛成 ISO2；失败返回空串。"""
    text = str(code or "").strip()
    if not text:
        return ""
    upper = text.upper()
    data = _load()
    if len(upper) == 2 and upper in data["by_iso2"]:
        return upper
    for record in data["by_iso2"].values():
        if upper == str(record.get("iso3") or "").upper():
            return record["iso2"]
    # 中英文名与别名（简称表里才有"中国"这类新闻常用写法）
    lowered = text.lower()
    for term in terms():
        if (term["lang"] == "zh" and term["term"] == text) or \
                (term["lang"] == "en" and term["term"] == lowered):
            return term["iso2"]
    return ""