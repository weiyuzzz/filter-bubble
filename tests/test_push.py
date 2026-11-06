"""推送开关、速读回退、关注搜索的测试。离线：不发出任何真实请求。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401,E402  (isolated DB + project root on sys.path)

from core import ai_api, ai_settings, brief, config, db, github, notify  # noqa: E402


def _write_settings(data):
    ai_settings.CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class PushBase(unittest.TestCase):
    def setUp(self):
        self.original_key = config.SERVERCHAN_SENDKEY
        self.original_path = ai_settings.CONFIG_PATH
        config.SERVERCHAN_SENDKEY = "SCT" + "x" * 20

    def tearDown(self):
        config.SERVERCHAN_SENDKEY = self.original_key
        if self.original_path.exists():
            self.original_path.unlink()

    def _set_switch(self, enabled):
        _write_settings({"version": 1, "push": {"enabled": enabled}})


class TestPushState(PushBase):
    def test_ready_when_switch_on_and_key_present(self):
        self._set_switch(True)
        state = notify.state()
        self.assertTrue(state["ready"])
        self.assertEqual(state["reason"], "")
        self.assertEqual(state["provider"], "Server酱 Turbo")
        self.assertEqual(state["time"], config.REFRESH_TIME)
        self.assertGreaterEqual(state["items"], 1)

    def test_switch_off_blocks_push_with_reason(self):
        self._set_switch(False)
        state = notify.state()
        self.assertFalse(state["ready"])
        self.assertFalse(state["enabled"])
        self.assertIn("开关", state["reason"])

    def test_missing_key_blocks_push(self):
        self._set_switch(True)
        config.SERVERCHAN_SENDKEY = ""
        state = notify.state()
        self.assertFalse(state["ready"])
        self.assertFalse(state["configured"])
        self.assertIn("SERVERCHAN_SENDKEY", state["reason"])

    def test_sctp_key_is_rejected_with_a_clear_reason(self):
        self._set_switch(True)
        config.SERVERCHAN_SENDKEY = "sctp1234567890"
        state = notify.state()
        self.assertFalse(state["ready"])
        self.assertIn("Server酱³", state["reason"])

    def test_switch_off_is_authoritative_even_for_manual_send(self):
        """开关关掉时连 force 也不发，否则用户会以为还在推。"""
        self._set_switch(False)
        conn = db.connect()
        try:
            with mock.patch.object(notify, "_post") as post:
                result = notify.send_daily({"date": "2026-10-26", "items": []}, conn=conn, force=True)
            post.assert_not_called()
        finally:
            conn.close()
        self.assertFalse(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertIn("开关", result["reason"])

    def test_status_exposes_switch_and_last_result(self):
        self._set_switch(True)
        db.init()
        conn = db.connect()
        try:
            status = notify.status(conn)
        finally:
            conn.close()
        for key in ("enabled", "configured", "ready", "reason", "provider",
                    "time", "items", "status", "date", "last_pushed_date"):
            self.assertIn(key, status)


class TestSettingsApi(PushBase):
    def test_switch_round_trips_through_the_settings_api(self):
        status, payload = ai_api.handle_post("/api/ai/settings", {"push": {"enabled": False}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["saved"]["push_enabled"], False)
        self.assertFalse(payload["settings"]["push"]["enabled"])
        self.assertFalse(payload["settings"]["push"]["ready"])
        on_disk = json.loads(ai_settings.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertFalse(on_disk["push"]["enabled"])

    def test_public_payload_reports_why_push_is_unavailable(self):
        self._set_switch(False)
        push = ai_api.handle_get("/api/ai/settings", {})[1]["push"]
        self.assertFalse(push["enabled"])
        self.assertFalse(push["ready"])
        self.assertIn("开关", push["reason"])
        self.assertTrue(push["configured"])  # key 还在，只是被开关挡住

    def test_enabling_again_restores_ready(self):
        ai_api.handle_post("/api/ai/settings", {"push": {"enabled": False}})
        status, payload = ai_api.handle_post("/api/ai/settings", {"push": {"enabled": True}})
        self.assertEqual(status, 200)
        self.assertTrue(payload["settings"]["push"]["enabled"])
        self.assertTrue(payload["settings"]["push"]["ready"])


class TestWiringGuards(unittest.TestCase):
    """防止回退到「只看环境变量」的旧行为，或让失败的推送每 25 秒重试。"""

    ROOT = Path(__file__).resolve().parent.parent

    def test_digest_gate_uses_the_switch_not_the_env(self):
        source = (self.ROOT / "core" / "digest.py").read_text(encoding="utf-8")
        self.assertIn("notify.state()", source)
        self.assertNotIn("config.PUSH_ENABLED", source)

    def test_failed_push_is_recorded_so_the_scheduler_does_not_loop(self):
        digest_src = (self.ROOT / "core" / "digest.py").read_text(encoding="utf-8")
        app_src = (self.ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("last_push_attempt_date", digest_src)
        self.assertIn("last_push_attempt_date", app_src)

    def test_scheduler_asks_notify_before_the_daily_push(self):
        source = (self.ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("push_state[\"ready\"]", source)


class TestBriefFallback(unittest.TestCase):
    """没有大模型时只显示 GitHub 简介，不再套模板编解读。"""

    def test_uses_the_github_description(self):
        result = brief.build({"description": "一个本地优先的笔记工具",
                              "language": "Rust", "topics": ["notes"]}, ["效率 / 自动化"])
        self.assertEqual(result["source"], brief.SOURCE_GITHUB)
        self.assertEqual(result["what"], "一个本地优先的笔记工具")
        self.assertEqual(result["detail"], "")
        self.assertEqual(result["highlights"], [])
        self.assertEqual(result["limits"], "")

    def test_removed_template_fields_are_gone(self):
        result = brief.build({"description": "x"}, ["开发者工具"])
        for field in ("audience", "problem", "usage", "flow", "scenarios", "difficulty"):
            self.assertNotIn(field, result, "模板字段应已删除：%s" % field)

    def test_prefers_the_translated_summary_when_present(self):
        result = brief.build({"summary_zh": "中文简介", "description": "english desc"}, [])
        self.assertEqual(result["what"], "中文简介")

    def test_empty_description_says_so_instead_of_inventing(self):
        result = brief.build({"description": ""}, [])
        self.assertEqual(result["what"], brief.NO_DESCRIPTION)
        self.assertIn("GitHub", result["note"])


class TestPushBodyMarker(unittest.TestCase):
    """简报里必须标明这条解读是不是大模型写的。"""

    def _payload(self, brief_data):
        return {
            "date": "2026-10-26",
            "items": [{
                "full_name": "acme/tool", "owner": "acme", "name": "tool", "rank": 1,
                "heat": 88, "url": "https://github.com/acme/tool", "brief": brief_data,
            }],
            "stats": {"total": 1, "daily": 1, "gained": 0},
        }

    def setUp(self):
        db.init()
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()

    def test_ai_brief_is_labelled(self):
        body = notify.render(self._payload({
            "source": "ai", "ai_model": "deepseek-flash", "what": "一个工具",
            "detail": "它解决什么问题、有什么特点。", "highlights": ["需要 Docker"],
            "limits": "仅支持 Linux",
        }), self.conn)["body"]
        self.assertIn("AI 解读", body)
        self.assertIn("需要 Docker", body)
        self.assertIn("仅支持 Linux", body)

    def test_fallback_brief_is_labelled_as_github(self):
        body = notify.render(self._payload({
            "source": "github", "what": "GitHub 上的一句话简介",
        }), self.conn)["body"]
        self.assertIn("GitHub 简介", body)
        self.assertNotIn("AI 解读", body)

    def test_removed_sections_no_longer_appear(self):
        body = notify.render(self._payload({"source": "github", "what": "x"}), self.conn)["body"]
        for gone in ("适合谁", "完整使用流程", "典型场景", "解决什么问题"):
            self.assertNotIn(gone, body, "旧模板段落仍然出现在简报里：%s" % gone)


class TestGithubUserSearch(unittest.TestCase):
    def setUp(self):
        self.original_get = github.http_get
        self.original_rate = github.RATE_REMAINING

    def tearDown(self):
        github.http_get = self.original_get
        github.RATE_REMAINING = self.original_rate

    def _patch(self, payload):
        github.RATE_REMAINING = 100
        raw = json.dumps(payload).encode()
        github.http_get = lambda url, **kwargs: raw

    def test_parses_users_and_organisations(self):
        self._patch({"items": [
            {"login": "torvalds", "type": "User", "html_url": "https://github.com/torvalds",
             "avatar_url": "https://avatars/t.png"},
            {"login": "facebook", "type": "Organization",
             "html_url": "https://github.com/facebook", "avatar_url": "https://avatars/f.png"},
        ]})
        rows = github.search_users("linux")
        self.assertEqual([r["login"] for r in rows], ["torvalds", "facebook"])
        self.assertEqual(rows[0]["type"], "User")
        self.assertEqual(rows[1]["type"], "Organization")

    def test_missing_avatar_falls_back_to_github_png(self):
        self._patch({"items": [{"login": "octocat", "type": "User"}]})
        self.assertIn("octocat.png", github.search_users("octo")[0]["avatar"])

    def test_empty_query_skips_the_request(self):
        def boom(*args, **kwargs):
            raise AssertionError("空查询不该发请求")

        github.http_get = boom
        self.assertEqual(github.search_users("   "), [])

    def test_rate_limit_guard_skips_the_request(self):
        github.RATE_REMAINING = 0

        def boom(*args, **kwargs):
            raise AssertionError("额度不足时不该发请求")

        github.http_get = boom
        self.assertEqual(github.search_users("linux"), [])

    def test_broken_json_or_http_failure_returns_empty(self):
        github.RATE_REMAINING = 100
        github.http_get = lambda url, **kwargs: b"{not json"
        self.assertEqual(github.search_users("linux"), [])
        github.http_get = lambda url, **kwargs: None
        self.assertEqual(github.search_users("linux"), [])

    def test_skips_entries_without_a_login(self):
        self._patch({"items": [{"type": "User"}, {"login": "ok", "type": "User"}]})
        self.assertEqual([r["login"] for r in github.search_users("x")], ["ok"])


if __name__ == "__main__":
    unittest.main()