"""第二阶段测试：关键词语法、已读标记、排名轨迹、自定义 RSS、调度新行为。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401,E402

from core import keywords, seen  # noqa: E402
from core import db  # noqa: E402
from core.radar import config as rcfg, sources, store  # noqa: E402


class TestKeywordGrammar(unittest.TestCase):
    def test_plain_lines_become_any_rules(self):
        groups = keywords.parse(["AI", "芯片"])
        self.assertEqual(len(groups), 1)
        self.assertEqual([t["value"] for t in groups[0]["any"]], ["ai", "芯片"])

    def test_group_sections_start_new_group(self):
        groups = keywords.parse(["[科技]", "+芯片", "[财经]", "+利率"])
        self.assertEqual([g["name"] for g in groups], ["科技", "财经"])

    def test_exclude_wins(self):
        groups = keywords.parse(["[科技]", "+芯片", "!抽奖"])
        self.assertTrue(keywords.excluded(groups, "芯片抽奖活动"))
        self.assertFalse(keywords.excluded(groups, "芯片发布"))

    def test_must_token_required(self):
        groups = keywords.parse(["[科技]", "+芯片"])
        self.assertTrue(keywords.match(groups, "国产芯片突破"))
        self.assertEqual(keywords.match(groups, "国产大飞机"), [])

    def test_regex_and_alias(self):
        groups = keywords.parse([r"/\b(llm|gpt)\b/ => 大模型"])
        token = groups[0]["any"][0]
        self.assertEqual(token["kind"], "regex")
        self.assertEqual(token["alias"], "大模型")
        self.assertTrue(keywords.match(groups, "new gpt release"))
        self.assertFalse(keywords.match(groups, "newsletter"))

    def test_invalid_regex_is_ignored(self):
        groups = keywords.parse(["/[/"])
        self.assertEqual(groups, [])

    def test_limit_directive(self):
        groups = keywords.parse(["[科技]", "+芯片", "@5"])
        self.assertEqual(groups[0]["limit"], 5)

    def test_comments_and_blank_lines_skipped(self):
        groups = keywords.parse(["", "# 注释", "AI"])
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["any"]), 1)


class TestKeywordFiltering(unittest.TestCase):
    def test_exclude_rule_drops_items(self):
        from core.radar import sources as radar_sources

        cfg = {"filters": {"rules": ["!抽奖"]}, "limits": {}}
        spec = {"source_id": "x", "kind": "hotlist", "max_items": 10}
        items = [
            {"title": "正常新闻", "raw_content": "", "engagement": {}},
            {"title": "转发抽奖送手机", "raw_content": "", "engagement": {}},
        ]
        kept, noise, low, truncated = radar_sources.filter_items(spec, items, cfg)
        self.assertEqual([i["title"] for i in kept], ["正常新闻"])
        self.assertEqual(noise, 1)

    def test_must_rule_keeps_only_matching(self):
        from core.radar import sources as radar_sources

        cfg = {"filters": {"rules": ["+芯片"]}, "limits": {}}
        spec = {"source_id": "x", "kind": "hotlist", "max_items": 10}
        items = [
            {"title": "国产芯片突破", "raw_content": "", "engagement": {}},
            {"title": "足球比赛结果", "raw_content": "", "engagement": {}},
        ]
        kept, _noise, _low, _t = radar_sources.filter_items(spec, items, cfg)
        self.assertEqual([i["title"] for i in kept], ["国产芯片突破"])


class TestSeenState(unittest.TestCase):
    def setUp(self):
        conn = db.connect()
        try:
            conn.execute("DELETE FROM seen_items")
            conn.commit()
        except Exception:  # noqa: BLE001
            pass
        finally:
            conn.close()

    def test_annotate_marks_new_then_seen(self):
        items = [{"full_name": "a/b"}, {"full_name": "c/d"}]
        fresh = seen.annotate("github", items, "full_name")
        self.assertEqual(fresh, 2)
        self.assertTrue(all(i["is_new"] for i in items))

        seen.mark("github", ["a/b"])
        again = [{"full_name": "a/b"}, {"full_name": "c/d"}]
        fresh2 = seen.annotate("github", again, "full_name")
        self.assertEqual(fresh2, 1)
        self.assertFalse(again[0]["is_new"])
        self.assertTrue(again[1]["is_new"])

    def test_kinds_are_isolated(self):
        seen.mark("radar", ["st-1"])
        items = [{"story_id": "st-1"}]
        seen.annotate("github", [{"full_name": "st-1"}], "full_name")
        self.assertTrue(seen.annotate("radar", items, "story_id") == 0)
        self.assertFalse(items[0]["is_new"])


class TestRankHistory(unittest.TestCase):
    def setUp(self):
        store.init()
        conn = db.connect()
        try:
            conn.execute("DELETE FROM radar_rank_history")
            conn.commit()
        finally:
            conn.close()

    def test_record_and_trend_up(self):
        conn = db.connect()
        try:
            items = [{"source_id": "weibo_hot", "url_hash": "h1", "engagement": {"rank": 8}}]
            store.record_ranks(conn, items, "2026-10-23", "2026-10-23T08:00:00+08:00")
            items = [{"source_id": "weibo_hot", "url_hash": "h1", "engagement": {"rank": 3}}]
            store.record_ranks(conn, items, "2026-10-23", "2026-10-23T09:00:00+08:00")
            conn.commit()
            trend = store.rank_map(conn, ["h1"])["h1"]
        finally:
            conn.close()
        self.assertEqual(trend["rank"], 3)
        self.assertEqual(trend["prev_rank"], 8)
        self.assertEqual(trend["delta"], 5)

    def test_items_without_rank_are_skipped(self):
        conn = db.connect()
        try:
            n = store.record_ranks(conn, [{"source_id": "s", "url_hash": "h2", "engagement": {}}],
                                   "2026-10-23", "t")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(n, 0)


class TestCustomRssFeeds(unittest.TestCase):
    def test_config_feed_becomes_source_spec(self):
        cfg = {"rss": {"feeds": [
            {"id": "hn", "name": "Hacker News", "url": "https://example.com/rss",
             "platform": "HN", "language": "en", "max_items": 15},
        ]}}
        specs = sources.active_specs(cfg)
        spec = next(s for s in specs if s["source_id"] == "rss_hn")
        self.assertEqual(spec["adapter"], "rss")
        self.assertEqual(spec["url"], "https://example.com/rss")
        self.assertEqual(spec["max_items"], 15)

    def test_incomplete_feed_is_ignored(self):
        cfg = {"rss": {"feeds": [{"id": "broken", "url": ""}]}}
        ids = [s["source_id"] for s in sources.active_specs(cfg)]
        self.assertNotIn("rss_broken", ids)


class TestScheduleDefaults(unittest.TestCase):
    def test_hourly_collect_daily_push(self):
        cfg = rcfg.load()
        schedule = cfg["schedule"]
        self.assertEqual(schedule["collect_interval_minutes"], 60)
        self.assertEqual(schedule["daily_time"], "08:00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
