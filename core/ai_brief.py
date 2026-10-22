"""GitHub 项目的 AI 解读。

把项目的简介、语言、topics、README 摘要交给全局配置的模型，生成：
适合谁 / 解决什么问题 / 怎么用 / 完整使用流程 / 典型场景 / 上手门槛 / 局限。

* 结果按 (仓库, 模型, prompt 版本) 缓存，同一个项目不会重复消耗调用；
* 模型不可用时静默跳过，页面继续显示原有的规则版解读；
* 模型只能读到下面列出的字段，提示词明确禁止编造未出现的事实。
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import sqlite3
from typing import Any, Dict, List, Optional

from . import ai_client, ai_settings, db

VERSION = 1
MAX_BRIEFS_DEFAULT = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_briefs (
    cache_key TEXT PRIMARY KEY,
    full_name TEXT,
    model TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_briefs_repo ON ai_briefs(full_name);
"""

SYSTEM = (
    "你是一个技术选型助手。只根据用户给出的项目信息作答，"
    "严禁编造未出现的功能、指标、公司或使用方式。信息不足时用「README 未说明」表达。"
    "输出严格 JSON，中文，简洁。"
)

USER = """项目：{full_name}
语言：{language}
Star：{stars}　Fork：{forks}
Topics：{topics}
官方简介：{description}
README 摘要：{readme}

请输出 JSON：
{{
  "what": "一句话说明它是做什么的（不超过 60 字）",
  "audience": "适合谁用（不超过 40 字）",
  "problem": "解决什么问题（不超过 60 字）",
  "usage": "怎么用：安装/运行方式（不超过 60 字，不确定就写 README 未说明）",
  "flow": ["上手步骤，3–4 条，每条不超过 30 字"],
  "scenarios": ["典型场景，2–3 条，每条不超过 30 字"],
  "difficulty": "上手门槛（不超过 20 字）",
  "limits": "已知局限或需要注意的地方（不超过 60 字，没有就写 README 未说明）"
}}"""


def _conn() -> sqlite3.Connection:
    conn = db.connect()
    conn.executescript(SCHEMA)
    return conn


def _key(item: Dict[str, Any], model: str) -> str:
    raw = "%s|%s|%d" % (item.get("full_name", ""), model, VERSION)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _cached(conn: sqlite3.Connection, key: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT payload FROM ai_briefs WHERE cache_key=?", (key,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload"])
    except ValueError:
        return None


