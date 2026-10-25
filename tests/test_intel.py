"""全球态势监控（阶段一 · 基础后端）离线测试。

全部使用 ``tests/_bootstrap.py`` 提供的临时数据库与临时配置：
* 不访问真实 ``data/filter-bubble.db``；
* 不访问网络（采集、大模型调用都显式打桩）；
* 大模型一律禁用，验证本模块零模型调用。
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401,E402  (isolated db + temp config on sys.path)

from core import db  # noqa: E402
from core.radar import api as radar_api  # noqa: E402
from core.radar import config as rcfg  # noqa: E402
from core.radar import intel, service, sources, store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

ORIGINAL_RADAR_EVENT_COLUMNS = {
    "id", "date", "title", "summary", "category", "keywords", "heat", "importance",
    "confidence", "item_count", "platform_count", "source_count", "first_seen_at",
    "last_updated_at", "origin_item_id", "tech_flag", "investigation_state", "metrics",
    "story_id",
}

ORIGINAL_TABLES = [
    "repos", "snapshots", "digests", "feedback", "weights", "bookmarks",
    "translations", "follows", "follow_events", "push_log", "readmes", "meta",
]

RESET_TABLES = (
    "radar_claim_evidence", "radar_claims", "radar_evidence", "radar_reports",
    "radar_event_snapshots", "radar_events", "radar_items", "radar_stories",
    "radar_summaries", "radar_sources", "radar_runs", "radar_threat_daily",
    "radar_rank_history", "radar_push_log",
)


def _cfg(**overrides):
    cfg = rcfg.load()
    # 默认禁掉大模型，保证离线且不产生任何 token 消耗
    cfg["llm"] = {"enabled": False, "provider": "", "base_url": "", "model": ""}
    cfg.update(overrides)
    return cfg


def _reset():
    db.init()      # 原有 12 张 GitHub 表（与其它测试模块同样的做法）
    store.init()
    conn = db.connect()
    try:
        for table in RESET_TABLES:
            try:
                conn.execute("DELETE FROM %s" % table)
            except Exception:  # noqa: BLE001 - 表不存在就跳过
                pass
        conn.commit()
    finally:
        conn.close()


def _event(event_id, date, heat=50.0, item_count=1, confidence="reported", platforms=None,
           flags=None, news_domains=1, raw_heat=None, importance=50.0):
    metrics = {
        "raw_heat": heat if raw_heat is None else raw_heat,
        "platforms": platforms if platforms is not None else ["平台A"],
        "flags": flags or [],
        "news_domains": news_domains,
        "lifecycle": "new",
        "previous_item_count": 0,
    }
    return {
        "id": event_id, "date": date, "title": "事件 %s" % event_id, "summary": "",
        "category": "综合", "keywords": [], "heat": heat, "importance": importance,
        "confidence": confidence, "item_count": item_count,
        "platform_count": len(metrics["platforms"]), "source_count": len(metrics["platforms"]),
        "first_seen_at": date + "T10:00:00+08:00", "last_updated_at": date + "T10:00:00+08:00",
        "origin_item_id": None, "tech_flag": False, "investigation_state": "none",
        "metrics": metrics, "story_id": None,
    }


def _seed_events(conn, events):
    for event in events:
        store.upsert_event(conn, event)
    conn.commit()


def _snapshot(conn, event_id, date, item_count, platform_count, heat):
    conn.execute(
        "INSERT INTO radar_event_snapshots(event_id, date, item_count, platform_count, heat) "
        "VALUES(?,?,?,?,?) ON CONFLICT(event_id, date) DO UPDATE SET "
        "item_count=excluded.item_count, platform_count=excluded.platform_count, heat=excluded.heat",
        (event_id, date, item_count, platform_count, heat))
    conn.commit()


def _mk_item(title, source_type="news", published="2026-10-26T09:00:00+08:00",
             platform="平台A", source_id="s1", raw_content="正文"):
    spec = {"source_id": source_id, "platform": platform, "language": "zh",
            "source_type": source_type}
    return sources.make_item(spec, title=title, url="https://example.com/%s" % title,
                             published_at=published, raw_content=raw_content)


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 1) Threat Timeline：公式、历史缺失、口径版本
# --------------------------------------------------------------------------- #

class TestThreatTimeline(unittest.TestCase):
    def setUp(self):
        _reset()
        self.cfg = _cfg()

    def test_fixed_caliber_formula_is_exact(self):
        """固定口径：intensity 用绝对 raw_heat，volume/breadth 用真实计数。"""
        conn = db.connect()
        try:
            date = "2026-10-20"
            _seed_events(conn, [
                _event("ev-a", date, raw_heat=80.0, item_count=100, platforms=["微博", "百度"]),
                _event("ev-b", date, raw_heat=60.0, item_count=60, platforms=["微信"]),
            ])
            row = intel.record_daily_snapshot(conn, date, self.cfg)
            conn.commit()
        finally:
            conn.close()

        # intensity = (80+60)/2/100 = 0.70 ; volume = 160/400 = 0.40 ; breadth = 3/12 = 0.25
        # growth 无前一日快照 → 缺失，权重在 0.40+0.25+0.20=0.85 上重新归一化
        # 100*(0.40*0.70 + 0.25*0.40 + 0.20*0.25)/0.85 = 50.58… → 50.6
        self.assertEqual(row["caliber_version"], 1)
        self.assertAlmostEqual(row["activity"], 50.6, places=1)
        self.assertAlmostEqual(row["credibility"], 100.0, places=1)
        self.assertAlmostEqual(row["top_activity_heat_mean"], 70.0, places=2)
        self.assertIn("growth", row["missing_inputs"])
        self.assertEqual(row["activity_components"]["inputs"],
                         {"intensity": 0.7, "volume": 0.4, "breadth": 0.25})
        self.assertTrue(row["activity_components"]["renormalized"])

    def test_snapshot_roundtrip_through_database(self):
        conn = db.connect()
        try:
            date = "2026-10-20"
            _seed_events(conn, [_event("ev-a", date, raw_heat=90.0, item_count=200)])
            intel.record_daily_snapshot(conn, date, self.cfg)
            conn.commit()
            stored = store.threat_daily_by_date(conn, date)
            self.assertIsNotNone(stored)
            self.assertEqual(stored["caliber_version"], 1)
            # intensity=0.9 volume=0.5 breadth=1/12 → 100*(0.4*0.9+0.25*0.5+0.2/12)/0.85 = 59.0
            self.assertEqual(float(stored["activity"]), 59.0)
        finally:
            conn.close()

    def test_absolute_heat_is_required_for_intensity(self):
        """metrics 完全没有 raw_heat 时，强度分项记为缺失，而不是拿 heat 顶上。"""
        conn = db.connect()
        try:
            date = "2026-10-20"
            event = _event("ev-a", date, heat=88.0, item_count=100)
            event["metrics"].pop("raw_heat")
            _seed_events(conn, [event])
            row = intel.record_daily_snapshot(conn, date, self.cfg)
            conn.commit()
        finally:
            conn.close()
        self.assertIn("intensity", row["missing_inputs"])
        self.assertNotIn("intensity", row["activity_components"]["inputs"])
        self.assertIsNone(row["top_activity_heat_mean"])

    def test_unverified_news_does_not_change_activity(self):
        """未经验证不能改变威胁活动：验证比例只影响可信度。"""
        base = [
            _event("ev-a", "2026-10-20", raw_heat=80.0, item_count=100, platforms=["微博", "百度"]),
            _event("ev-b", "2026-10-20", raw_heat=60.0, item_count=60, platforms=["微信"]),
        ]
        dirty = [
            _event("ev-a", "2026-10-20", raw_heat=80.0, item_count=100, platforms=["微博", "百度"],
                   confidence="unverified", news_domains=0, flags=["来源集中"]),
            _event("ev-b", "2026-10-20", raw_heat=60.0, item_count=60, platforms=["微信"],
                   confidence="unverified", news_domains=0, flags=["来源集中"]),
        ]
        conn = db.connect()
        try:
            _seed_events(conn, base)
            clean = intel.record_daily_snapshot(conn, "2026-10-20", self.cfg)
            conn.commit()
            conn.execute("DELETE FROM radar_threat_daily")
            conn.execute("DELETE FROM radar_events")
            conn.commit()
            _seed_events(conn, dirty)
            suspect = intel.record_daily_snapshot(conn, "2026-10-20", self.cfg)
            conn.commit()
        finally:
            conn.close()

        self.assertAlmostEqual(clean["activity"], suspect["activity"], places=3)
        # 可信度必须显著下降：verified 0 / 集中 / 两条都缺媒体报道
        # 100*(0.45*0 + 0.25*0 + 0.30*0.6) = 18.0
        self.assertAlmostEqual(suspect["credibility"], 18.0, places=1)
        self.assertGreater(clean["credibility"], suspect["credibility"])
        # 活动分项里不得出现验证类输入
        inputs = suspect["activity_components"]["inputs"]
        self.assertNotIn("verified_share", inputs)
        self.assertNotIn("unverified_share", inputs)

    def test_legacy_point_never_uses_rescaled_heat_or_current_values(self):
        """历史点只能由当时的快照反推：不放当天重标定 heat，也不用当前 raw_heat 回填。"""
        conn = db.connect()
        try:
            past = "2026-10-20"
            _snapshot(conn, "ev-x", past, item_count=10, platform_count=2, heat=92.0)
            _snapshot(conn, "ev-y", past, item_count=20, platform_count=2, heat=50.0)
            _snapshot(conn, "ev-z", past, item_count=30, platform_count=3, heat=40.0)
            # 今天的真实事件（raw_heat 很高）绝不能被算进历史点
            _seed_events(conn, [_event("ev-today", intel.today(self.cfg), raw_heat=100.0,
                                       item_count=300)])
            conn.commit()
            first = intel.threat_timeline(conn, days=1, end_date=past, cfg=self.cfg)
            point = first["points"][0]
            # 把 heat 全部改小，activity 必须一字不变（证明 heat 不参与计算）
            for event_id, heat in (("ev-x", 5.0), ("ev-y", 5.0), ("ev-z", 5.0)):
                conn.execute("UPDATE radar_event_snapshots SET heat=? WHERE event_id=? AND date=?",
                             (heat, event_id, past))
            conn.commit()
            second = intel.threat_timeline(conn, days=1, end_date=past, cfg=self.cfg)
        finally:
            conn.close()

        self.assertEqual(point["caliber_version"], 0)
        self.assertIsNone(point["credibility"])
        self.assertIn("absolute_intensity", point["missing_inputs"])
        self.assertEqual(point["activity"], second["points"][0]["activity"])
        self.assertFalse(point["diagnostics"]["relative_attention_comparable"])
        self.assertEqual(point["diagnostics"]["relative_attention_mean"], 60.67)
        # 反推口径的 activity 只由 volume/breadth 得出，远低于当天真实事件的水平
        self.assertAlmostEqual(point["activity"], 17.0, places=1)
        self.assertIn("unverified_share", point["missing_inputs"])

    def test_materialized_empty_day_is_not_reported_as_zero_activity(self):
        """已物化但当天没有事件的日子，必须报"没有观测"，不能读成"活动为 0"。"""
        conn = db.connect()
        try:
            date = "2026-10-22"
            row = intel.record_daily_snapshot(conn, date, self.cfg)  # 当天没有任何事件
            conn.commit()
            timeline = intel.threat_timeline(conn, days=1, end_date=date, cfg=self.cfg)
        finally:
            conn.close()
        self.assertEqual(row["event_count"], 0)
        point = timeline["points"][0]
        self.assertFalse(point["available"])
        self.assertIsNone(point["activity"])
        self.assertIsNone(point["credibility"])
        self.assertIn("events", point["missing_inputs"])
        self.assertEqual(timeline["history_days"], 0)
        self.assertEqual(timeline["history_confidence"], "none")

    def test_observed_activity_marks_partial_legacy_basis(self):
        conn = db.connect()
        try:
            _snapshot(conn, "ev-a", "2026-10-25", item_count=80, platform_count=3, heat=70.0)
            conn.commit()
            strategic = intel.strategic_overview(conn, cfg=self.cfg)
        finally:
            conn.close()
        observed = strategic["observed_activity"]
        self.assertEqual(observed["basis"], "legacy_caliber")
        self.assertTrue(observed["partial"])
        self.assertEqual(observed["date"], "2026-10-25")

    def test_history_confidence_and_no_interpolation(self):
        conn = db.connect()
        try:
            for date, items in (("2026-10-20", 10), ("2026-10-21", 20)):
                _snapshot(conn, "ev-%s" % date, date, item_count=items, platform_count=2, heat=60.0)
            conn.commit()
            timeline = intel.threat_timeline(conn, days=7, end_date="2026-10-21", cfg=self.cfg)
        finally:
            conn.close()

        self.assertEqual(len(timeline["points"]), 7)
        self.assertEqual(timeline["history_days"], 2)
        self.assertEqual(timeline["requested_days"], 7)
        self.assertEqual(timeline["history_confidence"], "insufficient")
        self.assertEqual(timeline["comparability"], "legacy_only")
        self.assertEqual(timeline["caliber_version"], 1)
        self.assertIn("绝对强度", timeline["caliber_note"])
        self.assertTrue(any("不做插值" in note for note in timeline["notes"]))
        empty = [p for p in timeline["points"] if not p["available"]]
        self.assertEqual(len(empty), 5)
        for point in empty:
            self.assertIsNone(point["activity"])
            self.assertEqual(point["missing_inputs"], ["no_data"])

    def test_trend_requires_same_caliber(self):
        conn = db.connect()
        try:
            for date, activity_hint in (("2026-10-19", 100), ("2026-10-20", 200)):
                _snapshot(conn, "ev-%s" % date, date, item_count=activity_hint,
                          platform_count=2, heat=60.0)
            conn.commit()
            timeline = intel.threat_timeline(conn, days=2, end_date="2026-10-20", cfg=self.cfg)
        finally:
            conn.close()
        self.assertIsNotNone(timeline["trend"])
        self.assertEqual(timeline["trend"]["basis"], "legacy_caliber")
        self.assertEqual(timeline["trend"]["activity"], "up")
        self.assertEqual(timeline["trend"]["caliber_version"], 0)

    def test_single_point_has_no_trend(self):
        conn = db.connect()
        try:
            _snapshot(conn, "ev-a", "2026-10-20", item_count=10, platform_count=2, heat=60.0)
            conn.commit()
            timeline = intel.threat_timeline(conn, days=1, end_date="2026-10-20", cfg=self.cfg)
        finally:
            conn.close()
        self.assertIsNone(timeline["trend"])


# --------------------------------------------------------------------------- #
# 2) 战略风险：归一化、数值范围、缺失输入
# --------------------------------------------------------------------------- #

class TestStrategicOverview(unittest.TestCase):
    def setUp(self):
        _reset()
        self.cfg = _cfg()

    def test_weighted_returns_none_when_every_input_is_missing(self):
        result = intel._weighted({"a": None, "b": None}, {"a": 0.5, "b": 0.5})
        self.assertIsNone(result["value"])
        self.assertEqual(result["missing"], ["a", "b"])

    def test_weighted_renormalizes_and_keeps_range(self):
        result = intel._weighted({"a": 1.0, "b": None}, {"a": 0.25, "b": 0.75})
        self.assertEqual(result["value"], 100.0)
        self.assertTrue(result["renormalized"])

    def test_all_scores_are_within_range_and_components_normalized(self):
        conn = db.connect()
        try:
            date = intel.today(self.cfg)
            _seed_events(conn, [
                _event("ev-a", date, raw_heat=90.0, item_count=120,
                       platforms=["微博", "百度", "微信"]),
                _event("ev-b", date, raw_heat=70.0, item_count=60, platforms=["知乎"]),
            ])
            intel.record_daily_snapshot(conn, date, self.cfg)
            store.ensure_source(conn, {"source_id": "good", "platform": "好源", "name": "好源",
                                       "kind": "rss", "url": "", "enabled": True, "notes": ""})
            conn.commit()
            payload = intel.build_payload(conn, self.cfg)
        finally:
            conn.close()

        strategic = payload["strategic"]
        for key in ("observed_activity", "credibility"):
            score = strategic[key]["score"]
            self.assertIsNotNone(score)
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 100.0)
        coverage_score = strategic["monitoring"]["coverage_score"]
        self.assertGreaterEqual(coverage_score, 0.0)
        self.assertLessEqual(coverage_score, 100.0)
        estimate = strategic["estimate"]["score"]
        self.assertGreaterEqual(estimate, 0.0)
        self.assertLessEqual(estimate, 100.0)
        for name, value in strategic["estimate"]["components"].items():
            self.assertGreaterEqual(value, 0.0, name)
            self.assertLessEqual(value, 1.0, name)
        for name, value in strategic["monitoring"]["components"]["inputs"].items():
            self.assertGreaterEqual(value, 0.0, name)
            self.assertLessEqual(value, 1.0, name)

    def test_estimate_combines_normalized_inputs_consistently(self):
        """TL 是 0–100，其它输入是 0–1：合成前必须统一归一化。"""
        conn = db.connect()
        try:
            store.ensure_source(conn, {"source_id": "good", "platform": "好源", "name": "好源",
                                       "kind": "rss", "url": "", "enabled": True, "notes": ""})
            conn.commit()
            strategic = intel.strategic_overview(conn, cfg=self.cfg)
        finally:
            conn.close()
        components = strategic["estimate"]["components"]
        weights = strategic["estimate"]["weights"]
        if components:
            expected = 100.0 * sum(weights[k] * components[k]
                                   for k in components) / sum(weights[k] for k in components)
            self.assertAlmostEqual(strategic["estimate"]["score"], round(expected, 1), places=1)
        self.assertTrue(all(0.0 <= value <= 1.0 for value in components.values()))

    def test_country_index_never_fabricates_scores(self):
        conn = db.connect()
        try:
            payload = intel.country_index(conn, cfg=self.cfg)
        finally:
            conn.close()
        self.assertFalse(payload["available"])
        self.assertEqual(payload["countries"], [])
        self.assertTrue(payload["reason"])

    def test_monitoring_blind_spot_is_separate_from_observed_activity(self):
        """源停用只影响监控覆盖，不得抬高观测到的威胁活动。"""
        conn = db.connect()
        try:
            date = intel.today(self.cfg)
            _seed_events(conn, [_event("ev-a", date, raw_heat=80.0, item_count=120)])
            intel.record_daily_snapshot(conn, date, self.cfg)
            store.ensure_source(conn, {"source_id": "good", "platform": "好源", "name": "好源",
                                       "kind": "rss", "url": "", "enabled": True, "notes": ""})
            conn.commit()
            healthy = intel.strategic_overview(conn, cfg=self.cfg)

            store.ensure_source(conn, {"source_id": "bad", "platform": "坏源", "name": "坏源",
                                       "kind": "rss", "url": "", "enabled": True, "notes": ""})
            conn.execute("UPDATE radar_sources SET status='paused', consecutive_failures=6, "
                         "paused_reason='连续失败 6 次，已自动停用' WHERE source_id='bad'")
            conn.commit()
            blinded = intel.strategic_overview(conn, cfg=self.cfg)
        finally:
            conn.close()

        self.assertEqual(healthy["observed_activity"]["score"],
                         blinded["observed_activity"]["score"])
        self.assertLess(blinded["monitoring"]["coverage_score"],
                        healthy["monitoring"]["coverage_score"])
        self.assertTrue(any(spot["kind"] == "paused"
                            for spot in blinded["monitoring"]["blind_spots"]))
        self.assertIn("监控盲区", blinded["monitoring"]["note"])
        self.assertTrue(any("监控盲区" in note for note in blinded["notes"]))

    def test_missing_inputs_are_reported_and_renormalized(self):
        conn = db.connect()
        try:
            # 有源可观测（监控维度可用），但今天没有任何事件 → 活动维度缺失
            store.ensure_source(conn, {"source_id": "good", "platform": "好源", "name": "好源",
                                       "kind": "rss", "url": "", "enabled": True, "notes": ""})
            conn.commit()
            strategic = intel.strategic_overview(conn, cfg=self.cfg)
        finally:
            conn.close()
        self.assertIsNone(strategic["observed_activity"]["score"])
        estimate = strategic["estimate"]
        self.assertIn("observed_activity", estimate["missing_inputs"])
        self.assertTrue(estimate["renormalized"])
        self.assertIsNotNone(estimate["score"])
        self.assertGreaterEqual(estimate["score"], 0.0)

    def test_empty_database_returns_no_estimate_instead_of_a_made_up_number(self):
        conn = db.connect()
        try:
            strategic = intel.strategic_overview(conn, cfg=self.cfg)
            coverage = intel.monitoring_coverage(conn, cfg=self.cfg)
        finally:
            conn.close()
        self.assertIsNone(coverage["coverage_score"])
        self.assertIn("sources", coverage["missing_inputs"])
        estimate = strategic["estimate"]
        self.assertIsNone(estimate["score"])
        self.assertIn("monitoring_blind_spot", estimate["missing_inputs"])
        self.assertIn("observed_activity", estimate["missing_inputs"])

    def test_estimate_is_explicitly_labelled_not_official(self):
        conn = db.connect()
        try:
            strategic = intel.strategic_overview(conn, cfg=self.cfg)
        finally:
            conn.close()
        estimate = strategic["estimate"]
        self.assertTrue(estimate["is_estimate"])
        self.assertIn("非官方", estimate["label"])
        self.assertIn("估算", estimate["label"])
        for note in strategic["notes"]:
            self.assertIsInstance(note, str)


# --------------------------------------------------------------------------- #
# 3) Intelligence Feed：来源与 story_id 与日报保持一致
# --------------------------------------------------------------------------- #

class TestIntelligenceFeed(unittest.TestCase):
    def setUp(self):
        _reset()
        self.cfg = _cfg()
        self.date = intel.today(self.cfg)
        conn = db.connect()
        try:
            store.save_report(conn, self.date, "daily", {
                "date": self.date, "version": 2,
                "highlights": [
                    {"story_id": "st-1", "event_id": "ev-1", "title": "甲",
                     "headline": "甲", "summary": "甲摘要", "confidence": "reported",
                     "confidence_label": "有媒体报道，尚未多方独立验证", "heat": 80.0},
                    {"story_id": "st-2", "event_id": "ev-2", "title": "乙",
                     "headline": "乙", "summary": "乙摘要", "confidence": "unverified",
                     "confidence_label": "尚未验证", "heat": 60.0},
                    {"story_id": "st-3", "event_id": "ev-3", "title": "丙",
                     "headline": "丙", "summary": "丙摘要", "confidence": "reported",
                     "confidence_label": "有媒体报道", "heat": 40.0},
                ],
            }, _now_iso())
            conn.commit()
        finally:
            conn.close()

    def test_story_ids_match_the_daily_report(self):
        conn = db.connect()
        try:
            feed = intel.intelligence_feed(conn, cfg=self.cfg)
            status, payload = radar_api.handle_get("/api/radar/report", {})
        finally:
            conn.close()
        self.assertEqual(status, 200)
        report_ids = [item["story_id"] for item in payload["payload"]["highlights"]]
        feed_ids = [item["story_id"] for item in feed["items"]]
        self.assertEqual(feed_ids, report_ids)
        self.assertEqual(feed["report_date"], self.date)
        self.assertTrue(all(item["is_new"] in (True, False) for item in feed["items"]))

    def test_summary_provider_is_transparent(self):
        conn = db.connect()
        try:
            feed = intel.intelligence_feed(conn, cfg=self.cfg)
        finally:
            conn.close()
        self.assertIn("extractive", feed["note"])

    def test_pagination_covers_all_items_without_loss(self):
        conn = db.connect()
        try:
            first = intel.intelligence_feed(conn, limit=2, offset=0, cfg=self.cfg)
            second = intel.intelligence_feed(conn, limit=2, offset=2, cfg=self.cfg)
        finally:
            conn.close()
        self.assertEqual([i["story_id"] for i in first["items"]], ["st-1", "st-2"])
        self.assertTrue(first["has_more"])
        self.assertEqual([i["story_id"] for i in second["items"]], ["st-3"])
        self.assertFalse(second["has_more"])

    def test_explicit_date_and_pending_state(self):
        conn = db.connect()
        try:
            by_date = intel.intelligence_feed(conn, date=self.date, cfg=self.cfg)
            missing = intel.intelligence_feed(conn, date="1999-01-01", cfg=self.cfg)
        finally:
            conn.close()
        self.assertEqual(len(by_date["items"]), 3)
        self.assertEqual(missing["items"], [])
        self.assertTrue(missing["pending"])
        self.assertEqual(missing["missing_inputs"], ["daily_report"])


# --------------------------------------------------------------------------- #
# 4) Live News：before_id / since_id 游标语义
# --------------------------------------------------------------------------- #

class TestLiveNewsCursors(unittest.TestCase):
    def setUp(self):
        _reset()
        self.cfg = _cfg()
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()

    def _add(self, count, prefix="n"):
        for index in range(count):
            store.insert_items(self.conn, [
                _mk_item("%s-%s" % (prefix, index), published="2026-10-26T09:%02d:00+08:00" % index)])
        self.conn.commit()
        return [row["id"] for row in self.conn.execute(
            "SELECT id FROM radar_items ORDER BY id ASC").fetchall()]

    def test_before_id_walks_backwards_without_gaps_or_overlap(self):
        ids = self._add(10)
        page1 = intel.live_news(self.conn, limit=4, cfg=self.cfg)
        page2 = intel.live_news(self.conn, limit=4, before_id=page1["cursor"]["before_id"],
                                cfg=self.cfg)
        page3 = intel.live_news(self.conn, limit=4, before_id=page2["cursor"]["before_id"],
                                cfg=self.cfg)

        got = ([item["id"] for item in page1["items"]]
               + [item["id"] for item in page2["items"]]
               + [item["id"] for item in page3["items"]])
        self.assertEqual(len(got), 10)
        self.assertEqual(len(set(got)), 10)
        self.assertEqual(set(got), set(ids))
        self.assertEqual([item["id"] for item in page1["items"]],
                         sorted(ids, reverse=True)[:4])
        self.assertTrue(page1["cursor"]["has_more_older"])
        self.assertFalse(page3["cursor"]["has_more_older"])
        page4 = intel.live_news(self.conn, limit=4, before_id=page3["cursor"]["before_id"],
                               cfg=self.cfg)
        self.assertEqual(page4["items"], [])

    def test_since_id_returns_only_newer_items(self):
        ids = self._add(5)
        boundary = ids[-1]
        self._add(3, prefix="new")
        page = intel.live_news(self.conn, limit=20, since_id=boundary, cfg=self.cfg)
        self.assertEqual(len(page["items"]), 3)
        newest_first = [item["id"] for item in page["items"]]
        self.assertEqual(newest_first, sorted(newest_first, reverse=True))
        self.assertGreater(page["cursor"]["since_id"], boundary)
        self.assertFalse(page["cursor"]["has_more_newer"])

    def test_since_id_truncation_never_skips_the_middle(self):
        ids = self._add(5)
        boundary = ids[-1]
        self._add(10, prefix="batch")
        seen = []
        cursor = boundary
        for _ in range(5):
            page = intel.live_news(self.conn, limit=3, since_id=cursor, cfg=self.cfg)
            if not page["items"]:
                break
            seen.extend(item["id"] for item in page["items"])
            cursor = page["cursor"]["since_id"]
            if not page["cursor"]["has_more_newer"]:
                break
        expected = [row["id"] for row in self.conn.execute(
            "SELECT id FROM radar_items WHERE id > ? ORDER BY id ASC", (boundary,)).fetchall()]
        self.assertEqual(sorted(seen), expected)
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), 10)

    def test_published_at_missing_falls_back_to_collected_at(self):
        store.insert_items(self.conn, [_mk_item("无时间", published="")])
        self.conn.commit()
        page = intel.live_news(self.conn, limit=5, cfg=self.cfg)
        item = page["items"][0]
        self.assertTrue(item["published_at_missing"])
        self.assertEqual(item["time_source"], "collected_at")
        self.assertEqual(item["effective_at"], item["collected_at"])
        self.assertTrue(item["collected_at"])

    def test_trend_only_items_are_flagged_without_content(self):
        store.insert_items(self.conn, [
            _mk_item("热榜词", source_type="social_trend", platform="微博", raw_content="")])
        self.conn.commit()
        item = intel.live_news(self.conn, limit=5, cfg=self.cfg)["items"][0]
        self.assertTrue(item["is_trend_only"])
        self.assertFalse(item["has_content"])
        self.assertIn("没有正文", intel.live_news(self.conn, limit=1, cfg=self.cfg)["note"])

    def test_limit_is_clamped_to_config(self):
        self._add(3)
        page = intel.live_news(self.conn, limit=99999, cfg=self.cfg)
        self.assertEqual(page["limit"], 200)
        page = intel.live_news(self.conn, limit=0, cfg=self.cfg)
        self.assertGreaterEqual(page["limit"], 1)

    def test_title_filter(self):
        self._add(2, prefix="普通")
        store.insert_items(self.conn, [_mk_item("独特关键词标题")])
        self.conn.commit()
        page = intel.live_news(self.conn, limit=10, q="独特关键词", cfg=self.cfg)
        self.assertEqual(page["returned"], 1)
        self.assertIn("独特关键词", page["items"][0]["title"])


# --------------------------------------------------------------------------- #
# 5) 兼容性：旧接口与旧字段
# --------------------------------------------------------------------------- #

class TestCompatibility(unittest.TestCase):
    def setUp(self):
        _reset()
        self.cfg = _cfg()

    def test_original_tables_still_exist(self):
        store.init()
        conn = db.connect()
        try:
            names = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        finally:
            conn.close()
        for table in ORIGINAL_TABLES:
            self.assertIn(table, names)

    def test_radar_events_keeps_every_original_column(self):
        store.init()
        conn = db.connect()
        try:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(radar_events)")}
        finally:
            conn.close()
        self.assertTrue(ORIGINAL_RADAR_EVENT_COLUMNS.issubset(columns),
                        "原有列被删除：%s" % sorted(ORIGINAL_RADAR_EVENT_COLUMNS - columns))

    def test_core_db_schema_has_no_radar_tables(self):
        source = (ROOT / "core" / "db.py").read_text(encoding="utf-8")
        self.assertNotIn("radar_", source)

    def test_geo_columns_are_reserved_and_never_backfilled(self):
        """本阶段只预留地理字段，绝不回填任何坐标或国家。"""
        conn = db.connect()
        try:
            _seed_events(conn, [_event("ev-a", intel.today(self.cfg), raw_heat=90.0)])
            intel.record_daily_snapshot(conn, intel.today(self.cfg), self.cfg)
            conn.commit()
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(radar_events)")}
            for name in ("origin_country_code", "event_country_code", "geo_lat", "geo_lon",
                         "geo_label", "geo_confidence", "geo_method", "geo_source"):
                self.assertIn(name, columns)
            filled = conn.execute(
                "SELECT COUNT(1) c FROM radar_events WHERE event_country_code IS NOT NULL "
                "OR origin_country_code IS NOT NULL OR geo_lat IS NOT NULL "
                "OR geo_lon IS NOT NULL OR geo_confidence IS NOT NULL").fetchone()
            country = intel.country_index(conn, cfg=self.cfg)
        finally:
            conn.close()
        self.assertEqual(int(filled["c"]), 0)
        self.assertFalse(country["available"])
        self.assertIn("origin_country_code", country.get("origin_vs_event", ""))

    def test_category_semantics_are_not_overwritten(self):
        """新增 theme/region 是独立字段，category 的既有取值不受影响。"""
        conn = db.connect()
        try:
            _seed_events(conn, [_event("ev-a", intel.today(self.cfg), raw_heat=90.0)])
            intel.record_daily_snapshot(conn, intel.today(self.cfg), self.cfg)
            conn.commit()
            row = conn.execute("SELECT category, theme, region FROM radar_events").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["category"], "综合")
        self.assertIsNone(row["theme"])
        self.assertIsNone(row["region"])

    def test_existing_report_payload_keys_unchanged(self):
        conn = db.connect()
        try:
            date = intel.today(self.cfg)
            store.save_report(conn, date, "daily", {
                "date": date, "version": 2, "stats": {}, "highlights": [], "zh_web": [],
                "intl": [], "tech": [], "coverage": {}, "evidence_gaps": [],
                "disclaimer": "x", "scoring_weights": {}, "investigation_updates": [],
                "summary_backend": {},
            }, _now_iso())
            conn.commit()
            status, payload = radar_api.handle_get("/api/radar/report", {})
        finally:
            conn.close()
        self.assertEqual(status, 200)
        self.assertEqual(payload["date"], date)
        for key in ("highlights", "zh_web", "intl", "tech", "coverage", "evidence_gaps",
                    "stats", "scoring_weights", "disclaimer", "version"):
            self.assertIn(key, payload["payload"], "旧字段被删除：%s" % key)

    def test_existing_endpoints_still_answer(self):
        for path in ("/api/radar/status", "/api/radar/health", "/api/radar/sources",
                     "/api/radar/history", "/api/radar/settings"):
            status, payload = radar_api.handle_get(path, {})
            self.assertEqual(status, 200, "%s 返回 %s" % (path, payload))

    def test_new_endpoints_expose_coverage_and_missing_inputs(self):
        for path in ("/api/radar/intel", "/api/radar/intel/threat", "/api/radar/intel/live",
                     "/api/radar/intel/countries", "/api/radar/intel/map"):
            status, payload = radar_api.handle_get(path, {})
            self.assertEqual(status, 200, path)
            self.assertIn("generated_at", payload, path)
            self.assertIn("missing_inputs", payload, path)

    def test_country_index_is_honestly_unavailable(self):
        """没有完成地理编码时，国家面板必须明确不可用，而不是给一张空榜单。"""
        status, payload = radar_api.handle_get("/api/radar/intel/countries", {})
        self.assertEqual(status, 200)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["countries"], [])
        self.assertTrue(payload["reason"])
        self.assertIn("baseline", payload["missing_inputs"])
        self.assertEqual(payload["coverage"], "media_only")

    def test_unknown_intel_subpath_is_diagnostic_404(self):
        status, payload = radar_api.handle_get("/api/radar/intel/nope", {})
        self.assertEqual(status, 404)
        self.assertIn("error", payload)

    def test_intel_failure_is_diagnostic_and_isolated(self):
        original = intel.build_payload
        intel.build_payload = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            status, payload = radar_api.handle_get("/api/radar/intel", {})
        finally:
            intel.build_payload = original
        self.assertEqual(status, 500)
        self.assertEqual(payload["error_type"], "RuntimeError")
        self.assertIn("boom", payload["error"])
        self.assertEqual(payload["path"], "/api/radar/intel")
        self.assertIn("hint", payload)
        # 既有接口不受影响
        status, _payload = radar_api.handle_get("/api/radar/status", {})
        self.assertEqual(status, 200)


# --------------------------------------------------------------------------- #
# 6) 不产生非预期的大模型调用
# --------------------------------------------------------------------------- #

class TestNoUnexpectedModelCalls(unittest.TestCase):
    def setUp(self):
        _reset()
        self.cfg = _cfg()

    def test_intel_never_calls_a_model(self):
        from core.radar import llm

        def _boom(*args, **kwargs):
            raise AssertionError("全球态势模块不允许调用大模型")

        originals = {}
        for name in ("chat", "chat_result", "chat_result_ex"):
            if hasattr(llm, name):
                originals[name] = getattr(llm, name)
                setattr(llm, name, _boom)
        try:
            conn = db.connect()
            try:
                date = intel.today(self.cfg)
                _seed_events(conn, [_event("ev-a", date, raw_heat=90.0, item_count=50)])
                intel.record_daily_snapshot(conn, date, self.cfg)
                conn.commit()
                intel.build_payload(conn, self.cfg)
                intel.threat_timeline(conn, cfg=self.cfg)
                intel.strategic_overview(conn, cfg=self.cfg)
                intel.monitoring_coverage(conn, cfg=self.cfg)
                intel.intelligence_feed(conn, cfg=self.cfg)
                intel.live_news(conn, cfg=self.cfg)
                intel.country_index(conn, cfg=self.cfg)
            finally:
                conn.close()
        finally:
            for name, original in originals.items():
                setattr(llm, name, original)

    def test_module_does_not_import_model_helpers(self):
        source = (ROOT / "core" / "radar" / "intel.py").read_text(encoding="utf-8")
        imports = "\n".join(line.strip() for line in source.splitlines()
                            if line.strip().startswith(("import ", "from ")))
        self.assertNotIn("llm", imports)
        self.assertNotIn("summarize", imports)
        self.assertNotIn("ai_client", imports)
        self.assertNotIn("chat_result", source)


# --------------------------------------------------------------------------- #
# 7) 写入失败与运行记录生命周期
# --------------------------------------------------------------------------- #

class TestRunLifecycle(unittest.TestCase):
    def setUp(self):
        _reset()
        self.cfg = _cfg()

    def test_stale_running_runs_are_closed_but_recent_ones_are_left(self):
        conn = db.connect()
        try:
            old = (dt.datetime.now().astimezone() - dt.timedelta(hours=10)).isoformat(
                timespec="seconds")
            recent = (dt.datetime.now().astimezone() - dt.timedelta(minutes=1)).isoformat(
                timespec="seconds")
            conn.execute("INSERT INTO radar_runs(id, kind, started_at, finished_at, status, "
                         "stats, error) VALUES('r-old','collect',?,NULL,'running','{}',NULL)",
                         (old,))
            conn.execute("INSERT INTO radar_runs(id, kind, started_at, finished_at, status, "
                         "stats, error) VALUES('r-new','collect',?,NULL,'running','{}',NULL)",
                         (recent,))
            conn.commit()
            closed = store.finish_stale_runs(conn, grace_minutes=180)
            rows = {row["id"]: dict(row) for row in conn.execute(
                "SELECT * FROM radar_runs").fetchall()}
        finally:
            conn.close()
        self.assertEqual(closed, 1)
        self.assertEqual(rows["r-old"]["status"], "failed")
        self.assertTrue(rows["r-old"]["finished_at"])
        self.assertIn("未正常结束", rows["r-old"]["error"])
        self.assertEqual(rows["r-new"]["status"], "running")

    def _run_pipeline(self, patch_report=False):
        original_collect = sources.collect
        sources.collect = lambda conn, cfg: {
            "collected_at": _now_iso(), "sources_enabled": 0, "sources_ok": 0,
            "sources_failed": 0, "items_raw": 0, "items_kept": 0, "failures": [],
        }
        original_build = None
        if patch_report:
            from core.radar import report as report_mod
            original_build = report_mod.build
            report_mod.build = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))

        conn = db.connect()
        try:
            store.insert_items(conn, [_mk_item("候选一"), _mk_item("候选二")])
            conn.commit()
        finally:
            conn.close()
        try:
            return service.collect_and_report(self.cfg, reason="test")
        finally:
            sources.collect = original_collect
            if original_build is not None:
                from core.radar import report as report_mod
                report_mod.build = original_build

    def test_successful_pipeline_writes_snapshot_and_leaves_no_running_row(self):
        result = self._run_pipeline()
        conn = db.connect()
        try:
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM radar_runs ORDER BY started_at DESC").fetchall()]
            today = intel.today(self.cfg)
            snapshots = store.threat_daily_range(conn, today, today)
        finally:
            conn.close()
        self.assertTrue(result["ok"], result)
        self.assertNotIn("running", [row["status"] for row in rows])
        self.assertTrue(rows)
        self.assertTrue(rows[0]["finished_at"])
        self.assertEqual(rows[0]["status"], "ok")
        self.assertTrue(snapshots, "流水线应物化当日固定口径快照")
        self.assertEqual(int(snapshots[0]["caliber_version"]), 1)

    def test_failing_pipeline_ends_the_task_and_records_the_error(self):
        result = self._run_pipeline(patch_report=True)
        conn = db.connect()
        try:
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM radar_runs ORDER BY started_at DESC").fetchall()]
        finally:
            conn.close()
        self.assertFalse(result["ok"])
        self.assertIn("boom", str(result.get("error")))
        self.assertNotIn("running", [row["status"] for row in rows],
                         "失败任务不得留下 running 记录")
        failed = [row for row in rows if row["status"] == "failed"]
        self.assertTrue(failed)
        self.assertTrue(failed[0]["finished_at"])
        self.assertIn("boom", str(failed[0]["error"]))


# --------------------------------------------------------------------------- #
# 8) 迁移与幂等
# --------------------------------------------------------------------------- #

class TestMigration(unittest.TestCase):
    def test_init_is_idempotent_and_adds_threat_table(self):
        store.init()
        store.init()
        conn = db.connect()
        try:
            names = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'radar_%'").fetchall()}
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(radar_threat_daily)")}
        finally:
            conn.close()
        self.assertIn("radar_threat_daily", names)
        for name in ("date", "caliber_version", "activity", "credibility", "components",
                     "missing_inputs"):
            if name == "components":
                self.assertIn("activity_components", columns)
                self.assertIn("credibility_components", columns)
            else:
                self.assertIn(name, columns)

    def test_payload_has_generated_at_window_and_coverage(self):
        conn = db.connect()
        try:
            payload = intel.build_payload(conn, _cfg())
        finally:
            conn.close()
        self.assertTrue(payload["generated_at"])
        self.assertIn("from", payload["window"])
        self.assertIn("to", payload["window"])
        self.assertIn("history", payload["coverage"])
        self.assertIn("missing_inputs", payload)
        self.assertIn("degraded", payload)
        self.assertFalse(payload["llm"]["used"])
        self.assertTrue(payload["disclaimer"])


if __name__ == "__main__":
    unittest.main(verbosity=2)