"""AnySearch 接入测试。离线：所有网络调用都被替换掉，不发真实请求。"""
from __future__ import annotations

import datetime as dt
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401,E402  (isolated DB + project root on sys.path)

from core import db  # noqa: E402
from core.radar import config as rcfg, investigate, sources, store  # noqa: E402
from core.radar.sources import anysearch  # noqa: E402


def _cfg(external_search=True, extract_top=0, min_local=3, **anysearch_overrides):
    """测试用配置：把 overrides 明确分给 anysearch 段和 investigation 段。"""
    cfg = rcfg.load()
    section = dict(cfg.get("anysearch") or {})
    section.update({"enabled": True, "queries": []})
    section.update(anysearch_overrides)
    cfg["anysearch"] = section
    cfg["investigation"] = dict(cfg.get("investigation") or {})
    cfg["investigation"].update({"external_search": external_search,
                                 "external_extract_top": extract_top,
                                 "min_local_evidence": min_local,
                                 "max_evidence": 60})
    return cfg


def _row(idx, title=None, url=None):
    return {"title": title or "搜索结果 %d" % idx,
            "url": url or "https://example%d.com/post" % idx,
            "snippet": "摘要 %d" % idx,
            "content": "正文内容 %d，包含一些分析。" % idx}


class TestDiscoverySource(unittest.TestCase):
    """方案 B：每条查询 = 一个发现源。"""

    def test_queries_become_sources(self):
        cfg = _cfg(queries=[{"id": "ai", "name": "AI", "query": "AI 进展"},
                            {"query": "芯片 制程"}])
        specs = [s for s in sources.all_specs(cfg) if s.get("adapter") == "anysearch"]
        self.assertEqual(len(specs), 2)
        ids = [s["source_id"] for s in specs]
        self.assertIn("anysearch_ai", ids)
        self.assertEqual(specs[0]["query"], "AI 进展")
        self.assertEqual(specs[0]["source_type"], anysearch.SOURCE_TYPE)
        # 每条都带节流参数，避免把 20 次的限流窗口烧光
        self.assertGreater(specs[0]["min_interval_minutes"], 0)

    def test_disabled_section_yields_no_sources(self):
        cfg = _cfg(enabled=False, queries=[{"query": "AI 进展"}])
        specs = [s for s in sources.all_specs(cfg) if s.get("adapter") == "anysearch"]
        self.assertEqual(specs, [])

    def test_per_query_enabled_flag(self):
        cfg = _cfg(queries=[{"id": "on", "query": "开了", "enabled": True},
                            {"id": "off", "query": "关了", "enabled": False}])
        active = [s["source_id"] for s in sources.active_specs(cfg) if s.get("adapter") == "anysearch"]
        self.assertIn("anysearch_on", active)
        self.assertNotIn("anysearch_off", active)

    def test_adapter_maps_rows_to_items(self):
        cfg = _cfg()
        spec = [s for s in sources.all_specs(_cfg(queries=[{"id": "ai", "query": "AI 进展"}]))
                if s.get("adapter") == "anysearch"][0]
        original = anysearch.search
        anysearch.search = lambda *a, **k: [_row(1), _row(2)]
        try:
            items = anysearch.fetch(spec, sources.make_item)
        finally:
            anysearch.search = original
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["source_type"], "search")
        self.assertEqual(items[0]["url"], "https://example1.com/post")
        self.assertIn("AnySearch 搜索结果", items[0]["raw_content"])
        self.assertNotIn("search", __import__("core.radar.score", fromlist=["x"]).NEWS_TYPES)

    def test_adapter_requires_query(self):
        spec = {"source_id": "anysearch_x", "adapter": "anysearch", "_anysearch_cfg": {}}
        with self.assertRaises(RuntimeError):
            anysearch.fetch(spec, sources.make_item)

    def test_empty_result_is_an_error_not_silent(self):
        spec = [s for s in sources.all_specs(_cfg(queries=[{"id": "ai", "query": "AI"}]))
                if s.get("adapter") == "anysearch"][0]
        original = anysearch.search
        anysearch.search = lambda *a, **k: []
        try:
            with self.assertRaises(RuntimeError):
                anysearch.fetch(spec, sources.make_item)
        finally:
            anysearch.search = original


