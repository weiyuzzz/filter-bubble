"""全局大模型客户端。

负责：解析配置、发请求、结构化错误、失败分类与重试、错误日志、用量统计。
两个模块（GitHub 项目解读、热点摘要）以及后续新功能都通过它调用模型。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import ai_secrets
from .config import DATA_DIR

PROVIDERS = ("openai_compatible", "ollama")
ERROR_LOG = Path(os.environ.get("AI_ERROR_LOG", str(DATA_DIR / "llm-errors.log")))

STATS: Dict[str, Any] = {"calls": 0, "failures": 0, "prompt_tokens": 0,
                         "completion_tokens": 0, "last_error": "", "last_latency_ms": 0}

RETRY_STATUSES = (429, 500, 502, 503, 504)
HINTS = {
    401: "API Key 无效或已过期，请在全局设置里重新填写。",
    403: "这个 Key 没有访问该模型的权限，或模型需要付费额度。",
    402: "账户额度不足，请检查 OpenRouter 的额度或换用免费模型。",
    404: "模型不存在或已下线，请检查模型名（OpenRouter 的 slug 形如 google/gemma-4-26b-a4b-it:free）。",
    429: "被上游限流了。免费模型共用上游池，通常等 1–2 分钟再试；也可以换一个模型。",
    500: "模型服务端出错，稍后重试。",
    502: "网关错误，稍后重试。",
    503: "模型服务暂时不可用，稍后重试。",
}


# ---------------- 配置 ----------------

def resolve(settings: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    """返回 (是否可用, 原因, 规范化配置)。settings 可以是整个 app_settings 或只有 llm 段。"""
    llm = settings.get("llm") if isinstance(settings.get("llm"), dict) else settings
    llm = llm or {}

    if not llm.get("enabled"):
        return False, "未启用（全局设置 → 接入大模型）", llm

    provider = str(llm.get("provider") or "openai_compatible")
    if provider not in PROVIDERS:
        return False, "不支持的接口类型：%s" % provider, llm

    model = str(llm.get("model") or "").strip()
    if not model:
        return False, "未配置模型名", llm

    base_url = str(llm.get("base_url") or "").strip()
    if not base_url:
        default_port = 11434 if provider == "ollama" else 1234
        base_url = "http://127.0.0.1:%d" % default_port
    base_url = base_url.rstrip("/")

    env_name = str(llm.get("api_key_env") or ai_secrets.ENV_NAME)
    if provider == "openai_compatible":
        key, _source = ai_secrets.get_api_key(env_name)
        if not key and not base_url.startswith(("http://127.0.0.1", "http://localhost")):
            return False, "未填写 API Key（全局设置 → API Key）", llm

    return True, "", {**llm, "provider": provider, "model": model,
                      "base_url": base_url, "api_key_env": env_name}


# ---------------- 错误处理 ----------------

def _log_error(status: int, model: str, message: str, raw: str = "") -> None:
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        line = "%s\tstatus=%s\tmodel=%s\t%s\t%s\n" % (
            dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            status, model, message.replace("\n", " ")[:200], (raw or "").replace("\n", " ")[:300])
        with ERROR_LOG.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except Exception:  # noqa: BLE001 - 日志失败不能影响主流程
        pass


def _classify(status: int, body: str, model: str) -> Dict[str, Any]:
    """把上游错误变成人能看懂的结构。"""
    message = ""
    raw = ""
    remedy = ""
    slug_hint = ""
    limit_source = ""
    try:
        payload = json.loads(body)
        error = payload.get("error") or {}
        message = str(error.get("message") or "")
        meta = error.get("metadata") or {}
        raw = str(meta.get("raw") or "")
        remedy = str(meta.get("remedy_hint") or "")
        limit_source = str(meta.get("limit_source") or "")
        slug_hint = str(error.get("metadata", {}).get("suggested_model") or "")
    except Exception:  # noqa: BLE001
        message = (body or "").strip()[:300]

    hint = HINTS.get(status, "")
    if status == 404:
        match = re.search(r"use this slug instead:\s*([\w./:\-]+)", message)
        if match:
            slug_hint = match.group(1)
            hint = "该模型没有免费额度。可以直接改用付费 slug：%s" % slug_hint
    if status == 429:
        if limit_source == "upstream_provider_shared_pool":
            hint = ("这个免费模型的上游共享池被限流了（不是你 key 的问题）。"
                    "等 1–2 分钟重试，或换用其他模型。")
        elif remedy:
            hint = remedy[:240]

    return {
        "status": status,
        "message": message or ("HTTP %s" % status),
        "detail": raw[:600],
        "hint": hint,
        "suggested_model": slug_hint,
        "retryable": status in RETRY_STATUSES,
    }


# ---------------- 请求 ----------------

def _post(url: str, payload: Dict[str, Any], headers: Dict[str, str],
          timeout: float) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace")), None
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        return None, _classify(exc.code, body, str(payload.get("model") or ""))
    except Exception as exc:  # noqa: BLE001
        return None, {
            "status": 0,
            "message": "%s: %s" % (type(exc).__name__, exc),
            "detail": "",
            "hint": "无法连接到接口地址，请检查网络、代理或 base_url 是否正确。",
            "suggested_model": "",
            "retryable": False,
        }


def _build_request(llm: Dict[str, Any], messages: List[Dict[str, str]],
                   json_mode: bool) -> Tuple[str, Dict[str, Any], Dict[str, str]]:
    provider = llm["provider"]
    temperature = float(llm.get("temperature", 0.2))
    if provider == "ollama":
        url = llm["base_url"] + "/api/chat"
        payload: Dict[str, Any] = {"model": llm["model"], "messages": messages, "stream": False,
                                   "options": {"temperature": temperature}}
        max_tokens = int(llm.get("max_tokens") or 1500)
        if max_tokens > 0:
            payload["options"]["num_predict"] = max_tokens
        if json_mode:
            payload["format"] = "json"
        return url, payload, {"Content-Type": "application/json"}

    base = llm["base_url"]
    url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
    payload = {"model": llm["model"], "messages": messages, "temperature": temperature}
    # 免费模型常常话很多，不限制长度容易把 JSON 截断；默认给 800 输出上限
    max_tokens = int(llm.get("max_tokens") or 1500)
    if max_tokens > 0:
        payload["max_tokens"] = max_tokens
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    key, _source = ai_secrets.get_api_key(llm.get("api_key_env") or ai_secrets.ENV_NAME)
    if key:
        headers["Authorization"] = "Bearer " + key
    if "openrouter.ai" in base:
        headers["HTTP-Referer"] = "http://127.0.0.1:%s" % os.environ.get("GITPULSE_PORT", "8791")
        headers["X-Title"] = "Filter Bubble"
    return url, payload, headers


def _extract(llm: Dict[str, Any], data: Dict[str, Any]) -> Optional[str]:
    if llm["provider"] == "ollama":
        return (data.get("message") or {}).get("content")
    choices = data.get("choices") or []
    if choices:
        return (choices[0].get("message") or {}).get("content")
    return None


def chat_result(messages: List[Dict[str, str]], settings: Dict[str, Any],
                json_mode: bool = True) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """调用模型，返回 (文本, 错误)。错误是结构化 dict，绝不抛异常。"""
    ok, reason, llm = resolve(settings)
    if not ok:
        return None, {"status": 0, "message": reason, "detail": "",
                      "hint": reason, "suggested_model": "", "retryable": False}

    url, payload, headers = _build_request(llm, messages, json_mode)
    timeout = float(llm.get("timeout_seconds") or 60)
    attempts = max(1, min(3, int(llm.get("max_attempts") or 2)))

    last_error: Optional[Dict[str, Any]] = None
    bumped = False
    for attempt in range(attempts + 1):
        started = time.time()
        data, error = _post(url, payload, headers, timeout)
        STATS["last_latency_ms"] = int((time.time() - started) * 1000)

        if data:
            text = _extract(llm, data)
            usage = data.get("usage") or {}
            STATS["calls"] += 1
            STATS["prompt_tokens"] += int(usage.get("prompt_tokens") or usage.get("prompt_eval_count") or 0)
            STATS["completion_tokens"] += int(
                usage.get("completion_tokens") or usage.get("eval_count") or 0)

            if text and text.strip():
                STATS["last_error"] = ""
                return text, None

            # 请求成功但正文为空：常见于推理模型把 token 全花在 reasoning 上
            choice = (data.get("choices") or [{}])[0]
            finish = choice.get("finish_reason")
            reasoning_tokens = ((usage.get("completion_tokens_details") or {})
                                .get("reasoning_tokens") or 0)
            truncated = finish == "length"
            message = ("模型输出被长度上限截断，正文为空" if truncated else "模型返回了空内容")
            last_error = {
                "status": 0,
                "code": "truncated" if truncated else "empty",
                "message": message,
                "detail": "finish_reason=%s, completion_tokens=%s, reasoning_tokens=%s"
                          % (finish, usage.get("completion_tokens"), reasoning_tokens),
                "hint": ("这个模型很可能是推理模型，思考过程吃掉了输出预算。"
                         "提高「最大输出 token」后重试，或换用非推理模型。"
                         if reasoning_tokens or truncated else "模型没有返回正文，稍后重试。"),
                "suggested_model": "",
                "retryable": truncated,
            }

            # 被截断：自动把输出上限放大 3 倍重试一次
            current_max = int(payload.get("max_tokens") or 0)
            if truncated and not bumped and current_max:
                bigger = min(4000, current_max * 3)
                payload["max_tokens"] = bigger
                if llm["provider"] == "ollama":
                    payload.setdefault("options", {})["num_predict"] = bigger
                bumped = True
                continue
        else:
            last_error = error or {"status": 0, "message": "未知错误", "detail": "",
                                   "hint": "", "suggested_model": "", "retryable": False}

            # 有些模型不支持 response_format：去掉后重试一次
            if last_error.get("status") == 400 and "response_format" in payload:
                payload.pop("response_format", None)
                continue

        if not last_error.get("retryable") or attempt + 1 >= attempts + 1:
            break
        time.sleep(min(6.0, 1.5 * (attempt + 1)))

    STATS["failures"] += 1
    STATS["last_error"] = (last_error or {}).get("message", "")[:300]
    _log_error(int((last_error or {}).get("status") or 0), llm["model"],
               (last_error or {}).get("message", ""), (last_error or {}).get("detail", ""))
    print("[ai] call failed:", STATS["last_error"][:160])
    return None, last_error


def chat(messages: List[Dict[str, str]], settings: Dict[str, Any],
         json_mode: bool = True) -> Optional[str]:
    """兼容旧调用：只要文本。"""
    text, _error = chat_result(messages, settings, json_mode)
    return text


# ---------------- 测试与状态 ----------------

TEST_MESSAGES = [
    {"role": "system", "content": "你是一个连通性测试端点。只回复 JSON。"},
    {"role": "user", "content": '请只输出 JSON：{"ok": true, "echo": "pong"}'},
]


def test_connection(settings: Dict[str, Any]) -> Dict[str, Any]:
    ok, reason, llm = resolve(settings)
    if not ok:
        return {"ok": False, "reason": reason, "provider": llm.get("provider", ""),
                "model": llm.get("model", ""), "latency_ms": 0, "error": None}

    started = time.time()
    text, error = chat_result(TEST_MESSAGES, settings, json_mode=True)
    latency = int((time.time() - started) * 1000)

    if text is None:
        return {
            "ok": False,
            "provider": llm["provider"],
            "model": llm["model"],
            "base_url": llm["base_url"],
            "latency_ms": latency,
            "reason": (error or {}).get("message", "模型没有返回内容"),
            "hint": (error or {}).get("hint", ""),
            "error": error,
            "log_path": str(ERROR_LOG),
        }

    sample = text.strip().replace("\n", " ")[:300]
    parsed = None
    try:
        parsed = json.loads(sample)
    except ValueError:
        parsed = None
    protocol_ok = bool(parsed and parsed.get("ok") is True) or "pong" in sample.lower()
    return {
        "ok": True, "responded": True, "protocol_ok": protocol_ok,
        "provider": llm["provider"], "model": llm["model"], "base_url": llm["base_url"],
        "latency_ms": latency, "sample": sample, "error": None, "hint": "",
        "note": "" if protocol_ok else "模型有返回，但没有按要求的 JSON 格式回复（仍可用于摘要，只是结构化程度较低）",
    }


def status(settings: Dict[str, Any]) -> Dict[str, Any]:
    ok, reason, llm = resolve(settings)
    return {
        "available": ok,
        "reason": reason,
        "provider": llm.get("provider", ""),
        "model": llm.get("model", ""),
        "base_url": llm.get("base_url", ""),
        "calls": STATS["calls"],
        "failures": STATS["failures"],
        "prompt_tokens": STATS["prompt_tokens"],
        "completion_tokens": STATS["completion_tokens"],
        "last_error": STATS["last_error"],
        "last_latency_ms": STATS["last_latency_ms"],
        "checked_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "key": ai_secrets.public_state(str((llm or {}).get("api_key_env") or ai_secrets.ENV_NAME)),
        "log_path": str(ERROR_LOG),
    }


def read_errors(limit: int = 20) -> List[str]:
    """读取最近的错误日志，供界面展示。"""
    try:
        if not ERROR_LOG.exists():
            return []
        lines = ERROR_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        return [line for line in lines if line.strip()][-limit:][::-1]
    except Exception:  # noqa: BLE001
        return []


def extract_json(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """从模型输出里抠出 JSON：容忍 ```json 围栏、前后废话、被截断的尾部。"""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned
        cleaned = cleaned.rstrip("`").strip()
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else None
    except ValueError:
        pass
    start = cleaned.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(cleaned[start:index + 1])
                    return data if isinstance(data, dict) else None
                except ValueError:
                    return None
    return None
