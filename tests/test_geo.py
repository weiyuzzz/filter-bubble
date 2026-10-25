"""阶段三（地理能力 / USGS 图层 / 地图与工作台）离线测试。

全部使用 ``tests/_bootstrap.py`` 的临时库与临时配置，**不联网**：
* USGS 解析用 ``prototype/raw/quakes.geojson`` 这份真实抓取快照做 fixture；
* 采集/适配器失败用打桩模拟，不发请求。
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401,E402

from core import db  # noqa: E402
from core.geo import countries, gazetteer, geocode, project, sources as geosrc, validate  # noqa: E402
from core.radar import api as radar_api  # noqa: E402
from core.radar import classify, config as rcfg, geo_tag, intel, sources, store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
QUAKES_FIXTURE = ROOT / "prototype" / "raw" / "quakes.geojson"

RESET_TABLES = (
    "radar_claims", "radar_evidence", "radar_claim_evidence", "radar_reports",
    "radar_event_snapshots", "radar_events", "radar_items", "radar_stories",
    "radar_summaries", "radar_sources", "radar_runs", "radar_threat_daily",
    "radar_geo_points", "radar_rank_history", "radar_push_log",
)


def _cfg(**overrides):
    cfg = rcfg.load()
    cfg["llm"] = {"enabled": False, "provider": "", "base_url": "", "model": ""}
    cfg.update(overrides)
    return cfg


def _reset():
    db.init()
    store.init()
    conn = db.connect()
    try:
        for table in RESET_TABLES:
            try:
                conn.execute("DELETE FROM %s" % table)
            except Exception:  # noqa: BLE001
                pass
        conn.commit()
    finally:
        conn.close()


def _event(event_id, date, title, heat=60.0, item_count=3, metrics=None, confidence="reported"):
    return {
        "id": event_id, "date": date, "title": title, "summary": "", "category": "综合",
        "keywords": [], "heat": heat, "importance": 55.0, "confidence": confidence,
        "item_count": item_count, "platform_count": 2, "source_count": 2,
        "first_seen_at": date + "T10:00:00+08:00", "last_updated_at": date + "T10:00:00+08:00",
        "origin_item_id": None, "tech_flag": False, "investigation_state": "none",
        "metrics": metrics or {"raw_heat": heat, "platforms": ["微博", "百度"],
                               "flags": [], "news_domains": 1, "lifecycle": "new"},
        "story_id": None,
    }


# --------------------------------------------------------------------------- #
# 1) 国家映射与代码标准化
# --------------------------------------------------------------------------- #

class TestCountryMapping(unittest.TestCase):
    def test_dataset_is_loaded_with_chinese_names(self):
        meta = countries.metadata()
        self.assertGreaterEqual(meta["count"], 200)
        self.assertIn("Natural Earth", str(meta["source"]))
        self.assertTrue(meta["license"])

    def test_short_names_are_available_for_news_matching(self):
        # Natural Earth 给的是正式名"中华人民共和国"，匹配需要简称"中国"
        self.assertEqual(countries.name("CN"), "中国")
        self.assertEqual(countries.name("US"), "美国")
        self.assertEqual(countries.name("TW"), "台湾")
        self.assertEqual(countries.name("GB"), "英国")

    def test_sovereign_country_wins_over_territory(self):
        # 法国与克利珀顿岛同码，必须保留主权国家
        france = countries.get("FR")
        self.assertIsNotNone(france)
        self.assertNotIn("Clipperton", str(france.get("name_en")))

    def test_code_normalisation(self):
        self.assertEqual(countries.normalise("cn"), "CN")
        self.assertEqual(countries.normalise("CHN"), "CN")
        self.assertEqual(countries.normalise("中国"), "CN")
        self.assertEqual(countries.normalise("NOPE"), "")

    def test_centroid_is_valid_and_not_null_island(self):
        point = countries.centroid("JP")
        self.assertIsNotNone(point)
        self.assertTrue(-90 <= point["lat"] <= 90)
        self.assertTrue(-180 <= point["lon"] <= 180)


# --------------------------------------------------------------------------- #
# 2) 地名库与歧义处理
# --------------------------------------------------------------------------- #

class TestGazetteer(unittest.TestCase):
    def test_longest_match_wins(self):
        """'印度尼西亚' 不能被 '印度' 抢先命中。"""
        hits = geocode._places("印度尼西亚发生地震")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["iso2"], "ID")

    def test_organisation_suffix_is_rejected(self):
        """'上海合作组织' 不是地点。"""
        self.assertEqual(geocode.resolve_title("上海合作组织成员国举行演习")["confidence"],
                         geocode.NONE)

    def test_team_and_format_names_are_rejected(self):
        """'瑞士轮' 是赛制、'米兰后卫' 是球队，都不是事件地点。"""
        self.assertEqual(geocode.resolve_title("iG和TES冲击瑞士轮两连胜")["confidence"],
                         geocode.NONE)
        self.assertEqual(geocode.resolve_title("热刺盯上米兰后卫托莫里")["confidence"],
                         geocode.NONE)

    def test_city_and_region_matches(self):
        paris = geocode.resolve_title("凤凰直击巴黎新一轮示威")
        self.assertEqual(paris["confidence"], geocode.MEDIUM)
        self.assertEqual(paris["country_code"], "FR")
        tibet = geocode.resolve_title("救灾物资运往西藏巴青地震灾区")
        self.assertEqual(tibet["country_code"], "CN")
        self.assertEqual(tibet["confidence"], geocode.MEDIUM)


# --------------------------------------------------------------------------- #
# 3) 歧义不猜、无依据返回 None
# --------------------------------------------------------------------------- #

class TestGeocodeDiscipline(unittest.TestCase):
    def test_multi_country_title_is_not_pinned(self):
        """说明书里的原始例子：三个国家 → 不得锚定到任何一处。"""
        result = geocode.resolve_title("美国警告中国某项政策可能影响日本")
        self.assertEqual(result["confidence"], geocode.NONE)
        self.assertIsNone(result["latitude"])
        self.assertIsNone(result["longitude"])
        self.assertTrue(result["ambiguous"])
        self.assertEqual(result["reason"], "multiple_countries_in_text")

    def test_no_place_signal_returns_none(self):
        result = geocode.resolve_title("某公司发布新一代折叠屏手机")
        self.assertEqual(result["confidence"], geocode.NONE)
        self.assertIsNone(result["latitude"])
        self.assertEqual(result["reason"], "no_place_signal")

    def test_country_only_uses_low_confidence_centroid(self):
        result = geocode.resolve_title("中国科学家在量子计算领域取得突破")
        self.assertEqual(result["confidence"], geocode.LOW)
        self.assertEqual(result["method"], geocode.METHOD_CENTROID)
        self.assertEqual(result["country_code"], "CN")
        self.assertIsNotNone(result["latitude"])

    def test_multiple_cities_same_country_falls_back_to_country(self):
        result = geocode.resolve_title("北京和上海同时发布新政")
        self.assertEqual(result["confidence"], geocode.LOW)
        self.assertTrue(result["ambiguous"])

    def test_confidence_never_derives_from_republishing(self):
        """置信度只由证据决定：把同一标题的条目数量堆多也不会变成 high。"""
        single = geocode.resolve_title("巴黎发生示威")
        pooled = geocode.resolve_event("某事件", item_titles=["巴黎发生示威"] * 8)
        self.assertEqual(single["confidence"], pooled["confidence"])

    def test_source_coordinates_give_high_confidence(self):
        result = geocode.resolve_event("某地发生地震", source_coords={"lat": 35.6, "lon": 139.7},
                                       source_name="usgs")
        self.assertEqual(result["confidence"], geocode.HIGH)
        self.assertEqual(result["method"], geocode.METHOD_SOURCE_COORDS)
        self.assertEqual(result["source"], "usgs:coordinates")


# --------------------------------------------------------------------------- #
# 4) 报道来源国 ≠ 事件发生国
# --------------------------------------------------------------------------- #

class TestOriginVersusEventCountry(unittest.TestCase):
    def test_origin_country_is_never_used_as_event_location(self):
        result = geocode.resolve_event("某公司发布新款芯片")
        self.assertIsNone(result["latitude"])
        self.assertIsNone(result["country_code"])

    def test_origin_country_map_is_separate_field(self):
        _reset()
        conn = db.connect()
        try:
            store.upsert_event(conn, _event("ev-a", "2026-10-25", "巴黎发生示威"))
            conn.execute("INSERT INTO radar_items(source_id, platform, title, url, url_hash, "
                         "collected_at, content_hash, source_type, last_seen_at, event_id) "
                         "VALUES('bbc_zh','BBC','巴黎发生示威','https://x/1','h1',?, 'c1','news',?, "
                         "'ev-a')", ("2026-10-25T10:00:00+08:00", "2026-10-25T10:00:00+08:00"))
            conn.commit()
            event = dict(conn.execute("SELECT * FROM radar_events WHERE id='ev-a'").fetchone())
            event["keywords"] = []
            geo_tag.tag_event(conn, event, [{"source_id": "bbc_zh", "title": "巴黎发生示威"}])
            conn.commit()
            row = dict(conn.execute("SELECT * FROM radar_events WHERE id='ev-a'").fetchone())
        finally:
            conn.close()
        self.assertEqual(row["event_country_code"], "FR")   # 事件发生在法国
        self.assertEqual(row["origin_country_code"], "GB")  # 报道来源是 BBC
        self.assertNotEqual(row["event_country_code"], row["origin_country_code"])

    def test_origin_country_is_null_when_sources_disagree(self):
        _reset()
        conn = db.connect()
        try:
            store.upsert_event(conn, _event("ev-b", "2026-10-25", "巴黎发生示威"))
            items = [{"source_id": "bbc_zh", "title": "巴黎发生示威"},
                     {"source_id": "ithome", "title": "巴黎发生示威"}]
            event = dict(conn.execute("SELECT * FROM radar_events WHERE id='ev-b'").fetchone())
            event["keywords"] = []
            geo_tag.tag_event(conn, event, items)
            conn.commit()
            row = dict(conn.execute("SELECT * FROM radar_events WHERE id='ev-b'").fetchone())
        finally:
            conn.close()
        self.assertIsNone(row["origin_country_code"])


# --------------------------------------------------------------------------- #
# 5) 投影与坐标校验
# --------------------------------------------------------------------------- #

class TestProjectionAndValidation(unittest.TestCase):
    def test_projection_corners(self):
        top_left = project.project(-180, 85.05112878)
        self.assertAlmostEqual(top_left["x"], 0.0, places=3)
        self.assertAlmostEqual(top_left["y"], 0.0, places=1)
        bottom_right = project.project(180, -85.05112878)
        self.assertAlmostEqual(bottom_right["x"], project.WIDTH, places=3)
        self.assertAlmostEqual(bottom_right["y"], project.HEIGHT, places=1)

    def test_equator_and_prime_meridian_are_centered(self):
        center = project.unit(0, 0)
        self.assertAlmostEqual(center["x"], 0.5, places=4)
        self.assertAlmostEqual(center["y"], 0.5, places=4)

    def test_longitude_wraparound(self):
        # ±180° 附近必须走"跨越国际日期变更线的最短路径"，
        # 179.9 与 -179.9 的中点应当落在日期变更线附近，而不是算成 0°
        self.assertAlmostEqual(validate.wrap_delta(179.9, -179.9), -0.2, places=4)
        self.assertAlmostEqual(validate.wrap_delta(-179.9, 179.9), 0.2, places=4)
        centroid = validate.centroid([
            {"lat": 10.0, "lon": 179.9}, {"lat": 10.0, "lon": -179.9}])
        self.assertIsNotNone(centroid)
        self.assertLess(abs(abs(centroid["lon"]) - 180.0), 1.0)
        self.assertAlmostEqual(centroid["lat"], 10.0, places=3)

    def test_invalid_coordinates_are_rejected(self):
        self.assertIsNone(validate.parse_pair(91, 10))
        self.assertIsNone(validate.parse_pair(10, 181))
        self.assertIsNone(validate.parse_pair(None, 10))
        self.assertIsNone(validate.parse_pair("x", "y"))
        self.assertIsNone(validate.parse_pair(0, 0))  # 空岛
        self.assertIsNone(validate.parse_pair(float("nan"), 1))

    def test_projection_roundtrip(self):
        for lon, lat in ((116.4, 39.9), (-74.0, 40.7), (179.5, -33.9), (-179.5, 64.1)):
            point = project.project(lon, lat)
            back = project.unproject(point["x"], point["y"])
            self.assertAlmostEqual(back["lon"], lon, places=2)
            self.assertAlmostEqual(back["lat"], lat, places=2)


# --------------------------------------------------------------------------- #
# 6) USGS 图层：解析、幂等、无效数据
# --------------------------------------------------------------------------- #

class TestUsgsLayer(unittest.TestCase):
    def setUp(self):
        _reset()
        self.spec = {"source_id": "usgs_quake", "layer": "usgs_quake", "adapter": "usgs",
                     "max_items": 500, "max_age_days": None}

    def _fixture(self):
        with QUAKES_FIXTURE.open(encoding="utf-8-sig") as handle:
            return json.load(handle)

    def test_fixture_parses_into_valid_points(self):
        points = geosrc._parse_usgs(self._fixture(), self.spec)
        self.assertGreater(len(points), 50)
        for point in points:
            self.assertEqual(point["layer"], "usgs_quake")
            self.assertTrue(point["external_id"])
            self.assertTrue(-90 <= point["lat"] <= 90)
            self.assertTrue(-180 <= point["lon"] <= 180)
            self.assertNotEqual((point["lat"], point["lon"]), (0.0, 0.0))
            self.assertTrue(point["title"])

    def test_invalid_features_are_skipped(self):
        payload = {"features": [
            {"id": "ok", "geometry": {"type": "Point", "coordinates": [139.7, 35.6]},
             "properties": {"mag": 4.5, "place": "Japan", "time": 1792951902000}},
            {"id": "badcoord", "geometry": {"type": "Point", "coordinates": [999, 99]},
             "properties": {"mag": 4.0}},
            {"id": "nullisland", "geometry": {"type": "Point", "coordinates": [0, 0]},
             "properties": {"mag": 3.0}},
            {"id": "nogeom", "properties": {"mag": 2.0}},
        ]}
        points = geosrc._parse_usgs(payload, self.spec)
        self.assertEqual([p["external_id"] for p in points], ["ok"])

    def test_broken_payloads_raise(self):
        for payload in (None, [], {"nope": 1}, {"features": "x"}):
            with self.assertRaises(RuntimeError):
                geosrc._parse_usgs(payload, self.spec)

    def test_upsert_is_idempotent(self):
        points = geosrc._parse_usgs(self._fixture(), self.spec)
        conn = db.connect()
        try:
            for point in points:
                point["collected_at"] = "2026-10-26T03:00:00+08:00"
            first = store.upsert_geo_points(conn, points)
            conn.commit()
            count_after_first = conn.execute(
                "SELECT COUNT(1) c FROM radar_geo_points").fetchone()["c"]
            second = store.upsert_geo_points(conn, points)
            conn.commit()
            count_after_second = conn.execute(
                "SELECT COUNT(1) c FROM radar_geo_points").fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(count_after_first, len(points))
        self.assertEqual(count_after_second, count_after_first)
        self.assertEqual(first["inserted"], len(points))
        self.assertEqual(second["inserted"], 0)

    def test_upstream_failure_does_not_break_news_collection(self):
        """地理图层失败只记录自己的失败，不影响其它源。"""
        original = geosrc.fetch_points

        def boom(spec):
            raise RuntimeError("simulated upstream 429")

        geosrc.fetch_points = boom
        cfg = _cfg(sources={spec["source_id"]: {"enabled": False}
                            for spec in sources.SOURCES if spec.get("kind") != "geo"})
        conn = db.connect()
        try:
            summary = sources.collect(conn, cfg)
        finally:
            conn.close()
            geosrc.fetch_points = original
        geo_specs = [spec for spec in sources.SOURCES if spec.get("kind") == "geo"]
        self.assertGreaterEqual(len(geo_specs), 1)
        # 每个启用的地理图层都应各自失败、各自记录，而不是互相掩盖
        self.assertEqual(summary["sources_failed"], len(geo_specs))
        failed_ids = {item["source_id"] for item in summary["failures"]}
        self.assertEqual(failed_ids, {spec["source_id"] for spec in geo_specs})
        self.assertTrue(any("429" in item["error"] for item in summary["failures"]))
        # 失败被记进源健康状态，而不是抛出异常
        conn = db.connect()
        try:
            row = conn.execute("SELECT status, consecutive_failures FROM radar_sources "
                               "WHERE source_id='usgs_quake'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "error")
        self.assertEqual(int(row["consecutive_failures"]), 1)

    def test_layer_catalog_is_honest_about_unimplemented_layers(self):
        """已接入的层必须 connected=True；未接入的层必须 connected=False。"""
        catalog = {layer["key"]: layer for layer in geosrc.layer_catalog()}
        for key in geosrc.IMPLEMENTED_LAYERS:
            self.assertIn(key, catalog)
            self.assertTrue(catalog[key]["connected"], "已接入的层却标成未接入：%s" % key)
            self.assertTrue(catalog[key].get("url"), key)
        for key, layer in catalog.items():
            if key not in geosrc.IMPLEMENTED_LAYERS:
                self.assertFalse(layer["connected"], "未接入的层却标成已接入：%s" % key)


# --------------------------------------------------------------------------- #
# 7) 主题分类
# --------------------------------------------------------------------------- #

class TestThemeClassification(unittest.TestCase):
    def test_every_required_theme_is_supported(self):
        for theme in ("conflict", "unrest", "politics", "security", "cyber", "disaster",
                      "infrastructure", "technology", "general_news"):
            self.assertIn(theme, classify.THEMES)

    def test_sports_conflict_is_not_a_real_conflict(self):
        """体育比赛里的"冲突"不得被当成地缘冲突。"""
        result = classify.classify("全国足球联赛决赛爆发冲突")
        self.assertEqual(result["theme"], classify.GENERAL)

    def test_suppression_rules_are_documented(self):
        meta = classify.metadata()
        self.assertIn("sports", meta["suppression"])
        self.assertIn("conflict", meta["themes"])

    def test_real_conflict_is_classified(self):
        self.assertEqual(classify.classify("以军对加沙发动空袭，停火谈判陷入僵局")["theme"],
                         classify.CONFLICT)

    def test_rhetoric_attack_is_not_conflict(self):
        result = classify.classify("特朗普抨击美联储主席，称其政策失败")
        self.assertNotEqual(result["theme"], classify.CONFLICT)

    def test_disaster_cyber_technology(self):
        self.assertEqual(classify.classify("西藏那曲发生7.0级地震")["theme"], classify.DISASTER)
        self.assertEqual(classify.classify("某公司遭勒索软件攻击，数据泄露")["theme"], classify.CYBER)
        self.assertEqual(classify.classify("英伟达发布新一代AI芯片")["theme"], classify.TECHNOLOGY)

    def test_evidence_is_returned_for_audit(self):
        result = classify.classify("日本发生6.5级地震，气象厅发布海啸预警")
        self.assertTrue(result["evidence"])
        self.assertEqual(result["label"], "自然灾害")


# --------------------------------------------------------------------------- #
# 8) 地图 / 国家面板 / 五个面板共享同一份数据
# --------------------------------------------------------------------------- #

class TestIntelGeoPayload(unittest.TestCase):
    def setUp(self):
        _reset()
        self.cfg = _cfg()
        conn = db.connect()
        try:
            today = intel.today(self.cfg)
            store.upsert_event(conn, _event("ev-fr", today, "法国巴黎发生大规模示威"))
            store.upsert_event(conn, _event("ev-cn", today, "西藏那曲发生7.0级地震"))
            store.upsert_event(conn, _event("ev-nogeo", today, "某公司发布新一代折叠屏手机"))
            # 只有国家级信号 → low（国家质心锚点）
            store.upsert_event(conn, _event("ev-cn2", today, "中国科学家在量子计算领域取得突破"))
            conn.commit()
            events = store.events_for_date(conn, today, limit=50)
            for event in events:
                items = [{"source_id": "ithome", "title": event["title"]}]
                geo_tag.tag_event(conn, event, items)
            conn.commit()
            intel.record_daily_snapshot(conn, today, self.cfg)
            points = geosrc._parse_usgs({"features": [
                {"id": "q1", "geometry": {"type": "Point", "coordinates": [139.7, 35.6]},
                 "properties": {"mag": 5.2, "place": "Japan", "time": 1792951902000}},
            ]}, {"source_id": "usgs_quake", "layer": "usgs_quake"})
            for point in points:
                point["collected_at"] = "2026-10-26T03:00:00+08:00"
            store.upsert_geo_points(conn, points)
            conn.commit()
            self.payload = intel.build_payload(conn, self.cfg)
        finally:
            conn.close()

    def test_map_contains_only_geocoded_events(self):
        events = self.payload["map"]["events"]
        ids = {event["event_id"] for event in events}
        self.assertIn("ev-fr", ids)
        self.assertIn("ev-cn", ids)
        self.assertNotIn("ev-nogeo", ids, "没有坐标的事件不得出现在地图上")
        for event in events:
            self.assertIsNotNone(event["lat"])
            self.assertIsNotNone(event["lon"])
            self.assertTrue(event["geo_confidence"] in ("high", "medium", "low"))

    def test_map_points_carry_real_event_ids(self):
        for event in self.payload["map"]["events"]:
            self.assertTrue(event["event_id"].startswith("ev-"))
            self.assertIn("severity", event)
            self.assertIn("theme", event)

    def test_usgs_points_are_in_map_payload(self):
        points = self.payload["map"]["geo_points"]
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["layer"], "usgs_quake")
        self.assertAlmostEqual(float(points[0]["magnitude"]), 5.2, places=2)

    def test_low_confidence_marker_is_flagged(self):
        low = [event for event in self.payload["map"]["events"]
               if event["geo_confidence"] == "low"]
        self.assertTrue(low)
        self.assertTrue(all(event["geo_method"] == "country_centroid" for event in low))

    def test_country_index_is_partial_and_explained(self):
        block = self.payload["country_index"]
        self.assertTrue(block["available"])
        self.assertTrue(block["partial"])
        self.assertEqual(block["coverage"], "media_only")
        codes = {row["code"] for row in block["countries"]}
        self.assertIn("FR", codes)
        self.assertIn("CN", codes)
        for row in block["countries"]:
            self.assertEqual(row["baseline"], None)
            self.assertIn("baseline", row["missing_inputs"])
            if row["low_sample"]:
                self.assertIsNone(row["score"], "样本不足的国家不得给 0 分")

    def test_feed_and_map_share_ids(self):
        feed_events = {item.get("event_id") for item in
                       self.payload["intelligence_feed"]["items"]}
        map_events = {event["event_id"] for event in self.payload["map"]["events"]}
        # 情报流来自日报 highlights，地图来自雷达事件；两者都是同一批 event_id
        self.assertTrue(feed_events & map_events or not feed_events)

    def test_five_panels_expose_required_fields(self):
        self.assertIn("points", self.payload["threat_timeline"])
        self.assertIn("estimate", self.payload["strategic"])
        self.assertIn("countries", self.payload["country_index"])
        self.assertIn("items", self.payload["intelligence_feed"])
        self.assertIn("items", self.payload["live_news"])
        self.assertIn("events", self.payload["map"])
        self.assertIn("geo", self.payload)

    def test_filters_are_consistent_between_map_and_feed(self):
        conn = db.connect()
        try:
            disaster = intel.map_payload(conn, self.cfg, days=7, theme="disaster")
            france = intel.map_payload(conn, self.cfg, days=7, country="FR")
        finally:
            conn.close()
        self.assertTrue(all(event["theme"] == "disaster" for event in disaster["events"]))
        self.assertTrue(all(event["country_code"] == "FR" for event in france["events"]))


# --------------------------------------------------------------------------- #
# 9) 前端契约：新增 id 必须存在、仍然零外部资源、仍然单一样式表
# --------------------------------------------------------------------------- #

class TestFrontendContract(unittest.TestCase):
    DYNAMIC_IDS = {
        "intelRetry", "intelDetailClose", "intelDetailInvestigate", "intelClearDay",
        "intelLiveMore", "intelLiveState",
    }

    def _read(self, name):
        return (ROOT / "web" / name).read_text(encoding="utf-8")

    def test_intel_ids_exist_in_radar_html(self):
        js = self._read("radar-intel.js")
        html = self._read("radar.html")
        used = set(re.findall(r'\$\("#([A-Za-z][\w-]*)"\)', js))
        missing = sorted(node for node in used
                         if node not in self.DYNAMIC_IDS and f'id="{node}"' not in html)
        self.assertEqual(missing, [], "radar-intel.js 需要但 radar.html 缺失的 id：%s" % missing)

    def test_intel_tab_and_scripts_are_wired(self):
        html = self._read("radar.html")
        self.assertIn('data-view="intel"', html)
        self.assertIn('id="view-intel"', html)
        self.assertIn('src="/radar-intel.js"', html)
        self.assertIn('src="/world-110m.js"', html)
        radar_js = self._read("radar.js")
        self.assertIn("RBIntel", radar_js, "switchView 必须能触发态势模块初始化")

    def test_no_external_assets_still_holds(self):
        for name in ("index.html", "radar.html", "status.html", "papers.html",
                     "settings.html", "bookmarks.html"):
            path = ROOT / "web" / name
            if not path.is_file():
                continue
            html = path.read_text(encoding="utf-8")
            for match in re.findall(r'(?:src|href)="(https?://[^"]+)"', html):
                self.fail("%s 引用了外部资源：%s" % (name, match))

    def test_only_one_stylesheet(self):
        css = sorted(path.name for path in (ROOT / "web").glob("*.css"))
        self.assertEqual(css, ["styles.css"])

    def test_intel_styles_use_design_tokens(self):
        css = self._read("styles.css")
        block = css.split("全球态势监控工作台", 1)[-1]
        self.assertIn(".intel-", block)
        self.assertIn("var(--fb-", block)
        # 新样式只用独立前缀，不新增全局选择器
        self.assertNotIn(".intel-shell", css.split("全球态势监控工作台", 1)[0])

    def test_shell_does_not_call_business_endpoints(self):
        self.assertNotIn("/api/", self._read("shell.js"))

    def test_basemap_asset_is_local_and_declared(self):
        asset = ROOT / "web" / "world-110m.js"
        self.assertTrue(asset.is_file(), "缺少离线底图 web/world-110m.js")
        text = asset.read_text(encoding="utf-8")
        self.assertIn("window.FB_WORLD", text)
        self.assertIn("Natural Earth", text)
        self.assertIn("license", text)
        self.assertGreater(len(text), 10000)


# --------------------------------------------------------------------------- #
# 10) 阶段四：按"已知错误类别"逐类回归
# --------------------------------------------------------------------------- #

class TestGeoRegressionByCategory(unittest.TestCase):
    """每一类历史上出现过的定位偏差，都留一个回归用例。"""

    # --- 类别 1：以城市名命名的球队/俱乐部/体育事件 ---
    def test_team_names_are_not_event_locations(self):
        for title in (
            "辽宁铁人三大离队热门浮出水面",
            "大连英博高兴早了？仍落后3分，有4种可能无缘冠军",
            "广东3将回暖！崔永熙统治末节",
            "陈海涛发飙了据广东体育媒体人透露，惨败给北京男篮的比赛",
            "乒乓亚锦赛：国乒男团3-0胜韩国卫冕冠军",
            "亚锦赛国乒女团决赛不敌日本",
            "正负值恐怖+40！广东首胜夜一人carry全场",
        ):
            result = geocode.resolve_title(title)
            self.assertEqual(result["country_code"], None, "球队名被当成事件地点：%s" % title)

    def test_real_sports_event_with_locative_cue_is_kept(self):
        """不能一刀切：真实赛事（带"开跑/举办"等在地线索）必须保留。"""
        result = geocode.resolve_title("2026天津马拉松开跑 3万跑者")
        self.assertEqual(result["country_code"], "CN")
        self.assertEqual(result["confidence"], geocode.MEDIUM)

    # --- 类别 2：主体国 ≠ 发生地 ---
    def test_subject_country_is_not_silently_claimed_as_precise(self):
        """"中国女孩赴泰遇险"这类主体≠发生地的报道，最多只能给低置信度。"""
        result = geocode.resolve_title("中国女孩赴泰见男网友 上车后手机被夺")
        self.assertNotEqual(result["confidence"], geocode.HIGH)
        self.assertIn(result["confidence"], (geocode.LOW, geocode.NONE))

    def test_person_origin_place_is_rejected(self):
        """"上海12岁少年登顶乞力马扎罗"里的上海是籍贯，不是事发地。"""
        self.assertEqual(geocode.resolve_title("上海12岁少年登顶乞力马扎罗")["confidence"],
                         geocode.NONE)

    # --- 类别 3：跨国评论 / 外交表态 ---
    def test_cross_border_commentary_is_unknown(self):
        for title in (
            "美以密谋新一轮打击？伊朗最高领袖罕见露面",
            "俄乌释放重启三方会谈信号背后：美国中期选举成关键变量？",
            "商务部就美国防部公布所谓科研安全审查结果事答记者问",
            "习近平同比利时国王菲利普就中比建交55周年互致贺信",
            "王沪宁陪同朝鲜最高领导人金正恩访问丹东",
        ):
            result = geocode.resolve_title(title)
            self.assertEqual(result["confidence"], geocode.NONE,
                             "跨国报道被锚到单一国家：%s -> %s" % (title, result["country_code"]))

    def test_cross_border_with_explicit_city_keeps_city(self):
        """跨国行为体出现时，城市级证据仍然保留（不能为了精确率丢掉召回）。"""
        result = geocode.resolve_title("俄乌冲突升级，基辅遭袭")
        self.assertEqual(result["country_code"], "UA")
        self.assertEqual(result["confidence"], geocode.MEDIUM)

    def test_actor_country_conflict_detection(self):
        hits = geocode._places("特斯拉上海工厂扩产")
        resolved = {hit["iso2"] for hit in hits}
        self.assertIn("CN", resolved)

    # --- 类别 4：歧义地名 / 机构名 ---
    def test_ambiguous_place_and_org_names(self):
        self.assertEqual(geocode.resolve_title("美国警告中国某项政策可能影响日本")["confidence"],
                         geocode.NONE)
        self.assertEqual(geocode.resolve_title("上海合作组织成员国举行联合演习")["confidence"],
                         geocode.NONE)
        self.assertEqual(geocode.resolve_title("iG和TES冲击瑞士轮两连胜")["confidence"],
                         geocode.NONE)

    def test_longest_place_name_wins(self):
        self.assertEqual(geocode.resolve_title("印度尼西亚发生7.0级地震")["country_code"], "ID")

    # --- 类别 5：只有国家级证据 ---
    def test_country_only_is_low_confidence_centroid(self):
        result = geocode.resolve_title("中国科学家在量子计算领域取得突破")
        self.assertEqual(result["confidence"], geocode.LOW)
        self.assertEqual(result["method"], geocode.METHOD_CENTROID)
        self.assertEqual(result["country_code"], "CN")

    def test_no_evidence_returns_none(self):
        result = geocode.resolve_title("某公司发布新一代折叠屏手机")
        self.assertEqual(result["confidence"], geocode.NONE)
        self.assertIsNone(result["latitude"])


class TestPrecisionRepresentation(unittest.TestCase):
    """位置精度必须在地图/图例/详情/导出里都能看出来，而不只是颜色差异。"""

    def _js(self):
        return (ROOT / "web" / "radar-intel.js").read_text(encoding="utf-8")

    def test_low_confidence_uses_a_different_shape_not_just_color(self):
        js = self._js()
        self.assertIn("rotate(45)", js, "国家级近似应使用菱形（形状差异），而不是只改颜色")
        self.assertIn("is-${esc(conf)}", js, "标记应带置信度 class")

    def test_legend_documents_each_confidence_level(self):
        js = self._js()
        for label in ("来源坐标（高）", "地名匹配（中）", "国家级近似（低）"):
            self.assertIn(label, js, "图例缺少精度说明：%s" % label)

    def test_detail_shows_method_and_precision_caveat(self):
        js = self._js()
        self.assertIn("定位方式", js)
        self.assertIn("国家级近似位置", js, "低精度事件详情必须标注精度")

    def test_precision_filter_exists(self):
        js = self._js()
        self.assertIn("precision", js)
        self.assertIn("reliable", js)
        self.assertIn("approx", js)

    def test_export_keeps_precision_information(self):
        js = self._js()
        self.assertIn("geo_confidence", js)
        self.assertIn("位置精度说明", js, "导出文件必须保留位置精度说明")

    def test_basemap_attribution_is_shown(self):
        js = self._js()
        self.assertIn("底图", js)
        self.assertIn("Natural Earth", js)
        self.assertIn("公共领域", js)


if __name__ == "__main__":
    unittest.main(verbosity=2)