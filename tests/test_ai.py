"""全局 AI 层测试：配置、密钥、错误分类、项目解读缓存。全部离线。"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401,E402

from core import ai_brief, ai_client, ai_secrets, ai_settings, brief, db  # noqa: E402


class TestGlobalSettings(unittest.TestCase):
    def setUp(self):
        if ai_settings.CONFIG_PATH.exists():
            ai_settings.CONFIG_PATH.unlink()

    def tearDown(self):
        if ai_settings.CONFIG_PATH.exists():
            ai_settings.CONFIG_PATH.unlink()

    def test_defaults_are_disabled(self):
        cfg = ai_settings.load()
        self.assertFalse(cfg["llm"]["enabled"])
        self.assertTrue(cfg["ai"]["enable_briefs"])
        self.assertNotIn("briefs_per_run", cfg["ai"])  # 条数限制已移除

    def test_save_and_reload(self):
        ai_settings.save({"llm": {"enabled": True, "provider": "openai",
                                  "base_url": "https://openrouter.ai/api/v1",
                                  "model": "google/gemma-4-26b-a4b-it:free"},
                          "ai": {"concurrency": 3}})
        cfg = ai_settings.load()
        self.assertTrue(cfg["llm"]["enabled"])
        self.assertEqual(cfg["llm"]["model"], "google/gemma-4-26b-a4b-it:free")
        self.assertEqual(cfg["ai"]["concurrency"], 3)

    def test_saved_file_has_no_secret_fields(self):
        ai_settings.save({"llm": {"enabled": True, "model": "m"}})
        raw = ai_settings.CONFIG_PATH.read_text(encoding="utf-8")
        self.assertNotIn("api_key", raw)
        self.assertNotIn("sk-or-", raw)


class TestSecretStore(unittest.TestCase):
    def setUp(self):
        os.environ.pop("RADAR_LLM_API_KEY", None)
        ai_secrets.set_api_key(None)

    def tearDown(self):
        os.environ.pop("RADAR_LLM_API_KEY", None)
        ai_secrets.set_api_key(None)

    def test_roundtrip_and_mask(self):
        self.assertTrue(ai_secrets.set_api_key("sk-or-v1-abcdefgh12345678"))
        state = ai_secrets.public_state()
        self.assertTrue(state["configured"])
        self.assertNotIn("abcdefgh", state["masked"])
        self.assertTrue(state["masked"].endswith("5678"))

    def test_env_wins(self):
        ai_secrets.set_api_key("file-key")
        os.environ["RADAR_LLM_API_KEY"] = "env-key"
        key, source = ai_secrets.get_api_key()
        self.assertEqual(key, "env-key")
        self.assertTrue(source.startswith("env:"))


class TestErrorClassification(unittest.TestCase):
    def test_rate_limit_from_shared_pool(self):
        body = json.dumps({"error": {
            "message": "Provider returned error",
            "code": 429,
            "metadata": {"raw": "temporarily rate-limited upstream",
                         "limit_source": "upstream_provider_shared_pool"},
        }})
        info = ai_client._classify(429, body, "google/gemma-4-26b-a4b-it:free")
        self.assertEqual(info["status"], 429)
        self.assertTrue(info["retryable"])
        self.assertIn("共享池", info["hint"])

    def test_model_gone_suggests_paid_slug(self):
        body = json.dumps({"error": {
            "message": "This model is unavailable for free. The paid version is available now - "
                       "use this slug instead: google/gemma-3-27b-it"}})
        info = ai_client._classify(404, body, "google/gemma-3-27b-it:free")
        self.assertEqual(info["suggested_model"], "google/gemma-3-27b-it")
        self.assertIn("google/gemma-3-27b-it", info["hint"])

    def test_auth_error_hint(self):
        info = ai_client._classify(401, '{"error":{"message":"No auth credentials found"}}', "m")
        self.assertIn("API Key", info["hint"])
        self.assertFalse(info["retryable"])

    def test_unknown_body_does_not_crash(self):
        info = ai_client._classify(500, "<html>oops</html>", "m")
        self.assertEqual(info["status"], 500)
        self.assertTrue(info["hint"])

    def test_unreachable_endpoint_is_explained(self):
        result = ai_client.test_connection({"llm": {
            "enabled": True, "provider": "openai_compatible",
            "base_url": "http://127.0.0.1:9/v1", "model": "x", "timeout_seconds": 2,
            "max_attempts": 1}})
        self.assertFalse(result["ok"])
        self.assertTrue(result["hint"] or result["reason"])

    def test_disabled_model_reports_reason(self):
        result = ai_client.test_connection({"llm": {"enabled": False}})
        self.assertFalse(result["ok"])
        self.assertIn("未启用", result["reason"])


class TestAiBrief(unittest.TestCase):
    def setUp(self):
        conn = db.connect()
        try:
            conn.execute("DELETE FROM ai_briefs")
            conn.commit()
        except Exception:  # noqa: BLE001
            pass
        finally:
            conn.close()

    def test_normalize_rejects_empty(self):
        self.assertIsNone(ai_brief._normalize({"what": ""}))
        self.assertIsNone(ai_brief._normalize("not a dict"))

    def test_normalize_keeps_detail_and_caps_highlights(self):
        data = ai_brief._normalize({
            "what": "一个工具",
            "detail": "它解决什么问题、有什么特点。" * 60,
            "highlights": ["a", "b", "c", "d", "e", "f", ""],
            "limits": "无",
        })
        self.assertEqual(data["source"], brief.SOURCE_AI)
        self.assertEqual(data["what"], "一个工具")
        self.assertEqual([h for h in data["highlights"]], ["a", "b", "c", "d", "e"])
        self.assertLessEqual(len(data["detail"]), 600)

    def test_apply_merges_and_marks_ai(self):
        item = {"brief": {"what": "GitHub 简介版"}, "full_name": "a/b"}
        ai_brief.apply(item, {"what": "AI 版", "detail": "解读"}, "model-x")
        self.assertEqual(item["brief"]["what"], "AI 版")
        self.assertEqual(item["brief"]["source"], brief.SOURCE_AI)
        self.assertTrue(item["brief"]["ai"])
        self.assertEqual(item["brief"]["ai_model"], "model-x")

    def test_enrich_is_noop_when_disabled(self):
        items = [{"full_name": "a/b", "brief": {}}]
        stats = ai_brief.enrich(items, {"llm": {"enabled": False}})
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["generated"], 0)
        self.assertNotIn("ai", items[0]["brief"])

    def test_cache_hit_does_not_call_model(self):
        settings = {"llm": {"enabled": True, "provider": "openai",
                            "base_url": "http://127.0.0.1:9/v1", "model": "cache-model",
                            "timeout_seconds": 1, "max_attempts": 1},
                    "ai": {"enable_briefs": True, "concurrency": 1}}
        item = {"full_name": "cache/test", "brief": {}}
        conn = ai_brief._conn()
        try:
            ai_brief._save(conn, ai_brief._key(item, "cache-model"), item, "cache-model",
                           {"what": "缓存里的解读", "detail": "缓存里的解读正文",
                            "highlights": ["要点一"], "limits": ""})
            conn.commit()
        finally:
            conn.close()
        stats = ai_brief.enrich([item], settings)
        self.assertEqual(stats["cached"], 1)
        self.assertEqual(stats["generated"], 0)
        self.assertEqual(item["brief"]["what"], "缓存里的解读")


class TestFallback(unittest.TestCase):
    """主接口额度用尽 → 切备用；第二天自动切回。"""

    def setUp(self):
        ai_client.reset_slot()
        os.environ.pop("RADAR_LLM_API_KEY_FALLBACK", None)
        ai_secrets.set_api_key(None, "fallback")
        self.cfg = {
            "llm": {
                "enabled": True, "provider": "openai_compatible",
                "base_url": "http://127.0.0.1:9/v1", "model": "primary-model",
                "timeout_seconds": 2, "max_attempts": 1,
                "fallback": {
                    "enabled": True, "provider": "openai_compatible",
                    "base_url": "http://127.0.0.1:9/v1", "model": "backup-model",
                },
            }
        }

    def tearDown(self):
        ai_client.reset_slot()
        ai_secrets.set_api_key(None, "fallback")

    def test_quota_errors_are_detected(self):
        self.assertTrue(ai_client.is_quota_error({"status": 429, "message": "x"}))
        self.assertTrue(ai_client.is_quota_error({"status": 402, "message": ""}))
        self.assertTrue(ai_client.is_quota_error(
            {"status": 0, "message": "Rate limit exceeded: free-models-per-day"}))
        self.assertFalse(ai_client.is_quota_error({"status": 500, "message": "server error"}))
        self.assertFalse(ai_client.is_quota_error(None))

    def test_fallback_disabled_reports_reason(self):
        ok, reason, _ = ai_client.resolve({"llm": {"enabled": True, "model": "m"}}, "fallback")
        self.assertFalse(ok)
        self.assertIn("未配置备用接口", reason)

    def test_fallback_resolves_when_configured(self):
        ok, _reason, resolved = ai_client.resolve(self.cfg, "fallback")
        self.assertTrue(ok)
        self.assertEqual(resolved["model"], "backup-model")
        self.assertEqual(resolved["slot"], "fallback")

    def test_switch_and_next_day_revert(self):
        ai_client.switch_to_fallback("quota exhausted")
        self.assertEqual(ai_client.active_slot(), "fallback")
        state = ai_client.slot_state()
        self.assertTrue(state["fallback_until"])
        self.assertIn("quota", state["reason"])

        # 模拟到第二天：把到期日改成昨天
        raw = ai_client._load_state()
        raw["fallback_until"] = "2020-01-01"
        ai_client._save_state(raw)
        self.assertEqual(ai_client.active_slot(), "primary")
        self.assertEqual(ai_client.slot_state()["fallback_until"], "")

    def test_reset_slot_returns_to_primary(self):
        ai_client.switch_to_fallback("x")
        self.assertEqual(ai_client.active_slot(), "fallback")
        ai_client.reset_slot()
        self.assertEqual(ai_client.active_slot(), "primary")

    def test_test_connection_targets_specific_slot(self):
        result = ai_client.test_connection(self.cfg, "fallback")
        self.assertEqual(result["slot"], "fallback")
        self.assertFalse(result["ok"])  # 端口关着，只验证没有触发切换
        self.assertEqual(ai_client.active_slot(), "primary")

    def test_status_exposes_fallback_and_slot(self):
        status = ai_client.status(self.cfg)
        self.assertEqual(status["active_slot"], "primary")
        self.assertTrue(status["fallback"]["configured"])
        self.assertIn("switch", status)

    def test_primary_and_fallback_keys_are_separate(self):
        ai_secrets.set_api_key("sk-primary-1234", "primary")
        ai_secrets.set_api_key("sk-backup-5678", "fallback")
        self.assertEqual(ai_secrets.get_api_key(ai_secrets.ENV_NAME, "primary")[0],
                         "sk-primary-1234")
        self.assertEqual(ai_secrets.get_api_key(ai_secrets.FALLBACK_ENV_NAME, "fallback")[0],
                         "sk-backup-5678")
        ai_secrets.set_api_key(None, "fallback")
        self.assertEqual(ai_secrets.get_api_key(ai_secrets.FALLBACK_ENV_NAME, "fallback")[0], "")
        self.assertEqual(ai_secrets.get_api_key(ai_secrets.ENV_NAME, "primary")[0],
                         "sk-primary-1234")
        ai_secrets.set_api_key(None, "primary")

    def test_settings_save_persists_fallback_without_key(self):
        from core import ai_api

        if ai_settings.CONFIG_PATH.exists():
            ai_settings.CONFIG_PATH.unlink()
        ai_api._save({
            "fallback": {"enabled": True, "provider": "openai_compatible",
                         "base_url": "https://backup.example/v1", "model": "backup-model"},
            "fallback_api_key": "sk-secret-backup",
        })
        raw = ai_settings.CONFIG_PATH.read_text(encoding="utf-8")
        self.assertIn("backup-model", raw)
        self.assertNotIn("sk-secret-backup", raw)
        self.assertEqual(ai_secrets.get_api_key(ai_secrets.FALLBACK_ENV_NAME, "fallback")[0],
                         "sk-secret-backup")
        if ai_settings.CONFIG_PATH.exists():
            ai_settings.CONFIG_PATH.unlink()


class TestTokenLimits(unittest.TestCase):
    """token 限额：主超→切备；两个都超→暂停；恢复后重新计数。"""

    def setUp(self):
        from core import ai_usage

        ai_usage.resume()
        ai_client.reset_slot()

    def tearDown(self):
        from core import ai_usage

        ai_usage.resume()
        ai_client.reset_slot()

    def test_record_and_limits(self):
        from core import ai_usage

        ai_usage.record("primary", 100, 50, 1)
        self.assertEqual(ai_usage.used("primary"), 150)
        self.assertEqual(ai_usage.remaining("primary", 1000), 850)
        self.assertFalse(ai_usage.exhausted("primary", 1000))
        ai_usage.record("primary", 900, 0, 1)
        self.assertTrue(ai_usage.exhausted("primary", 1000))
        self.assertIsNone(ai_usage.remaining("primary", 0))  # 0 = 不限

    def test_pause_and_resume(self):
        from core import ai_usage

        until = ai_usage.pause(24)
        self.assertTrue(until)
        self.assertTrue(ai_usage.paused())
        self.assertEqual(ai_usage.pause_until(), until)
        ai_usage.record("primary", 10, 10, 1)
        ai_usage.resume()
        self.assertFalse(ai_usage.paused())
        self.assertEqual(ai_usage.used("primary"), 0)

    def test_both_exhausted_pauses_without_network(self):
        from core import ai_usage

        ai_usage.record("primary", 100, 100, 1)
        ai_usage.record("fallback", 100, 100, 1)
        cfg = {
            "llm": {
                "enabled": True, "provider": "openai_compatible",
                "base_url": "http://127.0.0.1:9/v1", "model": "m", "timeout_seconds": 1,
                "max_attempts": 1,
                "token_limits": {"primary": 50, "fallback": 50, "window_hours": 24},
                "fallback": {"enabled": True, "provider": "openai_compatible",
                             "base_url": "http://127.0.0.1:9/v1", "model": "f"},
            }
        }
        text, error = ai_client.chat_result([{"role": "user", "content": "x"}], cfg)
        self.assertIsNone(text)
        self.assertEqual((error or {}).get("code"), "paused")
        self.assertTrue(ai_usage.paused())

    def test_paused_short_circuits(self):
        from core import ai_usage

        ai_usage.pause(24)
        text, error = ai_client.chat_result(
            [{"role": "user", "content": "x"}],
            {"llm": {"enabled": True, "model": "m",
                     "token_limits": {"primary": 1000, "fallback": 1000, "window_hours": 24}}})
        self.assertIsNone(text)
        self.assertEqual((error or {}).get("code"), "paused")

    def test_summary_shape(self):
        from core import ai_usage

        ai_usage.record("primary", 100, 20, 2)
        data = ai_usage.summary({"primary": 1000, "fallback": 500}, 24)
        self.assertEqual(data["slots"]["primary"]["tokens"], 120)
        self.assertEqual(data["slots"]["primary"]["remaining"], 880)
        self.assertAlmostEqual(data["slots"]["primary"]["ratio"], 0.12)
        self.assertFalse(data["paused"])
        self.assertIn("history", data)

    def test_history_rollover(self):
        from core import ai_usage

        data = ai_usage.load()
        data["date"] = "2020-01-01"
        data["slots"]["primary"] = {"prompt": 70, "completion": 30, "calls": 2}
        ai_usage._write(data)
        refreshed = ai_usage.load()
        self.assertEqual(refreshed["slots"]["primary"]["prompt"], 0)
        self.assertEqual(refreshed["slots"]["primary"]["completion"], 0)
        self.assertTrue(refreshed["history"])
        self.assertEqual(refreshed["history"][-1]["primary"], 100)

    def test_save_persists_token_limits(self):
        from core import ai_api

        if ai_settings.CONFIG_PATH.exists():
            ai_settings.CONFIG_PATH.unlink()
        saved = ai_api._save({"llm": {"token_limits": {"primary": 20000, "fallback": 50000,
                                                      "window_hours": 12}}})
        self.assertEqual(saved["saved"]["token_limit_primary"], 20000)
        limits = ai_settings.load()["llm"]["token_limits"]
        self.assertEqual(limits["primary"], 20000)
        self.assertEqual(limits["window_hours"], 12)
        if ai_settings.CONFIG_PATH.exists():
            ai_settings.CONFIG_PATH.unlink()


class TestProviders(unittest.TestCase):
    """接口类型分三类：OpenAI 兼容 / Anthropic 原生 / 本地模型。"""

    def setUp(self):
        from core import ai_usage

        ai_usage.resume()
        ai_client.reset_slot()

    def test_three_providers(self):
        self.assertEqual(set(ai_client.PROVIDERS), {"openai", "anthropic", "local"})

    def test_old_values_are_mapped(self):
        self.assertEqual(ai_client.normalize_provider("openai_compatible"), "openai")
        self.assertEqual(ai_client.normalize_provider("ollama"), "local")
        self.assertEqual(ai_client.normalize_provider("anthropic"), "anthropic")
        self.assertEqual(ai_client.normalize_provider("nonsense"), "openai")

    def test_anthropic_request_shape(self):
        llm = {"provider": "anthropic", "model": "claude-sonnet-4-20250514",
               "base_url": "https://api.anthropic.com", "max_tokens": 700,
               "temperature": 0.2, "slot": "primary", "api_key_env": "X"}
        url, payload, headers = ai_client._build_request(
            llm,
            [{"role": "system", "content": "sys"},
             {"role": "user", "content": "hi"},
             {"role": "assistant", "content": "yo"},
             {"role": "user", "content": "again"}],
            json_mode=True)
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(payload["system"], "sys")
        self.assertEqual(payload["max_tokens"], 700)
        self.assertEqual([m["role"] for m in payload["messages"]], ["user", "assistant", "user"])
        self.assertIn("anthropic-version", headers)
        self.assertNotIn("response_format", payload)

    def test_anthropic_extract_and_usage(self):
        llm = {"provider": "anthropic"}
        data = {"content": [{"type": "text", "text": "hello"},
                            {"type": "text", "text": "world"}],
                "usage": {"input_tokens": 11, "output_tokens": 7}}
        self.assertEqual(ai_client._extract(llm, data), "hello\nworld")
        self.assertEqual(ai_client._usage_of(llm, data), (11, 7))

    def test_local_request_shape(self):
        llm = {"provider": "local", "model": "qwen2.5:7b",
               "base_url": "http://127.0.0.1:11434", "max_tokens": 500,
               "temperature": 0.2, "slot": "primary", "api_key_env": "X"}
        url, payload, _headers = ai_client._build_request(llm, [{"role": "user", "content": "hi"}], True)
        self.assertEqual(url, "http://127.0.0.1:11434/api/chat")
        self.assertTrue(payload["stream"] is False)
        self.assertEqual(payload["options"]["num_predict"], 500)
        self.assertEqual(payload["format"], "json")

    def test_openai_request_shape(self):
        llm = {"provider": "openai", "model": "deepseek-chat",
               "base_url": "https://api.deepseek.com", "max_tokens": 300,
               "temperature": 0.2, "slot": "primary", "api_key_env": "X"}
        url, payload, _headers = ai_client._build_request(llm, [{"role": "user", "content": "hi"}], True)
        self.assertEqual(url, "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(payload["max_tokens"], 300)
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_chat_result_ex_reports_serving_model(self):
        """主备切换后，调用方要能拿到真正服务的模型名。"""
        original = ai_client._attempt
        ai_client._attempt = lambda llm, messages, json_mode, count=True: (
            "ok-text", None, {"prompt": 1, "completion": 1, "calls": 1, "latency_ms": 1})
        try:
            cfg = {"llm": {"enabled": True, "provider": "openai",
                           "base_url": "http://127.0.0.1:9/v1", "model": "primary-model",
                           "token_limits": {"primary": 0, "fallback": 0, "window_hours": 24}}}
            text, error, used = ai_client.chat_result_ex([{"role": "user", "content": "x"}], cfg)
        finally:
            ai_client._attempt = original
        self.assertEqual(text, "ok-text")
        self.assertIsNone(error)
        self.assertEqual(used.get("model"), "primary-model")
        self.assertEqual(used.get("slot"), "primary")


class TestEndpointJoining(unittest.TestCase):
    """接口地址拼接：/v1 /v2 /v3 /v4 与完整端点都要能用（千帆、方舟、智谱）。"""

    def test_versioned_base_urls(self):
        join = ai_client._join_endpoint
        self.assertEqual(join("https://qianfan.baidubce.com/v2", "openai", "/chat/completions"),
                         "https://qianfan.baidubce.com/v2/chat/completions")
        self.assertEqual(join("https://ark.cn-beijing.volces.com/api/v3", "openai",
                              "/chat/completions"),
                         "https://ark.cn-beijing.volces.com/api/v3/chat/completions")
        self.assertEqual(join("https://open.bigmodel.cn/api/paas/v4", "openai",
                              "/chat/completions"),
                         "https://open.bigmodel.cn/api/paas/v4/chat/completions")

    def test_plain_and_v1_base_urls(self):
        join = ai_client._join_endpoint
        self.assertEqual(join("https://api.deepseek.com", "openai", "/chat/completions"),
                         "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(join("https://api.deepseek.com/v1", "openai", "/chat/completions"),
                         "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(join("https://openrouter.ai/api/v1", "openai", "/chat/completions"),
                         "https://openrouter.ai/api/v1/chat/completions")

    def test_full_endpoint_is_used_as_is(self):
        self.assertEqual(
            ai_client._join_endpoint("https://qianfan.baidubce.com/v2/chat/completions",
                                     "openai", "/chat/completions"),
            "https://qianfan.baidubce.com/v2/chat/completions")

    def test_qianfan_request_shape(self):
        llm = {"provider": "openai", "model": "ernie-5.1", "preset": "qianfan",
               "base_url": "https://qianfan.baidubce.com/v2", "max_tokens": 500,
               "temperature": 0.2, "slot": "primary", "key_ref": "qianfan", "api_key_env": "X"}
        url, payload, _headers = ai_client._build_request(
            llm, [{"role": "user", "content": "hi"}], True)
        self.assertEqual(url, "https://qianfan.baidubce.com/v2/chat/completions")
        self.assertEqual(payload["response_format"], {"type": "json_object"})


class TestErrorKinds(unittest.TestCase):
    """把「瞬时限流」「额度用尽」「配置错误」分开——决定切接口还是冷却。"""

    def test_qianfan_overdue_is_quota(self):
        body = json.dumps({"error": {"code": "account_overdue", "type": "access_denied",
                                     "message": "Access denied due to overdue account"},
                           "id": "as-xxx"})
        info = ai_client._classify(403, body, "ernie-5.1", "openai", "qianfan")
        self.assertEqual(info["code"], "account_overdue")
        self.assertEqual(info["request_id"], "as-xxx")
        self.assertEqual(info["kind"], "quota")
        self.assertIn("欠费", info["hint"])

    def test_qianfan_rpm_is_rate_limit(self):
        body = json.dumps({"error": {"code": "rpm_rate_limit_exceeded",
                                     "type": "rate_limit_exceeded",
                                     "message": "Rate limit reached for RPM"}})
        info = ai_client._classify(429, body, "ernie-5.1", "openai", "qianfan")
        self.assertEqual(info["kind"], "rate_limit")

    def test_qianfan_plan_quota_is_quota(self):
        body = json.dumps({"error": {"code": "coding_plan_month_quota_exceeded",
                                     "type": "quota_exceeded",
                                     "message": "month quota has been exceeded"}})
        info = ai_client._classify(429, body, "ernie-5.1", "openai", "qianfan")
        self.assertEqual(info["kind"], "quota")

    def test_auth_failure_is_config(self):
        info = ai_client._classify(
            401, '{"error":{"message":"Authentication Fails, Your api key: ****8416 is invalid"}}',
            "deepseek-chat", "openai", "deepseek")
        self.assertEqual(info["kind"], "config")
        self.assertEqual(ai_client.error_kind(info), "config")

    def test_content_policy_403_is_not_config(self):
        body = json.dumps({"error": {"code": "system_unsafe", "type": "unsafe_request",
                                     "message": "the content of system field is invalid"}})
        info = ai_client._classify(403, body, "m", "openai", "qianfan")
        self.assertNotEqual(info["kind"], "config")


class TestKeyRefsAndWarnings(unittest.TestCase):
    """Key 按服务商隔离，并对「Key 与地址不匹配」给出提醒。"""

    def setUp(self):
        for ref in ("openrouter", "qianfan", "deepseek", "primary", "fallback"):
            ai_secrets.set_api_key(None, ref)

    def tearDown(self):
        for ref in ("openrouter", "qianfan", "deepseek", "primary", "fallback"):
            ai_secrets.set_api_key(None, ref)

    def test_keys_are_isolated_per_provider(self):
        ai_secrets.set_api_key("sk-or-v1-aaaa1111", "openrouter")
        ai_secrets.set_api_key("bce-v3/ALTAK-x/yyy", "qianfan")
        self.assertEqual(ai_secrets.get_api_key(None, "openrouter")[0], "sk-or-v1-aaaa1111")
        self.assertEqual(ai_secrets.get_api_key(None, "qianfan")[0], "bce-v3/ALTAK-x/yyy")
        self.assertEqual(ai_secrets.env_name_for("qianfan"), "RADAR_LLM_API_KEY_QIANFAN")
        self.assertTrue(ai_secrets.has_api_key("qianfan"))

    def test_mismatched_key_is_flagged(self):
        from core import ai_api

        warn = ai_api._key_warning("sk-or-v1-abcdefgh", {"preset": "deepseek"})
        self.assertIn("OpenRouter", warn)
        warn = ai_api._key_warning("bce-v3/ALTAK-x/yyy", {"preset": "deepseek"})
        self.assertIn("千帆", warn)
        self.assertEqual(ai_api._key_warning("sk-abcdefgh", {"preset": "deepseek"}), "")
        self.assertIn("bce-v3/", ai_api._key_warning("sk-abcdefgh", {"preset": "qianfan"}))


class TestSlots(unittest.TestCase):
    """1..N 个接口：迁移、增删、失败穿透、冷却。"""

    def setUp(self):
        ai_client.reset_slot()
        ai_client.clear_slot_health()
        from core import ai_usage

        ai_usage.resume()

    def test_legacy_config_migrates_to_slots(self):
        cfg = {"llm": {
            "enabled": True, "provider": "openai", "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat", "api_key_env": "RADAR_LLM_API_KEY",
            "fallback": {"enabled": True, "provider": "openai",
                         "base_url": "https://openrouter.ai/api/v1", "model": "x/y:free"},
            "token_limits": {"primary": 100, "fallback": 200, "window_hours": 12},
        }}
        slots = ai_settings.slots_of(cfg)
        self.assertEqual([s["id"] for s in slots], ["primary", "fallback"])
        self.assertEqual(slots[0]["quota"]["limit"], 100)
        self.assertEqual(slots[0]["key_ref"], "primary")
        self.assertEqual(slots[1]["quota"]["limit"], 200)
        self.assertEqual(slots[1]["model"], "x/y:free")
        self.assertEqual(slots[1]["provider"], "openai")

    def test_three_slots_save_and_delete(self):
        if ai_settings.CONFIG_PATH.exists():
            ai_settings.CONFIG_PATH.unlink()
        ai_settings.save({"llm": {"enabled": True, "slots": [
            {"id": "primary", "name": "主接口", "enabled": True, "preset": "deepseek",
             "provider": "openai", "base_url": "https://api.deepseek.com",
             "model": "deepseek-chat", "key_ref": "deepseek", "priority": 1,
             "quota": {"mode": "daily", "limit": 500000}},
            {"id": "qianfan", "name": "千帆 Token Plan", "enabled": True, "preset": "qianfan",
             "provider": "openai", "base_url": "https://qianfan.baidubce.com/v2",
             "model": "ernie-5.1", "key_ref": "qianfan", "priority": 2,
             "quota": {"mode": "window", "limit": 100000000, "window_hours": 5}},
        ]}})
        cfg = ai_settings.load()
        ids = [s["id"] for s in cfg["llm"]["slots"]]
        self.assertEqual(ids[:2], ["primary", "qianfan"])
        quota = next(s for s in cfg["llm"]["slots"] if s["id"] == "qianfan")["quota"]
        self.assertEqual(quota["mode"], "window")
        self.assertEqual(quota["window_hours"], 5)
        # 兼容镜像仍然是主接口
        self.assertEqual(cfg["llm"]["model"], "deepseek-chat")
        raw = ai_settings.CONFIG_PATH.read_text(encoding="utf-8")
        self.assertNotIn("api_key", raw)

        ai_settings.save({"llm": {"delete_slots": ["fallback"]}})
        self.assertNotIn("fallback", [s["id"] for s in ai_settings.load()["llm"]["slots"]])
        self.assertIn("qianfan", [s["id"] for s in ai_settings.load()["llm"]["slots"]])
        if ai_settings.CONFIG_PATH.exists():
            ai_settings.CONFIG_PATH.unlink()

    def _cfg(self):
        return {"llm": {"enabled": True, "max_attempts": 1, "timeout_seconds": 2, "slots": [
            {"id": "primary", "name": "主", "enabled": True, "preset": "deepseek",
             "provider": "openai", "base_url": "http://127.0.0.1:9/v1", "model": "model-a",
             "key_ref": "k1", "priority": 1, "quota": {"mode": "daily", "limit": 0}},
            {"id": "backup", "name": "备", "enabled": True, "preset": "qianfan",
             "provider": "openai", "base_url": "http://127.0.0.1:9/v2", "model": "model-b",
             "key_ref": "k2", "priority": 2, "quota": {"mode": "total", "limit": 0}},
        ]}}

    def test_hard_error_falls_through_and_then_cools_down(self):
        seen = []

        def fake(llm, messages, json_mode, count=True):
            seen.append(llm["slot"])
            if llm["slot"] == "primary":
                return None, {"status": 401, "message": "Authentication Fails", "detail": "",
                              "hint": "", "retryable": False, "code": "", "kind": "config",
                              "model": llm["model"]}, {"prompt": 0, "completion": 0,
                                                       "calls": 0, "latency_ms": 1}
            return "ok-text", None, {"prompt": 10, "completion": 5, "calls": 1, "latency_ms": 1}

        original = ai_client._attempt
        ai_client._attempt = fake
        try:
            text, error, used = ai_client.chat_result_ex([{"role": "user", "content": "x"}],
                                                         self._cfg())
            self.assertEqual(text, "ok-text")
            self.assertIsNone(error)
            self.assertEqual(used["slot"], "backup")
            self.assertEqual(seen, ["primary", "backup"])
            self.assertTrue(ai_client.slot_blocked("primary"))
            # 第二次：坏的接口在冷却里，不再白跑那条 401
            seen.clear()
            text, _error, used = ai_client.chat_result_ex([{"role": "user", "content": "x"}],
                                                          self._cfg())
            self.assertEqual(seen, ["backup"])
            self.assertEqual(used["slot"], "backup")
        finally:
            ai_client._attempt = original
            ai_client.clear_slot_health()

    def test_all_slots_blocked_does_not_call_model(self):
        cfg = {"llm": {"enabled": True, "slots": [
            {"id": "only", "name": "唯一", "enabled": True, "provider": "openai",
             "base_url": "http://127.0.0.1:9/v1", "model": "m", "key_ref": "k",
             "priority": 1, "quota": {"mode": "daily", "limit": 0}}]}}
        ai_client.mark_slot_error("only", {"status": 401, "message": "bad key"})
        called = []
        original = ai_client._attempt
        ai_client._attempt = lambda *a, **k: called.append(1) or ("x", None, {})
        try:
            text, error, _used = ai_client.chat_result_full([{"role": "user", "content": "x"}],
                                                            cfg)
        finally:
            ai_client._attempt = original
            ai_client.clear_slot_health()
        self.assertIsNone(text)
        self.assertEqual((error or {}).get("code"), "cooldown")
        self.assertEqual(called, [])

    def test_quota_view_and_duplicate_warning(self):
        info = ai_client.describe({"llm": {"enabled": True, "slots": [
            {"id": "primary", "name": "A", "enabled": True, "provider": "openai",
             "base_url": "https://api.deepseek.com", "model": "deepseek-chat", "key_ref": "a",
             "priority": 1},
            {"id": "second", "name": "B", "enabled": True, "provider": "openai",
             "base_url": "https://api.deepseek.com", "model": "deepseek-chat", "key_ref": "b",
             "priority": 2},
        ]}})
        self.assertTrue(any("完全相同" in w for w in info["warnings"]))
        self.assertEqual([slot["id"] for slot in info["slots"]], ["primary", "second"])


class TestQuotaModes(unittest.TestCase):
    """三种额度模式：自然日 / 每 N 小时窗口 / 总量。"""

    def setUp(self):
        from core import ai_usage

        ai_usage.resume()

    def test_window_quota_resets_when_window_passes(self):
        import datetime as dt

        from core import ai_usage

        quota = {"mode": "window", "limit": 100, "window_hours": 5}
        ai_usage.record("w", 120, 0, 1, quota=quota)
        self.assertTrue(ai_usage.exhausted_quota("w", quota))
        data = ai_usage.load()
        data["windows"]["w"] = (dt.datetime.now().astimezone() -
                                dt.timedelta(hours=6)).isoformat(timespec="seconds")
        ai_usage._write(data)
        self.assertFalse(ai_usage.exhausted_quota("w", quota))
        self.assertEqual(ai_usage.quota_view("w", quota)["mode"], "window")

    def test_total_quota_uses_cumulative_usage(self):
        from core import ai_usage

        quota = {"mode": "total", "limit": 100, "window_hours": 24, "reset_days": 0}
        ai_usage.record("t", 120, 0, 1, quota=quota)
        self.assertTrue(ai_usage.exhausted_quota("t", quota))
        # 窗口清零（跨天）也不影响总量型额度
        data = ai_usage.load()
        data["slots"]["t"] = {"prompt": 0, "completion": 0, "calls": 0}
        ai_usage._write(data)
        self.assertEqual(ai_usage.used("t"), 0)
        self.assertEqual(ai_usage.used_total("t"), 120)
        self.assertTrue(ai_usage.exhausted_quota("t", quota))
        self.assertEqual(ai_usage.quota_view("t", quota)["tokens"], 120)

    def test_api_save_reports_key_mismatch(self):
        from core import ai_api

        if ai_settings.CONFIG_PATH.exists():
            ai_settings.CONFIG_PATH.unlink()
        try:
            out = ai_api._save({"slots": [{
                "id": "primary", "name": "主接口", "preset": "deepseek", "provider": "openai",
                "base_url": "https://api.deepseek.com", "model": "deepseek-chat",
                "key_ref": "deepseek", "key": "sk-or-v1-abcdefgh12345678", "priority": 1}]})
            self.assertEqual(out["api_key_slots"], ["primary"])
            self.assertTrue(out["warnings"])
            self.assertIn("OpenRouter", out["warnings"][0])
            self.assertTrue(ai_secrets.has_api_key("deepseek"))
            slots = out["settings"]["llm"]["slots"]
            self.assertEqual(slots[0]["key"]["ref"], "deepseek")
            self.assertTrue(slots[0]["key"]["configured"])
            self.assertNotIn("abcdefgh", slots[0]["key"]["masked"])
        finally:
            ai_secrets.set_api_key(None, "deepseek")
            if ai_settings.CONFIG_PATH.exists():
                ai_settings.CONFIG_PATH.unlink()


if __name__ == "__main__":
    unittest.main(verbosity=2)
