"""Radar configuration: JSON file + environment overrides.

Kept separate from ``core.config`` on purpose — the GitHub digest reads that module
at import time, and the radar must never be able to change those values.
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
from typing import Any, Dict

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = pathlib.Path(os.environ.get("RADAR_CONFIG", str(BASE_DIR / "social_radar.json")))

# Defaults are the documented baseline. A missing/partial config file still runs.
DEFAULTS: Dict[str, Any] = {
    "version": 1,
    "enabled": True,
    "timezone": "Asia/Shanghai",
    "schedule": {
        # 每天几点生成日报并推送（一天只推一次）
        "daily_time": "08:00",
        # 采集频率：每多少分钟抓一次（60 = 每小时）。日报仍然每天只生成一次。
        "collect_interval_minutes": 60,
        "retry_after_minutes": 10,
        "max_retries": 3,
    },
    "limits": {
        "max_candidates": 600,
        "max_events": 220,
        "max_report_events": 12,
        "source_timeout_seconds": 12,
        "source_retries": 1,
        "min_minutes_between_collects": 20,
        "min_minutes_between_reports": 60,
        "hotlist_top_n": 10,
        "news_top_n": 40,
    },
    "cluster": {
        # 同一轮采集内的合并阈值（token 集合相似度）
        "within_run_threshold": 0.30,
        # 跨轮：新一组材料与已存在事件匹配的阈值（原来写死 0.55，过严导致同一件事被拆开）
        "match_threshold": 0.35,
        # 事件之上聚成「故事」的阈值
        "story_threshold": 0.42,
    },
    "backoff": {
        # 连续失败达到该次数后降频（每 degrade_interval_minutes 分钟才重试一次）
        "degrade_after": 3,
        "degrade_interval_minutes": 60,
        # 连续失败达到该次数后自动停用，直到手动重置
        "pause_after": 6,
    },
    # 第三方聚合源：抖音/知乎/贴吧等无直连公开接口的平台走这个 API
    "newsnow": {
        "api_url": "https://newsnow.busiyi.world/api/s",
        # 校验返回链接的域名；不匹配的条目直接丢弃（防第三方数据被篡改）
        "verify_domain": True,
    },
    # 统一搜索引擎（AnySearch）：可选。两种用法——
    #   1) queries 里每条查询 = 一个"发现源"，和 RSS 源平级；
    #   2) 调查阶段用作外部取证（见 investigation.external_search）。
    # 限流窗口只有 20 次（响应头 X-Ratelimit-Limit），所以发现源按 min_interval_minutes 节流。
    "anysearch": {
        "enabled": False,
        "api_base": "https://api.anysearch.com",
        # key 只从环境变量读，不进配置文件和仓库（与 openalex_api_key_env 同一套）
        "api_key_env": "ANYSEARCH_API_KEY",
        # 能力标签，留空 = 通用网页搜索；可选 academic.search / code.doc / finance.news / security.intel
        "tag": "",
        "zone": "cn",
        "language": "zh-CN",
        "timeout_seconds": 20,
        "extract_timeout_seconds": 30,
        "retries": 1,
        "min_interval_seconds": 1.0,
        "max_results": 10,
        # 发现源默认 4 小时才跑一次，避免每小时采集把 20 次的窗口烧光
        "min_interval_minutes": 240,
        # [{"id": "ai", "name": "AI 与大模型", "query": "AI 大模型 最新进展", "enabled": true}]
        "queries": [],
    },

    # 学术论文模块（paper_* 表，与雷达数据隔离）
    "papers": {
        "enabled": True,
        # 每天几点采集论文（与雷达 08:00 的日报错开）
        "daily_time": "08:30",
        # 采集间隔：24 = 每天只抓一次，避免反复抓取和反复调用模型
        "interval_hours": 24,
        # 回溯天数：覆盖数据源的更新延迟
        "lookback_days": 3,
        # 每个来源每次最多取多少条
        "fetch_per_source": 30,
        # 每个学科最多展示多少篇
        "per_discipline_limit": 10,
        "sources": {
            "crossref": True,
            "pubmed": True,
            "europepmc": True,
            "biorxiv": True,
            "medrxiv": True,
            "huggingface": True,
            # OpenAlex 需要 API Key，默认关闭；没有 Key 时会因共享预算返回 429
            "openalex": False,
        },
        "openalex_api_key_env": "OPENALEX_API_KEY",
        "crossref_mailto": "",
        # AI 不设条数上限，成本统一由全局 token 限额控制（ai_settings.llm.token_limits）
        "ai": {"enabled": True, "concurrency": 2},
    },
    "filters": {
        # 全局噪音词：命中即丢弃（娱乐段子、抽奖、标题党）
        "noise_keywords": [
            "吃不吃挑战", "返厂了", "抽奖", "转发抽", "表情包", "壁纸",
            "cp向", "磕糖", "二创", "鬼畜", "整活", "梗图",
        ],
        # 关键词语法（每行一条）：[组名] / +必须词 / !排除词 / /正则/ => 别名 / @每组上限
        # 留空表示不启用；示例见 docs/radar.md
        "rules": [],
        # 各来源的最低互动门槛（engagement 字段名 -> 最小值）
        "min_engagement": {
            "bilibili_popular": {"view": 50000},
            "hackernews": {"points": 40},
        },
    },

    # 用户自建 RSS 源：加源不用改代码
    # {"id": "hn", "name": "Hacker News", "url": "https://...", "platform": "HN",
    #  "language": "en", "source_type": "news", "max_items": 20, "enabled": true}
    "rss": {
        "feeds": [],
    },
    "scoring": {
        "velocity": 0.30,
        "cross_platform": 0.20,
        "engagement": 0.20,
        "verification": 0.20,
        "interest": 0.10,
    },
    "report": {
        "length": "standard",
        "top_events": 12,
        "section_limits": {"zh_web": 8, "intl": 8, "tech": 8, "investigations": 5},
    },
    "push": {
        "enabled": False,
        "channel": "radar-wechat",
        "items": 5,
        "alert_threshold": 82,
        "max_attempts": 3,
    },
    "interests": {
        "topics": [
            "AI 与大模型", "芯片与半导体", "开源软件", "国际科技公司",
            "游戏与消费电子", "国际新闻", "中国科技发展与科研成果",
            "产业竞争与经济事件",
        ],
        "keywords": [],
        "synonyms": {},
        "exclude": [],
        "languages": ["zh", "en"],
    },
    "investigation": {
        "min_heat": 70,
        "max_evidence": 60,
        "tech_auto_check": True,
        # 外部取证：本地证据不足时，用 AnySearch 搜一圈补充材料（见 anysearch 段）
        "external_search": False,
        "external_max_results": 6,
        # 对前几条抓正文（每条占一次额度，所以默认只抓 2 条）
        "external_extract_top": 2,
        # 本地证据少于这个条数才补，够多就不浪费额度
        "min_local_evidence": 3,
        "tech_keywords": [
            "芯片", "半导体", "光刻", "大模型", "人工智能", "量子", "可控核聚变",
            "超导", "6G", "卫星", "航空发动机", "新材料", "量产", "首次实现",
            "国际领先", "突破",
        ],
    },
    "retention": {"days": 90},
    "llm": {
        "enabled": False,
        "provider": "openai_compatible",
        "base_url": "",
        "model": "",
        "api_key_env": "RADAR_LLM_API_KEY",
        "timeout_seconds": 45,
        "max_attempts": 2,
        "max_events": 12,
        "temperature": 0.2,
        "note": "默认关闭。支持任意 OpenAI 兼容端点或本地 Ollama；未配置时回退到本地抽取式摘要",
    },
    "sources": {},
}

_ENV_OVERRIDES = {
    "RADAR_ENABLED": ("enabled", lambda v: v not in ("0", "false", "False", "")),
    "RADAR_DAILY_TIME": ("schedule.daily_time", str),
    "RADAR_PUSH_ENABLED": ("push.enabled", lambda v: v not in ("0", "false", "False", "")),
    "RADAR_PUSH_ITEMS": ("push.items", int),
    "RADAR_MAX_REPORT_EVENTS": ("report.top_events", int),
}


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _set_path(data: Dict[str, Any], dotted: str, value: Any) -> None:
    node = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def load() -> Dict[str, Any]:
    cfg = copy.deepcopy(DEFAULTS)
    try:
        if CONFIG_PATH.exists():
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig") or "{}")
            if isinstance(raw, dict):
                cfg = _merge(cfg, raw)
            else:
                print("[radar] config file is not a JSON object; using defaults")
    except Exception as exc:  # noqa: BLE001 - a broken config must not stop the app
        print("[radar] config load failed (%s): %s" % (CONFIG_PATH, exc))

    # 大模型是全局能力：以 app_settings.json 为准（旧配置里的 llm 会被合并进去）
    try:
        from .. import ai_settings
        global_llm = ai_settings.load().get("llm") or {}
        if global_llm:
            cfg["llm"] = {**(cfg.get("llm") or {}), **global_llm}
    except Exception as exc:  # noqa: BLE001
        print("[radar] global ai settings unavailable:", exc)

    for env_key, (dotted, caster) in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_key)
        if raw is None or raw == "":
            continue
        try:
            _set_path(cfg, dotted, caster(raw))
        except Exception:  # noqa: BLE001
            print("[radar] ignoring invalid %s=%r" % (env_key, raw))
    return cfg


def cached() -> Dict[str, Any]:
    """Reloaded on every call: edits to social_radar.json take effect without a restart."""
    return load()
