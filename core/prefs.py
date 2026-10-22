"""A tiny, transparent online preference model.

Every repo is turned into readable features (``lang:rust``, ``cat:ai``,
``topic:llm``, ``kw:agent`` …).  User feedback is replayed through logistic
regression, so the learned profile can always be rebuilt from scratch and is
easy to explain in the UI.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from typing import Any, Dict, Iterable, List, Tuple

from . import db

CATEGORIES: List[Tuple[str, str, List[str]]] = [
    ("ai", "AI / 大模型", ["llm", "gpt", "agent", " ai ", "ai-", "machine-learning", "deep-learning",
                        "model", "inference", "rag", "mcp", "prompt", "diffusion", "llama", "openai",
                        "claude", "transformer", "embedding", "vector", "finetune", "neural", "copilot"]),
    ("devtool", "开发者工具", ["cli", "terminal", "developer", "devtool", "editor", "ide", "debug",
                          "linter", "formatter", " git", "testing", "vscode", "neovim", "tui", "sdk"]),
    ("web", "Web / 前端", ["react", "vue", "svelte", "nextjs", "next.js", "css", "tailwind", "frontend",
                        "browser", "wasm", "three.js", "component", "web app", "html"]),
    ("infra", "基础设施 / 云", ["kubernetes", "docker", "server", "proxy", "nginx", "cloud", "deploy",
                           "devops", "terraform", "database", " sql", "redis", "kafka", "queue",
                           "distributed", "observability", "self-hosted", "homelab", "container"]),
    ("data", "数据 / 分析", ["data", "etl", "pipeline", "analytics", "spark", "pandas", "duckdb",
                         "visualization", "chart", "dashboard", "scraping", "crawler"]),
    ("security", "安全 / 隐私", ["security", "vulnerability", "exploit", "cve", "auth", "encryption",
                             "sandbox", "malware", "pentest", "privacy", "zero-knowledge"]),
    ("mobile", "移动端", ["ios", "android", "swift", "kotlin", "flutter", "react native", "mobile"]),
    ("resource", "学习 / 资源", ["tutorial", "course", "book", "awesome", "roadmap", "interview",
                             "learning", "examples", "guide", "cheatsheet", "notes", "handbook",
                             "curated list", "resources"]),
    ("productivity", "效率 / 自动化", ["productivity", "note", "task", "calendar", "workflow",
                                "automation", "script", "assistant", "knowledge base", " bookmark"]),
    ("creative", "创意 / 媒体", ["game", "engine", "graphics", "render", "shader", "music", "video",
                             "image", "photo", "design", "audio", "animation"]),
    ("systems", "系统 / 底层", ["kernel", "operating system", "compiler", "assembly", "runtime",
                            "virtual machine", "firmware", "embedded", "low-level", "filesystem"]),
]

STOPWORDS = set("""
a an the and or of for to in on with without from by is are be this that it its as at
your you we our can will not no more most all any into over under about than then
using use used build built based make makes made new simple fast modern open source
github repo repository project tool tools app apps library framework package code
written implementation support supports supported via easily easy powerful lightweight
minimal tiny small large high performance production ready free
一个 一款 支持 使用 基于 可以 项目 工具 开源 快速 简单
""".split())

TOKEN_RE = re.compile(r"[a-z][a-z0-9.+#_-]{2,}")

# Free-form keyword features are restricted to terms that actually describe a
# project's domain.  Without this, words like "anything" or "reverse" leak into
# the taste profile and make it noisy.
KEYWORD_ALLOW = set("""
agent agents llm gpt ai mcp rag prompt prompts inference model models codex claude copilot
embedding embeddings training fine-tune transformer neural
image images video videos audio voice music photo photos design graphics animation game games 3d
editor cli terminal shell vim neovim vscode ide compiler runtime kernel os
database sql postgres postgresql sqlite redis vector search indexing crawler scraping
kubernetes docker container containers cloud server proxy vpn network dns ssh self-hosted
homelab deploy devops observability monitoring
security privacy encryption auth sandbox exploit vulnerability malware audit
automation workflow productivity notes note task calendar bookmark knowledge docs documentation
browser web frontend backend api sdk framework react vue svelte nextjs tailwind css html
javascript typescript python rust golang zig
mobile ios android swift kotlin flutter
data analytics visualization chart charts dashboard etl pipeline pdf markdown json yaml
oss open-source local-first offline sync collaboration realtime
""".split())


def _blob(repo: Dict[str, Any]) -> str:
    parts = [repo.get("name") or "", repo.get("description") or "", " ".join(repo.get("topics") or [])]
    return (" ".join(parts)).lower()


def categories_for(repo: Dict[str, Any]) -> List[Tuple[str, str]]:
    blob = " %s " % _blob(repo).replace("-", " ").replace("_", " ")
    scored = []
    for key, label, words in CATEGORIES:
        hits = sum(1 for w in words if w in blob)
        if hits:
            scored.append((hits, key, label))
    scored.sort(reverse=True)
    return [(k, label) for _, k, label in scored[:2]]


def keywords_for(repo: Dict[str, Any]) -> List[str]:
    blob = " ".join(
        [repo.get("description") or "", " ".join(repo.get("topics") or [])]
    ).lower()
    seen: List[str] = []
    for token in TOKEN_RE.findall(blob):
        token = token.strip(".-_")
        candidates = [token] + [part for part in re.split(r"[-_]", token) if part]
        for candidate in candidates:
            if candidate in STOPWORDS or len(candidate) < 3 or candidate not in KEYWORD_ALLOW:
                continue
            if candidate.endswith("s") and candidate[:-1] in KEYWORD_ALLOW:
                candidate = candidate[:-1]
            if candidate not in seen:
                seen.append(candidate)
            if len(seen) >= 6:
                return seen
    return seen


def features_for(repo: Dict[str, Any]) -> List[str]:
    feats: List[str] = []
    lang = (repo.get("language") or "").strip().lower()
    if lang:
        feats.append("lang:" + re.sub(r"\s+", "-", lang))
    for key, _label in categories_for(repo):
        feats.append("cat:" + key)
    for topic in (repo.get("topics") or [])[:6]:
        topic = re.sub(r"[^a-z0-9.+#-]", "", str(topic).lower())
        if len(topic) >= 2:
            feats.append("topic:" + topic)
    for kw in keywords_for(repo):
        feats.append("kw:" + kw)
    age = repo.get("age_days")
    if isinstance(age, (int, float)):
        if age <= 30:
            feats.append("age:new")
        elif age > 730:
            feats.append("age:mature")
    stars = int(repo.get("stars") or 0)
    if stars >= 30000:
        feats.append("size:huge")
    elif stars <= 1500:
        feats.append("size:small")
    if int(repo.get("stars_today") or 0) >= 800:
        feats.append("heat:surge")
    return list(dict.fromkeys(feats))


def humanize(feature: str) -> str:
    kind, _, value = feature.partition(":")
    if kind == "lang":
        return value.replace("-", " ").title()
    if kind == "cat":
        for key, label, _ in CATEGORIES:
            if key == value:
                return label
        return value
    if kind == "topic":
        return "#" + value
    if kind == "kw":
        return value
    return {
        "age:new": "新项目",
        "age:mature": "成熟项目",
        "size:huge": "超人气项目",
        "size:small": "小而美",
        "heat:surge": "爆发式增长",
    }.get(feature, value)


def feature_kind(feature: str) -> str:
    return feature.partition(":")[0]


def _sigmoid(x: float) -> float:
    x = max(-8.0, min(8.0, x))
    return 1.0 / (1.0 + math.exp(-x))


def _active_samples(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT * FROM feedback ORDER BY id ASC").fetchall()
    latest: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        latest[row["full_name"]] = dict(row)
    return [r for r in latest.values() if int(r["signal"]) != 0]


def rebuild(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Replay feedback through logistic regression and persist weights."""
    samples = _active_samples(conn)
    weights: Dict[str, float] = {}
    bias = 0.0
    lr = 0.5
    l2 = 0.003
    epochs = 14
    ordered = samples
    for epoch in range(epochs):
        step = lr * (1.0 - epoch / (epochs + 3))
        for sample in ordered:
            feats = json.loads(sample.get("features") or "[]")
            y = 1.0 if int(sample["signal"]) > 0 else 0.0
            logit = bias + sum(weights.get(f, 0.0) for f in feats)
            p = _sigmoid(logit)
            err = y - p
            for f in feats:
                w = weights.get(f, 0.0) + step * (err - l2 * weights.get(f, 0.0))
                weights[f] = max(-3.0, min(3.0, w))
            bias = max(-2.0, min(2.0, bias + step * err))

    pos: Dict[str, int] = {}
    neg: Dict[str, int] = {}
    for sample in samples:
        bucket = pos if int(sample["signal"]) > 0 else neg
        for f in json.loads(sample.get("features") or "[]"):
            bucket[f] = bucket.get(f, 0) + 1
    now = db.now_iso()
    conn.execute("DELETE FROM weights")
    for f, w in weights.items():
        conn.execute(
            "INSERT INTO weights(feature, weight, pos, neg, updated_at) VALUES(?,?,?,?,?)",
            (f, w, pos.get(f, 0), neg.get(f, 0), now),
        )
    db.set_meta(conn, "bias", bias)
    db.set_meta(conn, "feedback_active", len(samples))
    conn.commit()
    return {"weights": weights, "bias": bias, "samples": len(samples)}


