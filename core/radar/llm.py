"""可选的大模型后端。

默认关闭，本机没有配置时全部调用都会返回 ``None``，由 ``summarize`` 回退到本地抽取式摘要。
支持两类端点：

* ``openai_compatible`` —— 任意 OpenAI 兼容的 ``/chat/completions``（本地 vLLM、LM Studio、
  One-API、云端厂商均可）
* ``ollama`` —— 本地 Ollama 的 ``/api/chat``

密钥只从环境变量读取，不写进代码、日志或返回给前端。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

PROVIDERS = ("openai_compatible", "ollama")

# 进程内的轻量成本统计，供 /api/radar/status 展示（不落库）
STATS: Dict[str, Any] = {"calls": 0, "failures": 0, "prompt_tokens": 0,
                         "completion_tokens": 0, "last_error": "", "last_latency_ms": 0}


def resolve(cfg: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    """返回 (是否可用, 不可用原因, 规范化配置)。"""
    llm = (cfg.get("llm") or {})
    if not llm.get("enabled"):
        return False, "未启用（social_radar.json → llm.enabled）", llm
    provider = str(llm.get("provider") or "openai_compatible")
    if provider not in PROVIDERS:
        return False, "不支持的 provider：%s" % provider, llm
    model = str(llm.get("model") or "").strip()
    if not model:
        return False, "未配置模型名（llm.model）", llm
    base_url = str(llm.get("base_url") or "").strip()
    if not base_url:
        default_port = 11434 if provider == "ollama" else 1234
        base_url = "http://127.0.0.1:%d" % default_port
    base_url = base_url.rstrip("/")
    if provider == "openai_compatible":
        env_name = str(llm.get("api_key_env") or "RADAR_LLM_API_KEY")
        api_key = (os.environ.get(env_name) or "").strip()
        if not api_key and not base_url.startswith(("http://127.0.0.1", "http://localhost")):
            return False, "缺少 API key（环境变量 %s）" % env_name, llm
    return True, "", {**llm, "provider": provider, "model": model, "base_url": base_url}


def _post(url: str, payload: Dict[str, Any], headers: Dict[str, str],
          timeout: float) -> Tuple[Optional[Dict[str, Any]], str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        return json.loads(raw), ""
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        return None, "HTTP %s %s" % (exc.code, body)
    except Exception as exc:  # noqa: BLE001
        return None, "%s: %s" % (type(exc).__name__, exc)


def chat(messages: List[Dict[str, str]], cfg: Dict[str, Any],
         json_mode: bool = True) -> Optional[str]:
    """调用模型，失败返回 None（绝不抛异常）。"""
    ok, reason, llm = resolve(cfg)
    if not ok:
        STATS["last_error"] = reason
        return None

    provider = llm["provider"]
    timeout = float(llm.get("timeout_seconds") or 45)
    attempts = max(1, int(llm.get("max_attempts") or 2))
    temperature = float(llm.get("temperature", 0.2))

    if provider == "ollama":
        url = llm["base_url"] + "/api/chat"
        payload = {"model": llm["model"], "messages": messages, "stream": False,
                   "options": {"temperature": temperature}}
        if json_mode:
            payload["format"] = "json"
        headers = {"Content-Type": "application/json"}
    else:
        base = llm["base_url"]
        url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
        payload = {"model": llm["model"], "messages": messages, "temperature": temperature}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        env_name = str(llm.get("api_key_env") or "RADAR_LLM_API_KEY")
        headers = {"Content-Type": "application/json"}
        key = (os.environ.get(env_name) or "").strip()
        if key:
            headers["Authorization"] = "Bearer " + key

    last_error = ""
    for attempt in range(attempts):
        started = time.time()
        data, error = _post(url, payload, headers, timeout)
        STATS["last_latency_ms"] = int((time.time() - started) * 1000)
        if data:
            STATS["calls"] += 1
            usage = (data.get("usage") or {})
            STATS["prompt_tokens"] += int(usage.get("prompt_tokens") or usage.get("prompt_eval_count") or 0)
            STATS["completion_tokens"] += int(
                usage.get("completion_tokens") or usage.get("eval_count") or 0)
            if provider == "ollama":
                return (data.get("message") or {}).get("content")
            choices = data.get("choices") or []
            if choices:
                return (choices[0].get("message") or {}).get("content")
            last_error = "响应里没有 choices"
        else:
            last_error = error
        if attempt + 1 < attempts:
            time.sleep(min(3.0, 0.8 * (attempt + 1)))

    STATS["failures"] += 1
    STATS["last_error"] = last_error[:300]
    print("[radar][llm] call failed:", last_error[:200])
    return None


def status(cfg: Dict[str, Any]) -> Dict[str, Any]:
    ok, reason, llm = resolve(cfg)
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
    }