def _save(conn: sqlite3.Connection, key: str, item: Dict[str, Any], model: str,
          payload: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO ai_briefs(cache_key, full_name, model, version, payload, created_at)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload, created_at=excluded.created_at""",
        (key, item.get("full_name", ""), model, VERSION,
         json.dumps(payload, ensure_ascii=False),
         dt.datetime.now().astimezone().isoformat(timespec="seconds")),
    )


def _clean(value: Any, limit: int) -> str:
    text = str(value or "").strip().replace("\n", " ")
    return text[:limit]


def _normalize(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(data, dict):
        return None
    what = _clean(data.get("what"), 120)
    if not what:
        return None

    def _list(key: str, limit: int) -> List[str]:
        raw = data.get(key)
        if not isinstance(raw, list):
            return []
        return [_clean(x, 60) for x in raw[:limit] if _clean(x, 60)]

    return {
        "what": what,
        "audience": _clean(data.get("audience"), 80),
        "problem": _clean(data.get("problem"), 120),
        "usage": _clean(data.get("usage"), 120),
        "flow": _list("flow", 4),
        "scenarios": _list("scenarios", 3),
        "difficulty": _clean(data.get("difficulty"), 40),
        "limits": _clean(data.get("limits"), 120),
    }


def generate(item: Dict[str, Any], settings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ok, _reason, llm = ai_client.resolve(settings)
    if not ok:
        return None
    prompt = USER.format(
        full_name=item.get("full_name", ""),
        language=item.get("language") or "未标注",
        stars=item.get("stars") or 0,
        forks=item.get("forks") or 0,
        topics="、".join((item.get("topics") or [])[:8]) or "无",
        description=_clean(item.get("description"), 300) or "无",
        readme=_clean(item.get("readme_zh") or item.get("readme") or "", 600) or "无",
    )
    text, error = ai_client.chat_result(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        settings, json_mode=True)
    if not text:
        return None
    data = ai_client.extract_json(text)
    if data is None:
        # 免费模型偶尔会把 JSON 写坏；再要一次，明确要求只输出 JSON
        retry_prompt = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": text[:800]},
            {"role": "user", "content": "上面的输出不是合法 JSON。请只输出一个完整的 JSON 对象，不要任何解释或代码块。"},
        ]
        retry_text, _err = ai_client.chat_result(retry_prompt, settings, json_mode=True)
        data = ai_client.extract_json(retry_text)
        if data is None:
            print("[ai] brief JSON invalid for", item.get("full_name"))
            return None
    return _normalize(data)


def apply(item: Dict[str, Any], ai: Dict[str, Any], model: str) -> None:
    """把 AI 结果合并进 item['brief']，保留原规则版字段作为兜底。"""
    brief = dict(item.get("brief") or {})
    brief.update({key: value for key, value in ai.items() if value})
    brief["ai"] = True
    brief["ai_model"] = model
    brief["note"] = "由 %s 基于项目简介、语言与 topics 生成；具体命令与限制以 README 为准。" % model
    item["brief"] = brief


def enrich(items: List[Dict[str, Any]], settings: Optional[Dict[str, Any]] = None,
           limit: Optional[int] = None, progress=None) -> Dict[str, int]:
    """为前 N 个项目补齐 AI 解读。返回 {cached, generated, failed, skipped}。"""
    settings = settings or ai_settings.load()
    ok, reason, llm = ai_client.resolve(settings)
    stats = {"cached": 0, "generated": 0, "failed": 0, "skipped": 0}
    if not ok:
        stats["skipped"] = len(items)
        return stats

    ai_cfg = settings.get("ai") or {}
    if not ai_cfg.get("enable_briefs", True):
        stats["skipped"] = len(items)
        return stats

    n = int(limit if limit is not None else ai_cfg.get("briefs_per_run", MAX_BRIEFS_DEFAULT))
    targets = items[:max(0, n)]
    model = llm["model"]

    conn = _conn()
    try:
        pending: List[Dict[str, Any]] = []
        for item in targets:
            key = _key(item, model)
            cached = _cached(conn, key)
            if cached:
                apply(item, cached, model)
                stats["cached"] += 1
            else:
                pending.append(item)

        if not pending:
            conn.commit()
            return stats

        workers = max(1, min(3, int(ai_cfg.get("concurrency") or 1)))
        if progress:
            progress("AI 解读 %d 个项目（模型 %s）" % (len(pending), model))

        def _one(item: Dict[str, Any]):
            return item, generate(item, settings)

        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            for item, ai in pool.map(_one, pending):
                if not ai:
                    stats["failed"] += 1
                    continue
                _save(conn, _key(item, model), item, model, ai)
                apply(item, ai, model)
                stats["generated"] += 1
        conn.commit()
    finally:
        conn.close()
    return stats


def cached_only(items: List[Dict[str, Any]], settings: Optional[Dict[str, Any]] = None) -> int:
    """只读缓存，不发请求（用于 rerank 等不该产生费用的路径）。"""
    settings = settings or ai_settings.load()
    ok, _reason, llm = ai_client.resolve(settings)
    if not ok:
        return 0
    model = llm["model"]
    hit = 0
    conn = _conn()
    try:
        for item in items:
            cached = _cached(conn, _key(item, model))
            if cached:
                apply(item, cached, model)
                hit += 1
    finally:
        conn.close()
    return hit


def generate_one(item: Dict[str, Any], settings: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """单项目生成（供「立即生成」按钮使用）：先查缓存，命中就不再调用模型。"""
    settings = settings or ai_settings.load()
    ok, _reason, llm = ai_client.resolve(settings)
    if not ok:
        return None
    model = llm["model"]
    conn = _conn()
    try:
        cached = _cached(conn, _key(item, model))
        if cached:
            apply(item, cached, model)
            return cached
        ai = generate(item, settings)
        if not ai:
            return None
        _save(conn, _key(item, model), item, model, ai)
        conn.commit()
    finally:
        conn.close()
    apply(item, ai, model)
    return ai
