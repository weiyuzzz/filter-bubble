"""全局大模型客户端。

负责：解析配置、发请求、结构化错误、失败分类与重试、接口健康与冷却、错误日志、用量统计。
所有模块（GitHub 项目解读、热点摘要、学术论文、全球雷达）都通过它调用模型。

接口模型（v2）：
* ``app_settings.json → llm.slots`` 是有序接口列表（1..N 个），按 ``priority`` 依次尝试；
* 每个接口有自己的服务商预设、协议、地址、模型、Key 归属（``key_ref``）和额度规则；
* 失败分类决定后续行为：
  - ``config``（401/403/404 等配置问题）→ 该接口冷却一段时间，不再每次白跑一条请求；
  - ``quota``（402 / 额度用尽 / 欠费 / 套餐窗口用满）→ 该接口按额度窗口冷却，换下一个接口；
  - ``rate_limit``（RPM/TPM 限流）→ 只是瞬时限流，本次换下一个接口，不做长时间冷却；
* 所有接口额度都用尽 → 暂停 AI，窗口结束后自动恢复并重新计数。
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

from . import ai_secrets, ai_settings, ai_usage
from .config import DATA_DIR

# 三类协议：OpenAI 兼容（DeepSeek / 千帆 / OpenRouter / 各类中转）、Anthropic 原生、本地模型
PROVIDERS = ("openai", "anthropic", "local")
PROVIDER_LABELS = {
    "openai": "OpenAI 兼容",
    "anthropic": "Anthropic (Claude)",
    "local": "本地模型",
}
# 旧值兼容：早期版本用的是 openai_compatible / ollama
PROVIDER_ALIASES = {"openai_compatible": "openai", "ollama": "local"}

# 服务商预设：选预设 → 带出协议、地址、Key 归属、额度习惯与常见模型
PRESETS: Dict[str, Dict[str, Any]] = {
    "deepseek": {
        "label": "DeepSeek 官方",
        "protocol": "openai",
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "key_hints": ["sk-"],
        "key_ref": "deepseek",
        "quota": {"mode": "daily", "limit": 0, "window_hours": 24},
        "note": "官方按量计费，地址支持 /v1 前缀，直接填域名即可。",
    },
    "qianfan": {
        "label": "百度千帆 Token Plan（个人版）",
        "protocol": "openai",
        "base_url": "https://qianfan.baidubce.com/v2/tokenplan/personal",
        "models": ["deepseek-v4.1-flash", "deepseek-v4.1-flash-1017", "deepseek-v4-pro",
                   "deepseek-v4-flash-0731", "glm-5.3", "glm-5.2", "glm-5.3-flash"],
        "key_hints": ["bce-v3/"],
        "key_ref": "qianfan",
        "quota": {"mode": "window", "limit": 0, "window_hours": 5},
        "note": ("Token Plan 专属 Key（形如 bce-v3/ALTAK.../...），地址带 /v2/tokenplan/personal，"
                 "请求头自动带 Bearer。套餐是多模型共享 Token 池，额度模式建议「窗口（5 小时）」"
                 "或「总量」；个人版只支持 GLM 与 DeepSeek 系列，ERNIE / Qwen 会返回 "
                 "token_plan_person_model_not_supported。"),
    },
    "qianfan_pay": {
        "label": "百度千帆（按量计费 v2）",
        "protocol": "openai",
        "base_url": "https://qianfan.baidubce.com/v2",
        "models": ["ernie-5.1", "ernie-4.5-turbo-128k", "deepseek-v4-pro", "glm-5.3"],
        "key_hints": ["bce-v3/"],
        "key_ref": "qianfan_pay",
        "quota": {"mode": "daily", "limit": 0, "window_hours": 24},
        "note": "按量计费的 API Key，和 Token Plan 专属 Key 是两套独立体系，互不通用。",
    },
    "openrouter": {
        "label": "OpenRouter",
        "protocol": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "models": [],
        "key_hints": ["sk-or-"],
        "key_ref": "openrouter",
        "quota": {"mode": "daily", "limit": 0, "window_hours": 24},
        "note": "免费模型按日限额，额度用尽会返回 429/free-models-per-day。",
    },
    "dashscope": {
        "label": "阿里云百炼（通义千问）",
        "protocol": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": [],
        "key_hints": ["sk-"],
        "key_ref": "dashscope",
        "quota": {"mode": "daily", "limit": 0, "window_hours": 24},
        "note": "OpenAI 兼容模式地址带 /compatible-mode/v1。",
    },
    "moonshot": {
        "label": "Moonshot (Kimi)",
        "protocol": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "models": [],
        "key_hints": ["sk-"],
        "key_ref": "moonshot",
        "quota": {"mode": "daily", "limit": 0, "window_hours": 24},
        "note": "",
    },
    "zhipu": {
        "label": "智谱 GLM",
        "protocol": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": [],
        "key_hints": [],
        "key_ref": "zhipu",
        "quota": {"mode": "daily", "limit": 0, "window_hours": 24},
        "note": "地址以 /v4 结尾，客户端会自动补 /chat/completions。",
    },
    "ark": {
        "label": "火山方舟（豆包）",
        "protocol": "openai",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": [],
        "key_hints": [],
        "key_ref": "ark",
        "quota": {"mode": "daily", "limit": 0, "window_hours": 24},
        "note": "地址以 /v3 结尾；模型名通常填接入点 ID。",
    },
    "siliconflow": {
        "label": "硅基流动",
        "protocol": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": [],
        "key_hints": ["sk-"],
        "key_ref": "siliconflow",
        "quota": {"mode": "daily", "limit": 0, "window_hours": 24},
        "note": "",
    },
    "openai": {
        "label": "OpenAI 官方",
        "protocol": "openai",
        "base_url": "https://api.openai.com",
        "models": [],
        "key_hints": ["sk-"],
        "key_ref": "openai",
        "quota": {"mode": "daily", "limit": 0, "window_hours": 24},
        "note": "",
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "protocol": "anthropic",
        "base_url": "https://api.anthropic.com",
        "models": ["claude-sonnet-4-20250514"],
        "key_hints": ["sk-ant-"],
        "key_ref": "anthropic",
        "quota": {"mode": "daily", "limit": 0, "window_hours": 24},
        "note": "走原生 /v1/messages 协议，不使用 OpenAI 的 response_format。",
    },
    "local": {
        "label": "本地模型（Ollama / vLLM）",
        "protocol": "local",
        "base_url": "http://127.0.0.1:11434",
        "models": ["qwen2.5:7b"],
        "key_hints": [],
        "key_ref": "local",
        "quota": {"mode": "daily", "limit": 0, "window_hours": 24},
        "note": "本地地址不需要 Key；服务在别的机器上时改成对应 IP。",
    },
}


def preset_list() -> List[Dict[str, Any]]:
    out = []
    for preset_id, item in PRESETS.items():
        entry = {"id": preset_id}
        entry.update(item)
        out.append(entry)
    return out


def normalize_provider(value: str) -> str:
    value = str(value or "").strip()
    value = PROVIDER_ALIASES.get(value, value)
    return value if value in PROVIDERS else "openai"


ANTHROPIC_VERSION = "2023-06-01"
# 地址里已经带了版本段（/v1、/v2/tokenplan/personal 这类）就不再额外补 /v1
_VERSION_IN_PATH = re.compile(r"/v[0-9]+[a-z]*(/|$)", re.I)
ERROR_LOG = Path(os.environ.get("AI_ERROR_LOG", str(DATA_DIR / "llm-errors.log")))
STATE_PATH = Path(os.environ.get("AI_STATE", str(DATA_DIR / "ai_state.json")))

STATS: Dict[str, Any] = {"calls": 0, "failures": 0, "prompt_tokens": 0,
                         "completion_tokens": 0, "last_error": "", "last_latency_ms": 0}

# 最近一次成功调用实际用的是哪个接口 / 模型（切换后用于归属，避免记成主接口的模型名）
LAST_USED: Dict[str, Any] = {"slot": "", "name": "", "model": "", "provider": "", "at": ""}


def last_used() -> Dict[str, Any]:
    return dict(LAST_USED)


RETRY_STATUSES = (429, 500, 502, 503, 504)
# 这些错误说明「这个接口暂时用不了」，值得换下一个接口
QUOTA_STATUSES = (402, 429)
QUOTA_PATTERNS = ("quota", "insufficient", "free-models-per-day", "exceeded your", "budget",
                  "credit", "balance", "overdue", "欠费", "额度", "余额不足", "用尽")
RATE_PATTERNS = ("rate limit", "rate_limit", "rpm", "tpm", "too many requests",
                 "requests per", "限流", "频率过高", "请求过于频繁")
# 内容安全类的 403 是「这一次请求」的问题，不代表接口配置坏了
PER_REQUEST_403 = ("unsafe", "content", "敏感", "审核", "policy")
HINTS = {
    401: "API Key 无效或已过期，请在全局设置里重新填写；Key 与服务商一一对应，换地址要一起换。",
    403: "这个 Key 没有访问该模型的权限、账号欠费，或本次内容被安全策略拦截。",
    402: "账户额度不足（欠费或套餐用尽），请充值/续费，或换用其它接口。",
    404: "模型不存在或已下线，请检查模型名（OpenRouter 的 slug 形如 google/gemma-4-26b-a4b-it:free）。",
    429: "被上游限流了。RPM/TPM 限流等 1–2 分钟再试；额度类限制要等窗口刷新或换接口。",
    500: "模型服务端出错，稍后重试。",
    502: "网关错误，稍后重试。",
    503: "模型服务暂时不可用，稍后重试。",
}

# 接口配置类错误：冷却久一点，避免每次调用都白跑一条请求
CONFIG_COOLDOWN_SECONDS = int(os.environ.get("AI_CONFIG_COOLDOWN_SECONDS", "1800") or 1800)
QUOTA_COOLDOWN_FALLBACK_HOURS = 6.0


# ---------------- 接口偏好与健康状态 ----------------

def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _today() -> str:
    return _now().date().isoformat()


def _parse_time(value: Any) -> Optional[dt.datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            return dt.datetime.fromisoformat(text + "T23:59:59").astimezone()
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.astimezone()
    except ValueError:
        return None


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
    """当前优先使用哪个接口。切换后按「到某个时刻为止」计时，到期自动回到主接口。"""
    state = _load_state()
    # 兼容旧字段：fallback_until 是老的切换标记，优先读它
    until = _parse_time(state.get("fallback_until") or state.get("preferred_until"))
    if until is None:
        return "primary"
    if _now() < until:
        return str(state.get("preferred") or "fallback")
    state.pop("preferred_until", None)
    state.pop("fallback_until", None)
    state.pop("preferred", None)
    state.pop("fallback_reason", None)
    _save_state(state)
    return "primary"


def switch_to_fallback(reason: str = "", hours: float = 12.0) -> None:
    """主接口额度用尽：优先用备用接口若干小时（到期自动回到主接口）。"""
    state = _load_state()
    until = (_now() + dt.timedelta(hours=float(hours or 12))).isoformat(timespec="seconds")
    state["preferred"] = "fallback"
    state["preferred_until"] = until
    # 兼容旧字段名
    state["fallback_until"] = until
    state["fallback_reason"] = (reason or "")[:200]
    state["switched_at"] = _now().isoformat(timespec="seconds")
    _save_state(state)
    print("[ai] 主接口不可用，已优先切换到备用接口（到 %s）" % until)


def reset_slot() -> None:
    state = _load_state()
    for key in ("preferred", "preferred_until", "fallback_until", "fallback_reason", "switched_at"):
        state.pop(key, None)
    _save_state(state)


def slot_state() -> Dict[str, Any]:
    state = _load_state()
    until = _parse_time(state.get("fallback_until") or state.get("preferred_until"))
    return {
        "active": active_slot(),
        "fallback_until": str(state.get("fallback_until") or state.get("preferred_until") or "")
                          if until and _now() < until else "",
        "reason": state.get("fallback_reason") or "",
        "switched_at": state.get("switched_at") or "",
        "slots": state.get("slots") or {},
    }


def slot_health(slot_id: str) -> Dict[str, Any]:
    slot_id = str(slot_id or "")
    entry = (_load_state().get("slots") or {}).get(slot_id) or {}
    until = _parse_time(entry.get("cooldown_until"))
    blocked = bool(until and _now() < until)
    return {
        "state": str(entry.get("state") or "unknown"),
        "reason": str(entry.get("reason") or ""),
        "last_error": str(entry.get("last_error") or ""),
        "last_error_at": str(entry.get("last_error_at") or ""),
        "last_ok_at": str(entry.get("last_ok_at") or ""),
        "fail_streak": int(entry.get("fail_streak") or 0),
        "ok_calls": int(entry.get("ok_calls") or 0),
        "cooldown_until": str(entry.get("cooldown_until") or ""),
        "blocked": blocked,
    }


def slot_blocked(slot_id: str) -> bool:
    return bool(slot_health(slot_id).get("blocked"))


def mark_slot_ok(slot_id: str, model: str = "") -> None:
    slot_id = str(slot_id or "")
    if not slot_id:
        return
    state = _load_state()
    slots = state.setdefault("slots", {})
    entry = dict(slots.get(slot_id) or {})
    entry.update({"state": "ok", "last_ok_at": _now().isoformat(timespec="seconds"),
                  "fail_streak": 0, "cooldown_until": "", "reason": ""})
    entry["ok_calls"] = int(entry.get("ok_calls") or 0) + 1
    if model:
        entry["last_ok_model"] = str(model)[:120]
    slots[slot_id] = entry
    _save_state(state)


def mark_slot_error(slot_id: str, error: Optional[Dict[str, Any]],
                    quota: Optional[Dict[str, Any]] = None) -> None:
    """记录失败，并按错误类型决定冷却时长。"""
    slot_id = str(slot_id or "")
    if not slot_id:
        return
    kind = error_kind(error)
    state = _load_state()
    slots = state.setdefault("slots", {})
    entry = dict(slots.get(slot_id) or {})
    entry["state"] = {"config": "invalid", "quota": "exhausted", "rate_limit": "throttled"}.get(
        kind, "error")
    entry["reason"] = str((error or {}).get("message") or (error or {}).get("hint") or "")[:240]
    entry["last_error"] = entry["reason"]
    entry["last_error_at"] = _now().isoformat(timespec="seconds")
    entry["fail_streak"] = int(entry.get("fail_streak") or 0) + 1
    entry["cooldown_until"] = ""
    if kind == "config":
        entry["cooldown_until"] = (_now() + dt.timedelta(seconds=CONFIG_COOLDOWN_SECONDS)
                                   ).isoformat(timespec="seconds")
    elif kind == "quota":
        reset = ""
        if quota:
            try:
                reset = ai_usage.reset_at(slot_id, quota)
            except Exception:  # noqa: BLE001
                reset = ""
        until = _parse_time(reset)
        if until is None or until <= _now():
            until = _now() + dt.timedelta(hours=QUOTA_COOLDOWN_FALLBACK_HOURS)
        entry["cooldown_until"] = until.isoformat(timespec="seconds")
    slots[slot_id] = entry
    _save_state(state)


def clear_slot_health(slot_id: Optional[str] = None) -> None:
    state = _load_state()
    if slot_id:
        (state.get("slots") or {}).pop(str(slot_id), None)
    else:
        state["slots"] = {}
    _save_state(state)


# ---------------- 错误分类 ----------------

def error_message(error: Optional[Dict[str, Any]]) -> str:
    if not error:
        return ""
    return " ".join(str(error.get(key) or "") for key in ("message", "detail", "code", "type"))


def is_rate_limit_error(error: Optional[Dict[str, Any]]) -> bool:
    """瞬时限流：RPM/TPM、429 且不含额度字样。"""
    return error_kind(error) == "rate_limit"


def is_quota_error(error: Optional[Dict[str, Any]]) -> bool:
    """额度类错误（含限流）：兼容旧语义，用于「换个接口」的判断。"""
    if not error:
        return False
    status = int(error.get("status") or 0)
    if status in QUOTA_STATUSES:
        return True
    message = error_message(error).lower()
    return any(pattern in message for pattern in QUOTA_PATTERNS)


def error_kind(error: Optional[Dict[str, Any]]) -> str:
    """把错误归成 config / quota / rate_limit / ""（其它）。

    * ``config``：Key 无效、无权限、模型不存在 —— 接口本身有问题，冷却久一点；
    * ``quota``：额度用尽、欠费、套餐窗口用满 —— 按额度窗口冷却；
    * ``rate_limit``：RPM/TPM 瞬时限流 —— 只影响这一次，不做长冷却；
    * 内容安全类的 403 只针对「这一次请求」，不改变接口状态（返回 ""）。
    """
    if not error:
        return ""
    status = int(error.get("status") or 0)
    message = error_message(error).lower()
    if status == 403 and any(pattern in message for pattern in PER_REQUEST_403):
        return ""
    if status == 402:
        return "quota"
    if any(pattern in message for pattern in QUOTA_PATTERNS):
        return "quota"
    if status == 403:
        return "config"
    if status in (401, 404):
        return "config"
    if status == 429:
        return "rate_limit"
    if any(pattern in message for pattern in RATE_PATTERNS):
        return "rate_limit"
    return ""


def _provider_hint(status: int, provider: str, preset: str, message: str) -> str:
    """给国内服务商补一句更贴近实际的解释。"""
    if preset == "qianfan" or "qianfan" in str(preset):
        if status == 401:
            return ("千帆鉴权失败：Key 要填控制台创建的 API Key（形如 bce-v3/ALTAK-...），"
                    "请求头必须带 Bearer 前缀；Coding/Token Plan 的专属 Key 与按量 Key 不通用。")
        if status == 403:
            if "overdue" in message or "欠费" in message:
                return "百度智能云账号已欠费（account_overdue），请充值后再试。"
            return "千帆返回 403：可能是账号欠费、模型未开通，或本次内容被安全策略拦截。"
        if status == 429:
            if "quota" in message:
                return "套餐额度已用尽（quota_exceeded）：等窗口刷新（5 小时/周/月）或升级套餐。"
            return "千帆 RPM/TPM 超限额：降低并发或购买配额，稍后会自动重试。"
        if status == 404:
            return "千帆报模型不存在：检查模型名（如 ernie-5.1），或该模型未在控制台开通。"
    return ""


def _log_error(status: int, model: str, message: str, raw: str = "", slot: str = "",
               code: str = "", request_id: str = "") -> None:
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        line = "%s\tslot=%s\tstatus=%s\tmodel=%s\tcode=%s\treq=%s\t%s\t%s\n" % (
            _now().isoformat(timespec="seconds"), slot or "-", status, model or "-",
            code or "-", request_id or "-", str(message).replace("\n", " ")[:200],
            (raw or "").replace("\n", " ")[:300])
        with ERROR_LOG.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except Exception:  # noqa: BLE001 - 日志失败不能影响主流程
        pass


def _classify(status: int, body: str, model: str, provider: str = "",
              preset: str = "") -> Dict[str, Any]:
    """把上游错误变成人能看懂的结构。"""
    message = ""
    raw = ""
    remedy = ""
    slug_hint = ""
    limit_source = ""
    code = ""
    kind = ""
    request_id = ""
    try:
        payload = json.loads(body)
        error = payload.get("error") or {}
        if not isinstance(error, dict):  # 有些网关直接给字符串
            error = {"message": str(error)}
        message = str(error.get("message") or error.get("msg") or "")
        code = str(error.get("code") or error.get("error_code") or payload.get("error_code") or "")
        kind = str(error.get("type") or payload.get("type") or "")
        request_id = str(error.get("request_id") or payload.get("id") or
                         payload.get("request_id") or "")
        meta = error.get("metadata") or {}
        raw = str(meta.get("raw") or payload.get("error_msg") or "")
        remedy = str(meta.get("remedy_hint") or "")
        limit_source = str(meta.get("limit_source") or "")
        slug_hint = str(meta.get("suggested_model") or "")
        if not message:
            message = str(payload.get("error_msg") or payload.get("message") or "")
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
    extra = _provider_hint(status, provider, preset, (message + " " + code + " " + kind).lower())
    if extra:
        hint = extra
    if not hint:
        hint = HINTS.get(status, "")

    return {
        "status": status,
        "code": code,
        "type": kind,
        "request_id": request_id,
        "model": model,
        "provider": provider,
        "preset": preset,
        "message": message or ("HTTP %s" % status),
        "detail": raw[:600],
        "hint": hint,
        "suggested_model": slug_hint,
        "retryable": status in RETRY_STATUSES,
        "kind": error_kind({"status": status, "message": message, "detail": raw, "code": code}),
    }


# ---------------- 配置解析 ----------------

def _slots(settings: Any) -> List[Dict[str, Any]]:
    return ai_settings.slots_of(settings)


def _ordered_slots(settings: Any) -> List[Dict[str, Any]]:
    """按 priority 排序，并把「当前偏好」的接口提前。"""
    slots = list(_slots(settings))
    slots.sort(key=lambda item: int(item.get("priority") or 0))
    preferred = active_slot()
    if preferred and preferred != "primary":
        index = next((i for i, item in enumerate(slots) if item.get("id") == preferred), None)
        if index is None and preferred == "fallback":
            index = next((i for i, item in enumerate(slots) if item.get("id") == "fallback"), None)
        if index:
            slots.insert(0, slots.pop(index))
    return slots


def _find_slot(slots: List[Dict[str, Any]], slot: Any) -> Optional[Dict[str, Any]]:
    key = ai_settings.slug(slot, "primary")
    for item in slots:
        if item.get("id") == key:
            return item
    if key in ("primary", "0", ""):
        return slots[0] if slots else None
    if key in ("fallback", "1"):
        found = next((item for item in slots if item.get("id") == "fallback"), None)
        if found:
            return found
        return slots[1] if len(slots) > 1 else None
    try:
        index = int(key)
    except ValueError:
        return None
    return slots[index] if 0 <= index < len(slots) else None


def _slot_config(settings: Any, item: Dict[str, Any]) -> Dict[str, Any]:
    """把全局参数继承到单个接口上，并补齐运行期字段。"""
    llm = ai_settings._llm_block(settings)
    cfg: Dict[str, Any] = dict(item)
    for key in ("timeout_seconds", "max_attempts", "temperature", "max_tokens"):
        if llm.get(key) not in (None, ""):
            cfg[key] = llm[key]
    cfg["timeout_seconds"] = float(cfg.get("timeout_seconds") or 60)
    cfg["max_attempts"] = max(1, min(3, int(cfg.get("max_attempts") or 2)))
    cfg["temperature"] = float(cfg.get("temperature") if cfg.get("temperature") is not None else 0.2)
    cfg["max_tokens"] = int(cfg.get("max_tokens") or 1500)
    cfg["quota"] = ai_settings.norm_quota(item.get("quota"))
    cfg["key_ref"] = ai_secrets.normalize_ref(item.get("key_ref") or item.get("id") or "primary")
    cfg["api_key_env"] = ai_secrets.env_name_for(cfg["key_ref"])
    cfg["slot"] = item.get("id") or "primary"   # 兼容旧字段名
    return cfg


def _llm_block(settings: Any, slot: str = "primary") -> Dict[str, Any]:
    """兼容旧接口：返回某个 slot 的原始（未鉴权校验）配置。"""
    slots = _slots(settings)
    item = _find_slot(slots, slot)
    if item is None:
        return {}
    return _slot_config(settings, item)


def resolve(settings: Any, slot: str = "primary") -> Tuple[bool, str, Dict[str, Any]]:
    """返回 (是否可用, 原因, 规范化配置)。settings 可以是整个配置或只有 llm 段。"""
    slots = _slots(settings)
    item = _find_slot(slots, slot)
    key = ai_settings.slug(slot, "primary")
    if item is None:
        reason = ("未配置备用接口" if key == "fallback"
                  else "未配置模型接口（全局设置 → 模型接口）")
        return False, reason, {}

    cfg = _slot_config(settings, item)
    master = bool(ai_settings._llm_block(settings).get("enabled", True))
    if not (master and bool(item.get("enabled"))):
        reason = ("未启用（全局设置 → 接入大模型）" if key in ("primary", "0", "")
                  else "该接口未启用")
        return False, reason, cfg

    provider = normalize_provider(cfg.get("provider") or "openai")
    cfg["provider"] = provider

    model = str(cfg.get("model") or "").strip()
    if not model:
        return False, "未配置模型名", cfg
    cfg["model"] = model

    base_url = str(cfg.get("base_url") or "").strip()
    if not base_url:
        default_port = 11434 if provider == "local" else 1234
        base_url = "http://127.0.0.1:%d" % default_port
    cfg["base_url"] = base_url.rstrip("/")

    if provider in ("openai", "anthropic"):
        api_key, source = ai_secrets.get_api_key(cfg["api_key_env"], cfg["key_ref"])
        cfg["key_source"] = source
        if not api_key and not cfg["base_url"].startswith(("http://127.0.0.1", "http://localhost",
                                                           "http://[::1]")):
            return False, "未填写 API Key（全局设置 → 接口的 Key）", cfg
        cfg["api_key"] = api_key

    return True, "", cfg


def describe(settings: Any) -> Dict[str, Any]:
    """给界面用的接口总览：每个接口的配置、可用性、健康、额度、Key 状态。"""
    slots = list(_slots(settings))
    slots.sort(key=lambda item: int(item.get("priority") or 0))
    master = bool(ai_settings._llm_block(settings).get("enabled", True))
    out: List[Dict[str, Any]] = []
    warnings: List[str] = []
    any_ok = False
    first_reason = ""
    for item in slots:
        slot_id = item.get("id") or "primary"
        ok, reason, cfg = resolve(settings, slot_id)
        quota = ai_settings.norm_quota(item.get("quota"))
        view = ai_usage.quota_view(slot_id, quota)
        health = slot_health(slot_id)
        key_ref = ai_secrets.normalize_ref(item.get("key_ref") or slot_id)
        out.append({
            "id": slot_id,
            "name": item.get("name") or slot_id,
            "enabled": bool(item.get("enabled")) and master,
            "preset": item.get("preset") or "",
            "provider": normalize_provider(item.get("provider") or "openai"),
            "base_url": item.get("base_url") or "",
            "model": item.get("model") or "",
            "key_ref": key_ref,
            "priority": int(item.get("priority") or 0),
            "quota": quota,
            "available": ok,
            "unavailable_reason": reason,
            "key": ai_secrets.public_state(ai_secrets.env_name_for(key_ref), key_ref),
            "health": health,
            "usage": view,
            "last_used": bool(LAST_USED.get("slot") == slot_id),
        })
        if ok:
            any_ok = True
        elif not first_reason:
            first_reason = reason
    enabled_slots = [s for s in out if s["enabled"]]
    for slot in enabled_slots:
        for other in enabled_slots:
            # 同一对接口只提醒一次
            if other["id"] <= slot["id"]:
                continue
            if slot["base_url"] and slot["base_url"] == other["base_url"] and \
                    slot["model"] and slot["model"] == other["model"]:
                warnings.append("「%s」和「%s」的地址与模型完全相同：一边额度用尽时另一边会一起失效，"
                                "建议换成另一家服务商。" % (slot["name"], other["name"]))
    seen = set()
    warnings = [w for w in warnings if not (w in seen or seen.add(w))]
    if not out:
        warnings.append("还没有配置任何模型接口。")
    return {
        "slots": out,
        "available": any_ok,
        "unavailable_reason": first_reason,
        "warnings": warnings,
        "active_slot": active_slot(),
        "switch": slot_state(),
        "last_used": last_used(),
        "providers": list(PROVIDERS),
        "provider_labels": dict(PROVIDER_LABELS),
        "presets": preset_list(),
    }


# ---------------- 请求 ----------------

def _join_endpoint(base: str, provider: str, suffix: str) -> str:
    """拼接接口地址：支持 /v1 /v2 /v3 /v4（可以在结尾，也可以是路径中的一段，例如
    千帆 Token Plan 的 ``/v2/tokenplan/personal``），也支持直接填完整端点。"""
    base = str(base or "").rstrip("/")
    if not base:
        return suffix
    if base.endswith(suffix):
        return base
    if provider == "anthropic" and base.endswith("/messages"):
        return base
    if _VERSION_IN_PATH.search(base):
        return base + suffix
    return base + "/v1" + suffix


def _post(url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout: float,
          model: str = "", provider: str = "", preset: str = ""
          ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
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
        error = _classify(exc.code, body, model or str(payload.get("model") or ""),
                          provider, preset)
        retry_after = ""
        try:
            retry_after = str(exc.headers.get("Retry-After") or "")
        except Exception:  # noqa: BLE001
            pass
        if retry_after:
            error["retry_after"] = retry_after
        return None, error
    except Exception as exc:  # noqa: BLE001
        return None, {
            "status": 0,
            "message": "%s: %s" % (type(exc).__name__, exc),
            "detail": "",
            "hint": "无法连接到接口地址，请检查网络、代理或 base_url 是否正确。",
            "suggested_model": "",
            "model": model,
            "code": "",
            "type": "",
            "request_id": "",
            "retryable": False,
            "kind": "config",
        }


def _build_request(llm: Dict[str, Any], messages: List[Dict[str, str]],
                   json_mode: bool) -> Tuple[str, Dict[str, Any], Dict[str, str]]:
    provider = llm.get("provider") or "openai"
    temperature = float(llm.get("temperature", 0.2))
    max_tokens = int(llm.get("max_tokens") or 1500)
    base = str(llm.get("base_url") or "")
    key, _source = ai_secrets.get_api_key(
        llm.get("api_key_env") or None, llm.get("key_ref") or llm.get("slot") or "primary")
    if not key:
        key = str(llm.get("api_key") or "")

    # ---- 本地模型（Ollama / vLLM 的 /api/chat）----
    if provider == "local":
        url = base if base.endswith("/api/chat") else base.rstrip("/") + "/api/chat"
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

    # ---- Anthropic 原生（Claude / 千帆 Anthropic 兼容口）----
    if provider == "anthropic":
        url = _join_endpoint(base, provider, "/messages")
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

    # ---- OpenAI 兼容（OpenAI / DeepSeek / 千帆 / OpenRouter / 中转）----
    url = _join_endpoint(base, provider, "/chat/completions")
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
    provider = llm.get("provider")
    if provider == "local":
        return (data.get("message") or {}).get("content")
    if provider == "anthropic":
        blocks = data.get("content") or []
        texts = [b.get("text") or "" for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(t for t in texts if t) or None
    choices = data.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # 有些网关返回分块内容
            content = "\n".join(str((part or {}).get("text") or "") for part in content
                                if isinstance(part, dict))
        text = content or message.get("reasoning_content") or ""
        return text or None
    return None


def _usage_of(llm: Dict[str, Any], data: Dict[str, Any]) -> Tuple[int, int]:
    """不同接口的 usage 字段名不一样，统一成 (prompt, completion)。"""
    usage = data.get("usage") or {}
    if llm.get("provider") == "anthropic":
        return (int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
                int(usage.get("output_tokens") or usage.get("completion_tokens") or 0))
    if llm.get("provider") == "local":
        return (int(usage.get("prompt_eval_count") or usage.get("prompt_tokens") or 0),
                int(usage.get("eval_count") or usage.get("completion_tokens") or 0))
    return (int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            int(usage.get("completion_tokens") or usage.get("output_tokens") or 0))


def _retry_sleep(error: Optional[Dict[str, Any]], attempt: int) -> float:
    raw = str((error or {}).get("retry_after") or "").strip()
    if raw:
        try:
            return max(0.5, min(15.0, float(raw)))
        except ValueError:
            pass
    return min(6.0, 1.5 * (attempt + 1))


def _attempt(llm: Dict[str, Any], messages: List[Dict[str, str]], json_mode: bool,
             count: bool = True) -> Tuple[Optional[str], Optional[Dict[str, Any]], Dict[str, int]]:
    """针对单个接口的一次完整调用（含内部重试与截断处理）。

    返回 (文本, 错误, 本次用量)。``count=False`` 时不写全局统计（用于连接测试）。
    """
    url, payload, headers = _build_request(llm, messages, json_mode)
    timeout = float(llm.get("timeout_seconds") or 60)
    attempts = max(1, min(3, int(llm.get("max_attempts") or 2)))
    usage: Dict[str, int] = {"prompt": 0, "completion": 0, "calls": 0, "latency_ms": 0}

    last_error: Optional[Dict[str, Any]] = None
    bumped = False
    for attempt in range(attempts + 1):  # +1 给「截断后放大上限」留一次机会
        started = time.time()
        data, error = _post(url, payload, headers, timeout, str(llm.get("model") or ""),
                            str(llm.get("provider") or ""), str(llm.get("preset") or ""))
        latency = int((time.time() - started) * 1000)
        usage["latency_ms"] = latency
        if count:
            STATS["last_latency_ms"] = latency

        if data:
            text = _extract(llm, data)
            prompt_tokens, completion_tokens = _usage_of(llm, data)
            usage["prompt"] += prompt_tokens
            usage["completion"] += completion_tokens
            usage["calls"] += 1
            if count:
                STATS["calls"] += 1
                STATS["prompt_tokens"] += prompt_tokens
                STATS["completion_tokens"] += completion_tokens

            if text and text.strip():
                if count:
                    STATS["last_error"] = ""
                return text, None, usage

            # 请求成功但正文为空：常见于推理模型把 token 全花在 reasoning 上
            choice = (data.get("choices") or [{}])[0]
            finish = choice.get("finish_reason")
            reasoning_tokens = ((data.get("usage") or {}).get("completion_tokens_details") or {}
                                ).get("reasoning_tokens") or 0
            truncated = finish == "length"
            message = ("模型输出被长度上限截断，正文为空" if truncated else "模型返回了空内容")
            last_error = {
                "status": 0,
                "code": "truncated" if truncated else "empty",
                "model": str(llm.get("model") or ""),
                "message": message,
                "detail": "finish_reason=%s, completion_tokens=%s, reasoning_tokens=%s"
                          % (finish, completion_tokens, reasoning_tokens),
                "hint": ("这个模型很可能是推理模型，思考过程吃掉了输出预算。"
                         "提高「最大输出 token」后重试，或换用非推理模型。"
                         if reasoning_tokens or truncated else "模型没有返回正文，稍后重试。"),
                "suggested_model": "",
                "retryable": truncated,
                "kind": "",
            }
            if truncated and not bumped:
                current_max = int(payload.get("max_tokens") or 0)
                if current_max:
                    bigger = min(8000, current_max * 3)
                    payload["max_tokens"] = bigger
                    if llm.get("provider") == "local":
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

        if not last_error.get("retryable") or attempt + 1 >= attempts:
            break
        time.sleep(_retry_sleep(last_error, attempt))
    return None, last_error, usage


# ---------------- 调用 ----------------

def _token_limits(settings: Any) -> Dict[str, Any]:
    """兼容旧调用：把 slots 的额度上限汇总成 primary/fallback/window_hours。"""
    llm = ai_settings._llm_block(settings)
    slots = _slots(settings)

    def _limit(slot_id: str) -> int:
        item = _find_slot(slots, slot_id)
        return int(((item or {}).get("quota") or {}).get("limit") or 0)

    return {
        "primary": _limit("primary"),
        "fallback": _limit("fallback"),
        "window_hours": float(llm.get("quota_window_hours") or 24),
    }


def chat_result_full(messages: List[Dict[str, str]], settings: Any,
                     json_mode: bool = True):
    """调用模型，返回 (文本, 错误, 实际使用的接口信息)。

    错误是结构化 dict，绝不抛异常；第三个值是 {slot, name, model, provider}，
    调用方靠它正确归属缓存与展示。错误里还会带 ``slots`` 列出每个接口的失败原因。
    """
    limits = _token_limits(settings)
    window_hours = float(limits.get("window_hours") or 24)

    until = ai_usage.pause_until()
    if until:
        return None, {
            "status": 0, "code": "paused", "kind": "quota",
            "message": "AI 已暂停：所有接口的 token 额度都用完了",
            "detail": "paused_until=%s" % until,
            "hint": "%s 之后自动恢复并重新计数" % until,
            "suggested_model": "", "retryable": False, "slots": [],
        }, {}

    all_slots = _ordered_slots(settings)
    ready: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    blocked: List[Dict[str, Any]] = []
    exhausted: List[Dict[str, Any]] = []
    first_reason = ""

    for item in all_slots:
        slot_id = item.get("id") or "primary"
        ok, reason, cfg = resolve(settings, slot_id)
        if not ok:
            first_reason = first_reason or reason
            continue
        quota = cfg.get("quota") or {}
        if ai_usage.exhausted_quota(slot_id, quota):
            exhausted.append({"id": slot_id, "name": item.get("name") or slot_id,
                              "reason": "本地记账：该接口额度已用尽",
                              "reset_at": ai_usage.reset_at(slot_id, quota)})
            first_reason = first_reason or ("%s 额度已用尽" % slot_id)
            print("[ai] 接口 %s token 额度已用尽，跳过" % slot_id)
            continue
        if slot_blocked(slot_id):
            health = slot_health(slot_id)
            blocked.append({"id": slot_id, "name": item.get("name") or slot_id,
                            "reason": health.get("reason") or "接口冷却中",
                            "cooldown_until": health.get("cooldown_until"),
                            "state": health.get("state")})
            continue
        ready.append((item, cfg))

    if not ready:
        if blocked:
            names = "；".join("%s %s（%s 后恢复）" % (item["name"], item["reason"] or "失败",
                                                    item["cooldown_until"] or "稍")
                             for item in blocked)
            return None, {
                "status": 0, "code": "cooldown", "kind": "config",
                "message": "所有接口都在冷却中，本次不发起请求",
                "detail": names[:600],
                "hint": "等冷却结束，或在全局设置里修正接口配置/清除失败状态。",
                "suggested_model": "", "retryable": False, "slots": blocked,
            }, {}
        if exhausted:
            until = ai_usage.pause(window_hours)
            detail = "；".join("%s：%s" % (item["name"], item.get("reset_at") or "额度重置后")
                              for item in exhausted)
            return None, {
                "status": 0, "code": "paused", "kind": "quota",
                "message": "所有接口的 token 额度都用完了，AI 先暂停",
                "detail": ("paused_until=%s\n%s" % (until, detail))[:600],
                "hint": "%s 之后自动恢复并重新计数；也可以调高额度上限、加一个接口或换服务商。"
                        % until,
                "suggested_model": "", "retryable": False,
                "slots": exhausted,
            }, {}

    errors: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for index, (item, cfg) in enumerate(ready):
        slot_id = item.get("id") or "primary"
        text, error, usage = _attempt(cfg, messages, json_mode, count=True)
        ai_usage.record(slot_id, usage.get("prompt", 0), usage.get("completion", 0),
                        usage.get("calls", 0), quota=cfg.get("quota"))
        if text:
            used = {"slot": slot_id, "name": item.get("name") or slot_id,
                    "model": cfg.get("model", ""), "provider": cfg.get("provider", ""),
                    "preset": cfg.get("preset", "") or item.get("preset", ""),
                    "at": _now().isoformat(timespec="seconds")}
            mark_slot_ok(slot_id, used["model"])
            LAST_USED.update(used)
            return text, None, used

        errors.append((item, error or {}))
        mark_slot_error(slot_id, error, cfg.get("quota"))
        if index == 0 and error_kind(error) == "quota":
            # 首选接口额度用尽：记下「优先用备用」，界面也会显示
            switch_to_fallback(str((error or {}).get("message") or ""), hours=window_hours)

    first_error = dict(errors[0][1]) if errors else {}
    first_error["slots"] = [
        {"id": (item.get("id") or ""), "name": item.get("name") or "",
         "status": err.get("status", 0), "code": err.get("code", ""),
         "kind": err.get("kind", "") or error_kind(err),
         "message": err.get("message", ""), "hint": err.get("hint", "")}
        for item, err in errors
    ]
    for item, err in errors:
        _log_error(int(err.get("status") or 0), str(err.get("model") or item.get("model") or ""),
                   str(err.get("message") or ""), str(err.get("detail") or ""),
                   slot=str(item.get("id") or ""), code=str(err.get("code") or ""),
                   request_id=str(err.get("request_id") or ""))
    STATS["failures"] += 1
    STATS["last_error"] = str(first_error.get("message") or first_reason)[:300]
    print("[ai] call failed:", STATS["last_error"][:160])
    return None, first_error, {}


def chat_result(messages: List[Dict[str, str]], settings: Any,
                json_mode: bool = True) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """调用模型，返回 (文本, 错误)。"""
    text, error, _used = chat_result_full(messages, settings, json_mode)
    return text, error


def chat_result_ex(messages: List[Dict[str, str]], settings: Any,
                   json_mode: bool = True) -> Tuple[Optional[str], Optional[Dict[str, Any]], Dict[str, Any]]:
    """和 chat_result 一样，但额外返回「实际服务这次调用的接口与模型」。"""
    return chat_result_full(messages, settings, json_mode)


def chat(messages: List[Dict[str, str]], settings: Any, json_mode: bool = True) -> Optional[str]:
    """兼容旧调用：只要文本。"""
    text, _error = chat_result(messages, settings, json_mode)
    return text


# ---------------- 测试与状态 ----------------

TEST_MESSAGES = [
    {"role": "system", "content": "你是一个连通性测试端点。只回复 JSON。"},
    {"role": "user", "content": '请只输出 JSON：{"ok": true, "echo": "pong"}'},
]


def test_connection(settings: Any, slot: str = "primary", record: bool = False) -> Dict[str, Any]:
    """测指定接口。默认不触发切换/冷却，避免测试改变运行状态。"""
    slots = _slots(settings)
    item = _find_slot(slots, slot)
    slot_id = (item or {}).get("id") or ai_settings.slug(slot, "primary")
    ok, reason, llm = resolve(settings, slot)
    if not ok:
        return {"ok": False, "reason": reason, "slot": slot_id,
                "name": (item or {}).get("name") or slot_id,
                "provider": llm.get("provider", ""), "preset": (item or {}).get("preset", ""),
                "model": llm.get("model", ""), "latency_ms": 0, "error": None}

    started = time.time()
    text, error, usage = _attempt(llm, TEST_MESSAGES, True, count=False)
    latency = int((time.time() - started) * 1000)

    if record:
        if text:
            mark_slot_ok(slot_id, llm.get("model", ""))
        else:
            mark_slot_error(slot_id, error, llm.get("quota"))

    if text is None:
        return {
            "ok": False,
            "slot": slot_id,
            "name": item.get("name") or slot_id,
            "provider": llm["provider"],
            "preset": item.get("preset", ""),
            "model": llm["model"],
            "base_url": llm["base_url"],
            "latency_ms": latency,
            "reason": (error or {}).get("message", "模型没有返回内容"),
            "hint": (error or {}).get("hint", ""),
            "kind": (error or {}).get("kind") or error_kind(error),
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
        "ok": True, "responded": True, "protocol_ok": protocol_ok, "slot": slot_id,
        "name": item.get("name") or slot_id, "provider": llm["provider"],
        "preset": item.get("preset", ""), "model": llm["model"], "base_url": llm["base_url"],
        "latency_ms": latency, "sample": sample, "tokens": usage.get("prompt", 0) +
        usage.get("completion", 0), "error": None, "hint": "", "kind": "",
        "note": "" if protocol_ok else "模型有返回，但没有按要求的 JSON 格式回复（仍可用于摘要，只是结构化程度较低）",
    }


def test_all_slots(settings: Any, record: bool = False) -> Dict[str, Any]:
    """依次测试每个启用的接口（用于「保存即自检」）。"""
    results = []
    for item in _ordered_slots(settings):
        ok, _reason, llm = resolve(settings, item.get("id") or "primary")
        if not llm and not ok:
            results.append({"ok": False, "slot": item.get("id"), "name": item.get("name"),
                            "reason": _reason, "skipped": True})
            continue
        results.append(test_connection(settings, item.get("id") or "primary", record=record))
    return {"results": results,
            "ok": all(r.get("ok") for r in results) if results else False}


def status(settings: Any) -> Dict[str, Any]:
    info = describe(settings)
    llm = ai_settings._llm_block(settings)
    slots = info["slots"]
    first = slots[0] if slots else {}
    fallback = next((item for item in slots if item["id"] == "fallback"), None)
    if fallback is None and len(slots) > 1:
        fallback = slots[1]
    limits = _token_limits(settings)
    return {
        "available": info["available"],
        "reason": info["unavailable_reason"],
        "active_slot": info["active_slot"],
        "switch": info["switch"],
        "last_used": info["last_used"],
        "slots": slots,
        "warnings": info["warnings"],
        "providers": info["providers"],
        "provider_labels": info["provider_labels"],
        "presets": info["presets"],
        "fallback": {
            "configured": bool((fallback or {}).get("enabled")),
            "available": bool((fallback or {}).get("available")),
            "reason": (fallback or {}).get("unavailable_reason", ""),
            "provider": (fallback or {}).get("provider", ""),
            "model": (fallback or {}).get("model", ""),
            "base_url": (fallback or {}).get("base_url", ""),
            "key": (fallback or {}).get("key") or {},
        },
        "provider": first.get("provider", ""),
        "model": first.get("model", ""),
        "base_url": first.get("base_url", ""),
        "key": first.get("key") or {},
        "calls": STATS["calls"],
        "failures": STATS["failures"],
        "prompt_tokens": STATS["prompt_tokens"],
        "completion_tokens": STATS["completion_tokens"],
        "last_error": STATS["last_error"],
        "last_latency_ms": STATS["last_latency_ms"],
        "checked_at": _now().isoformat(timespec="seconds"),
        "log_path": str(ERROR_LOG),
        "token_limits": limits,
        "config": {"enabled": bool(llm.get("enabled")),
                   "timeout_seconds": llm.get("timeout_seconds"),
                   "max_attempts": llm.get("max_attempts"),
                   "temperature": llm.get("temperature"),
                   "max_tokens": llm.get("max_tokens"),
                   "quota_window_hours": llm.get("quota_window_hours")},
        "usage": ai_usage.summary(
            {item["id"]: item.get("quota") for item in slots},
            float(limits.get("window_hours") or 24)),
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