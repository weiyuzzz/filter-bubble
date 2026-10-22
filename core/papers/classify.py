"""学科分类：把不同来源的领域信息映射到统一的 8 个学科。

规则优先级：
1. 来源自带的领域字段（OpenAlex 的 field、PubMed 的 MeSH、bioRxiv 的 server）；
2. 期刊/会议名里出现的领域词；
3. 标题与摘要里的特征词，且要求命中数量达到阈值——单个普通词不足以判定学科。

一篇论文可以有多个学科；「全部」视图按论文唯一标识去重。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List

# 顺序即前端 Tab 顺序
DISCIPLINES: List[Dict[str, str]] = [
    {"key": "all", "label": "全部论文"},
    {"key": "cs", "label": "计算机科学与人工智能"},
    {"key": "physics", "label": "物理学"},
    {"key": "medicine", "label": "医学与生物医学"},
    {"key": "finance", "label": "金融与经济学"},
    {"key": "math", "label": "数学"},
    {"key": "biology", "label": "生物学"},
    {"key": "engineering", "label": "工程与电子信息"},
]

KEYS = [d["key"] for d in DISCIPLINES]
LABELS = {d["key"]: d["label"] for d in DISCIPLINES}

# 来源直接给领域时的映射
SOURCE_DISCIPLINE = {
    "pubmed": "medicine",
    "medrxiv": "medicine",
    "biorxiv": "biology",
    "europepmc": "medicine",
    "huggingface": "cs",
    "openalex": "",
    "crossref": "",
}

KEYWORDS: Dict[str, List[str]] = {
    "cs": [
        "machine learning", "deep learning", "neural network", "computer vision", "natural language",
        "large language model", "llm", "reinforcement learning", "algorithm", "software",
        "database", "distributed system", "compiler", "operating system", "cybersecurity",
        "artificial intelligence", "transformer", "diffusion model", "reasoning", "benchmark",
        "dataset", "inference", "training", "embedding", "retrieval", "agent",
    ],
    "physics": [
        "quantum", "particle physics", "condensed matter", "astrophysics", "cosmology",
        "relativity", "optics", "plasma", "gravitational wave", "superconduct",
        "electromagnetic", "thermodynamic", "neutrino", "photon", "magnetic field",
    ],
    "medicine": [
        "clinical", "patient", "randomized", "cohort", "diagnosis", "treatment", "therapy",
        "disease", "mortality", "epidemiolog", "surgery", "vaccine", "cancer", "hospital",
        "symptom", "trial", "biomarker", "infection", "syndrome", "medical",
    ],
    "finance": [
        "finance", "financial", "asset pricing", "stock", "market return", "economic",
        "monetary", "fiscal", "trade", "investment", "portfolio", "volatility", "banking",
        "labor market", "inflation", "gdp", "game theory", "auction", "credit",
    ],
    "math": [
        "theorem", "proof", "lemma", "algebra", "topology", "number theory", "geometry",
        "combinatoric", "manifold", "differential equation", "stochastic process",
        "graph theory", "polynomial", "conjecture", "asymptotic", "convergence",
    ],
    "biology": [
        "gene", "protein", "cell", "genomic", "species", "ecolog", "evolution",
        "molecular", "enzyme", "metabolism", "rna", "dna", "neuron", "organism",
        "microbiome", "transcription", "phenotype", "biomarker",
    ],
    "engineering": [
        "signal processing", "circuit", "antenna", "semiconductor", "power system",
        "control system", "robotics", "telecommunication", "photovoltaic", "sensor",
        "embedded", "fpga", "wireless", "radar", "mechanical", "thermal management",
        "electronic", "electrical", "manufacturing",
    ],
}

# 期刊/会议名里的强特征词，命中即视为该学科（权重高）
JOURNAL_HINTS: Dict[str, List[str]] = {
    "cs": ["ieee transactions on pattern", "journal of machine learning", "computer science",
           "artificial intelligence", "information systems", "software engineering",
           "neural information processing", "acm transactions", "ieee access"],
    "physics": ["physical review", "physics", "astrophysical journal", "journal of high energy",
                "condensed matter", "quantum", "optics express", "nuclear"],
    "medicine": ["lancet", "jama", "new england journal", "bmj", "clinical", "medical",
                 "health", "nursing", "cancer", "cardiology", "radiology"],
    "finance": ["finance", "financial", "econometric", "economic", "banking", "accounting",
                "journal of money", "asset pricing"],
    "math": ["mathemat", "journal of algebra", "number theory", "topology", "statistics",
             "annals of mathematics", "combinatorics"],
    "biology": ["cell", "biology", "genome", "ecology", "evolution", "molecular",
                "nature biotechnology", "plant", "zoology"],
    "engineering": ["ieee transactions on industrial", "signal processing", "power systems",
                    "electron devices", "circuits and systems", "antennas", "robotics",
                    "mechanical engineering", "electrical"],
}

MIN_KEYWORD_HITS = 2
MIN_TITLE_HITS = 2
_TOKEN_RE = re.compile(r"[a-z][a-z\-]{2,}")


def _blob(title: str, abstract: str) -> str:
    return ((title or "") + " " + (abstract or "")).lower()


def _hits(blob: str, words: Iterable[str]) -> int:
    return sum(1 for word in words if word in blob)


def classify(source: str, title: str, abstract: str = "",
             venue: str = "", subjects: Iterable[str] = ()) -> List[str]:
    """返回该论文的学科列表（可能多个）。无法判断时返回空列表。"""
    text = _blob(title, abstract)
    venue_text = (venue or "").lower()
    subject_text = " ".join(str(s).lower() for s in (subjects or []))

    scores: Dict[str, float] = {}

    hint = SOURCE_DISCIPLINE.get(source, "")
    if hint:
        scores[hint] = scores.get(hint, 0) + 2.0

    for key, words in JOURNAL_HINTS.items():
        if venue_text and any(word in venue_text for word in words):
            scores[key] = scores.get(key, 0) + 3.0
        if subject_text and any(word in subject_text for word in words):
            scores[key] = scores.get(key, 0) + 2.0

    title_lower = (title or "").lower()
    for key, words in KEYWORDS.items():
        body_hits = _hits(text, words)
        title_hits = _hits(title_lower, words)
        if body_hits >= MIN_KEYWORD_HITS or title_hits >= MIN_TITLE_HITS:
            scores[key] = scores.get(key, 0) + float(body_hits) + 2.0 * title_hits

    if not scores:
        return []

    best = max(scores.values())
    # 只保留与最高分接近的学科，避免"沾边就算"
    picked = [key for key, score in scores.items() if score >= max(2.0, best * 0.6)]
    order = {key: idx for idx, key in enumerate(KEYS)}
    picked.sort(key=lambda k: order.get(k, 99))
    return picked


def normalize_discipline(value: str) -> str:
    value = (value or "all").strip().lower()
    return value if value in KEYS else "all"


def label_for(key: str) -> str:
    return LABELS.get(key, key)


def describe(item: Dict[str, Any]) -> List[str]:
    """论文条目的学科标签（给前端展示用）。"""
    discs = item.get("disciplines") or []
    return [LABELS.get(d, d) for d in discs if d in LABELS]