class TestExternalSearchDiscipline(unittest.TestCase):
    """方案 A：外部取证不得注水"独立来源"。"""

    def setUp(self):
        store.init()
        self.conn = db.connect()
        # 同一个库被多个用例共用，investigate 的 search_pool 会把别的用例的条目也捞进来。
        # 这些断言依赖"本地只有我造的这几条"，所以先清空雷达数据。
        for table in ("radar_claim_evidence", "radar_claims", "radar_evidence", "radar_reports",
                      "radar_event_snapshots", "radar_event_items", "radar_events", "radar_items",
                      "radar_rank_history", "radar_sources"):
            try:
                self.conn.execute("DELETE FROM %s" % table)
            except Exception:  # noqa: BLE001 - 表不存在就跳过
                pass
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _event_with_items(self, count):
        # 每次给条目一个唯一的域名前缀，最后只把这次的条目挂到事件上：
        # 测试共用同一个库，recent_items 会把别的用例的条目也捞出来。
        tag = uuid.uuid4().hex[:10]
        title = "某芯片-%s 发布" % tag
        items = []
        for i in range(count):
            items.append({
                "source_id": "s-%s-%d" % (tag, i), "platform": "平台%d" % i,
                "title": title,
                "url": "https://news-%s-%d.com/a" % (tag, i),
                "url_hash": "h-%s-%d" % (tag, i), "author_name": "",
                "published_at": "2026-10-25T09:00:00+08:00",
                "collected_at": "2026-10-25T09:10:00+08:00", "language": "zh", "engagement": {},
                "content_hash": "c-%s-%d" % (tag, i),
                "raw_content": "%s，采用 3nm 制程。" % title,
                "source_type": "news", "fetch_status": "ok",
                "last_seen_at": "2026-10-25T09:10:00+08:00",
            })
        store.ensure_source(self.conn, {"source_id": "s-%s-0" % tag, "platform": "平台0",
                                        "name": "源0", "kind": "rss", "url": "", "enabled": True})
        store.insert_items(self.conn, items)
        self.conn.commit()
        rows = store.recent_items(self.conn, "1970-01-01T00:00:00+00:00", limit=500)
        ids = [r["id"] for r in rows if ("news-%s-" % tag) in (r.get("url") or "")]
        self.assertEqual(len(ids), count)
        event_id = "ev-anysearch-%s" % tag
        store.upsert_event(self.conn, {
            "id": event_id, "date": "2026-10-25", "title": title,
            "summary": "", "keywords": ["芯片"], "item_count": len(ids), "platform_count": 1,
            "source_count": 1, "first_seen_at": "2026-10-25T09:10:00+08:00",
            "last_updated_at": "2026-10-25T09:10:00+08:00", "metrics": {}, "tech_flag": True,
        })
        store.assign_event(self.conn, ids, event_id)
        self.conn.commit()
        return event_id

    def test_external_items_only_added_when_local_is_thin(self):
        event_id = self._event_with_items(4)  # 本地 4 条 > min_local_evidence 3 → 不补
        called = {"n": 0}
        original = anysearch.search

        def fake(*a, **k):
            called["n"] += 1
            return [_row(1)]

        anysearch.search = fake
        try:
            investigate.run(self.conn, event_id, _cfg())
        finally:
            anysearch.search = original
        self.assertEqual(called["n"], 0)

    def test_external_items_are_fetched_and_marked(self):
        event_id = self._event_with_items(1)  # 本地只有 1 条 → 触发外部取证
        original = anysearch.search
        anysearch.search = lambda *a, **k: [_row(1), _row(2)]
        try:
            payload = investigate.run(self.conn, event_id, _cfg())
        finally:
            anysearch.search = original
        self.assertEqual(payload["external_evidence"], 2)
        self.assertEqual(payload["external_domains"], 2)
        self.assertGreater(payload["external_evidence"], 0)
        self.assertTrue(any("搜索引擎材料" in g for g in payload["gaps"]),
                        payload["gaps"])

    def test_search_results_do_not_inflate_independent_domains(self):
        """核心不变量：10 条搜索结果的域名不能算成 10 个独立来源。"""
        event_id = self._event_with_items(1)
        original = anysearch.search
        anysearch.search = lambda *a, **k: [_row(i) for i in range(1, 10)]
        try:
            payload = investigate.run(self.conn, event_id, _cfg())
        finally:
            anysearch.search = original
        # 本地只有 1 个域名，独立来源数就必须是 1
        self.assertEqual(payload["independent_domains"], 1)
        self.assertEqual(payload["external_evidence"], 9)
        self.assertTrue(any("独立来源不足" in g for g in payload["gaps"]))

    def test_search_results_cannot_reach_multi_support(self):
        """搜索引擎的转载站不允许把说法顶到 multi_support。"""
        event_id = self._event_with_items(1)
        original = anysearch.search
        anysearch.search = lambda *a, **k: [
            {**_row(i), "content": "某公司发布新款芯片，采用 3nm 制程。"} for i in range(1, 8)
        ]
        try:
            payload = investigate.run(self.conn, event_id, _cfg())
        finally:
            anysearch.search = original
        statuses = {c["status"] for c in payload["claims"]}
        self.assertNotIn("multi_support", statuses)

    def test_failure_does_not_break_investigation(self):
        event_id = self._event_with_items(1)
        original = anysearch.search

        def boom(*a, **k):
            raise RuntimeError("AnySearch 请求失败：HTTP 429")

        anysearch.search = boom
        try:
            payload = investigate.run(self.conn, event_id, _cfg())
        finally:
            anysearch.search = original
        self.assertEqual(payload["external_evidence"], 0)  # 静默降级，调查照样出结果
        self.assertIn("evidence_count", payload)

    def test_extract_fills_body_for_top_results(self):
        event_id = self._event_with_items(1)
        cfg = _cfg(extract_top=1)
        seen_extract = {"url": ""}
        original_search, original_extract = anysearch.search, anysearch.extract
        anysearch.search = lambda *a, **k: [_row(1), _row(2)]
        anysearch.extract = lambda url, *a, **k: seen_extract.update({"url": url}) or "抓到的完整正文" * 30
        try:
            payload = investigate.run(self.conn, event_id, cfg)
        finally:
            anysearch.search, anysearch.extract = original_search, original_extract
        self.assertEqual(seen_extract["url"], "https://example1.com/post")
        self.assertEqual(payload["external_evidence"], 2)