def load_model(conn: sqlite3.Connection) -> Tuple[Dict[str, float], float]:
    weights = {r["feature"]: float(r["weight"]) for r in conn.execute("SELECT * FROM weights")}
    bias = float(db.get_meta(conn, "bias", "0") or 0)
    return weights, bias


def score(features: Iterable[str], weights: Dict[str, float], bias: float) -> float:
    return _sigmoid(bias + sum(weights.get(f, 0.0) for f in features))


def reason_for(features: List[str], weights: Dict[str, float], ready: bool) -> Tuple[str, List[str]]:
    if not ready:
        return "热度优先 · 点 👍 教我你的口味", []
    matched = [f for f in features if weights.get(f, 0.0) > 0.04]
    # Prefer interpretable domain features over stage features such as
    # "size:huge" when explaining *why* a project was recommended.
    priority = {"cat": 4, "lang": 3, "topic": 2, "kw": 1}
    matched.sort(
        key=lambda f: (priority.get(feature_kind(f), 0), weights.get(f, 0.0)),
        reverse=True,
    )
    labels: List[str] = []
    for f in matched:
        label = humanize(f)
        if label not in labels:
            labels.append(label)
        if len(labels) == 2:
            break
    if labels:
        return "偏爱 %s" % " · ".join(labels), labels
    return "探索位 · 和你常看的领域不太一样", []


