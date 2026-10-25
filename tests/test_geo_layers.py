"""阶段四 B：EONET / GDACS 图层与国际新闻源的离线测试。

全部离线：解析用真实抓取样本（`tests/fixtures/eonet_events.json`、
`tests/fixtures/gdacs_events.json`），抓取用打桩，绝不依赖外部网络。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401,E402

from core import db  # noqa: E402
from core.geo import sources as geosrc  # noqa: E402
from core.radar import api as radar_api  # noqa: E402
from core.radar import config as rcfg  # noqa: E402
from core.radar import intel, sources, store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"

RESET_TABLES = (
    "radar_events", "radar_items", "radar_stories", "radar_sources",
    "radar_geo_points", "radar_threat_daily", "radar_reports",
    "radar_event_snapshots", "radar_claims", "radar_evidence",
)


def _fixture(name):
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return json.load(handle)


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


def _cfg(**overrides):
    cfg = rcfg.load()
    cfg["llm"] = {"enabled": False}
    cfg["anysearch"] = {"enabled": False}
    cfg.update(overrides)
    return cfg


EONET_SPEC = {"source_id": "eonet", "layer": "eonet", "adapter": "eonet",
              "max_items": 200, "days": 7}
GDACS_SPEC = {"source_id": "gdacs", "layer": "gdacs", "adapter": "gdacs",
              "max_items": 300, "current_only": False}


# --------------------------------------------------------------------------- #
# 1-5：解析、几何、幂等、无效坐标
# --------------------------------------------------------------------------- #

class TestEonetParsing(unittest.TestCase):
    def test_parses_captured_response(self):
        points = geosrc._parse_eonet(_fixture("eonet_events.json"), EONET_SPEC)
        self.assertEqual(len(points), 6)
        for point in points:
            self.assertTrue(point["external_id"].startswith("EONET_"))
            self.assertEqual(point["layer"], "eonet")
            self.assertTrue(-90 <= point["lat"] <= 90)
            self.assertTrue(-180 <= point["lon"] <= 180)
            self.assertNotEqual((point["lat"], point["lon"]), (0.0, 0.0))
            self.assertTrue(point["title"])
            self.assertIn(point["hazard"], set(geosrc.HAZARD_LABELS))
            self.assertIn(point["position_precision"], ("point", "area"))

    def test_hazard_mapping_from_categories(self):
        points = geosrc._parse_eonet(_fixture("eonet_events.json"), EONET_SPEC)
        hazards = {point["hazard"] for point in points}
        # 抓取样本里至少含野火；冰山归 other
        self.assertIn("wildfire", hazards)

    def test_spec_hazard_filter_is_applied(self):
        spec = dict(EONET_SPEC, hazards=["wildfire"])
        points = geosrc._parse_eonet(_fixture("eonet_events.json"), spec)
        self.assertTrue(points)
        self.assertTrue(all(point["hazard"] == "wildfire" for point in points))

    def test_polygon_is_area_not_a_precise_point(self):
        """Polygon 必须标 area，且中心落在多边形 bbox 内——不得冒充精确点。"""
        points = geosrc._parse_eonet(_fixture("eonet_polygon.json"), EONET_SPEC)
        self.assertEqual(len(points), 1)
        point = points[0]
        self.assertEqual(point["geometry_type"], "Polygon")
        self.assertEqual(point["position_precision"], "area")
        self.assertIsInstance(point["geometry_json"], list)
        ring = point["geometry_json"][0]
        xs = [c[0] for c in ring]
        ys = [c[1] for c in ring]
        self.assertTrue(min(xs) - 0.5 <= point["lon"] <= max(xs) + 0.5)
        self.assertTrue(min(ys) - 0.5 <= point["lat"] <= max(ys) + 0.5)

    def test_point_geometry_uses_source_coordinates(self):
        points = geosrc._parse_eonet(_fixture("eonet_events.json"), EONET_SPEC)
        point = next(p for p in points if p["geometry_type"] == "Point")
        self.assertEqual(point["position_precision"], "point")
        self.assertEqual(len(point["geometry_json"]), 2)

    def test_invalid_or_missing_geometry_is_skipped(self):
        payload = {"events": [
            {"id": "E1", "title": "bad coord",
             "categories": [{"id": "wildfires"}],
             "geometry": [{"type": "Point", "coordinates": [999, 99], "date": "2026-10-20T00:00:00Z"}]},
            {"id": "E2", "title": "null island",
             "categories": [{"id": "wildfires"}],
             "geometry": [{"type": "Point", "coordinates": [0, 0], "date": "2026-10-20T00:00:00Z"}]},
            {"id": "E3", "title": "no geometry", "categories": [{"id": "wildfires"}], "geometry": []},
            {"id": "E4", "title": "ok", "categories": [{"id": "wildfires"}],
             "geometry": [{"type": "Point", "coordinates": [10.5, 45.2], "date": "2026-10-20T00:00:00Z"}]},
        ]}
        points = geosrc._parse_eonet(payload, EONET_SPEC)
        self.assertEqual([p["external_id"] for p in points], ["E4"])

    def test_broken_payloads_raise(self):
        for payload in (None, [], {"nope": 1}, {"events": "x"}):
            with self.assertRaises(RuntimeError):
                geosrc._parse_eonet(payload, EONET_SPEC)


class TestGdacsParsing(unittest.TestCase):
    def test_parses_captured_response(self):
        points = geosrc._parse_gdacs(_fixture("gdacs_events.json"), GDACS_SPEC)
        self.assertEqual(len(points), 12)
        hazards = {point["hazard"] for point in points}
        self.assertIn("earthquake", hazards)
        self.assertIn("storm", hazards)
        for point in points:
            self.assertTrue(point["external_id"])
            self.assertTrue(-90 <= point["lat"] <= 90)
            self.assertIn(point["hazard"], set(geosrc.HAZARD_LABELS))
            self.assertEqual(point["position_precision"], "point")

    def test_unique_id_combines_type_event_episode(self):
        points = geosrc._parse_gdacs(_fixture("gdacs_events.json"), GDACS_SPEC)
        for point in points:
            parts = point["external_id"].split("-")
            self.assertEqual(len(parts), 3, point["external_id"])

    def test_current_only_filter(self):
        points = geosrc._parse_gdacs(_fixture("gdacs_events.json"),
                                     dict(GDACS_SPEC, current_only=True))
        for point in points:
            self.assertEqual(str(point["raw"].get("iscurrent", "true")).lower(), "true")
        self.assertLessEqual(len(points), 12)

    def test_invalid_coordinates_are_skipped(self):
        payload = {"features": [
            {"geometry": {"type": "Point", "coordinates": [0, 0]},
             "properties": {"eventtype": "EQ", "eventid": 1, "episodeid": 1}},
            {"geometry": {"type": "Point", "coordinates": [200, 10]},
             "properties": {"eventtype": "EQ", "eventid": 2, "episodeid": 1}},
            {"geometry": {"type": "Point", "coordinates": [10, 10]},
             "properties": {"eventtype": "EQ", "eventid": 3, "episodeid": 1}},
        ]}
        points = geosrc._parse_gdacs(payload, GDACS_SPEC)
        self.assertEqual([p["external_id"] for p in points], ["EQ-3-1"])

    def test_broken_payloads_raise(self):
        for payload in (None, [], {}, {"features": 5}):
            with self.assertRaises(RuntimeError):
                geosrc._parse_gdacs(payload, GDACS_SPEC)


class TestGeoPointIdempotency(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_repeat_capture_updates_instead_of_duplicating(self):
        points = geosrc._parse_gdacs(_fixture("gdacs_events.json"), GDACS_SPEC)
        for point in points:
            point["collected_at"] = "2026-10-26T04:00:00+08:00"
        conn = db.connect()
        try:
            first = store.upsert_geo_points(conn, points)
            conn.commit()
            count_first = conn.execute("SELECT COUNT(1) c FROM radar_geo_points").fetchone()["c"]
            second = store.upsert_geo_points(conn, points)
            conn.commit()
            count_second = conn.execute("SELECT COUNT(1) c FROM radar_geo_points").fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(count_first, len(points))
        self.assertEqual(count_second, count_first)
        self.assertEqual(first["inserted"], len(points))
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["updated"], len(points))

    def test_geometry_columns_roundtrip(self):
        points = geosrc._parse_eonet(_fixture("eonet_polygon.json"), EONET_SPEC)
        for point in points:
            point["collected_at"] = "2026-10-26T04:00:00+08:00"
        conn = db.connect()
        try:
            store.upsert_geo_points(conn, points)
            conn.commit()
            row = dict(conn.execute("SELECT * FROM radar_geo_points").fetchone())
        finally:
            conn.close()
        self.assertEqual(row["geometry_type"], "Polygon")
        self.assertEqual(row["position_precision"], "area")
        self.assertEqual(row["hazard"], "wildfire")
        self.assertIsInstance(json.loads(row["geometry_json"]), list)


# --------------------------------------------------------------------------- #
# 6-7：上游故障与失败隔离
# --------------------------------------------------------------------------- #

class TestUpstreamFailures(unittest.TestCase):
    def setUp(self):
        _reset()

    def _collect_with(self, cfg, patched):
        original = geosrc.fetch_points
        geosrc.fetch_points = patched
        conn = db.connect()
        try:
            return sources.collect(conn, cfg)
        finally:
            conn.close()
            geosrc.fetch_points = original

    def test_http_429_is_recorded_and_does_not_stop_others(self):
        """一个源 429/超时，其它源照常采集，健康状态各自记录。"""
        def patched(spec):
            if spec["source_id"] == "gdacs":
                raise RuntimeError("HTTP 429: rate limited")
            if spec["source_id"] == "eonet":
                return [dict(p, collected_at="2026-10-26T04:00:00+08:00")
                        for p in geosrc._parse_eonet(_fixture("eonet_events.json"), EONET_SPEC)]
            if spec["source_id"] == "usgs_quake":
                return []
            raise RuntimeError("unexpected: %s" % spec["source_id"])

        cfg = _cfg(sources={spec["source_id"]: {"enabled": spec.get("kind") == "geo"}
                            for spec in sources.SOURCES})
        summary = self._collect_with(cfg, patched)
        self.assertEqual(summary["sources_failed"], 1)
        self.assertTrue(any("429" in row["error"] for row in summary["failures"]))
        layers = {row["layer"]: row for row in summary["geo_layers"]}
        self.assertEqual(layers["eonet"]["points"], 6)

        conn = db.connect()
        try:
            rows = {row["source_id"]: dict(row) for row in conn.execute(
                "SELECT * FROM radar_sources WHERE source_id IN ('eonet','gdacs')")}
        finally:
            conn.close()
        self.assertEqual(rows["eonet"]["status"], "ok")
        self.assertTrue(rows["eonet"]["last_ok_at"])
        self.assertEqual(rows["gdacs"]["status"], "error")
        self.assertEqual(int(rows["gdacs"]["consecutive_failures"]), 1)
        self.assertIn("429", str(rows["gdacs"]["last_error"]))

    def test_empty_result_is_not_an_error(self):
        def patched(spec):
            return []

        cfg = _cfg(sources={spec["source_id"]: {"enabled": spec.get("kind") == "geo"}
                            for spec in sources.SOURCES})
        summary = self._collect_with(cfg, patched)
        self.assertEqual(summary["sources_failed"], 0)
        # 空结果算成功：调用方仍会记录一条 0 点位的图层统计，这不是错误
        self.assertTrue(all(row["points"] == 0 for row in summary["geo_layers"]))

    def test_geo_points_never_become_news_items(self):
        """地理图层不得污染新闻条目与聚类。"""
        def patched(spec):
            if spec["source_id"] == "eonet":
                return geosrc._parse_eonet(_fixture("eonet_events.json"), EONET_SPEC)
            return []

        cfg = _cfg(sources={spec["source_id"]: {"enabled": spec.get("kind") == "geo"}
                            for spec in sources.SOURCES})
        self._collect_with(cfg, patched)
        conn = db.connect()
        try:
            items = conn.execute("SELECT COUNT(1) c FROM radar_items").fetchone()["c"]
            points = conn.execute("SELECT COUNT(1) c FROM radar_geo_points").fetchone()["c"]
            events = conn.execute("SELECT COUNT(1) c FROM radar_events").fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(int(items), 0, "地理图层不应写入 radar_items")
        self.assertEqual(int(events), 0, "地理图层不应产生新闻事件")
        self.assertGreater(int(points), 0)


# --------------------------------------------------------------------------- #
# 8-10：地图负载、重复提示、国家字段语义
# --------------------------------------------------------------------------- #

class TestMapIntegration(unittest.TestCase):
    def setUp(self):
        _reset()

    def _seed_points(self):
        conn = db.connect()
        try:
            eonet = geosrc._parse_eonet(_fixture("eonet_events.json"), EONET_SPEC)
            gdacs = geosrc._parse_gdacs(_fixture("gdacs_events.json"), GDACS_SPEC)
            for point in eonet + gdacs:
                point["collected_at"] = "2026-10-26T04:00:00+08:00"
                # 统一到最近时间窗内，避免样本里的历史日期被窗口过滤掉。
                # closed_at 也要设：窗口判定用"活动区间是否与窗口重叠"。
                point["observed_at"] = "2026-10-25T00:00:00+00:00"
                point["closed_at"] = "2026-10-26T00:00:00+00:00"
            store.upsert_geo_points(conn, eonet + gdacs)
            conn.commit()
            return len(eonet) + len(gdacs)
        finally:
            conn.close()

    def test_map_payload_exposes_hazards_and_geometry(self):
        total = self._seed_points()
        conn = db.connect()
        try:
            payload = intel.map_payload(conn, _cfg(), days=30)
        finally:
            conn.close()
        self.assertTrue(payload["available"])
        self.assertEqual(len(payload["geo_points"]), total)
        self.assertIn("by_hazard", payload["counts"])
        self.assertIn("hazard_groups", payload)
        self.assertGreaterEqual(len(payload["hazard_groups"]), 4)
        first = payload["geo_points"][0]
        for key in ("layer", "external_id", "hazard", "geometry_type",
                    "position_precision", "title", "observed_at"):
            self.assertIn(key, first)

    def test_hazard_filter_narrows_points(self):
        self._seed_points()
        conn = db.connect()
        try:
            only_eq = intel.map_payload(conn, _cfg(), days=30, hazards=["earthquake"])
        finally:
            conn.close()
        self.assertTrue(only_eq["geo_points"])
        self.assertTrue(all(p["hazard"] == "earthquake" for p in only_eq["geo_points"]))

    def test_duplicate_hints_flag_but_do_not_merge(self):
        """跨来源疑似同一事件：只标记 related_sources，不合并、不减数。"""
        points = [
            {"layer": "usgs_quake", "external_id": "q1", "latitude": 35.60,
             "longitude": 139.70, "observed_at": "2026-10-25T10:00:00+00:00", "title": "quake"},
            {"layer": "gdacs", "external_id": "EQ-1-1", "latitude": 35.62,
             "longitude": 139.72, "observed_at": "2026-10-25T11:00:00+00:00", "title": "gdacs eq"},
            {"layer": "eonet", "external_id": "e-far", "latitude": -30.0,
             "longitude": -60.0, "observed_at": "2026-10-25T11:00:00+00:00", "title": "far"},
        ]
        pairs = intel._attach_duplicate_hints(points)
        self.assertEqual(pairs, 1)
        self.assertEqual(len(points), 3, "提示不能导致合并")
        self.assertTrue(points[0]["related_sources"])
        self.assertIn("未自动合并", points[0]["related_sources"][0]["note"])
        self.assertEqual(points[2]["related_sources"], [])

    def test_geo_points_carry_no_fabricated_country(self):
        """灾害点没有国家字段就不该有；不得把来源国当成事件国。"""
        self._seed_points()
        conn = db.connect()
        try:
            row = dict(conn.execute("SELECT * FROM radar_geo_points LIMIT 1").fetchone())
        finally:
            conn.close()
        self.assertNotIn("country_code", row)
        self.assertNotIn("event_country_code", row)

    def test_api_exposes_hazard_parameter(self):
        self._seed_points()
        status, payload = radar_api.handle_get("/api/radar/intel/map",
                                                {"days": "30", "hazards": "volcano"})
        self.assertEqual(status, 200)
        self.assertTrue(all(p["hazard"] == "volcano" for p in payload["geo_points"]))
        self.assertEqual(payload["filters"]["hazards"], ["volcano"])

    def test_main_endpoint_keeps_all_panels(self):
        self._seed_points()
        status, payload = radar_api.handle_get("/api/radar/intel", {})
        self.assertEqual(status, 200)
        for key in ("threat_timeline", "strategic", "intelligence_feed",
                    "live_news", "country_index", "map", "coverage", "degraded"):
            self.assertIn(key, payload)


# --------------------------------------------------------------------------- #
# 11：国际新闻源注册与语义
# --------------------------------------------------------------------------- #

class TestInternationalSources(unittest.TestCase):
    NEW = ("bbc_world", "guardian_world", "aljazeera", "npr_world")

    def test_new_sources_are_registered_as_rss(self):
        specs = {spec["source_id"]: spec for spec in sources.SOURCES}
        for source_id in self.NEW:
            self.assertIn(source_id, specs)
            spec = specs[source_id]
            self.assertEqual(spec["kind"], "rss")
            self.assertEqual(spec["adapter"], "rss")
            self.assertEqual(spec["language"], "en")
            self.assertTrue(spec["url"].startswith("https://"))
            self.assertNotIn("key", spec["url"].lower())

    def test_new_sources_use_existing_adapter(self):
        for source_id in self.NEW:
            spec = next(s for s in sources.SOURCES if s["source_id"] == source_id)
            self.assertIn(spec["adapter"], sources.ADAPTERS,
                          "必须复用现有 ADAPTERS，不能另起一套")

    def test_geo_specs_are_registered_and_not_news(self):
        specs = {spec["source_id"]: spec for spec in sources.SOURCES}
        for source_id in ("eonet", "gdacs", "usgs_quake"):
            self.assertEqual(specs[source_id]["kind"], "geo")
            self.assertIn(specs[source_id]["adapter"], sources.GEO_ADAPTERS)

    def test_layer_catalog_matches_connectivity(self):
        catalog = {layer["key"]: layer for layer in geosrc.layer_catalog()}
        for key in geosrc.IMPLEMENTED_LAYERS:
            self.assertTrue(catalog[key]["connected"], key)
            self.assertTrue(catalog[key]["url"], key)


if __name__ == "__main__":
    unittest.main(verbosity=2)