def _isolated_cfg(**anysearch_overrides):
    """只保留测试用源：把静态源（RSS/热榜/newsnow）全部关掉。

    不这么做的话，``sources.collect`` 会真的去抓 30 多个外部源，既慢又会把
    热榜条目写进共用的测试库，污染别的用例。
    """
    cfg = _cfg(**anysearch_overrides)
    cfg["sources"] = {spec["source_id"]: {"enabled": False} for spec in sources.SOURCES}
    cfg["rss"] = {"feeds": []}
    return cfg


class TestSourceThrottle(unittest.TestCase):
    """发现源按 min_interval_minutes 跳过，避免烧光限流窗口。"""

    def setUp(self):
        store.init()
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()

    def _only_spec(self, cfg):
        active = [s for s in sources.active_specs(cfg) if s.get("adapter") == "anysearch"]
        self.assertEqual(len(active), 1)
        return active[0]

    def test_recent_success_skips_next_run(self):
        cfg = _isolated_cfg(queries=[{"id": "ai", "query": "AI 进展", "min_interval_minutes": 240}])
        spec = self._only_spec(cfg)
        store.ensure_source(self.conn, spec)
        store.record_source_result(self.conn, spec["source_id"], True, 3, 100, None,
                                   dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        self.conn.commit()

        called = {"n": 0}
        original = anysearch.search

        def fake(*a, **k):
            called["n"] += 1
            return [_row(1)]

        anysearch.search = fake
        try:
            result = sources.collect(self.conn, cfg)
        finally:
            anysearch.search = original
        skipped = {s["source_id"] for s in result["skipped"]}
        self.assertIn(spec["source_id"], skipped)
        self.assertEqual(called["n"], 0)  # 被节流就不该发请求

    def test_runs_when_interval_elapsed(self):
        cfg = _isolated_cfg(queries=[{"id": "ai", "query": "AI 进展", "min_interval_minutes": 240}])
        spec = self._only_spec(cfg)
        store.ensure_source(self.conn, spec)
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=10)).isoformat(timespec="seconds")
        store.record_source_result(self.conn, spec["source_id"], True, 3, 100, None, old)
        self.conn.commit()

        called = {"n": 0}
        original = anysearch.search

        def fake(*a, **k):
            called["n"] += 1
            return [_row(1)]

        anysearch.search = fake
        try:
            result = sources.collect(self.conn, cfg)
        finally:
            anysearch.search = original
        skipped = {s["source_id"] for s in result["skipped"]}
        self.assertNotIn(spec["source_id"], skipped)
        self.assertEqual(called["n"], 1)

    def test_source_without_interval_is_never_throttled(self):
        cfg = _isolated_cfg(queries=[{"id": "ai", "query": "AI 进展", "min_interval_minutes": 0}])
        spec = self._only_spec(cfg)
        store.ensure_source(self.conn, spec)
        store.record_source_result(self.conn, spec["source_id"], True, 3, 100, None,
                                   dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        self.conn.commit()

        called = {"n": 0}
        original = anysearch.search

        def fake(*a, **k):
            called["n"] += 1
            return []

        anysearch.search = fake
        try:
            result = sources.collect(self.conn, cfg)
        finally:
            anysearch.search = original
        skipped = {s["source_id"] for s in result["skipped"]}
        self.assertNotIn(spec["source_id"], skipped)
        self.assertEqual(called["n"], 1)  # 没有节流就该真的跑一次


class TestVerticalParams(unittest.TestCase):
    """垂直域（finance.quote 等）有必填参数，只给 tag 会被后端判 400。"""

    def _capture(self, spec, cfg):
        captured = {}
        original = anysearch.post_json

        def fake(url, payload, **kwargs):
            captured["payload"] = payload
            from core.radar.fetch import FetchResult
            return ({"code": 0, "data": {"results": [_row(1)]}},
                    FetchResult(True, 200, "{}", "", 1, url))

        anysearch.post_json = fake
        try:
            anysearch.fetch(spec, sources.make_item)
        finally:
            anysearch.post_json = original
        return captured["payload"]

    def test_query_params_reach_the_api(self):
        cfg = _cfg(queries=[{"id": "stock", "query": "AAPL", "tag": "finance.quote",
                             "params": {"type": "stock", "symbol": "AAPL"}}])
        spec = [s for s in sources.all_specs(cfg) if s.get("adapter") == "anysearch"][0]
        payload = self._capture(spec, cfg)
        self.assertEqual(payload["tag"], "finance.quote")
        self.assertEqual(payload["params"], {"type": "stock", "symbol": "AAPL"})

    def test_global_params_are_merged_but_query_wins(self):
        cfg = _cfg(params={"type": "stock", "cn_code": ""},
                   queries=[{"id": "x", "query": "AAPL", "tag": "finance.quote",
                             "params": {"symbol": "AAPL"}}])
        spec = [s for s in sources.all_specs(cfg) if s.get("adapter") == "anysearch"][0]
        payload = self._capture(spec, cfg)
        self.assertEqual(payload["params"], {"type": "stock", "cn_code": "", "symbol": "AAPL"})

    def test_no_params_field_when_nothing_configured(self):
        cfg = _cfg(queries=[{"id": "plain", "query": "AI 进展"}])
        spec = [s for s in sources.all_specs(cfg) if s.get("adapter") == "anysearch"][0]
        payload = self._capture(spec, cfg)
        self.assertNotIn("params", payload)

    def test_bad_params_config_is_ignored(self):
        cfg = _cfg(queries=[{"id": "bad", "query": "x", "params": "type=stock"}])
        spec = [s for s in sources.all_specs(cfg) if s.get("adapter") == "anysearch"][0]
        self.assertEqual(spec["params"], {})
        payload = self._capture(spec, cfg)
        self.assertNotIn("params", payload)

    def test_max_results_is_capped_at_ten(self):
        cfg = _cfg(queries=[{"id": "many", "query": "x", "max_results": 500}])
        spec = [s for s in sources.all_specs(cfg) if s.get("adapter") == "anysearch"][0]
        payload = self._capture(spec, cfg)
        self.assertEqual(payload["max_results"], 10)


class TestErrorHygiene(unittest.TestCase):
    """402 的响应体里可能带账号密码，错误信息绝不能夹带正文。"""

    def test_post_json_error_never_includes_body(self):
        from core.radar import fetch as radar_fetch

        secret_body = '{"message":"api_key=as_sk_LEAKED password=hunter2"}'

        class FakeResponse:
            status = 402
            headers = {}

            def read(self):
                return secret_body.encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        original = radar_fetch.urllib.request.urlopen

        def fake_urlopen(*a, **k):
            raise __import__("urllib.error", fromlist=["x"]).HTTPError(
                "https://api.anysearch.com/v1/search", 402, "quota", {}, None)

        radar_fetch.urllib.request.urlopen = fake_urlopen
        try:
            data, result = radar_fetch.post_json("https://api.anysearch.com/v1/search", {"query": "x"})
        finally:
            radar_fetch.urllib.request.urlopen = original
        self.assertIsNone(data)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 402)
        self.assertNotIn("LEAKED", result.error)
        self.assertNotIn("hunter2", result.error)

    def test_extract_rejects_non_http_url(self):
        self.assertEqual(anysearch.extract("file:///etc/passwd", _cfg()), "")

    def test_extract_body_has_only_url_field(self):
        """extract 多传字段会 400（实测踩过），请求体必须只有一个 key。"""
        captured = {}
        original = anysearch.post_json

        def fake(url, payload, **kwargs):
            captured["payload"] = payload
            from core.radar.fetch import FetchResult
            return {"code": 0, "data": {"content": "正文"}}, FetchResult(True, 200, "{}", "", 1, url)

        anysearch.post_json = fake
        try:
            anysearch.extract("https://example.com/a", _cfg())
        finally:
            anysearch.post_json = original
        self.assertEqual(list(captured["payload"].keys()), ["url"])


if __name__ == "__main__":
    unittest.main()