def profile(conn: sqlite3.Connection) -> Dict[str, Any]:
    weights, bias = load_model(conn)
    rows = {r["feature"]: dict(r) for r in conn.execute("SELECT * FROM weights")}
    samples = len(_active_samples(conn))
    ready = samples >= 3

    def pack(kind: str, limit: int = 6) -> List[Dict[str, Any]]:
        vals = []
        for f, w in weights.items():
            if feature_kind(f) != kind or w <= 0.03:
                continue
            row = rows.get(f, {})
            if kind in ("topic", "kw") and int(row.get("pos", 0)) < 2:
                continue
            vals.append(
                {
                    "feature": f,
                    "label": humanize(f),
                    "weight": round(w, 3),
                    "pos": row.get("pos", 0),
                    "neg": row.get("neg", 0),
                }
            )
        vals.sort(key=lambda x: x["weight"], reverse=True)
        return vals[:limit]

    avoid = []
    for f, w in weights.items():
        if w >= -0.03:
            continue
        row = rows.get(f, {})
        if int(row.get("neg", 0)) < 2:
            continue
        avoid.append(
            {"feature": f, "label": humanize(f), "weight": round(w, 3),
             "pos": row.get("pos", 0), "neg": row.get("neg", 0)}
        )
    avoid.sort(key=lambda x: x["weight"])

    return {
        "ready": ready,
        "samples": samples,
        "bias": round(bias, 3),
        "languages": pack("lang"),
        "categories": pack("cat"),
        "topics": pack("topic", 5),
        "keywords": pack("kw", 5),
        "avoid": avoid[:6],
        "updated_at": db.get_meta(conn, "profile_updated"),
    }
