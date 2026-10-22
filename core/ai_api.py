"""全局设置 API：大模型配置、连接测试、错误日志。

与具体业务模块无关，GitHub 项目解读与热点摘要共用。
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

from . import ai_client, ai_secrets, ai_settings

_LLM_KEYS = ("enabled", "provider", "base_url", "model", "api_key_env",
             "timeout_seconds", "max_attempts", "temperature", "max_tokens")
_AI_KEYS = ("briefs_per_run", "concurrency", "enable_briefs")


def _ok(payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    return 200, payload


def _err(status: int, message: str) -> Tuple[int, Dict[str, Any]]:
    return status, {"error": message}


def _public() -> Dict[str, Any]:
    cfg = ai_settings.load()
    llm = cfg.get("llm") or {}
    ok, reason, resolved = ai_client.resolve(cfg)
    return {
        "llm": {
            "enabled": bool(llm.get("enabled")),
            "provider": llm.get("provider") or "openai_compatible",
            "base_url": llm.get("base_url") or "",
            "model": llm.get("model") or "",
            "api_key_env": llm.get("api_key_env") or ai_secrets.ENV_NAME,
            "timeout_seconds": llm.get("timeout_seconds"),
            "providers": list(ai_client.PROVIDERS),
            "available": ok,
            "unavailable_reason": reason,
            "key": ai_secrets.public_state(str(llm.get("api_key_env") or ai_secrets.ENV_NAME)),
            "stats": {k: ai_client.STATS.get(k) for k in
                      ("calls", "failures", "prompt_tokens", "completion_tokens", "last_error")},
        },
        "ai": {
            "briefs_per_run": int((cfg.get("ai") or {}).get("briefs_per_run") or 12),
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
            result = ai_client.test_connection(cfg)
            return _ok({"result": result, "settings": _public()})
        if path == "/api/ai/clear-key":
            changed = ai_secrets.set_api_key(None)
            return _ok({"cleared": changed, "settings": _public()})
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
                provider = str(value or "")
                if provider in ai_client.PROVIDERS:
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
            elif key == "briefs_per_run":
                updates["ai"][key] = max(0, min(30, _coerce(incoming_ai[key], "int")))
            elif key == "concurrency":
                updates["ai"][key] = max(1, min(3, _coerce(incoming_ai[key], "int")))
            saved[key] = updates["ai"][key]
        except (TypeError, ValueError):
            continue

    ai_settings.save(updates)

    if body.get("clear_api_key"):
        if ai_secrets.set_api_key(None):
            saved["api_key"] = "cleared"
    elif str(body.get("api_key") or "").strip():
        if ai_secrets.set_api_key(str(body["api_key"])):
            saved["api_key"] = "saved"

    return {"saved": saved, "settings": _public()}
