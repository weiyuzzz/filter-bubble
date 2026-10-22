"""Regression tests: the existing GitHub digest must behave exactly as before.

These assert on the shipped source and schema, so they fail loudly if anyone
rewrites or removes the original GitHub pipeline while adding the radar module.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401,E402

from core import config, db, digest, prefs  # noqa: E402
from core.radar import store as radar_store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
DB_SOURCE = (ROOT / "core" / "db.py").read_text(encoding="utf-8")


def setUpModule():
    db.init()

ORIGINAL_TABLES = [
    "repos", "snapshots", "digests", "feedback", "weights", "bookmarks",
    "translations", "follows", "follow_events", "push_log", "readmes", "meta",
]

ORIGINAL_ROUTES = [
    "/api/health", "/api/status", "/api/digest", "/api/prefs", "/api/push",
    "/api/follows", "/api/refresh", "/api/feedback", "/api/bookmark", "/api/follow",
    "/api/follows/refresh", "/api/rerank", "/api/reset",
]


class TestOriginalConfigUntouched(unittest.TestCase):
    def test_github_defaults(self):
        self.assertEqual(config.HOST, "127.0.0.1")
        self.assertEqual(config.PORT, 8791)
        self.assertEqual(config.REFRESH_TIME, "08:00")
        self.assertEqual(config.TIMEZONE, "Asia/Shanghai")
        self.assertEqual(config.MAX_ITEMS, 30)
        self.assertTrue(config.DB_PATH.name.endswith(".db"))

    def test_data_dir_defaults_to_project_data(self):
        config_src = (ROOT / "core" / "config.py").read_text(encoding="utf-8")
        self.assertIn('BASE_DIR / "data"', config_src)


class TestOriginalSchemaUntouched(unittest.TestCase):
    def test_all_original_tables_still_declared(self):
        db.init()
        conn = db.connect()
        try:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        finally:
            conn.close()
        names = {r["name"] for r in rows}
        for table in ORIGINAL_TABLES:
            self.assertIn(table, names, "原始表被删除了：%s" % table)

    def test_radar_tables_are_additive_only(self):
        for table in ORIGINAL_TABLES:
            self.assertNotIn("radar_", table)
        self.assertIn("CREATE TABLE IF NOT EXISTS repos", DB_SOURCE)
        self.assertNotIn("radar_", DB_SOURCE, "radar 表不应写进原有 db.SCHEMA")

    def test_radar_init_creates_only_radar_tables(self):
        radar_store.init()
        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'radar_%'"
            ).fetchall()
        finally:
            conn.close()
        names = {r["name"] for r in rows}
        for expected in ("radar_sources", "radar_items", "radar_events", "radar_claims",
                         "radar_evidence", "radar_claim_evidence", "radar_reports",
                         "radar_push_log", "radar_runs"):
            self.assertIn(expected, names)


class TestOriginalRoutesStillServed(unittest.TestCase):
    def test_every_github_route_still_present(self):
        for route in ORIGINAL_ROUTES:
            self.assertIn('"%s"' % route, APP_SOURCE, "原有接口被删除：%s" % route)

    def test_radar_routes_are_prefixed(self):
        for match in re.findall(r'"(/api/radar[a-z/]*)"', APP_SOURCE):
            self.assertTrue(match.startswith("/api/radar"))

    def test_radar_is_wrapped_so_it_cannot_break_github_flow(self):
        self.assertIn("def _radar(", APP_SOURCE)
        self.assertGreaterEqual(APP_SOURCE.count("except Exception"), 1)
        # radar import must be inside a try/except
        index = APP_SOURCE.find("from core.radar import api as radar_api")
        self.assertGreater(index, 0)
        window = APP_SOURCE[max(0, index - 200):index]
        self.assertIn("try:", window)


class TestOriginalDigestPipelineIntact(unittest.TestCase):
    def test_module_imports_and_time_helpers_work(self):
        self.assertTrue(hasattr(digest, "generate"))
        self.assertTrue(hasattr(digest, "refresh_async"))
        self.assertTrue(hasattr(digest, "rerank"))
        self.assertTrue(hasattr(digest, "latest"))
        self.assertIsInstance(digest.local_today().isoformat(), str)
        self.assertIsNotNone(digest.local_now())

    def test_state_keys_unchanged(self):
        state = digest.get_state()
        for key in ("running", "stage", "started_at", "finished_at", "last_error"):
            self.assertIn(key, state)

    def test_prefs_module_still_works(self):
        conn = db.connect()
        try:
            profile = prefs.profile(conn)
        finally:
            conn.close()
        self.assertIn("ready", profile)
        self.assertIn("languages", profile)

    def test_github_prompt_scheduler_thread_name_unchanged(self):
        self.assertIn('name="filter-bubble-scheduler"', APP_SOURCE)
        self.assertIn("def _scheduler(", APP_SOURCE)

    def test_radar_scheduler_is_a_separate_thread(self):
        self.assertIn('name="radar-scheduler"', (ROOT / "core" / "radar" / "service.py").read_text(encoding="utf-8"))
        self.assertIn("start_scheduler", APP_SOURCE)

    def test_digest_table_readable(self):
        conn = db.connect()
        try:
            row = conn.execute("SELECT COUNT(*) c FROM digests").fetchone()
        finally:
            conn.close()
        self.assertGreaterEqual(row["c"], 0)


class TestNoCrossContamination(unittest.TestCase):
    def test_radar_does_not_touch_github_meta_keys(self):
        service_src = (ROOT / "core" / "radar" / "service.py").read_text(encoding="utf-8")
        for key in ("last_generated_date", "last_refresh_failed_at", "profile_updated"):
            pattern = r'(set_meta|get_meta)\(\s*conn\s*,\s*"%s"' % re.escape(key)
            self.assertIsNone(re.search(pattern, service_src),
                              "radar 不应读写 GitHub 的状态键：%s" % key)
        self.assertNotIn("INSERT INTO digests", service_src)

    def test_radar_does_not_import_app_or_rewrite_config(self):
        for name in ("report.py", "service.py", "investigate.py", "api.py"):
            src = (ROOT / "core" / "radar" / name).read_text(encoding="utf-8")
            self.assertNotIn("import app", src)
            self.assertNotIn("SET GLOBALS", src.upper())
            self.assertNotIn("config.PORT =", src)
            self.assertNotIn("config.REFRESH_TIME =", src)

    def test_radar_tables_do_not_collide_with_github_tables(self):
        actual = (ROOT / "core" / "radar" / "store.py").read_text(encoding="utf-8")
        for table in ORIGINAL_TABLES:
            self.assertNotRegex(actual, r"CREATE TABLE IF NOT EXISTS %s\b" % table)


if __name__ == "__main__":
    unittest.main(verbosity=2)
