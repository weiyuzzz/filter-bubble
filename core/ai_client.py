"""全局大模型客户端。

负责：解析配置、发请求、结构化错误、失败分类与重试、错误日志、用量统计。
所有模块（GitHub 项目解读、热点摘要、学术论文）都通过它调用模型。

支持主接口 + 备用接口：
* 主接口额度用尽（429/402/额度类错误）时自动切到备用接口；
* 切换状态写进 ``data/ai_state.json``，记录「切到哪天为止」；
* 到了第二天自动切回主接口，避免一直用备用的（备用通常更贵或更慢）。
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

from . import ai_secrets, ai_usage
from .config import DATA_DIR

# 三类接口：OpenAI 兼容（含 DeepSeek / OpenRouter / 各类中转）、Anthropic 原生、本地模型
PROVIDERS = ("openai", "anthropic", "local")
PROVIDER_LABELS = {
    "openai": "OpenAI 兼容",
    "anthropic": "Anthropic (Claude)",
    "local": "本地模型",
}
# 旧值兼容：早期版本用的是 openai_compatible / ollama
PROVIDER_ALIASES = {"openai_compatible": "openai", "ollama": "local"}


def normalize_provider(value: str) -> str:
    value = str(value or "").strip()
    value = PROVIDER_ALIASES.get(value, value)
    return value if value in PROVIDERS else "openai"


ANTHROPIC_VERSION = "2023-06-01"
ERROR_LOG = Path(os.environ.get("AI_ERROR_LOG", str(DATA_DIR / "llm-errors.log")))
STATE_PATH = Path(os.environ.get("AI_STATE", str(DATA_DIR / "ai_state.json")))

STATS: Dict[str, Any] = {"calls": 0, "failures": 0, "prompt_tokens": 0,
                         "completion_tokens": 0, "last_error": "", "last_latency_ms": 0}

# 最近一次成功调用实际用的是哪个接口 / 模型（切换后用于归属，避免记成主接口的模型名）
LAST_USED: Dict[str, Any] = {"slot": "", "model": "", "provider": "", "at": ""}


def last_used() -> Dict[str, Any]:
    return dict(LAST_USED)

RETRY_STATUSES = (429, 500, 502, 503, 504)
# 这些错误说明「这个接口暂时用不了」，值得切备用
QUOTA_STATUSES = (402, 429)
QUOTA_PATTERNS = ("rate limit", "quota", "insufficient", "free-models-per-day",
                  "exceeded your", "budget", "credit")
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


# ---------------- 主/备切换状态 ----------------

def _today() -> str:
    return dt.datetime.now().astimezone().date().isoformat()


def _load_state() -> Dict[str, Any]:
    try:
        if STATE_PATH.exists():
            data = json.loads(STATE_PATH.read_text(encoding="utf-8-sig") or "{}")
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_state(state: Dict[str, Any]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print("[ai] state write failed:", exc)


def active_slot() -> str:
    """当前应该优先用哪个接口。切到备用后，到第二天自动回到主接口。"""
    state = _load_state()
    until = str(state.get("fallback_until") or "")
    if until and _today() <= until:
        return "fallback"
    if until and _today() > until:
        # 新的一天：清掉切换标记，回到主接口
        state.pop("fallback_until", None)
        state.pop("fallback_reason", None)
        _save_state(state)
    return "primary"


def switch_to_fallback(reason: str = "") -> None:
    """主接口额度用尽：切到备用，并记录到明天为止。"""
    state = _load_state()
    tomorrow = (dt.datetime.now().astimezone().date() + dt.timedelta(days=1)).isoformat()
    state["fallback_until"] = tomorrow
    state["fallback_reason"] = (reason or "")[:200]
    state["switched_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    _save_state(state)
    print("[ai] 主接口额度用尽，已切换到备用接口（到 %s）" % tomorrow)


def reset_slot() -> None:
    state = _load_state()
    state.pop("fallback_until", None)
    state.pop("fallback_reason", None)
    state.pop("switched_at", None)
    _save_state(state)


def slot_state() -> Dict[str, Any]:
    state = _load_state()
    return {
        "active": active_slot(),
        "fallback_until": state.get("fallback_until") or "",
        "reason": state.get("fallback_reason") or "",
        "switched_at": state.get("switched_at") or "",
    }


def is_quota_error(error: Optional[Dict[str, Any]]) -> bool:
    if not error:
        return False
    status = int(error.get("status") or 0)
    if status in QUOTA_STATUSES:
        return True
    message = (str(error.get("message") or "") + " " + str(error.get("detail") or "")).lower()
    return any(pattern in message for pattern in QUOTA_PATTERNS)


# ---------------- 配置 ----------------

def _llm_block(settings: Dict[str, Any], slot: str = "primary") -> Dict[str, Any]:
    """取出主接口或备用接口的配置；备用继承主接口的通用参数。"""
    llm = settings.get("llm") if isinstance(settings.get("llm"), dict) else settings
    llm = dict(llm or {})
    if slot != "fallback":
        return llm

    fallback = llm.get("fallback") if isinstance(llm.get("fallback"), dict) else {}
    inherited = {k: llm.get(k) for k in ("enabled", "timeout_seconds", "max_attempts",
                                         "temperature", "max_tokens") if k in llm}
    merged = {**inherited, **fallback}
    # 备用是否可用由它自己的 enabled 决定；没写就跟随主开关
    merged["enabled"] = bool(fallback.get("enabled", False)) and bool(llm.get("enabled", True))
    merged.setdefault("api_key_env", ai_secrets.FALLBACK_ENV_NAME)
    return merged


def resolve(settings: Dict[str, Any], slot: str = "primary") -> Tuple[bool, str, Dict[str, Any]]:
    """返回 (是否可用, 原因, 规范化配置)。settings 可以是整个 app_settings 或只有 llm 段。"""
    llm = _llm_block(settings, slot)

    if not llm.get("enabled"):
        reason = "未启用（全局设置 → 接入大模型）" if slot == "primary" else "未配置备用接口"
        return False, reason, llm

    provider = normalize_provider(llm.get("provider") or "openai")

    model = str(llm.get("model") or "").strip()
    if not model:
        return False, "未配置模型名", llm

    base_url = str(llm.get("base_url") or "").strip()
    if not base_url:
        default_port = 11434 if provider == "local" else 1234
        base_url = "http://127.0.0.1:%d" % default_port
    base_url = base_url.rstrip("/")

    env_name = str(llm.get("api_key_env") or
                   (ai_secrets.FALLBACK_ENV_NAME if slot == "fallback" else ai_secrets.ENV_NAME))
    if provider in ("openai", "anthropic"):
        key, _source = ai_secrets.get_api_key(env_name, slot)
        if not key and not base_url.startswith(("http://127.0.0.1", "http://localhost")):
            return False, "未填写 API Key（全局设置 → API Key）", llm

    return True, "", {**llm, "provider": provider, "model": model,
                      "base_url": base_url, "api_key_env": env_name, "slot": slot}


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
    max_tokens = int(llm.get("max_tokens") or 1500)
    base = llm["base_url"]
    key, _source = ai_secrets.get_api_key(
        llm.get("api_key_env") or ai_secrets.ENV_NAME, llm.get("slot") or "primary")

    # ---- 本地模型（Ollama / vLLM 的 /api/chat）----
    if provider == "local":
        url = base + "/api/chat"
        payload: Dict[str, Any] = {"model": llm["model"], "messages": messages, "stream": False,
                                   "options": {"temperature": temperature}}
        if max_tokens > 0:
            payload["options"]["num_predict"] = max_tokens
        if json_mode:
            payload["format"] = "json"
        headers = {"Content-Type": "application/json"}
        if key:  # 有些自建服务需要鉴权
            headers["Authorization"] = "Bearer " + key
        return url, payload, headers

    # ---- Anthropic 原生（Claude）----
    if provider == "anthropic":
        url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
        system_parts = [m["content"] for m in messages if m.get("role") == "system"]
        dialog = [{"role": m["role"], "content": m["content"]}
                  for m in messages if m.get("role") in ("user", "assistant")]
        payload = {"model": llm["model"], "messages": dialog,
                   "max_tokens": max_tokens if max_tokens > 0 else 1500,
                   "temperature": temperature}
        if system_parts:
            payload["system"] = "\n".join(system_parts)
        headers = {"Content-Type": "application/json",
                   "anthropic-version": ANTHROPIC_VERSION}
        if key:
            headers["x-api-key"] = key
        return url, payload, headers

    # ---- OpenAI 兼容（OpenAI / DeepSeek / OpenRouter / 中转）----
    url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
    payload = {"model": llm["model"], "messages": messages, "temperature": temperature}
    if max_tokens > 0:
        payload["max_tokens"] = max_tokens
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    if "openrouter.ai" in base:
        headers["HTTP-Referer"] = "http://127.0.0.1:%s" % os.environ.get("GITPULSE_PORT", "8791")
        headers["X-Title"] = "Filter Bubble"
    return url, payload, headers


def _extract(llm: Dict[str, Any], data: Dict[str, Any]) -> Optional[str]:
    provider = llm["provider"]
    if provider == "local":
        return (data.get("message") or {}).get("content")
    if provider == "anthropic":
        blocks = data.get("content") or []
        texts = [b.get("text") or "" for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(t for t in texts if t) or None
    choices = data.get("choices") or []
    if choices:
        return (choices[0].get("message") or {}).get("content")
    return None


def _usage_of(llm: Dict[str, Any], data: Dict[str, Any]) -> Tuple[int, int]:
    """不同接口的 usage 字段名不一样，统一成 (prompt, completion)。"""
    usage = data.get("usage") or {}
    if llm["provider"] == "anthropic":
        return (int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0))
    if llm["provider"] == "local":
        return (int(usage.get("prompt_eval_count") or 0), int(usage.get("eval_count") or 0))
    return (int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))


def _attempt(llm: Dict[str, Any], messages: List[Dict[str, str]],
             json_mode: bool) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """针对单个接口的一次完整调用（含内部重试与截断处理）。"""
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
            prompt_tokens, completion_tokens = _usage_of(llm, data)
            STATS["calls"] += 1
            STATS["prompt_tokens"] += prompt_tokens
            STATS["completion_tokens"] += completion_tokens

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
                          % (finish, completion_tokens, reasoning_tokens),
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

    return None, last_error


def _token_limits(settings: Dict[str, Any]) -> Dict[str, Any]:
    """读取 token 限额配置。0 表示不限。"""
    llm = settings.get("llm") if isinstance(settings.get("llm"), dict) else settings
    raw = (llm or {}).get("token_limits") or {}
    return {
        "primary": int(raw.get("primary") or 0),
        "fallback": int(raw.get("fallback") or 0),
        "window_hours": float(raw.get("window_hours") or 24),
    }


def chat_result_full(messages: List[Dict[str, str]], settings: Dict[str, Any],
                     json_mode: bool = True):
    """调用模型，返回 (文本, 错误, 实际使用的接口信息)。

    错误是结构化 dict，绝不抛异常；第三个值是 {slot, model, provider}，
    主备切换后调用方靠它正确归属缓存与展示。
    """
    limits = _token_limits(settings)

    until = ai_usage.pause_until()
    if until:
        return None, {
            "status": 0, "code": "paused",
            "message": "AI 已暂停：主接口与备用接口的 token 额度都用完了",
            "detail": "paused_until=%s" % until,
            "hint": "%s 之后自动恢复并重新计数" % until,
            "suggested_model": "", "retryable": False,
        }, {}

    order = ["primary", "fallback"] if active_slot() == "primary" else ["fallback", "primary"]

    plans: List[Tuple[str, Dict[str, Any]]] = []
    first_reason = ""
    for slot in order:
        ok, reason, llm = resolve(settings, slot)
        if not ok:
            first_reason = first_reason or reason
            continue
        if ai_usage.exhausted(slot, limits.get(slot) or 0):
            first_reason = first_reason or ("%s 接口 token 额度已用尽（本地记账）" % slot)
            print("[ai] %s 接口 token 额度已用尽，跳过" % slot)
            continue
        plans.append((slot, llm))

    if not plans:
        until = ai_usage.pause(limits.get("window_hours") or 24)
        return None, {
            "status": 0, "code": "paused",
            "message": "主接口与备用接口的 token 额度都用完了，AI 先暂停",
            "detail": "paused_until=%s" % until,
            "hint": "%s 之后自动恢复并重新计数；也可以调高 token_limits 或换接口" % until,
            "suggested_model": "", "retryable": False,
        }, {}

    first_error: Optional[Dict[str, Any]] = None
    for index, (slot, llm) in enumerate(plans):
        before = (STATS["prompt_tokens"], STATS["completion_tokens"], STATS["calls"])
        text, error = _attempt(llm, messages, json_mode)
        ai_usage.record(
            slot,
            STATS["prompt_tokens"] - before[0],
            STATS["completion_tokens"] - before[1],
            STATS["calls"] - before[2],
        )
        if text:
            used = {"slot": slot, "model": llm.get("model", ""),
                    "provider": llm.get("provider", ""),
                    "at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
            LAST_USED.update(used)
            return text, None, used
        if not first_error:
            first_error = error

        # 主接口额度类错误 → 切到备用（如果还有备用可用）
        if slot == "primary" and is_quota_error(error):
            if any(s == "fallback" for s, _c in plans[index + 1:]):
                switch_to_fallback((error or {}).get("message", ""))
                continue
            fb_ok, _why, _cfg = resolve(settings, "fallback")
            if fb_ok:
                switch_to_fallback((error or {}).get("message", ""))
                continue

    # 走完全部可用接口仍失败：如果是额度问题且备用也耗尽，就暂停
    exhausted_all = all(ai_usage.exhausted(slot, limits.get(slot) or 0)
                        for slot, _llm in plans) if plans else False
    if is_quota_error(first_error) and exhausted_all:
        ai_usage.pause(limits.get("window_hours") or 24)

    STATS["failures"] += 1
    STATS["last_error"] = (first_error or {}).get("message", "")[:300] or first_reason[:300]
    _log_error(int((first_error or {}).get("status") or 0),
               (first_error or {}).get("model", "") or "",
               (first_error or {}).get("message", "") or first_reason,
               (first_error or {}).get("detail", ""))
    print("[ai] call failed:", STATS["last_error"][:160])
    return None, first_error, {}


def chat_result(messages: List[Dict[str, str]], settings: Dict[str, Any],
                json_mode: bool = True) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """调用模型，返回 (文本, 错误)。"""
    text, error, _used = chat_result_full(messages, settings, json_mode)
    return text, error


def chat_result_ex(messages: List[Dict[str, str]], settings: Dict[str, Any],
                   json_mode: bool = True) -> Tuple[Optional[str], Optional[Dict[str, Any]], Dict[str, Any]]:
    """和 chat_result 一样，但额外返回「实际服务这次调用的接口与模型」。

    主备切换后，调用方需要用它来正确归属（缓存键、界面展示的模型名）。
    """
    return chat_result_full(messages, settings, json_mode)


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


def test_connection(settings: Dict[str, Any], slot: str = "primary") -> Dict[str, Any]:
    """测指定接口（primary / fallback）。不触发自动切换，避免测试改变运行状态。"""
    ok, reason, llm = resolve(settings, slot)
    if not ok:
        return {"ok": False, "reason": reason, "provider": llm.get("provider", ""),
                "model": llm.get("model", ""), "latency_ms": 0, "error": None, "slot": slot}

    started = time.time()
    text, error = _attempt(llm, TEST_MESSAGES, True)
    latency = int((time.time() - started) * 1000)

    if text is None:
        return {
            "ok": False,
            "slot": slot,
            "provider": llm["provider"],
            "model": llm["model"],
            "base_url": llm["base_url"],
            "latency_ms": latency,
            "reason": (error or {}).get("message", "模型没有返回内容"),
            "hint": (error or {}).get("hint", ""),
            "quota": is_quota_error(error),
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
        "ok": True, "responded": True, "protocol_ok": protocol_ok, "slot": slot,
        "provider": llm["provider"], "model": llm["model"], "base_url": llm["base_url"],
        "latency_ms": latency, "sample": sample, "error": None, "hint": "",
        "note": "" if protocol_ok else "模型有返回，但没有按要求的 JSON 格式回复（仍可用于摘要，只是结构化程度较低）",
    }


def status(settings: Dict[str, Any]) -> Dict[str, Any]:
    ok, reason, llm = resolve(settings)
    active = active_slot()
    fb_ok, fb_reason, fb = resolve(settings, "fallback")
    return {
        "available": ok,
        "reason": reason,
        "active_slot": active,
        "switch": slot_state(),
        "fallback": {
            "configured": bool(_llm_block(settings, "fallback").get("enabled")),
            "available": fb_ok,
            "reason": fb_reason,
            "provider": fb.get("provider", ""),
            "model": fb.get("model", ""),
            "base_url": fb.get("base_url", ""),
            "key": ai_secrets.public_state(
                str(fb.get("api_key_env") or ai_secrets.FALLBACK_ENV_NAME), "fallback"),
        },
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
        "token_limits": _token_limits(settings),
        "usage": ai_usage.summary(_token_limits(settings),
                                  float((_token_limits(settings).get("window_hours") or 24))),
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
