"""全局 AI 设置：读写项目根目录的 app_settings.json。

设计原则：
* 一份配置，所有模块共用（GitHub 项目解读、热点摘要、学术论文、全球雷达）；
* 接口数量不写死：``llm.slots`` 是一个有序列表（1..N 个接口），按 ``priority`` 依次尝试；
* 兼容旧格式：``llm`` 顶层字段 + ``llm.fallback`` + ``llm.token_limits`` 会被自动
  迁移成 ``primary`` / ``fallback`` 两个 slot，老调用方继续只读 ``llm.model`` 这类镜像字段；
* 密钥不在这里，见 core/ai_secrets.py；key 只用 ``key_ref`` 引用，所以文件里不会出现
  ``api_key`` 字样。

落盘格式（version 2）::

    {
      "version": 2,
      "llm": {
        "enabled": true,
        "timeout_seconds": 33, "max_attempts": 2,
        "temperature": 0.5, "max_tokens": 999,
        "quota_window_hours": 24,
        "slots": [
          {"id": "primary", "name": "主接口", "enabled": true, "preset": "deepseek",
           "provider": "openai", "base_url": "https://api.deepseek.com",
           "model": "deepseek-chat", "key_ref": "deepseek", "priority": 1,
           "quota": {"mode": "daily", "limit": 500000, "window_hours": 24, "reset_days": 0}}
        ]
      },
      "ai": {...}, "push": {...}
    }
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
import re
from typing import Any, Dict, List, Optional

from . import ai_secrets

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = pathlib.Path(os.environ.get("AI_CONFIG", str(BASE_DIR / "app_settings.json")))
LEGACY_PATH = pathlib.Path(os.environ.get("RADAR_CONFIG", str(BASE_DIR / "social_radar.json")))

VERSION = 2

# 额度模式：daily 自然日清零 / window 每 N 小时一个窗口（套餐常见）/ total 总量（累计不重置）
QUOTA_MODES = ("daily", "window", "total")
QUOTA_KEYS = ("mode", "limit", "window_hours", "reset_days")
SLOT_KEYS = ("id", "name", "enabled", "preset", "provider", "base_url", "model",
             "key_ref", "priority", "quota", "note")
LLM_GLOBAL_KEYS = ("enabled", "timeout_seconds", "max_attempts", "temperature",
                   "max_tokens", "quota_window_hours")


def _slot(slot_id: str, name: str, priority: int, key_ref: str = "") -> Dict[str, Any]:
    return {
        "id": slot_id,
        "name": name,
        "enabled": False,
        "preset": "",
        "provider": "openai",
        "base_url": "",
        "model": "",
        "key_ref": key_ref or slot_id,
        "priority": priority,
        "quota": {"mode": "daily", "limit": 0, "window_hours": 24, "reset_days": 0},
        "note": "",
    }


DEFAULTS: Dict[str, Any] = {
    "version": VERSION,
    "llm": {
        "enabled": False,
        "timeout_seconds": 60,
        "max_attempts": 2,
        "temperature": 0.2,
        "max_tokens": 1500,
        # 额度窗口（小时）：窗口型套餐的刷新周期，也是「全部用尽后暂停」的时长
        "quota_window_hours": 24,
        "slots": [_slot("primary", "主接口", 1, "primary"),
                  _slot("fallback", "备用接口", 2, "fallback")],
    },
    "ai": {
        # 并发请求数，免费模型建议 1–2，避免触发上游限流
        "concurrency": 1,
        # 是否在生成日报时自动调用模型
        "enable_briefs": True,
    },
    # 微信简报推送开关（在「全局设置」里改；密钥仍在 .env）
    "push": {
        "enabled": True,
    },
}

_LEGACY_INHERIT = ("enabled", "provider", "base_url", "model", "api_key_env", "key_ref")


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _read(path: pathlib.Path) -> Dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
            if isinstance(data, dict):
                return data
    except Exception as exc:  # noqa: BLE001
        print("[ai] settings read failed (%s): %s" % (path, exc))
    return {}


# ---------------- 归一化 ----------------

def slug(value: Any, fallback: str = "slot") -> str:
    text = re.sub(r"[^a-z0-9_.-]+", "-", str(value or "").strip().lower()).strip("-._")
    return text or fallback


def norm_quota(raw: Any) -> Dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    mode = str(raw.get("mode") or "daily").strip().lower()
    if mode not in QUOTA_MODES:
        mode = "daily"
    try:
        limit = max(0, int(float(raw.get("limit") or 0)))
    except (TypeError, ValueError):
        limit = 0
    try:
        window = float(raw.get("window_hours") or 24)
    except (TypeError, ValueError):
        window = 24.0
    if window <= 0:
        window = 24.0
    try:
        reset_days = max(0, int(float(raw.get("reset_days") or 0)))
    except (TypeError, ValueError):
        reset_days = 0
    return {"mode": mode, "limit": limit, "window_hours": window, "reset_days": reset_days}


def norm_slot(raw: Dict[str, Any], index: int = 0) -> Dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    slot_id = slug(raw.get("id") or raw.get("name") or "slot-%d" % (index + 1), "slot-%d" % (index + 1))
    out = {
        "id": slot_id,
        "name": str(raw.get("name") or slot_id)[:60] or slot_id,
        "enabled": bool(raw.get("enabled")),
        "preset": slug(raw.get("preset") or "", ""),
        "provider": str(raw.get("provider") or "openai").strip() or "openai",
        "base_url": str(raw.get("base_url") or "").strip()[:300],
        "model": str(raw.get("model") or "").strip()[:200],
        "key_ref": ai_secrets.normalize_ref(raw.get("key_ref") or raw.get("api_key_env") or slot_id),
        "priority": int(raw.get("priority") or (index + 1)),
        "quota": norm_quota(raw.get("quota")),
        "note": str(raw.get("note") or "")[:300],
    }
    return out


def _llm_block(settings: Any) -> Dict[str, Any]:
    """settings 可能是整份配置，也可能直接是 llm 段。"""
    if isinstance(settings, dict):
        llm = settings.get("llm")
        if isinstance(llm, dict):
            return llm
        if any(key in settings for key in ("provider", "base_url", "model", "slots", "fallback")):
            return settings
    return {}


def _legacy_slots(llm: Dict[str, Any]) -> List[Dict[str, Any]]:
    """旧格式（llm 平铺 + llm.fallback + llm.token_limits）→ slot 列表。"""
    limits = llm.get("token_limits") if isinstance(llm.get("token_limits"), dict) else {}
    window_hours = limits.get("window_hours") or llm.get("quota_window_hours") or 24

    def _quota(limit: Any) -> Dict[str, Any]:
        return {"mode": "daily", "limit": limit or 0, "window_hours": window_hours,
                "reset_days": 0}

    primary = _slot("primary", "主接口", 1, "primary")
    for key in _LEGACY_INHERIT:
        if key in llm and llm.get(key) not in (None, ""):
            primary[key] = llm[key]
    primary["key_ref"] = ai_secrets.normalize_ref(
        llm.get("key_ref") or _env_to_ref(llm.get("api_key_env")) or "primary")
    primary["quota"] = _quota(limits.get("primary"))

    slots = [primary]
    fallback_raw = llm.get("fallback") if isinstance(llm.get("fallback"), dict) else {}
    if fallback_raw:
        item = _slot("fallback", "备用接口", 2, "fallback")
        for key in _LEGACY_INHERIT:
            if key in fallback_raw and fallback_raw.get(key) not in (None, ""):
                item[key] = fallback_raw[key]
        # 旧行为：备用开关跟随总开关（enabled = 备用开关 and 总开关）
        item["enabled"] = bool(fallback_raw.get("enabled")) and bool(llm.get("enabled", True))
        item["key_ref"] = ai_secrets.normalize_ref(
            fallback_raw.get("key_ref") or _env_to_ref(fallback_raw.get("api_key_env"))
            or "fallback")
        item["quota"] = _quota(limits.get("fallback"))
        slots.append(item)
    return slots


def _env_to_ref(env_name: Any) -> str:
    env_name = str(env_name or "").strip().upper()
    if not env_name:
        return ""
    if env_name == ai_secrets.ENV_NAME:
        return "primary"
    if env_name == ai_secrets.FALLBACK_ENV_NAME:
        return "fallback"
    prefix = "RADAR_LLM_API_KEY_"
    if env_name.startswith(prefix):
        return env_name[len(prefix):].lower()
    return ""


def slots_of(settings: Any) -> List[Dict[str, Any]]:
    """统一入口：任何形态的配置 → 归一化后的 slot 列表（保持顺序）。"""
    llm = _llm_block(settings)
    raw = llm.get("slots")
    if isinstance(raw, list):
        if raw:
            return [norm_slot(item, index) for index, item in enumerate(raw)
                    if isinstance(item, dict)]
        # 显式空列表：只有 llm 段里还残留旧字段时才迁移，否则就是「一个接口都没有」
        if not any(llm.get(key) for key in ("provider", "base_url", "model")):
            return []
    if not llm:
        return [norm_slot(copy.deepcopy(item), index)
                for index, item in enumerate(DEFAULTS["llm"]["slots"])]
    return _legacy_slots(llm)


def normalize(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """补全 defaults + 统一 slots + 生成兼容镜像字段（只在内存里）。"""
    slots = slots_of(cfg)
    out = _merge(DEFAULTS, cfg)
    llm = dict(out.get("llm") or {})
    slots.sort(key=lambda item: int(item.get("priority") or 0))
    for index, item in enumerate(slots):
        if not item.get("priority"):
            item["priority"] = index + 1
    llm["slots"] = slots

    master = bool(llm.get("enabled"))
    first = slots[0] if slots else {}
    # 兼容镜像：老代码只读这些字段
    llm["provider"] = first.get("provider") or "openai"
    llm["base_url"] = first.get("base_url") or ""
    llm["model"] = first.get("model") or ""
    llm["api_key_env"] = ai_secrets.env_name_for(first.get("key_ref") or "primary")

    fb = next((item for item in slots if item["id"] == "fallback"), None)
    if fb is None and len(slots) > 1:
        fb = slots[1]
    if fb is not None:
        llm["fallback"] = {
            "enabled": bool(fb.get("enabled")),
            "provider": fb.get("provider") or "openai",
            "base_url": fb.get("base_url") or "",
            "model": fb.get("model") or "",
            "key_ref": fb.get("key_ref") or "fallback",
            "api_key_env": ai_secrets.env_name_for(fb.get("key_ref") or "fallback"),
        }
    else:
        llm["fallback"] = {"enabled": False, "provider": "openai", "base_url": "",
                           "model": "", "key_ref": "fallback",
                           "api_key_env": ai_secrets.FALLBACK_ENV_NAME}

    def _limit(slot_id: str) -> int:
        item = next((s for s in slots if s["id"] == slot_id), None)
        return int((item or {}).get("quota", {}).get("limit") or 0)

    llm["token_limits"] = {"primary": _limit("primary"), "fallback": _limit("fallback"),
                           "window_hours": float(llm.get("quota_window_hours") or 24)}
    llm["enabled"] = master

    out["version"] = VERSION
    out["llm"] = llm
    out["ai"] = _merge(DEFAULTS["ai"], out.get("ai") or {})
    out["ai"].pop("briefs_per_run", None)
    out["push"] = _merge(DEFAULTS["push"], out.get("push") or {})
    return out


def load() -> Dict[str, Any]:
    """读取配置：老格式自动迁移（不落盘，除非之后调用 save）。"""
    raw = _read(CONFIG_PATH)
    if not raw and not CONFIG_PATH.exists():
        legacy = (_read(LEGACY_PATH).get("llm") or {})
        if legacy:
            keep = {k: v for k, v in legacy.items()
                    if k in ("enabled", "provider", "base_url", "model", "api_key_env",
                             "timeout_seconds", "max_attempts", "temperature", "max_tokens")}
            if keep:
                raw = {"llm": keep}
    return normalize(copy.deepcopy(raw))


def _apply_legacy_slot(llm: Dict[str, Any], incoming: Dict[str, Any], slot_id: str) -> None:
    """旧前端字段（provider/base_url/model/api_key_env）→ 指定 slot。"""
    slots = llm.setdefault("slots", [])
    target = next((s for s in slots if s["id"] == slot_id), None)
    if target is None:
        target = norm_slot({"id": slot_id, "name": slot_id, "priority": len(slots) + 1}, len(slots))
        slots.append(target)
    for key in _LEGACY_INHERIT:
        if key in incoming:
            if key == "key_ref":
                target["key_ref"] = ai_secrets.normalize_ref(incoming[key])
            elif key == "api_key_env":
                ref = _env_to_ref(incoming[key])
                if ref:
                    target["key_ref"] = ref
            else:
                target[key] = incoming[key]


def save(updates: Dict[str, Any]) -> Dict[str, Any]:
    """只写白名单字段；兼容旧字段与新的 slots 列表。返回写盘后的完整配置。"""
    current = load()
    llm = current.setdefault("llm", {})
    incoming_llm = updates.get("llm") if isinstance(updates.get("llm"), dict) else {}

    # 全局参数
    for key in LLM_GLOBAL_KEYS:
        if key in incoming_llm and key != "enabled":
            llm[key] = incoming_llm[key]
    if "enabled" in incoming_llm:
        llm["enabled"] = bool(incoming_llm["enabled"])

    # 旧字段 → 主接口
    if any(key in incoming_llm for key in _LEGACY_INHERIT):
        _apply_legacy_slot(llm, incoming_llm, "primary")

    # 旧备用接口块
    incoming_fb = incoming_llm.get("fallback")
    if isinstance(incoming_fb, dict) and incoming_fb:
        merged = dict(incoming_fb)
        if "enabled" in merged:
            merged["enabled"] = bool(merged["enabled"])
        _apply_legacy_slot(llm, merged, "fallback")

    # 旧的 token_limits → 各 slot 的 quota 上限
    incoming_limits = incoming_llm.get("token_limits")
    if not isinstance(incoming_limits, dict):
        incoming_limits = updates.get("token_limits")
    if isinstance(incoming_limits, dict) and incoming_limits:
        if "window_hours" in incoming_limits:
            llm["quota_window_hours"] = incoming_limits["window_hours"]
        for key in ("primary", "fallback"):
            if key in incoming_limits:
                target = next((s for s in llm.get("slots", []) if s["id"] == key), None)
                if target is None:
                    target = norm_slot({"id": key, "name": key,
                                        "priority": len(llm.get("slots", [])) + 1},
                                       len(llm.get("slots", [])))
                    llm.setdefault("slots", []).append(target)
                quota = norm_quota(target.get("quota"))
                quota["limit"] = incoming_limits[key]
                target["quota"] = quota

    # 新的 slots 列表（按 id 合并；delete_slots 删除，order 重排）
    incoming_slots = incoming_llm.get("slots")
    if not isinstance(incoming_slots, list):
        incoming_slots = updates.get("slots") if isinstance(updates.get("slots"), list) else None
    delete_slots = incoming_llm.get("delete_slots") or updates.get("delete_slots") or []
    if isinstance(delete_slots, str):
        delete_slots = [delete_slots]
    drop = set()
    if delete_slots:
        drop = {slug(item, "") for item in delete_slots}
        llm["slots"] = [s for s in llm.get("slots", []) if s["id"] not in drop]
    if incoming_slots is not None:
        existing = {s["id"]: s for s in llm.get("slots", [])}
        merged_slots: List[Dict[str, Any]] = []
        for index, raw in enumerate(incoming_slots):
            if not isinstance(raw, dict):
                continue
            raw = dict(raw)
            slot_id = slug(raw.get("id") or "slot-%d" % (index + 1), "slot-%d" % (index + 1))
            if slot_id in drop:  # 同一次请求里既要改又要删 → 删优先
                continue
            raw["id"] = slot_id
            raw.setdefault("priority", index + 1)
            base = existing.get(slot_id)
            if base:
                patched = dict(base)
                for key in SLOT_KEYS:
                    if key in raw:
                        patched[key] = raw[key]
                patched["id"] = slot_id
                merged_slots.append(norm_slot(patched, index))
            else:
                merged_slots.append(norm_slot(raw, index))
        # 未在请求里出现的旧 slot 保留（除非被删除）
        for slot_id, base in existing.items():
            if slot_id not in {s["id"] for s in merged_slots}:
                merged_slots.append(norm_slot(base, len(merged_slots)))
        for index, item in enumerate(merged_slots):
            item["priority"] = int(item.get("priority") or (index + 1))
        merged_slots.sort(key=lambda item: item["priority"])
        llm["slots"] = merged_slots

    # 其它段
    ai = current.setdefault("ai", {})
    incoming_ai = updates.get("ai") if isinstance(updates.get("ai"), dict) else {}
    for key in ("concurrency", "enable_briefs"):
        if key in incoming_ai:
            ai[key] = incoming_ai[key]
    push = current.setdefault("push", {})
    incoming_push = updates.get("push") if isinstance(updates.get("push"), dict) else {}
    if "enabled" in incoming_push:
        push["enabled"] = bool(incoming_push["enabled"])

    # 落盘：只写白名单字段（绝不写 key / api_key_env）
    document = {
        "version": VERSION,
        "llm": {key: llm.get(key) for key in LLM_GLOBAL_KEYS},
        "ai": {"concurrency": int(ai.get("concurrency") or 1),
               "enable_briefs": bool(ai.get("enable_briefs", True))},
        "push": {"enabled": bool(push.get("enabled", True))},
    }
    document["llm"]["slots"] = [
        {
            "id": item["id"], "name": item["name"], "enabled": bool(item["enabled"]),
            "preset": item.get("preset") or "", "provider": item.get("provider") or "openai",
            "base_url": item.get("base_url") or "", "model": item.get("model") or "",
            "key_ref": item.get("key_ref") or item["id"],
            "priority": int(item.get("priority") or 1),
            "quota": norm_quota(item.get("quota")),
        }
        for item in llm.get("slots", [])
    ]
    try:
        document["llm"]["timeout_seconds"] = int(float(document["llm"].get("timeout_seconds") or 60))
        document["llm"]["max_attempts"] = int(float(document["llm"].get("max_attempts") or 2))
        document["llm"]["max_tokens"] = int(float(document["llm"].get("max_tokens") or 1500))
        document["llm"]["temperature"] = float(document["llm"].get("temperature") or 0.2)
        document["llm"]["quota_window_hours"] = float(document["llm"].get("quota_window_hours") or 24)
    except (TypeError, ValueError):
        pass
    document["llm"]["enabled"] = bool(llm.get("enabled"))

    CONFIG_PATH.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    return load()


def public() -> Dict[str, Any]:
    return load()