"""Radar unit tests. Offline: no adapter here touches the network."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401,E402  (isolated DB + project root on sys.path)

from core import db  # noqa: E402
from core.radar import cluster, config as rcfg, investigate, push, report, score, service, sources, store  # noqa: E402


def _item(source_id="s1", platform="平台A", title="测试标题", url=None, published="2026-10-22T10:00:00+08:00",
          source_type="news", language="zh", engagement=None):
    spec = {"source_id": source_id, "platform": platform, "language": language,
            "source_type": source_type}
    item = sources.make_item(spec, title=title, url=url or "https://example.com/%s" % abs(hash(title)),
                             published_at=published, raw_content=title, engagement=engagement or {})
    item["id"] = None
    return item


class TestTokenizeAndSimilarity(unittest.TestCase):
    def test_cjk_bigrams_overlap(self):
        a = cluster.tokens("中国芯片突破引发关注")
        b = cluster.tokens("芯片突破引发关注 中国团队")
        self.assertGreater(cluster.similarity(a, b), 0.3)

    def test_unrelated_titles_do_not_merge(self):
        a = cluster.tokens("西藏发生地震")
        b = cluster.tokens("世界杯决赛结果")
        self.assertLess(cluster.similarity(a, b), 0.1)

    def test_stopwords_filtered(self):
        self.assertNotIn("中国", cluster.tokens("中国 新闻 报道"))


class TestDedupeAndClustering(unittest.TestCase):
    def test_identical_content_collapses(self):
        items = [_item(title="同一条新闻标题"), _item(title="同一条新闻标题", url="https://other.example/x")]
        groups = cluster.cluster_items(items)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)

    def test_distinct_events_stay_separate(self):
        items = [_item(title="某公司发布新款芯片"), _item(title="某地发生严重交通事故")]
        groups = cluster.cluster_items(items)
        self.assertEqual(len(groups), 2)

    def test_never_drops_items(self):
        items = [_item(title="事件 %d 号" % i) for i in range(20)]
        groups = cluster.cluster_items(items)
        self.assertEqual(sum(len(g) for g in groups), 20)


class TestScoring(unittest.TestCase):
    def test_engagement_normalised_per_platform(self):
        a = [_item(platform="微博", engagement={"rank": 1}) for _ in range(3)]
        b = [_item(platform="V2EX", engagement={"rank": 25}) for _ in range(3)]
        self.assertGreater(score.engagement_score(a), score.engagement_score(b))

    def test_unverified_heat_is_not_labelled_verified(self):
        event = {"id": "e1", "date": "2026-10-22", "title": "某热榜话题",
                 "keywords": ["热榜"], "_items": [_item(source_type="social_trend", platform="微博",
                                                        engagement={"rank": 1})],
                 "first_seen_at": "2026-10-22T10:00:00+08:00"}
        store.init()
        conn = db.connect()
        try:
            scored = score.score_events(conn, "2026-10-22", [event], rcfg.cached(), "2026-10-22T12:00:00+08:00")
        finally:
            conn.close()
        self.assertEqual(scored[0]["confidence"], "unverified")

    def test_news_sources_raise_confidence(self):
        items = [_item(source_id="a", url="https://a.example/1"),
                 _item(source_id="b", url="https://b.example/1"),
                 _item(source_id="c", url="https://c.example/1")]
        event = {"id": "e2", "date": "2026-10-22", "title": "多地媒体报道的事件",
                 "keywords": ["报道"], "_items": items, "first_seen_at": "2026-10-22T10:00:00+08:00"}
        store.init()
        conn = db.connect()
        try:
            scored = score.score_events(conn, "2026-10-22", [event], rcfg.cached(), "2026-10-22T12:00:00+08:00")
        finally:
            conn.close()
        self.assertEqual(scored[0]["confidence"], "verified_multi")

    def test_concentration_penalty_is_reported(self):
        items = [_item(source_id="same") for _ in range(6)]
        event = {"id": "e3", "date": "2026-10-22", "title": "同一来源大量内容",
                 "keywords": ["内容"], "_items": items, "first_seen_at": "2026-10-22T10:00:00+08:00"}
        store.init()
        conn = db.connect()
        try:
            scored = score.score_events(conn, "2026-10-22", [event], rcfg.cached(), "2026-10-22T12:00:00+08:00")
        finally:
            conn.close()
        self.assertIn("来源集中", scored[0]["metrics"]["flags"])


class TestClaimStatus(unittest.TestCase):
    def test_insufficient_without_evidence(self):
        status, _ = investigate._claim_status([], set(), False)
        self.assertEqual(status, "insufficient")

    def test_single_source_is_partial(self):
        status, _ = investigate._claim_status(["support"], {"a.example"}, False)
        self.assertEqual(status, "partial")

    def test_multi_independent_is_multi_support(self):
        status, _ = investigate._claim_status(["support"] * 3, {"a", "b", "c"}, False)
        self.assertEqual(status, "multi_support")

    def test_conflict_is_disputed(self):
        status, _ = investigate._claim_status(["support", "refute"], {"a"}, False)
        self.assertEqual(status, "disputed")

    def test_refutation_only_is_contradicted(self):
        status, _ = investigate._claim_status(["refute"], {"a"}, False)
        self.assertEqual(status, "contradicted")

    def test_correction_marks_corrected(self):
        status, _ = investigate._claim_status(["refute", "correction"], {"a"}, False)
        self.assertEqual(status, "corrected")

    def test_allegation_gets_cautious_rationale(self):
        claim = {"text": "某人涉嫌造假", "claimant": "网友", "stated_at": ""}
        result = investigate._classify_claim(claim, [], "2026-10-22T10:00:00+08:00")
        self.assertIn("指控", result["rationale"])
        self.assertEqual(result["status"], "insufficient")


class TestInvestigationOffline(unittest.TestCase):
    def test_unknown_query_reports_missing_origin(self):
        store.init()
        conn = db.connect()
        try:
            payload = investigate.run_query(conn, "完全不存在的关键词组xyzzy", rcfg.cached())
        finally:
            conn.close()
        self.assertIsNone(payload["origin"]["earliest"])
        self.assertIn("尚未找到最早来源", payload["origin"]["note"])
        self.assertEqual(payload["claims"], [])
        self.assertTrue(payload["gaps"])

    def test_origin_picks_earliest_and_warns(self):
        evidence = [
            {"title": "后续报道", "url": "https://b.example/x", "platform": "B", "source_type": "news",
             "published_at": "2026-10-22T12:00:00+08:00", "raw_content": "", "collected_at": ""},
            {"title": "最早报道", "url": "https://a.example/x", "platform": "A", "source_type": "news",
             "published_at": "2026-10-22T08:00:00+08:00", "raw_content": "", "collected_at": ""},
        ]
        origin = investigate.build_origin(evidence)
        self.assertEqual(origin["earliest"]["title"], "最早报道")
        self.assertIn("不等于事件真正起源", origin["note"])

    def test_tech_report_marks_incomparable(self):
        event = {"title": "某芯片突破", "tech_flag": True}
        tech = investigate.tech_report(event, [], [], rcfg.cached())
        self.assertIn("无法", tech["sections"]["comparability"])
        self.assertTrue(tech["sections"]["comparability_rules"])

    def test_no_fabricated_sources_when_pool_empty(self):
        payload = investigate.run_query(db.connect(), "另一个不存在的查询 zzz", rcfg.cached())
        self.assertEqual(payload["evidence_count"], 0)
        self.assertEqual(payload["independent_domains"], 0)


class TestFreeTextSearch(unittest.TestCase):
    def test_terms_split_on_cjk_boundaries(self):
        terms = investigate.query_terms("量子位 大模型, 芯片")
        self.assertIn("量子位", terms)
        self.assertIn("芯片", terms)

    def test_search_matches_chinese_substring(self):
        store.init()
        conn = db.connect()
        try:
            title = "某厂商发布新款AI芯片"
            store.insert_items(conn, [_item(title=title, url="https://search.example/1")])
            conn.commit()
            hits = investigate.search_pool(conn, "芯片 发布", days=30)
        finally:
            conn.close()
        self.assertTrue(any(title in (h.get("title") or "") for h in hits))

    def test_search_returns_empty_for_unknown_topic(self):
        store.init()
        conn = db.connect()
        try:
            hits = investigate.search_pool(conn, "完全无关的查询词zzz", days=30)
        finally:
            conn.close()
        self.assertEqual(hits, [])


class TestSourceRegistry(unittest.TestCase):
    def test_every_adapter_exists(self):
        for spec in sources.SOURCES:
            self.assertIn(spec["adapter"], sources.ADAPTERS, spec["source_id"])

    def test_unavailable_platforms_are_never_enabled(self):
        for spec in sources.SOURCES:
            if spec["kind"] == "unavailable":
                self.assertFalse(spec.get("enabled"), spec["source_id"])

    def test_no_paid_api_required(self):
        """Every enabled source must be key-free."""
        for spec in sources.SOURCES:
            if spec.get("enabled"):
                self.assertNotIn("key", spec.get("url", "").lower())
                self.assertNotIn("token", spec.get("url", "").lower())

    def test_disabled_sources_are_skipped(self):
        cfg = {"sources": {"weibo_hot": {"enabled": False}}}
        ids = [s["source_id"] for s in sources.active_specs(cfg)]
        self.assertNotIn("weibo_hot", ids)

    def test_one_failing_adapter_does_not_stop_others(self):
        store.init()
        conn = db.connect()
        original = dict(sources.ADAPTERS)

        def boom(spec, make_item):
            raise RuntimeError("simulated failure")

        def okay(spec, make_item):
            return [make_item(spec, title="正常来源的一条内容")]

        try:
            sources.ADAPTERS["boom"] = boom
            sources.ADAPTERS["okay"] = okay
            specs = [
                {"source_id": "bad", "platform": "坏源", "name": "坏源", "kind": "x", "adapter": "boom",
                 "url": "", "enabled": True},
                {"source_id": "good", "platform": "好源", "name": "好源", "kind": "x", "adapter": "okay",
                 "url": "", "enabled": True},
            ]
            saved = sources.SOURCES
            sources.SOURCES = specs
            try:
                summary = sources.collect(conn, {"sources": {}})
            finally:
                sources.SOURCES = saved
        finally:
            sources.ADAPTERS.clear()
            sources.ADAPTERS.update(original)
            conn.close()

        self.assertEqual(summary["sources_ok"], 1)
        self.assertEqual(summary["sources_failed"], 1)
        self.assertEqual(summary["failures"][0]["source_id"], "bad")


class TestPushSafety(unittest.TestCase):
    def test_push_disabled_by_default(self):
        store.init()
        conn = db.connect()
        try:
            result = push.send_daily(conn, {"date": "2026-10-22", "highlights": []},
                                     {"push": {"enabled": False}})
        finally:
            conn.close()
        self.assertTrue(result.get("skipped"))

    def test_missing_key_is_reported_not_raised(self):
        store.init()
        conn = db.connect()
        try:
            result = push.send_daily(conn, {"date": "2026-10-22", "highlights": []},
                                     {"push": {"enabled": True, "fallback_to_main_key": False}}, manual=True)
        finally:
            conn.close()
        self.assertFalse(result.get("ok", True))
        self.assertIn("RADAR", result.get("reason", ""))


class TestConfigSafety(unittest.TestCase):
    def test_partial_config_keeps_defaults(self):
        cfg = rcfg._merge(rcfg.DEFAULTS, {"scoring": {"velocity": 0.5}})
        self.assertEqual(cfg["scoring"]["velocity"], 0.5)
        self.assertEqual(cfg["scoring"]["cross_platform"], 0.2)
        self.assertEqual(cfg["schedule"]["daily_time"], "09:00")

    def test_weights_are_documented_defaults(self):
        scoring = rcfg.DEFAULTS["scoring"]
        self.assertAlmostEqual(scoring["velocity"], 0.3)
        self.assertAlmostEqual(scoring["cross_platform"], 0.2)
        self.assertAlmostEqual(scoring["engagement"], 0.2)
        self.assertAlmostEqual(scoring["verification"], 0.2)
        self.assertAlmostEqual(scoring["interest"], 0.1)


class TestReportStructure(unittest.TestCase):
    def _seed_event(self, conn, date="2026-10-22"):
        items = [_item(source_id="a", url="https://a.example/1", title="测试事件报道"),
                 _item(source_id="b", platform="微博", url="https://b.example/1",
                       title="测试事件热榜", source_type="social_trend")]
        store.insert_items(conn, items)
        rows = conn.execute("SELECT * FROM radar_items").fetchall()
        event = {
            "id": "ev-test", "date": date, "title": "测试事件报道",
            "summary": "", "category": "综合", "keywords": ["测试"],
            "heat": 80, "importance": 60, "confidence": "reported",
            "item_count": len(rows), "platform_count": 2, "source_count": 2,
            "first_seen_at": "2026-10-22T09:00:00+08:00", "last_updated_at": "2026-10-22T09:00:00+08:00",
            "origin_item_id": rows[0]["id"], "tech_flag": False, "investigation_state": "none",
            "metrics": {"platforms": ["平台A", "微博"], "lifecycle": "new", "flags": []},
        }
        store.upsert_event(conn, event)
        store.assign_event(conn, [r["id"] for r in rows], event["id"])
        conn.commit()
        return event

    def test_report_has_all_sections(self):
        store.init()
        conn = db.connect()
        try:
            self._seed_event(conn)
            payload = report.build(conn, "2026-10-22", rcfg.cached())
        finally:
            conn.close()
        for key in ("highlights", "zh_web", "intl", "tech", "investigation_updates",
                    "coverage", "evidence_gaps", "disclaimer"):
            self.assertIn(key, payload)
        self.assertTrue(payload["highlights"])

    def test_push_rendering_contains_disclaimer(self):
        store.init()
        conn = db.connect()
        try:
            self._seed_event(conn)
            payload = report.build(conn, "2026-10-22", rcfg.cached())
        finally:
            conn.close()
        rendered = report.render_push(payload)
        self.assertIn("全球热点雷达", rendered["title"])
        self.assertIn("不是平台官方指标", rendered["body"])


class TestServiceState(unittest.TestCase):
    def test_state_is_serialisable(self):
        state = service.get_state()
        self.assertIn("running", state)
        self.assertIsInstance(state["running"], bool)

    def test_not_running_by_default(self):
        self.assertFalse(service.get_state()["running"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
