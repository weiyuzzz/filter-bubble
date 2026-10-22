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


if __name__ == "__main__":
    unittest.main(verbosity=2)
