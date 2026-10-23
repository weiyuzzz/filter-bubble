"""全局设置 API：大模型配置、连接测试、错误日志。

与具体业务模块无关，GitHub 项目解读与热点摘要共用。
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

from . import ai_client, ai_secrets, ai_settings, ai_usage

_LLM_KEYS = ("enabled", "provider", "base_url", "model", "api_key_env",
             "timeout_seconds", "max_attempts", "temperature", "max_tokens")
_AI_KEYS = ("concurrency", "enable_briefs")


def _ok(payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    return 200, payload


def _err(status: int, message: str) -> Tuple[int, Dict[str, Any]]:
    return status, {"error": message}


def _public() -> Dict[str, Any]:
    cfg = ai_settings.load()
    llm = cfg.get("llm") or {}
    fb = llm.get("fallback") or {}
    ok, reason, resolved = ai_client.resolve(cfg)
    fb_ok, fb_reason, _fb_resolved = ai_client.resolve(cfg, "fallback")
    return {
        "llm": {
            "enabled": bool(llm.get("enabled")),
            "provider": ai_client.normalize_provider(llm.get("provider") or "openai"),
            "base_url": llm.get("base_url") or "",
            "model": llm.get("model") or "",
            "api_key_env": llm.get("api_key_env") or ai_secrets.ENV_NAME,
            "timeout_seconds": llm.get("timeout_seconds"),
            "providers": list(ai_client.PROVIDERS),
            "provider_labels": dict(ai_client.PROVIDER_LABELS),            "available": ok,
            "unavailable_reason": reason,
            "key": ai_secrets.public_state(str(llm.get("api_key_env") or ai_secrets.ENV_NAME)),
            "stats": {k: ai_client.STATS.get(k) for k in
                      ("calls", "failures", "prompt_tokens", "completion_tokens", "last_error")},
            # 主/备切换状态
            "active_slot": ai_client.active_slot(),
            "switch": ai_client.slot_state(),
            "token_limits": llm.get("token_limits") or {"primary": 0, "fallback": 0,
                                                        "window_hours": 24},
            "usage": ai_client.status(cfg).get("usage") or {},
            "fallback": {
                "enabled": bool(fb.get("enabled")),
                "provider": ai_client.normalize_provider(fb.get("provider") or "openai"),
                "base_url": fb.get("base_url") or "",
                "model": fb.get("model") or "",
                "available": fb_ok,
                "unavailable_reason": fb_reason,
                "api_key_env": fb.get("api_key_env") or ai_secrets.FALLBACK_ENV_NAME,
                "key": ai_secrets.public_state(
                    str(fb.get("api_key_env") or ai_secrets.FALLBACK_ENV_NAME), "fallback"),
            },
        },
        "ai": {
            "concurrency": int((cfg.get("ai") or {}).get("concurrency") or 1),
            "enable_briefs": bool((cfg.get("ai") or {}).get("enable_briefs", True)),
        },
        "path": str(ai_settings.CONFIG_PATH),
        "secrets_path": str(ai_secrets.SECRETS_PATH),
        "error_log": str(ai_client.ERROR_LOG),
        "model_resolved": resolved.get("model", ""),
    }


def handle_get(path: str, query: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    try:
        if path in ("/api/ai/settings", "/api/ai/status"):
            return _ok(_public())
        if path == "/api/ai/errors":
            return _ok({"errors": ai_client.read_errors(limit=30)})
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
            slot = str(body.get("slot") or "primary")
            if slot not in ("primary", "fallback"):
                slot = "primary"
            result = ai_client.test_connection(cfg, slot)
            return _ok({"result": result, "settings": _public()})
        if path == "/api/ai/clear-key":
            changed = ai_secrets.set_api_key(None)
            return _ok({"cleared": changed, "settings": _public()})
        if path == "/api/ai/usage":
            cfg = ai_settings.load()
            return _ok(ai_client.status(cfg).get("usage") or {})
        if path == "/api/ai/resume":
            ai_usage.resume()
            return _ok({"resumed": True, "settings": _public()})
        return _err(404, "not found")
    except Exception as exc:  # noqa: BLE001
        print("[ai] api POST error:", exc)
        return _err(500, str(exc))


def _coerce(value: Any, kind: str) -> Any:
    if kind == "bool":
        return bool(value)
    if kind == "int":
        return int(value)
    if kind == "float":
        return float(value)
    return str(value or "").strip()


def _save(body: Dict[str, Any]) -> Dict[str, Any]:
    updates: Dict[str, Any] = {"llm": {}, "ai": {}}
    saved: Dict[str, Any] = {}

    incoming_llm = body.get("llm") if isinstance(body.get("llm"), dict) else {}
    for key in _LLM_KEYS:
        if key not in incoming_llm:
            continue
        value = incoming_llm[key]
        try:
            if key == "enabled":
                updates["llm"][key] = _coerce(value, "bool")
            elif key == "provider":
                provider = ai_client.normalize_provider(str(value or ""))
                updates["llm"][key] = provider
            elif key in ("timeout_seconds", "max_attempts"):
                updates["llm"][key] = max(1, min(180, _coerce(value, "int")))
            elif key == "max_tokens":
                updates["llm"][key] = max(50, min(4000, _coerce(value, "int")))
            elif key == "temperature":
                updates["llm"][key] = max(0.0, min(1.5, _coerce(value, "float")))
            else:
                updates["llm"][key] = str(value or "")[:300]
            saved[key] = updates["llm"].get(key)
        except (TypeError, ValueError):
            continue

    incoming_ai = body.get("ai") if isinstance(body.get("ai"), dict) else {}
    for key in _AI_KEYS:
        if key not in incoming_ai:
            continue
        try:
            if key == "enable_briefs":
                updates["ai"][key] = _coerce(incoming_ai[key], "bool")
            elif key == "concurrency":
                updates["ai"][key] = max(1, min(4, _coerce(incoming_ai[key], "int")))
            saved[key] = updates["ai"][key]
        except (TypeError, ValueError):
            continue

    # 备用接口
    incoming_fb = body.get("fallback") if isinstance(body.get("fallback"), dict) else {}
    if incoming_fb:
        fb_updates: Dict[str, Any] = {}
        for key in ("enabled", "provider", "base_url", "model", "api_key_env"):
            if key not in incoming_fb:
                continue
            value = incoming_fb[key]
            if key == "enabled":
                fb_updates[key] = _coerce(value, "bool")
            elif key == "provider":
                fb_updates[key] = ai_client.normalize_provider(str(value or ""))
            else:
                fb_updates[key] = str(value or "")[:300]
            saved["fallback_" + key] = fb_updates[key]
        if fb_updates:
            updates["llm"]["fallback"] = fb_updates

    # token 限额（前端可能放在 body 顶层，也可能嵌在 llm 里，两种都接受）
    incoming_limits = body.get("token_limits")
    if not isinstance(incoming_limits, dict):
        incoming_limits = incoming_llm.get("token_limits")
    if isinstance(incoming_limits, dict) and incoming_limits:
        limits: Dict[str, Any] = {}
        for key in ("primary", "fallback"):
            if key in incoming_limits:
                limits[key] = max(0, _coerce(incoming_limits[key], "int"))
                saved["token_limit_" + key] = limits[key]
        if "window_hours" in incoming_limits:
            limits["window_hours"] = max(1.0, min(168.0, _coerce(incoming_limits["window_hours"], "float")))
            saved["token_window_hours"] = limits["window_hours"]
        if limits:
            updates["llm"]["token_limits"] = limits

    ai_settings.save(updates)

    if body.get("reset_slot"):
        ai_client.reset_slot()
        saved["slot"] = "primary"

    if body.get("clear_api_key"):
        if ai_secrets.set_api_key(None, "primary"):
            saved["api_key"] = "cleared"
    elif str(body.get("api_key") or "").strip():
        if ai_secrets.set_api_key(str(body["api_key"]), "primary"):
            saved["api_key"] = "saved"

    if body.get("clear_fallback_api_key"):
        if ai_secrets.set_api_key(None, "fallback"):
            saved["fallback_api_key"] = "cleared"
    elif str(body.get("fallback_api_key") or "").strip():
        if ai_secrets.set_api_key(str(body["fallback_api_key"]), "fallback"):
            saved["fallback_api_key"] = "saved"

    return {"saved": saved, "settings": _public()}
