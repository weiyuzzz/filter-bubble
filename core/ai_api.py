"""全局设置 API：模型接口（1..N 个）、连接测试、接口健康、错误日志、用量。

与具体业务模块无关，GitHub 项目解读 / 热点摘要 / 学术论文 / 全球雷达共用。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from . import ai_client, ai_secrets, ai_settings, ai_usage

_LLM_GLOBAL_KEYS = ("enabled", "timeout_seconds", "max_attempts", "temperature",
                    "max_tokens", "quota_window_hours")
_LLM_KEYS = _LLM_GLOBAL_KEYS  # 兼容旧名字
_SLOT_KEYS = ("id", "name", "enabled", "preset", "provider", "base_url", "model",
              "key_ref", "priority", "note")
_AI_KEYS = ("concurrency", "enable_briefs")


def _ok(payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    return 200, payload


def _err(status: int, message: str) -> Tuple[int, Dict[str, Any]]:
    return status, {"error": message}


def _push_public(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """推送开关 + 实际能不能发（密钥是否配好）。"""
    from . import notify

    enabled = bool((cfg.get("push") or {}).get("enabled", True))
    state = notify.state()
    return {
        "enabled": enabled,
        # enabled 是开关，ready 才是「开关开着且密钥可用」
        "ready": bool(state.get("ready")),
        "configured": bool(state.get("configured")),
        "reason": state.get("reason") or "",
        "provider": state.get("provider") or "",
        "time": state.get("time") or "",
        "items": state.get("items") or 0,
    }


def _public() -> Dict[str, Any]:
    cfg = ai_settings.load()
    llm = cfg.get("llm") or {}
    info = ai_client.describe(cfg)
    slots = info["slots"]
    first = slots[0] if slots else {}
    fb = next((item for item in slots if item["id"] == "fallback"), None)
    if fb is None and len(slots) > 1:
        fb = slots[1]
    status = ai_client.status(cfg)
    return {
        "version": ai_settings.VERSION,
        "llm": {
            **{key: llm.get(key) for key in _LLM_GLOBAL_KEYS},
            "providers": info["providers"],
            "provider_labels": info["provider_labels"],
            "presets": info["presets"],
            "slots": slots,
            "available": info["available"],
            "unavailable_reason": info["unavailable_reason"],
            "warnings": info["warnings"],
            "active_slot": info["active_slot"],
            "switch": info["switch"],
            "last_used": info["last_used"],
            "stats": {k: ai_client.STATS.get(k) for k in
                      ("calls", "failures", "prompt_tokens", "completion_tokens", "last_error")},
            "usage": status.get("usage") or {},
            "token_limits": llm.get("token_limits") or {},
            # ---- 兼容旧前端的字段（主接口视角） ----
            "provider": llm.get("provider") or "openai",
            "base_url": llm.get("base_url") or "",
            "model": llm.get("model") or "",
            "api_key_env": llm.get("api_key_env") or ai_secrets.ENV_NAME,
            "key": first.get("key") or {},
            "fallback": {
                "enabled": bool((fb or {}).get("enabled")),
                "provider": (fb or {}).get("provider") or "openai",
                "base_url": (fb or {}).get("base_url") or "",
                "model": (fb or {}).get("model") or "",
                "available": bool((fb or {}).get("available")),
                "unavailable_reason": (fb or {}).get("unavailable_reason") or "",
                "api_key_env": (fb or {}).get("key", {}).get("env_name")
                               or ai_secrets.FALLBACK_ENV_NAME,
                "key": (fb or {}).get("key") or {},
            },
        },
        "ai": {
            "concurrency": int((cfg.get("ai") or {}).get("concurrency") or 1),
            "enable_briefs": bool((cfg.get("ai") or {}).get("enable_briefs", True)),
        },
        "push": _push_public(cfg),
        "path": str(ai_settings.CONFIG_PATH),
        "secrets_path": str(ai_secrets.SECRETS_PATH),
        "error_log": str(ai_client.ERROR_LOG),
        "model_resolved": first.get("model") or "",
    }


def handle_get(path: str, query: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    try:
        if path in ("/api/ai/settings", "/api/ai/status"):
            return _ok(_public())
        if path == "/api/ai/errors":
            return _ok({"errors": ai_client.read_errors(limit=30)})
        if path == "/api/ai/presets":
            return _ok({"presets": ai_client.preset_list(),
                        "providers": list(ai_client.PROVIDERS),
                        "provider_labels": dict(ai_client.PROVIDER_LABELS)})
        if path == "/api/ai/usage":
            cfg = ai_settings.load()
            return _ok(ai_client.status(cfg).get("usage") or {})
        return _err(404, "not found")
    except Exception as exc:  # noqa: BLE001
        print("[ai] api GET error:", exc)
        return _err(500, str(exc))


def handle_post(path: str, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    try:
        if path == "/api/ai/settings":
            return _ok(_save(body))
        if path == "/api/ai/test":
            cfg = ai_settings.load()
            record = bool(body.get("record"))
            if body.get("all"):
                return _ok({"results": ai_client.test_all_slots(cfg, record=record),
                            "settings": _public()})
            slot = str(body.get("slot") or "primary")
            result = ai_client.test_connection(cfg, slot, record=record)
            return _ok({"result": result, "settings": _public()})
        if path in ("/api/ai/clear-key", "/api/ai/clear_key"):
            ref = str(body.get("key_ref") or body.get("slot") or "primary")
            changed = ai_secrets.clear_api_key(ref)
            return _ok({"cleared": changed, "key_ref": ref, "settings": _public()})
        if path == "/api/ai/usage":
            cfg = ai_settings.load()
            return _ok(ai_client.status(cfg).get("usage") or {})
        if path == "/api/ai/resume":
            ai_usage.resume()
            return _ok({"resumed": True, "settings": _public()})
        if path == "/api/ai/health/reset":
            slot = str(body.get("slot") or "").strip()
            ai_client.clear_slot_health(slot or None)
            if body.get("reset_slot"):
                ai_client.reset_slot()
            return _ok({"reset": True, "slot": slot or "all", "settings": _public()})
        if path == "/api/ai/health/test-all":
            cfg = ai_settings.load()
            return _ok({"results": ai_client.test_all_slots(cfg, record=True),
                        "settings": _public()})
        return _err(404, "not found")
    except Exception as exc:  # noqa: BLE001
        print("[ai] api POST error:", exc)
        return _err(500, str(exc))


def _coerce(value: Any, kind: str) -> Any:
    if kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() not in ("0", "false", "no", "off", "")
        return bool(value)
    if kind == "int":
        return int(float(value))
    if kind == "float":
        return float(value)
    return str(value or "").strip()


def _clamp(value: Any, low: float, high: float, kind: str = "int", default: Any = 0) -> Any:
    try:
        number = _coerce(value, kind)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _key_warning(key: str, slot: Dict[str, Any]) -> str:
    """Key 与服务商不匹配的提醒（这是最常见的 401 来源）。"""
    key = str(key or "")
    if not key:
        return ""
    hits: List[Tuple[str, str]] = []
    for preset_id, preset in ai_client.PRESETS.items():
        for hint in preset.get("key_hints") or []:
            if key.startswith(hint):
                hits.append((preset_id, hint))
    if hits:
        longest = max(len(hint) for _pid, hint in hits)
        top = {pid for pid, hint in hits if len(hint) == longest}
        preset = str(slot.get("preset") or "")
        if len(top) == 1 and preset not in top:
            other = top.pop()
            return "这把 Key 看起来是「%s」的（以 %s 开头），当前接口是「%s」——多半会 401。" % (
                ai_client.PRESETS[other]["label"], hits[0][1],
                ai_client.PRESETS.get(preset, {}).get("label") or "自定义")
    preset = str(slot.get("preset") or "")
    expect = (ai_client.PRESETS.get(preset) or {}).get("key_hints") or []
    if expect and not any(key.startswith(hint) for hint in expect):
        return "「%s」的 Key 通常以 %s 开头，请确认没有填错服务商。" % (
            ai_client.PRESETS[preset]["label"], " / ".join(expect))
    return ""


def _save(body: Dict[str, Any]) -> Dict[str, Any]:
    """保存设置。兼容旧字段（api_key/fallback/token_limits）与新的 slots 列表。"""
    updates: Dict[str, Any] = {"llm": {}, "ai": {}}
    saved: Dict[str, Any] = {}
    warnings: List[str] = []

    incoming_llm = body.get("llm") if isinstance(body.get("llm"), dict) else {}
    for key in _LLM_GLOBAL_KEYS:
        if key not in incoming_llm:
            continue
        value = incoming_llm[key]
        try:
            if key == "enabled":
                updates["llm"][key] = _coerce(value, "bool")
            elif key == "timeout_seconds":
                updates["llm"][key] = _clamp(value, 1, 300, "int", 60)
            elif key == "max_attempts":
                updates["llm"][key] = _clamp(value, 1, 3, "int", 2)
            elif key == "max_tokens":
                updates["llm"][key] = _clamp(value, 50, 8000, "int", 1500)
            elif key == "temperature":
                updates["llm"][key] = _clamp(value, 0.0, 1.5, "float", 0.2)
            elif key == "quota_window_hours":
                updates["llm"][key] = _clamp(value, 1, 168, "float", 24)
            saved[key] = updates["llm"][key]
        except (TypeError, ValueError):
            continue

    # ---- 新格式：slots 列表 ----
    current_slots = {s["id"]: s for s in ai_settings.slots_of(ai_settings.load())}
    incoming_slots = body.get("slots") if isinstance(body.get("slots"), list) else None
    if incoming_slots is None:
        incoming_slots = incoming_llm.get("slots") if isinstance(incoming_llm.get("slots"), list) else None
    slot_updates: List[Dict[str, Any]] = []
    keyed_slots: List[str] = []
    if incoming_slots is not None:
        for index, raw in enumerate(incoming_slots):
            if not isinstance(raw, dict):
                continue
            item: Dict[str, Any] = {}
            slot_id = ai_settings.slug(raw.get("id") or "slot-%d" % (index + 1),
                                       "slot-%d" % (index + 1))
            item["id"] = slot_id
            for key in ("name", "base_url", "model", "note"):
                if key in raw:
                    item[key] = str(raw[key] or "")[:300]
            if "enabled" in raw:
                item["enabled"] = _coerce(raw["enabled"], "bool")
            if "preset" in raw:
                item["preset"] = ai_settings.slug(raw["preset"], "") if raw["preset"] else ""
            if "provider" in raw:
                item["provider"] = ai_client.normalize_provider(str(raw["provider"] or ""))
            if "key_ref" in raw and str(raw["key_ref"] or "").strip():
                item["key_ref"] = ai_secrets.normalize_ref(raw["key_ref"])
            if "priority" in raw:
                item["priority"] = _clamp(raw["priority"], 1, 99, "int", index + 1)
            raw_quota = raw.get("quota") if isinstance(raw.get("quota"), dict) else None
            if raw_quota:
                mode = str(raw_quota.get("mode") or "daily").strip().lower()
                quota: Dict[str, Any] = {"mode": mode if mode in ai_settings.QUOTA_MODES else "daily"}
                if "limit" in raw_quota:
                    quota["limit"] = _clamp(raw_quota["limit"], 0, 10 ** 12, "int", 0)
                if "window_hours" in raw_quota:
                    quota["window_hours"] = _clamp(raw_quota["window_hours"], 0.25, 168, "float", 24)
                if "reset_days" in raw_quota:
                    quota["reset_days"] = _clamp(raw_quota["reset_days"], 0, 365, "int", 0)
                item["quota"] = quota
            # 单接口的 Key（写一次就不再回传）
            new_key = str(raw.get("key") or "").strip()
            if raw.get("clear_key"):
                cleared = ai_secrets.clear_api_key(item.get("key_ref") or slot_id)
                saved["cleared_key_%s" % slot_id] = cleared
            elif new_key:
                ref = ai_secrets.normalize_ref(item.get("key_ref") or slot_id)
                if ai_secrets.set_api_key(new_key, ref):
                    saved["key_%s" % slot_id] = "saved"
                    keyed_slots.append(slot_id)
                    preset = str(item.get("preset") or
                                 (current_slots.get(slot_id) or {}).get("preset") or "")
                    warning = _key_warning(new_key, {"preset": preset})
                    if warning:
                        warnings.append("%s（%s）" % (warning, item.get("name") or slot_id))
            slot_updates.append(item)
        if slot_updates:
            updates["llm"]["slots"] = slot_updates
            saved["slots"] = [item["id"] for item in slot_updates]

    delete_slots = body.get("delete_slots")
    if isinstance(delete_slots, (list, str)):
        updates["llm"]["delete_slots"] = delete_slots
        saved["deleted_slots"] = delete_slots if isinstance(delete_slots, list) else [delete_slots]

    # ---- 旧格式兼容：平铺字段 / fallback 块 / token_limits ----
    legacy_slot: Dict[str, Any] = {}
    for key in ("provider", "base_url", "model", "api_key_env", "key_ref"):
        if key in incoming_llm:
            legacy_slot[key] = incoming_llm[key]
    if legacy_slot:
        updates["llm"].update(legacy_slot)
        saved.update(legacy_slot)

    incoming_fb = body.get("fallback") if isinstance(body.get("fallback"), dict) else None
    if incoming_fb is None and isinstance(incoming_llm.get("fallback"), dict):
        incoming_fb = incoming_llm["fallback"]
    if incoming_fb:
        fb_update: Dict[str, Any] = {}
        for key in ("enabled", "provider", "base_url", "model", "api_key_env", "key_ref"):
            if key not in incoming_fb:
                continue
            value = incoming_fb[key]
            if key == "enabled":
                fb_update[key] = _coerce(value, "bool")
            elif key == "provider":
                fb_update[key] = ai_client.normalize_provider(str(value or ""))
            else:
                fb_update[key] = str(value or "")[:300]
            saved["fallback_" + key] = fb_update[key]
        if fb_update:
            updates["llm"]["fallback"] = fb_update

    incoming_limits = body.get("token_limits")
    if not isinstance(incoming_limits, dict):
        incoming_limits = incoming_llm.get("token_limits")
    if isinstance(incoming_limits, dict) and incoming_limits:
        limits: Dict[str, Any] = {}
        for key in ("primary", "fallback"):
            if key in incoming_limits:
                limits[key] = _clamp(incoming_limits[key], 0, 10 ** 12, "int", 0)
                saved["token_limit_" + key] = limits[key]
        if "window_hours" in incoming_limits:
            limits["window_hours"] = _clamp(incoming_limits["window_hours"], 1, 168, "float", 24)
            saved["token_window_hours"] = limits["window_hours"]
        if limits:
            updates["llm"]["token_limits"] = limits

    # ---- AI 解读与推送 ----
    incoming_ai = body.get("ai") if isinstance(body.get("ai"), dict) else {}
    for key in _AI_KEYS:
        if key not in incoming_ai:
            continue
        try:
            if key == "enable_briefs":
                updates["ai"][key] = _coerce(incoming_ai[key], "bool")
            elif key == "concurrency":
                updates["ai"][key] = _clamp(incoming_ai[key], 1, 4, "int", 1)
            saved[key] = updates["ai"][key]
        except (TypeError, ValueError):
            continue

    incoming_push = body.get("push") if isinstance(body.get("push"), dict) else {}
    if "enabled" in incoming_push:
        updates["push"] = {"enabled": _coerce(incoming_push["enabled"], "bool")}
        saved["push_enabled"] = updates["push"]["enabled"]

    ai_settings.save(updates)

    # ---- 旧字段写入的 Key（写、清） ----
    if body.get("reset_slot"):
        ai_client.reset_slot()
        saved["slot"] = "primary"
    if body.get("clear_health"):
        ai_client.clear_slot_health()
        saved["health"] = "cleared"

    if body.get("clear_api_key"):
        if ai_secrets.clear_api_key("primary"):
            saved["api_key"] = "cleared"
    elif str(body.get("api_key") or "").strip():
        if ai_secrets.set_api_key(str(body["api_key"]), "primary"):
            saved["api_key"] = "saved"

    if body.get("clear_fallback_api_key"):
        if ai_secrets.clear_api_key("fallback"):
            saved["fallback_api_key"] = "cleared"
    elif str(body.get("fallback_api_key") or "").strip():
        if ai_secrets.set_api_key(str(body["fallback_api_key"]), "fallback"):
            saved["fallback_api_key"] = "saved"

    return {"saved": saved, "settings": _public(), "warnings": warnings,
            "api_key_slots": keyed_slots}