"""全局 AI 层测试：配置、密钥、错误分类、项目解读缓存。全部离线。"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401,E402

from core import ai_brief, ai_client, ai_secrets, ai_settings, db  # noqa: E402


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
        self.assertEqual(cfg["ai"]["briefs_per_run"], 12)

    def test_save_and_reload(self):
        ai_settings.save({"llm": {"enabled": True, "provider": "openai_compatible",
                                  "base_url": "https://openrouter.ai/api/v1",
                                  "model": "google/gemma-4-26b-a4b-it:free"},
                          "ai": {"briefs_per_run": 5, "concurrency": 1}})
        cfg = ai_settings.load()
        self.assertTrue(cfg["llm"]["enabled"])
        self.assertEqual(cfg["llm"]["model"], "google/gemma-4-26b-a4b-it:free")
        self.assertEqual(cfg["ai"]["briefs_per_run"], 5)

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

    def test_normalize_trims_lists(self):
        data = ai_brief._normalize({
            "what": "一个工具", "audience": "开发者", "problem": "重复劳动",
            "usage": "pip install", "flow": ["a", "b", "c", "d", "e"],
            "scenarios": ["s1", "s2", "s3", "s4"], "difficulty": "低", "limits": "无",
        })
        self.assertEqual(len(data["flow"]), 4)
        self.assertEqual(len(data["scenarios"]), 3)

    def test_apply_merges_and_marks_ai(self):
        item = {"brief": {"what": "规则版", "audience": "旧值"}, "full_name": "a/b"}
        ai_brief.apply(item, {"what": "AI 版", "audience": "新值"}, "model-x")
        self.assertEqual(item["brief"]["what"], "AI 版")
        self.assertTrue(item["brief"]["ai"])
        self.assertEqual(item["brief"]["ai_model"], "model-x")

    def test_enrich_is_noop_when_disabled(self):
        items = [{"full_name": "a/b", "brief": {}}]
        stats = ai_brief.enrich(items, {"llm": {"enabled": False}})
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["generated"], 0)
        self.assertNotIn("ai", items[0]["brief"])

    def test_cache_hit_does_not_call_model(self):
        settings = {"llm": {"enabled": True, "provider": "openai_compatible",
                            "base_url": "http://127.0.0.1:9/v1", "model": "cache-model",
                            "timeout_seconds": 1, "max_attempts": 1},
                    "ai": {"enable_briefs": True, "briefs_per_run": 5, "concurrency": 1}}
        item = {"full_name": "cache/test", "brief": {}}
        conn = ai_brief._conn()
        try:
            ai_brief._save(conn, ai_brief._key(item, "cache-model"), item, "cache-model",
                           {"what": "缓存里的解读", "audience": "", "problem": "", "usage": "",
                            "flow": [], "scenarios": [], "difficulty": "", "limits": ""})
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
        ai_client._attempt = lambda llm, messages, json_mode: ("ok-text", None)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
