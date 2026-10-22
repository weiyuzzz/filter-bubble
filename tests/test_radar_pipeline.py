"""Pipeline tests: clustering, story layer, value filtering, backoff, summarisation.

All offline: adapters are replaced with fakes, the LLM is replaced with a local stub.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401,E402

from core import db  # noqa: E402
from core.radar import cluster, config as rcfg, llm, report, service, sources, store, summarize  # noqa: E402


def _item(source_id="s1", platform="平台A", title="标题", url=None, source_type="news",
          engagement=None, published="2026-10-23T09:00:00+08:00"):
    spec = {"source_id": source_id, "platform": platform, "language": "zh", "source_type": source_type}
    item = sources.make_item(spec, title=title, url=url or "https://example.com/%s" % abs(hash(title)),
                             published_at=published, raw_content=title, engagement=engagement or {})
    item["id"] = None
    return item


def _cfg(**overrides):
    cfg = rcfg.load()
    cfg.update(overrides)
    return cfg


def _reset_tables():
    store.init()
    conn = db.connect()
    try:
        for table in ("radar_events", "radar_stories", "radar_items", "radar_summaries",
                      "radar_sources", "radar_event_snapshots"):
            conn.execute("DELETE FROM %s" % table)
        conn.commit()
    finally:
        conn.close()


class TestClusterThreshold(unittest.TestCase):
    def test_near_duplicate_titles_merge_across_runs(self):
        """0.55 的旧阈值会把相似度 0.53 的同一件事拆成两个事件。"""
        _reset_tables()
        conn = db.connect()
        try:
            first = _item(title="西藏那曲市巴青县发生7.0级地震，震源深度10千米")
            store.insert_items(conn, [first])
            rows = conn.execute("SELECT * FROM radar_items").fetchall()
            events = cluster.build_events(conn, "2026-10-23",
                                          [dict(r) for r in rows], _cfg(), "2026-10-23T10:00:00+08:00")
            self.assertEqual(len(events), 1)
            store.upsert_event(conn, events[0])
            conn.commit()

            # 第二次采集：措辞略有不同
            second = _item(title="西藏那曲巴青县发生7.0级地震",
                           url="https://example.com/second")
            events2 = cluster.build_events(conn, "2026-10-23", [second],
                                           _cfg(), "2026-10-23T11:00:00+08:00")
        finally:
            conn.close()
        self.assertEqual(len(events2), 1)
        self.assertEqual(events2[0]["id"], events[0]["id"], "跨轮应复用同一个事件 id")

    def test_same_run_groups_join_the_match_pool(self):
        """同一轮里新建的事件必须立刻可被后续分组匹配。"""
        _reset_tables()
        conn = db.connect()
        try:
            items = [
                _item(title="某公司发布新款芯片 性能提升", url="https://a.example/1"),
                _item(title="某公司发布新款芯片 性能提升明显", url="https://b.example/2"),
            ]
            cfg = _cfg(cluster={"within_run_threshold": 0.30, "match_threshold": 0.35,
                                "story_threshold": 0.42})
            events = cluster.build_events(conn, "2026-10-23", items, cfg, "2026-10-23T10:00:00+08:00")
        finally:
            conn.close()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["item_count"], 2)


class TestStoryLayer(unittest.TestCase):
    def _events(self, titles):
        return [{"id": "ev-%d" % i, "title": t, "heat": 50 - i, "importance": 40,
                 "item_count": 2, "platform_count": 2, "confidence": "reported",
                 "category": "", "metrics": {"platforms": ["微博"]},
                 "first_seen_at": "2026-10-23T09:00:00+08:00"}
                for i, t in enumerate(titles)]

    def test_angles_of_same_story_merge(self):
        events = self._events([
            "西藏那曲市巴青县发生7.0级地震，震源深度10千米",
            "那曲地震为震中近5年最大地震",
            "国乒女团决赛不敌日本",
        ])
        stories = cluster.build_stories(db.connect(), "2026-10-23", events, _cfg(),
                                        "2026-10-23T10:00:00+08:00")
        quake = [s for s in stories if "地震" in s["title"] or "地震" in str(s["metrics"])]
        self.assertTrue(quake)
        self.assertGreaterEqual(max(s["event_count"] for s in quake), 2)

    def test_unrelated_stories_stay_separate(self):
        events = self._events(["星巴克推出新门店政策", "国乒女团决赛不敌日本"])
        stories = cluster.build_stories(db.connect(), "2026-10-23", events, _cfg(),
                                        "2026-10-23T10:00:00+08:00")
        self.assertEqual(len(stories), 2)

    def test_story_ids_are_unique_and_stable(self):
        events = self._events(["甲事件发生", "乙事件发生", "甲事件发生后续"])
        stories = cluster.build_stories(db.connect(), "2026-10-23", events, _cfg(),
                                        "2026-10-23T10:00:00+08:00")
        ids = [s["id"] for s in stories]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_event_gets_a_story(self):
        events = self._events(["A 事件", "B 事件", "C 事件"])
        cluster.build_stories(db.connect(), "2026-10-23", events, _cfg(), "2026-10-23T10:00:00+08:00")
        self.assertTrue(all(e.get("story_id") for e in events))


class TestValueFilters(unittest.TestCase):
    def test_noise_keyword_dropped(self):
        cfg = _cfg()
        spec = {"source_id": "weibo_hot", "platform": "微博", "kind": "hotlist"}
        items = [_item(title="吃不吃挑战 第三期"), _item(title="真正的技术讲解", url="https://x.example/2")]
        kept, noise, low, truncated = sources.filter_items(spec, items, cfg)
        self.assertEqual(noise, 1)
        self.assertEqual(len(kept), 1)

    def test_min_engagement_filter(self):
        cfg = _cfg()
        spec = {"source_id": "bilibili_popular", "platform": "哔哩哔哩", "kind": "hotlist"}
        items = [
            _item(title="低播放视频", url="https://x.example/low", engagement={"view": 100}),
            _item(title="高播放视频", url="https://x.example/high", engagement={"view": 900000}),
        ]
        kept, noise, low, truncated = sources.filter_items(spec, items, cfg)
        self.assertEqual(low, 1)
        self.assertEqual(len(kept), 1)

    def test_hotlist_subsampling(self):
        cfg = _cfg(limits={"hotlist_top_n": 3, "news_top_n": 40})
        spec = {"source_id": "weibo_hot", "platform": "微博", "kind": "hotlist"}
        items = [_item(title="热搜 %d" % i, url="https://x.example/%d" % i) for i in range(10)]
        kept, noise, low, truncated = sources.filter_items(spec, items, cfg)
        self.assertEqual(len(kept), 3)
        self.assertEqual(truncated, 7)

    def test_news_source_is_not_subsampled_to_hotlist_limit(self):
        cfg = _cfg(limits={"hotlist_top_n": 3, "news_top_n": 40})
        spec = {"source_id": "cn_scroll", "platform": "中国新闻网", "kind": "rss"}
        items = [_item(title="新闻 %d" % i, url="https://x.example/n%d" % i) for i in range(12)]
        kept, _, _, _ = sources.filter_items(spec, items, cfg)
        self.assertEqual(len(kept), 12)


class TestSourceBackoff(unittest.TestCase):
    def _spec(self, source_id="flaky"):
        return {"source_id": source_id, "platform": "测试", "name": "测试源",
                "kind": "rss", "adapter": "okay", "url": "", "enabled": True}

    def _install_fakes(self):
        original_adapters = dict(sources.ADAPTERS)
        original_sources = sources.SOURCES

        def boom(spec, make_item):
            raise RuntimeError("simulated failure")

        def okay(spec, make_item):
            return [make_item(spec, title="正常内容 %s" % spec["source_id"])]

        sources.ADAPTERS["boom"] = boom
        sources.ADAPTERS["okay"] = okay
        return original_adapters, original_sources

    def test_source_paused_after_repeated_failures(self):
        _reset_tables()
        original_adapters, original_sources = self._install_fakes()
        conn = db.connect()
        try:
            sources.SOURCES = [dict(self._spec(), adapter="boom")]
            cfg = _cfg(backoff={"degrade_after": 2, "pause_after": 3, "degrade_interval_minutes": 60})
            for _ in range(3):
                store.mark_source_paused(conn, "flaky", "")  # ensure row exists
                break
            store.ensure_source(conn, sources.SOURCES[0])
            conn.execute("UPDATE radar_sources SET consecutive_failures=5, status='error' "
                         "WHERE source_id='flaky'")
            conn.commit()
            summary = sources.collect(conn, cfg)
            row = conn.execute("SELECT status, paused_reason FROM radar_sources WHERE source_id='flaky'").fetchone()
        finally:
            sources.SOURCES = original_sources
            sources.ADAPTERS.clear()
            sources.ADAPTERS.update(original_adapters)
            conn.close()
        self.assertEqual(summary["sources_skipped"], 1)
        self.assertEqual(row["status"], "paused")
        self.assertIn("停用", row["paused_reason"] or "")

    def test_success_resets_failure_counter(self):
        _reset_tables()
        original_adapters, original_sources = self._install_fakes()
        conn = db.connect()
        try:
            spec = dict(self._spec(), adapter="okay")
            sources.SOURCES = [spec]
            store.ensure_source(conn, spec)
            conn.execute("UPDATE radar_sources SET consecutive_failures=2, status='error' "
                         "WHERE source_id='flaky'")
            conn.commit()
            sources.collect(conn, _cfg())
            row = conn.execute("SELECT consecutive_failures, status FROM radar_sources "
                               "WHERE source_id='flaky'").fetchone()
        finally:
            sources.SOURCES = original_sources
            sources.ADAPTERS.clear()
            sources.ADAPTERS.update(original_adapters)
            conn.close()
        self.assertEqual(row["consecutive_failures"], 0)
        self.assertEqual(row["status"], "ok")

    def test_reset_clears_paused_state(self):
        _reset_tables()
        conn = db.connect()
        try:
            store.ensure_source(conn, self._spec())
            conn.execute("UPDATE radar_sources SET consecutive_failures=9, status='paused' "
                         "WHERE source_id='flaky'")
            conn.commit()
            store.reset_source_failures(conn, "flaky")
            conn.commit()
            row = conn.execute("SELECT consecutive_failures, status, paused_reason FROM radar_sources "
                               "WHERE source_id='flaky'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["consecutive_failures"], 0)
        self.assertEqual(row["status"], "unknown")
        self.assertIsNone(row["paused_reason"])


class TestSummarisation(unittest.TestCase):
    def test_extractive_quotes_collected_titles(self):
        event = {"id": "ev-x", "title": "某地发生地震", "heat": 90}
        items = [_item(title="某地发生7.0级地震", platform="微博", url="https://x.example/a"),
                 _item(title="救援队已抵达震区", platform="网易", url="https://x.example/b")]
        result = summarize.extractive(event, items)
        self.assertIn("据微博", result["summary"])
        self.assertIn("据网易", result["summary"])
        self.assertNotIn("2 个平台", result["summary"], "不应再出现模板句")

    def test_points_only_keep_measurable_numbers(self):
        event = {"title": "比赛"}
        items = [_item(title="国乒3比0韩国 4连冠", url="https://x.example/1"),
                 _item(title="时速400公里 7.0级地震", url="https://x.example/2")]
        result = summarize.extractive(event, items)
        self.assertIn("400公里", result["points"])
        self.assertIn("7.0级", result["points"])
        self.assertNotIn("3", result["points"])

    def test_disabled_llm_falls_back(self):
        _reset_tables()
        cfg = _cfg(llm={"enabled": False})
        conn = db.connect()
        try:
            result = summarize.summarize_event(conn, {"id": "ev-1", "title": "标题"},
                                               [_item(title="一条标题", url="https://x.example/1")],
                                               cfg, use_cache=False)
        finally:
            conn.close()
        self.assertEqual(result["provider"], "extractive")

    def test_cache_prevents_second_call(self):
        _reset_tables()
        cfg = _cfg()
        conn = db.connect()
        try:
            event = {"id": "ev-cache", "title": "缓存测试"}
            items = [_item(title="缓存测试标题", url="https://x.example/cache")]
            first = summarize.summarize_event(conn, event, items, cfg, use_cache=False)
            calls_before = llm.STATS["calls"]
            second = summarize.summarize_event(conn, event, items, cfg, use_cache=True)
        finally:
            conn.close()
        self.assertFalse(first.get("cached"))
        self.assertTrue(second.get("cached"))
        self.assertEqual(llm.STATS["calls"], calls_before)

    def test_llm_resolution_reports_missing_model(self):
        ok, reason, _ = llm.resolve({"llm": {"enabled": True, "model": ""}})
        self.assertFalse(ok)
        self.assertIn("模型", reason)

    def test_llm_parse_rejects_garbage(self):
        self.assertIsNone(summarize._parse_llm("not json at all"))
        self.assertIsNone(summarize._parse_llm(None))
        parsed = summarize._parse_llm('{"headline":"t","summary":"s","points":["1"]}')
        self.assertEqual(parsed["headline"], "t")


class TestReportStats(unittest.TestCase):
    def test_report_exposes_new_counters(self):
        _reset_tables()
        conn = db.connect()
        try:
            items = [_item(title="统计口径测试事件", url="https://x.example/stat")]
            store.insert_items(conn, items)
            rows = [dict(r) for r in conn.execute("SELECT * FROM radar_items").fetchall()]
            events = cluster.build_events(conn, "2026-10-23", rows, _cfg(), "2026-10-23T10:00:00+08:00")
            for event in events:
                store.assign_event(conn, event.get("_item_ids") or [], event["id"])
                store.upsert_event(conn, event)
            stories = cluster.build_stories(conn, "2026-10-23", events, _cfg(), "2026-10-23T10:00:00+08:00")
            store.replace_stories(conn, "2026-10-23", stories)
            conn.commit()
            payload = report.build(conn, "2026-10-23", _cfg(),
                                   {"items_raw": 10, "items_kept": 7, "items_filtered": 3})
        finally:
            conn.close()
        stats = payload["stats"]
        for key in ("collected", "filtered", "stored", "covered", "stories", "events"):
            self.assertIn(key, stats)
        self.assertEqual(stats["collected"], 10)
        self.assertEqual(stats["stored"], 7)
        self.assertGreaterEqual(stats["covered"], 1)

    def test_highlights_have_summary_and_angles(self):
        _reset_tables()
        conn = db.connect()
        try:
            items = [_item(title="多角度故事测试", url="https://x.example/angle")]
            store.insert_items(conn, items)
            rows = [dict(r) for r in conn.execute("SELECT * FROM radar_items").fetchall()]
            events = cluster.build_events(conn, "2026-10-23", rows, _cfg(), "2026-10-23T10:00:00+08:00")
            for event in events:
                store.assign_event(conn, event.get("_item_ids") or [], event["id"])
                store.upsert_event(conn, event)
            stories = cluster.build_stories(conn, "2026-10-23", events, _cfg(), "2026-10-23T10:00:00+08:00")
            store.replace_stories(conn, "2026-10-23", stories)
            conn.commit()
            payload = report.build(conn, "2026-10-23", _cfg())
        finally:
            conn.close()
        highlight = payload["highlights"][0]
        for key in ("story_id", "headline", "summary", "summary_provider", "angles", "angle_count"):
            self.assertIn(key, highlight)
        self.assertEqual(highlight["summary_provider"], "extractive")


if __name__ == "__main__":
    unittest.main(verbosity=2)
