"""采集监控 API / 存储层测试（隔离数据库、不联网、不写真实用户数据）。

覆盖：
* 运行记录的时间范围 / 任务 / 状态筛选、分页、排序（开始时间、耗时、错误数）
* 运行详情：阶段顺序、未执行阶段的 skipped / not_applicable / unknown、来源结果、
  信息追踪（含未入库条目的原因）
* 来源管理：四个独立概念（配置存在 / 当前启用 / 最近成功 / 有新数据）、0 条≠失败、
  按连续失败 / 长期没有成功 / 耗时排序
* 日志：等级 / 任务 / run_id / source_id / 阶段 / 错误码 / 关键词筛选、分页、
  详情、密钥脱敏
* 空数据与接口失败的真实状态（未知而不是 0）
* 既有真实历史（radar_runs / paper_source_runs / digests / push_log）的导入与幂等
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401,E402

from core import db, monitor  # noqa: E402
from core.monitor import api, redact, store  # noqa: E402


def reset() -> None:
    """每个测试用例一套干净数据（表结构保留）。"""
    store.ensure()          # 确保监控表已建好（幂等）
    conn = db.connect()
    try:
        for table in ("monitor_runs", "monitor_stages", "monitor_source_results",
                      "monitor_item_actions", "monitor_logs"):
            conn.execute("DELETE FROM %s" % table)
        conn.execute("DELETE FROM meta WHERE key LIKE 'monitor_%'")
        conn.commit()
    finally:
        conn.close()


def iso(minutes_ago: int) -> str:
    import datetime as dt
    return (dt.datetime.now().astimezone()
            - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def make_run(task="news_collect", trigger="schedule", status="ok", minutes_ago=5,
             duration_minutes=2, errors=None, **counts):
    """用真实的埋点接口造一条运行记录（等价于流水线自己写进去的那条）。

    ``minutes_ago`` / ``duration_minutes`` 用来摆放真实的时间坐标：先按真实流程收尾，
    再把时间戳改到测试需要的位置（真实运行时这些时间戳由流水线自己写）。
    """
    run = monitor.run(task, trigger=trigger)
    run.start()
    for message in (errors or []):
        run.add_error(message, stage="fetch")   # 必须在运行中写：收尾后连接已关闭
    run.finish(status=status, **counts)
    conn = db.connect()
    try:
        conn.execute("UPDATE monitor_runs SET started_at=?, finished_at=?, duration_ms=? "
                     "WHERE run_id=?",
                     (iso(minutes_ago), iso(minutes_ago - duration_minutes),
                      duration_minutes * 60000, run.run_id))
        conn.commit()
    finally:
        conn.close()
    return run


class MonitorTestBase(unittest.TestCase):
    def setUp(self):
        db.init()
        reset()

    def api(self, path, **query):
        return api.handle_get(path, {k: str(v) for k, v in query.items() if v is not None})

    def api_ok(self, path, **query):
        status, payload = self.api(path, **query)
        self.assertEqual(status, 200, payload)
        return payload


class TestEmptyStates(MonitorTestBase):
    def test_empty_overview_is_unknown_not_zero(self):
        payload = self.api_ok("/api/monitor/overview")
        # 没有任何运行 → 成功率是「未知」，不是 0%
        self.assertTrue(payload["success_rate"]["unknown"])
        self.assertIsNone(payload["success_rate"]["value"])
        self.assertIn("没有已结束的运行记录", payload["success_rate"]["unknown_reason"])
        self.assertTrue(payload["item_flow"]["unknown"])
        self.assertIsNone(payload["item_flow"]["new"])
        self.assertIsNone(payload["last_run"])
        self.assertTrue(payload["last_success"]["unknown"])
        for key in ("last_run", "success_rate", "item_flow", "sources", "last_success", "errors"):
            self.assertIn(key, payload["caliber"], "统计口径必须可解释")

    def test_empty_run_list_and_logs(self):
        runs = self.api_ok("/api/monitor/runs")
        self.assertEqual(runs["total"], 0)
        self.assertEqual(runs["runs"], [])
        logs = self.api_ok("/api/monitor/logs")
        self.assertEqual(logs["total"], 0)
        self.assertEqual(logs["logs"], [])
        timeline = self.api_ok("/api/monitor/timeline", window="24h")
        self.assertEqual(timeline["total"], 0)

    def test_missing_records_return_404(self):
        status, payload = self.api("/api/monitor/run", id="nope")
        self.assertEqual(status, 404)
        self.assertIn("没有这条运行记录", payload["error"])
        status, _ = self.api("/api/monitor/log", id="999999")
        self.assertEqual(status, 404)
        status, _ = self.api("/api/monitor/run", )
        self.assertEqual(status, 400, "缺少 id 时必须报 400")

    def test_monitor_api_is_read_only(self):
        status, payload = api.handle_post("/api/monitor/runs", {"task": "x"})
        self.assertEqual(status, 405)
        self.assertIn("只读", payload["error"])

    def test_health_and_meta(self):
        health = self.api_ok("/api/monitor/health")
        self.assertTrue(health["ok"])
        self.assertTrue(health["readonly"])
        meta = self.api_ok("/api/monitor/meta")
        self.assertEqual([task["value"] for task in meta["tasks"]],
                         ["news_collect", "event_report", "github_daily", "papers_collect",
                          "geo_collect", "summary", "wechat_push"])
        self.assertIn("levels", meta)
        self.assertIn("caliber", meta)


class TestRunFilters(MonitorTestBase):
    def setUp(self):
        super().setUp()
        make_run("news_collect", "schedule", "ok", minutes_ago=10, duration_minutes=1,
                 sources_attempted=3, items_fetched=40, items_new=5, items_duplicate=35)
        make_run("event_report", "api", "failed", minutes_ago=120, duration_minutes=9,
                 sources_attempted=2, items_fetched=10, items_new=0, error_count=2)
        make_run("papers_collect", "cli", "partial", minutes_ago=60 * 24 * 30, duration_minutes=3,
                 sources_attempted=6, items_fetched=120, items_new=9, items_duplicate=80)

    def test_window_filter(self):
        window = self.api_ok("/api/monitor/runs", window="24h")
        self.assertEqual(window["total"], 2)
        week = self.api_ok("/api/monitor/runs", window="7d")
        self.assertEqual(week["total"], 2, "30 天前的那条不应出现在 7 天窗口里")
        everything = self.api_ok("/api/monitor/runs", window="all")
        self.assertEqual(everything["total"], 3)

    def test_task_and_status_filters(self):
        self.assertEqual(self.api_ok("/api/monitor/runs", window="all",
                                     task="event_report")["total"], 1)
        self.assertEqual(self.api_ok("/api/monitor/runs", window="all",
                                     task="news_collect,event_report")["total"], 2)
        self.assertEqual(self.api_ok("/api/monitor/runs", window="all",
                                     status="failed")["total"], 1)
        self.assertEqual(self.api_ok("/api/monitor/runs", window="all",
                                     trigger="cli")["total"], 1)
        self.assertEqual(self.api_ok("/api/monitor/runs", window="all",
                                     task="wechat_push")["total"], 0)

    def test_keyword_filter_matches_error_text(self):
        run = make_run("event_report", "api", "failed", minutes_ago=3,
                       errors=["cn_scroll 抓取失败：timeout"])
        hit = self.api_ok("/api/monitor/runs", window="all", q="cn_scroll")
        self.assertEqual(hit["total"], 1)
        self.assertEqual(hit["runs"][0]["run_id"], run.run_id)
        self.assertEqual(self.api_ok("/api/monitor/runs", window="all", q="查无此词")["total"], 0)

    def test_pagination(self):
        first = self.api_ok("/api/monitor/runs", window="all", page=1, page_size=2)
        self.assertEqual(len(first["runs"]), 2)
        self.assertEqual(first["pages"], 2)
        self.assertTrue(first["has_more"])
        second = self.api_ok("/api/monitor/runs", window="all", page=2, page_size=2)
        self.assertEqual(len(second["runs"]), 1)
        self.assertFalse(second["has_more"])
        ids = {run["run_id"] for run in first["runs"]} | {run["run_id"] for run in second["runs"]}
        self.assertEqual(len(ids), 3, "分页不能重复返回同一行")

    def test_page_size_is_clamped(self):
        payload = self.api_ok("/api/monitor/runs", window="all", page_size=99999)
        self.assertLessEqual(payload["page_size"], api.MAX_PAGE_SIZE)

    def test_sorting_by_started_duration_and_errors(self):
        by_time = self.api_ok("/api/monitor/runs", window="all", sort="started_at", order="desc")
        self.assertEqual(by_time["runs"][0]["task"], "news_collect")
        by_duration = self.api_ok("/api/monitor/runs", window="all", sort="duration", order="desc")
        self.assertEqual(by_duration["runs"][0]["task"], "event_report")
        by_errors = self.api_ok("/api/monitor/runs", window="all", sort="error_count", order="desc")
        self.assertEqual(by_errors["runs"][0]["task"], "event_report")
        self.assertGreaterEqual(by_errors["runs"][0]["error_count"],
                                by_errors["runs"][-1]["error_count"])
        # 未知排序字段不会被拼进 SQL，而是退回默认排序
        fallback = self.api_ok("/api/monitor/runs", window="all", sort="evil; drop table",
                               order="desc")
        self.assertEqual(fallback["sort"], "started_at")

    def test_counts_are_not_inflated_by_zero(self):
        runs = self.api_ok("/api/monitor/runs", window="all", sort="started_at", order="asc")
        by_task = {run["task"]: run for run in runs["runs"]}
        self.assertEqual(by_task["papers_collect"]["items_new"], 9)
        self.assertEqual(by_task["news_collect"]["items_duplicate"], 35)
        # 没有记录的计数列是 None（未知），不会用 0 冒充
        self.assertIsNone(by_task["event_report"]["items_duplicate"])


class TestRunDetail(MonitorTestBase):
    def _build(self):
        run = monitor.run("event_report", trigger="api")
        run.start()
        with run.phase("fetch") as phase:
            phase.result = {"sources_ok": 1, "items_fetched": 40}
        run.stage("cluster", "ok", {"events": 12})
        run.stage("summary", "not_applicable", None, note="没有启用摘要后端")
        run.source_result("cn_scroll", "ok", name="中国新闻网·滚动", platform="中国新闻网",
                          kind="rss", latency_ms=1880, http_status=200, fetched=40, parsed=40,
                          items_new=5, items_duplicate=35, items_filtered=0)
        run.source_result("v2ex_hot", "failed", name="V2EX 热门", kind="hotlist",
                          latency_ms=24800, error="RuntimeError: URLError: <urlopen error timed out>",
                          reason="降频中", fetched=0)
        run.source_result("weibo_hot", "skipped", name="微博热搜", kind="hotlist",
                          reason="距上次成功不足 240 分钟，本轮跳过")
        run.items([
            {"source_id": "cn_scroll", "kind": "news", "title": "芯片制程节点取得突破",
             "url": "https://news.example.com/chip", "url_hash": "hash-new",
             "published_at": iso(30), "collected_at": iso(5), "action": "new",
             "reason": "首次入库", "item_id": "101"},
            {"source_id": "cn_scroll", "kind": "news", "title": "广告：点击领取优惠券",
             "url": "https://news.example.com/ad", "url_hash": "hash-filtered",
             "collected_at": iso(5), "action": "filtered",
             "reason": "命中噪音关键词：优惠券"},
            {"source_id": "cn_scroll", "kind": "news", "title": None, "url": None,
             "collected_at": iso(5), "action": "duplicate", "reason": "URL 已入库过"},
        ])
        run.log("ERROR", "V2EX 热门（v2ex_hot）：urlopen error timed out", stage="fetch",
                source_id="v2ex_hot", error_code="timeout", details={"http_status": 0})
        run.log("INFO", "本轮跳过 1 个来源", stage="fetch")
        run.finish(status="partial", items_fetched=40, items_new=5, items_duplicate=35,
                   error_count=1)
        return run

    def test_detail_contains_stages_sources_items_and_logs(self):
        run = self._build()
        payload = self.api_ok("/api/monitor/run", id=run.run_id)
        self.assertEqual(payload["run"]["task_label"], "事件聚类与日报")
        self.assertEqual(payload["run"]["trigger_label"], "接口触发")
        names = [stage["name"] for stage in payload["stages"]]
        plan = store.TASK_STAGES["event_report"]
        # 计划内的阶段按真实顺序齐全（本次额外执行过的阶段排在计划之后）
        self.assertEqual([name for name in names if name in plan], plan)
        self.assertIn("summary", names)
        by_name = {stage["name"]: stage for stage in payload["stages"]}
        self.assertEqual(by_name["fetch"]["status"], "ok")
        self.assertEqual(by_name["cluster"]["status"], "ok")
        self.assertEqual(by_name["summary"]["status"], "not_applicable")
        # 没有执行过的阶段不能显示成成功
        self.assertEqual(by_name["report"]["status"], "skipped")
        self.assertEqual(by_name["push"]["status"], "skipped")
        self.assertIn("没有执行", by_name["report"]["result"]["note"])
        self.assertEqual(len(payload["sources"]), 3)
        statuses = {row["source_id"]: row["status"] for row in payload["sources"]}
        self.assertEqual(statuses["cn_scroll"], "ok")
        self.assertEqual(statuses["v2ex_hot"], "failed")
        self.assertEqual(statuses["weibo_hot"], "skipped")
        self.assertEqual(payload["items"]["total"], 3)
        self.assertEqual(payload["logs"]["total"], 2)

    def test_item_trace_shows_reasons_and_survives_bad_data(self):
        run = self._build()
        payload = self.api_ok("/api/monitor/run/items", id=run.run_id)
        actions = {item["action"]: item for item in payload["items"]}
        self.assertEqual(actions["new"]["reason"], "首次入库")
        self.assertEqual(actions["filtered"]["reason"], "命中噪音关键词：优惠券")
        self.assertFalse(actions["filtered"]["in_radar"], "被过滤的条目不在雷达库里")
        self.assertIsNone(actions["filtered"]["item_id"])
        # 标题/链接缺失时不能整表报错
        broken = actions["duplicate"]
        self.assertIsNone(broken["title"])
        self.assertEqual(broken["link"], "")
        self.assertEqual(payload["summary"]["by_action"]["filtered"], 1)

    def test_item_filter_and_pagination(self):
        run = self._build()
        filtered = self.api_ok("/api/monitor/run/items", id=run.run_id, action="filtered")
        self.assertEqual(filtered["total"], 1)
        search = self.api_ok("/api/monitor/run/items", id=run.run_id, q="优惠券")
        self.assertEqual(search["total"], 1)
        page = self.api_ok("/api/monitor/run/items", id=run.run_id, page=1, page_size=2)
        self.assertEqual(len(page["items"]), 2)
        self.assertTrue(page["has_more"])

    def test_run_logs_are_scoped_and_filterable(self):
        run = self._build()
        payload = self.api_ok("/api/monitor/run/logs", id=run.run_id, level="ERROR")
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["logs"][0]["error_code"], "timeout")
        self.assertEqual(payload["logs"][0]["source_id"], "v2ex_hot")
        all_logs = self.api_ok("/api/monitor/run/logs", id=run.run_id)
        self.assertEqual(all_logs["total"], 2)

    def test_unknown_run_id_in_subresources(self):
        status, _ = self.api("/api/monitor/run/items", id="missing")
        self.assertEqual(status, 404)
        status, _ = self.api("/api/monitor/run/logs", id="missing")
        self.assertEqual(status, 404)


class TestOverviewStats(MonitorTestBase):
    def test_success_rate_uses_terminal_runs_only(self):
        make_run("news_collect", "schedule", "ok", minutes_ago=5)
        make_run("event_report", "schedule", "partial", minutes_ago=65)
        make_run("geo_collect", "schedule", "failed", minutes_ago=125)
        running = monitor.run("news_collect", trigger="schedule")
        running.start()   # 运行中：不进成功率分母
        payload = self.api_ok("/api/monitor/overview")
        rate = payload["success_rate"]
        self.assertEqual(rate["ok"], 1)
        self.assertEqual(rate["partial"], 1)
        self.assertEqual(rate["failed"], 1)
        self.assertEqual(rate["total"], 3)
        self.assertAlmostEqual(rate["value"], 1 / 3, places=4)
        self.assertAlmostEqual(rate["value_including_partial"], 2 / 3, places=4)
        self.assertIn("部分成功", payload["caliber"]["success_rate"])
        running.finish(status="interrupted")

    def test_item_flow_reports_coverage(self):
        make_run("news_collect", "schedule", "ok", minutes_ago=5, items_new=4,
                 items_duplicate=36, items_filtered=10, items_fetched=50)
        make_run("papers_collect", "schedule", "ok", minutes_ago=15, items_fetched=30)
        payload = self.api_ok("/api/monitor/overview")
        flow = payload["item_flow"]
        self.assertEqual(flow["new"], 4)
        self.assertEqual(flow["duplicate"], 36)
        self.assertEqual(flow["filtered"], 10)
        self.assertEqual(flow["coverage"]["items_new"], 1)
        self.assertEqual(flow["coverage"]["items_duplicate"], 1)
        self.assertEqual(flow["coverage"]["items_fetched"], 2)
        self.assertEqual(flow["runs_in_window"], 2)

    def test_last_run_and_last_success(self):
        make_run("geo_collect", "schedule", "ok", minutes_ago=30)
        latest = make_run("wechat_push", "schedule", "skipped", minutes_ago=2)
        payload = self.api_ok("/api/monitor/overview")
        self.assertEqual(payload["last_run"]["run_id"], latest.run_id)
        self.assertFalse(payload["last_success"]["unknown"])
        self.assertEqual(payload["last_success"]["task"], "geo_collect")

    def test_open_errors_lists_unresolved_error_groups(self):
        monitor.log_event("ERROR", "cn_scroll 抓取失败：HTTP 429", task="news_collect",
                          source_id="cn_scroll", stage="fetch", error_code="http_429")
        monitor.log_event("INFO", "cn_scroll 恢复正常", task="news_collect",
                          source_id="cn_scroll", stage="fetch")
        monitor.log_event("ERROR", "v2ex_hot 抓取失败：超时", task="news_collect",
                          source_id="v2ex_hot", stage="fetch", error_code="timeout")
        payload = self.api_ok("/api/monitor/overview")
        issues = payload["open_errors"]
        self.assertEqual(issues["count"], 1, "已恢复的分组不算未解决")
        self.assertEqual(issues["items"][0]["source_id"], "v2ex_hot")
        self.assertEqual(issues["items"][0]["error_code"], "timeout")
        self.assertIn("未解决", payload["caliber"]["errors"])


class TestSourcesView(MonitorTestBase):
    SPECS = [
        {"source_id": "cn_scroll", "name": "中国新闻网·滚动", "platform": "中国新闻网",
         "kind": "rss", "enabled": True},
        {"source_id": "weibo_hot", "name": "微博热搜", "platform": "微博", "kind": "hotlist",
         "enabled": True},
        {"source_id": "x_twitter", "name": "X / Twitter", "platform": "X", "kind": "unavailable",
         "enabled": False, "notes": "无合规免费入口"},
        {"source_id": "zhihu_hot", "name": "知乎热榜", "platform": "知乎", "kind": "hotlist",
         "enabled": False},
    ]

    def _states(self):
        """radar_sources 里由真实采集写入的健康状态。"""
        store.ensure()
        conn = db.connect()
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS radar_sources (
                source_id TEXT PRIMARY KEY, platform TEXT, name TEXT, kind TEXT, url TEXT,
                enabled INTEGER DEFAULT 1, status TEXT DEFAULT 'unknown', last_ok_at TEXT,
                last_error TEXT, last_error_at TEXT, consecutive_failures INTEGER DEFAULT 0,
                last_item_count INTEGER DEFAULT 0, last_latency_ms INTEGER, notes TEXT,
                paused_reason TEXT)""")
            conn.execute("DELETE FROM radar_sources")
            conn.execute(
                "INSERT INTO radar_sources(source_id, platform, name, kind, enabled, status,"
                " last_ok_at, consecutive_failures, last_item_count, last_latency_ms)"
                " VALUES('cn_scroll','中国新闻网','中国新闻网·滚动','rss',1,'ok',?,0,40,1880)",
                (iso(20),))
            conn.execute(
                "INSERT INTO radar_sources(source_id, platform, name, kind, enabled, status,"
                " last_ok_at, last_error, last_error_at, consecutive_failures, last_item_count,"
                " last_latency_ms, paused_reason)"
                " VALUES('weibo_hot','微博','微博热搜','hotlist',1,'paused',?,"
                " 'RuntimeError: URLError: <urlopen error timed out>',?,6,0,24800,"
                " '连续失败 6 次，已自动停用')",
                (iso(60 * 40), iso(60 * 24)))
            conn.commit()
        finally:
            conn.close()

    def test_source_rows_distinguish_four_concepts(self):
        self._states()
        conn = db.connect()
        try:
            view = store.sources_view(conn, self.SPECS)
        finally:
            conn.close()
        by_id = {row["source_id"]: row for row in view["sources"]}
        # 配置存在
        self.assertEqual(view["summary"]["configured"], 4)
        # 当前启用
        self.assertEqual(view["summary"]["enabled"], 2)
        self.assertTrue(by_id["cn_scroll"]["enabled"])
        self.assertFalse(by_id["zhihu_hot"]["enabled"])
        self.assertEqual(by_id["zhihu_hot"]["status"], "disabled")
        # 无合规入口
        self.assertEqual(by_id["x_twitter"]["status"], "unavailable")
        # 最近成功：cn_scroll 成功过，weibo_hot 也成功过（60*40 分钟前）
        self.assertFalse(by_id["cn_scroll"]["never_succeeded"])
        self.assertIsNotNone(by_id["weibo_hot"]["last_ok_at"])
        self.assertIsNone(by_id["zhihu_hot"]["last_ok_at"])
        self.assertTrue(by_id["zhihu_hot"]["never_succeeded"])
        # 熔断原因逐字保留
        self.assertEqual(by_id["weibo_hot"]["paused_reason"], "连续失败 6 次，已自动停用")
        self.assertEqual(by_id["weibo_hot"]["consecutive_failures"], 6)
        # 从没跑过的来源，条目数/耗时必须是「未知」而不是 0
        self.assertIsNone(by_id["zhihu_hot"]["last_fetched"])
        self.assertIsNone(by_id["zhihu_hot"]["last_latency_ms"])
        self.assertIsNone(by_id["zhihu_hot"]["last_new"])

    def test_zero_items_success_is_not_a_failure(self):
        self._states()
        run = monitor.run("news_collect", trigger="schedule")
        run.start()
        # 真实返回 0 条的来源：状态必须是“成功但 0 条”，不计失败
        run.source_result("weibo_hot", "empty", name="微博热搜", kind="hotlist",
                          fetched=0, parsed=0, items_new=0,
                          reason="本次返回 0 条可用条目（不等于失败）")
        run.finish(status="ok")
        conn = db.connect()
        try:
            view = store.sources_view(conn, self.SPECS)
            detail = store.source_detail(conn, "weibo_hot")
        finally:
            conn.close()
        row = {item["source_id"]: item for item in view["sources"]}["weibo_hot"]
        self.assertEqual(row["last_result_status"], "empty")
        self.assertEqual(row["last_result_label"], "成功但 0 条")
        self.assertEqual(row["empty_successes"], 1)
        self.assertEqual(row["failures_recorded"], 0)
        self.assertEqual(detail["results"][0]["reason"], "本次返回 0 条可用条目（不等于失败）")
        self.assertEqual(view["summary"]["error"], 0)

    def test_sorting_by_failures_stale_and_latency(self):
        self._states()
        conn = db.connect()
        try:
            by_failures = store.sources_view(conn, self.SPECS, sort="failures", order="desc")
            self.assertEqual(by_failures["sources"][0]["source_id"], "weibo_hot")
            by_stale = store.sources_view(conn, self.SPECS, sort="stale", order="desc")
            self.assertIn(by_stale["sources"][0]["source_id"], ("x_twitter", "zhihu_hot"))
            by_latency = store.sources_view(conn, self.SPECS, sort="latency", order="desc")
            self.assertEqual(by_latency["sources"][0]["source_id"], "weibo_hot")
        finally:
            conn.close()

    def test_search_and_status_filter(self):
        self._states()
        conn = db.connect()
        try:
            searched = store.sources_view(conn, self.SPECS, q="微博")
            self.assertEqual([row["source_id"] for row in searched["sources"]], ["weibo_hot"])
            paused = store.sources_view(conn, self.SPECS, statuses=["paused"])
            self.assertEqual([row["source_id"] for row in paused["sources"]], ["weibo_hot"])
            enabled_only = store.sources_view(conn, self.SPECS, only_enabled=True)
            self.assertTrue(all(row["enabled"] for row in enabled_only["sources"]))
        finally:
            conn.close()

    def test_api_sources_and_source_detail(self):
        self._states()
        payload = self.api_ok("/api/monitor/sources", sort="failures")
        self.assertTrue(any(row["source_id"] == "weibo_hot" for row in payload["sources"]))
        self.assertIn("四个独立概念", payload["summary"]["coverage"]["note"])
        detail = self.api_ok("/api/monitor/source", id="weibo_hot")
        self.assertEqual(detail["source_id"], "weibo_hot")
        self.assertIn("attempts_recorded", detail["source"])
        self.assertIsInstance(detail["results"], list)

    def test_unsupported_source_sort_falls_back_instead_of_misleading(self):
        """UI 传了不支持的排序键时，接口要明确退化成默认排序，而不是悄悄换一种顺序。"""
        self._states()
        bogus = self.api_ok("/api/monitor/sources", sort="还不在白名单里", order="asc")
        self.assertEqual(bogus["sort"], "failures")
        self.assertEqual(bogus["order"], "asc")
        failures_asc = self.api_ok("/api/monitor/sources", sort="failures", order="asc")
        self.assertEqual([row["source_id"] for row in bogus["sources"]],
                         [row["source_id"] for row in failures_asc["sources"]])


class TestLogsApi(MonitorTestBase):
    def setUp(self):
        super().setUp()
        run = make_run("event_report", "api", "partial", minutes_ago=5)
        import time
        time.sleep(1.05)   # 让 run_id 不同的日志时间戳有区分度
        monitor.log_event("ERROR", "cn_scroll 抓取失败：HTTP 429", task="news_collect",
                          run_id=run.run_id, source_id="cn_scroll", stage="fetch",
                          error_code="http_429", details={"http_status": 429,
                                                          "url": "https://example.com/rss"})
        monitor.log_event("WARNING", "v2ex_hot 降频中", task="news_collect",
                          source_id="v2ex_hot", stage="fetch")
        monitor.log_event("INFO", "事件聚类完成：12 个事件", task="event_report",
                          run_id=run.run_id, stage="cluster")
        self.run_id = run.run_id

    def test_filters(self):
        self.assertEqual(self.api_ok("/api/monitor/logs")["total"], 3)
        self.assertEqual(self.api_ok("/api/monitor/logs", level="ERROR")["total"], 1)
        self.assertEqual(self.api_ok("/api/monitor/logs", level="ERROR,WARNING")["total"], 2)
        self.assertEqual(self.api_ok("/api/monitor/logs", source_id="cn_scroll")["total"], 1)
        self.assertEqual(self.api_ok("/api/monitor/logs", run_id=self.run_id)["total"], 2)
        self.assertEqual(self.api_ok("/api/monitor/logs", stage="cluster")["total"], 1)
        self.assertEqual(self.api_ok("/api/monitor/logs", error_code="http_429")["total"], 1)
        self.assertEqual(self.api_ok("/api/monitor/logs", task="event_report")["total"], 1)
        self.assertEqual(self.api_ok("/api/monitor/logs", q="429")["total"], 1)
        self.assertEqual(self.api_ok("/api/monitor/logs", q="查无此词")["total"], 0)
        self.assertEqual(self.api_ok("/api/monitor/logs", level="NOT_A_LEVEL")["total"], 3)

    def test_level_counts_match_the_list_in_every_window(self):
        """等级计数必须和列表同一个窗口：window=all 时不能变成空字典（页面会显示假的 ERROR 0）。"""
        for window in ("24h", "7d", "all"):
            payload = self.api_ok("/api/monitor/logs", window=window, page_size=2)
            listed = payload["total"]
            counted = sum(payload["counts_by_level"].values())
            self.assertEqual(counted, listed,
                             "window=%s 的等级计数(%s)与列表总数(%s)不一致"
                             % (window, payload["counts_by_level"], listed))
        self.assertIn("ERROR", self.api_ok("/api/monitor/logs", window="all")["counts_by_level"])

    def test_pagination_and_incremental(self):
        page = self.api_ok("/api/monitor/logs", page=1, page_size=2)
        self.assertEqual(len(page["logs"]), 2)
        self.assertTrue(page["has_more"])
        ids = [log["id"] for log in page["logs"]]
        self.assertEqual(ids, sorted(ids, reverse=True), "日志按时间倒序")
        newest = ids[0]
        incremental = self.api_ok("/api/monitor/logs", since_id=newest)
        self.assertEqual(incremental["total"], 0)
        self.assertEqual(self.api_ok("/api/monitor/logs", page_size=99999)["page_size"],
                         api.MAX_PAGE_SIZE)

    def test_log_detail_is_redacted(self):
        monitor.log_event("ERROR", "push failed with key SCT1234567890abcdef",
                          task="wechat_push", stage="push",
                          details={"api_key": "sk-live-abcdef123456", "sendkey": "sctp9876543210",
                                   "http_status": 401, "run_id": self.run_id})
        listing = self.api_ok("/api/monitor/logs", q="push failed")
        log_id = listing["logs"][0]["id"]
        detail = self.api_ok("/api/monitor/log", id=log_id)["log"]
        blob = detail["message"] + detail["details_json"]
        self.assertNotIn("SCT1234567890abcdef", blob)
        self.assertNotIn("sk-live-abcdef123456", blob)
        self.assertNotIn("sctp9876543210", blob)
        self.assertIn("***", blob)
        # 非敏感上下文必须保留，脱敏不能把日志变成不可用
        self.assertIn("http_status", detail["details_json"])
        self.assertEqual(detail["details"]["http_status"], 401)

    def test_redaction_rules_are_shared_with_the_writer(self):
        # 脱敏只做“减法”：正文保留可读性，密钥片段换成占位符
        self.assertIn("***", redact.redact_text('api_key="sk-abcdef123456"'))
        self.assertNotIn("sk-abcdef123456", redact.redact_text('api_key="sk-abcdef123456"'))
        # URL 里的敏感参数被整条去掉，链接还能用
        cleaned = redact.strip_url_secrets(
            "https://example.com/a?token=abc&x=1&api_key=zzz#frag")
        self.assertIn("x=1", cleaned)
        self.assertNotIn("token=abc", cleaned)
        self.assertNotIn("api_key=zzz", cleaned)
        # 只允许 http/https 作为外链：脚本 / data: / 自定义协议一律不成链接
        self.assertEqual(redact.safe_link("javascript:alert(1)"), "")
        self.assertEqual(redact.safe_link("data:text/html;base64,AAAA"), "")
        self.assertEqual(redact.safe_link("radar://cn_scroll/abc"), "")
        self.assertTrue(redact.safe_link("https://example.com/a").startswith("https://"))
        self.assertIn("***", redact.redact_text("https://sctapi.ftqq.com/SCT1234567890abcd.send"))

    def test_html_in_untrusted_news_text_is_not_executed_on_the_page(self):
        """上游新闻正文/标题里的 HTML 与脚本只能当文本；页面脚本不得用 innerHTML 渲染数据。"""
        page = (Path(__file__).resolve().parent.parent / "web" / "monitor.js").read_text(
            encoding="utf-8")
        self.assertNotIn("innerHTML", page,
                         "monitor.js 不得用 innerHTML 渲染（上游数据都是不可信文本）")
        self.assertIn("textContent", page)

    def test_malicious_payloads_are_returned_as_data_not_code(self):
        payload = "<img src=x onerror=alert(1)>"
        monitor.log_event("ERROR", payload, task="news_collect", stage="fetch")
        monitor.log_event("INFO", "</script><script>alert(2)</script>", task="news_collect")
        listing = self.api_ok("/api/monitor/logs", q="onerror")
        # 接口按 JSON 原样返回（前端负责当文本渲染），不会二次加工成 HTML
        self.assertEqual(listing["logs"][0]["message"], payload)
        self.assertNotIn("<", self.api_ok("/api/monitor/run", id=self.run_id)["caliber"]["errors"])


class TestLegacyImport(MonitorTestBase):
    """既有真实历史（radar_runs / digests / 推送日志）投影进监控表，且必须幂等。"""

    def _legacy_tables(self):
        """在真实旧表结构下塞入真实历史记录（radar_runs / paper_source_runs）。

        时间坐标固定到秒并显式复用：`paper_source_runs` 的一整轮抓取必须落在同一个
        ``started_at`` 上，导入器才会把逐来源行收成「一次论文采集运行」。如果这里先后调用
        两次 ``now()``，只要跨过一秒就会变成两次运行，测试会随机失败。
        """
        import datetime as dt
        now = dt.datetime.now().astimezone().replace(microsecond=0)
        self.radar_ok_at = (now - dt.timedelta(hours=24)).isoformat(timespec="seconds")
        self.radar_failed_at = (now - dt.timedelta(hours=50)).isoformat(timespec="seconds")
        self.papers_at = (now - dt.timedelta(hours=26)).isoformat(timespec="seconds")
        conn = db.connect()
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS radar_runs (
                id TEXT PRIMARY KEY, kind TEXT, started_at TEXT, finished_at TEXT,
                status TEXT, stats TEXT, error TEXT)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS paper_source_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, started_at TEXT,
                finished_at TEXT, status TEXT, fetched INTEGER, with_abstract INTEGER,
                duration_ms INTEGER, error TEXT)""")
            conn.execute("DELETE FROM radar_runs")
            conn.execute("DELETE FROM paper_source_runs")
            import json
            stats = {
                "reason": "schedule",
                "collect": {"sources_enabled": 3, "sources_ok": 2, "sources_failed": 1,
                            "items_raw": 60, "items_kept": 55, "items_new": 7,
                            "items_seen": 48, "items_filtered": 5,
                            "per_source": [
                                {"source_id": "cn_scroll", "raw": 30, "kept": 30, "new": 7,
                                 "dropped_noise": 0, "dropped_low_engagement": 0,
                                 "truncated": 0},
                            ],
                            "failures": [{"source_id": "gdelt", "name": "GDELT",
                                          "error": "RuntimeError: HTTP 429"}]},
                "events": 12, "stories": 5, "items_considered": 55,
                "report_date": "2026-10-20",
                "push": {"ok": False, "skipped": True, "reason": "雷达推送未启用"},
            }
            conn.execute(
                "INSERT INTO radar_runs(id, kind, started_at, finished_at, status, stats, error)"
                " VALUES(?,?,?,?,?,?,?)",
                ("run-legacy1", "collect", self.radar_ok_at,
                 (now - dt.timedelta(hours=23)).isoformat(timespec="seconds"), "ok",
                 json.dumps(stats, ensure_ascii=False), None))
            conn.execute(
                "INSERT INTO radar_runs(id, kind, started_at, finished_at, status, stats, error)"
                " VALUES(?,?,?,?,?,?,?)",
                ("run-legacy2", "collect", self.radar_failed_at,
                 (now - dt.timedelta(hours=49)).isoformat(timespec="seconds"), "failed", "{}",
                 "OperationalError: database is locked"))
            conn.execute(
                "INSERT INTO paper_source_runs(source, started_at, finished_at, status, fetched,"
                " with_abstract, duration_ms, error) VALUES('crossref',?,?,'ok',30,28,1200,'')",
                (self.papers_at, self.papers_at))
            conn.execute(
                "INSERT INTO paper_source_runs(source, started_at, finished_at, status, fetched,"
                " with_abstract, duration_ms, error) VALUES('huggingface',?,?,'failed',0,0,800,"
                "'RuntimeError: URLError: <urlopen error timed out>')",
                (self.papers_at, self.papers_at))
            conn.commit()
        finally:
            conn.close()

    def test_import_creates_real_historical_runs(self):
        self._legacy_tables()
        conn = db.connect()
        try:
            result = store.import_legacy(conn, force=True)
        finally:
            conn.close()
        self.assertEqual(result["errors"], [], "导入不得报错")
        payload = self.api_ok("/api/monitor/runs", window="all", sort="started_at", order="asc")
        tasks = [run["task"] for run in payload["runs"]]
        self.assertIn("event_report", tasks)
        self.assertIn("papers_collect", tasks)
        imported = [run for run in payload["runs"] if run["origin"] == "legacy"]
        # 旧库里每一行 radar_runs 就是一次真实的采集运行；同一 started_at 的一组论文来源
        # 是同一轮论文抓取 → 2 条雷达运行 + 1 条论文运行 = 3 条，而不是把两次采集揉成一条。
        self.assertEqual(len(imported), 3, "两条 radar_runs + 一组论文来源 = 3 条运行")
        self.assertEqual(len([r for r in imported if r["task"] == "event_report"]), 2)
        self.assertEqual(len([r for r in imported if r["task"] == "papers_collect"]), 1)
        paper_run = next(r for r in imported if r["task"] == "papers_collect")
        self.assertEqual(paper_run["sources_attempted"], 2, "一组里的两个来源都算尝试过")
        self.assertEqual(paper_run["status"], "partial", "一成功一失败 = 部分成功")
        radar_run = next(run for run in payload["runs"]
                         if run["task"] == "event_report" and run["items_new"] == 7)
        self.assertFalse(radar_run["live"])
        self.assertEqual(radar_run["trigger"], "schedule")
        self.assertEqual(radar_run["items_new"], 7)
        self.assertEqual(radar_run["items_duplicate"], 48)
        self.assertEqual(radar_run["items_filtered"], 5)
        self.assertEqual(radar_run["error_count"], 1)
        # 详情：阶段只有真实有证据的写进去，其余标成「未知（历史记录没有分阶段数据）」
        detail = self.api_ok("/api/monitor/run", id=radar_run["run_id"])
        stages = {stage["name"]: stage for stage in detail["stages"]}
        # 本轮是 3 个来源里 2 成 1 败 → 抓取阶段是「部分成功」，不是「成功」
        self.assertEqual(stages["fetch"]["status"], "partial")
        self.assertEqual(stages["persist"]["status"], "ok")
        self.assertEqual(stages["push"]["status"], "skipped")
        self.assertEqual(stages["parse"]["status"], "unknown",
                         "历史记录不能假装知道解析阶段执行过没有")
        # 来源结果：成功的与失败的各一条，失败带真实错误码
        sources = {row["source_id"]: row for row in detail["sources"]}
        self.assertEqual(sources["cn_scroll"]["items_new"], 7)
        self.assertEqual(sources["gdelt"]["status"], "failed")
        self.assertEqual(sources["gdelt"]["error_code"], "http_429")
        # 另一条旧运行是真实失败：错误文本与「无法确定」的阶段都要照实显示，不能补成成功
        failed_run = next(run for run in payload["runs"]
                          if run["task"] == "event_report" and run["status"] == "failed")
        self.assertEqual(failed_run["error_count"], 1)
        self.assertIn("database is locked",
                      " ".join(entry["message"] for entry in failed_run["errors"]))
        failed_detail = self.api_ok("/api/monitor/run", id=failed_run["run_id"])
        failed_stages = {stage["name"]: stage for stage in failed_detail["stages"]}
        self.assertEqual(failed_stages["fetch"]["status"], "unknown",
                         "旧记录没有 collect 明细时，fetch 阶段只能是「无法确定」")
        self.assertIn("无法区分", failed_stages["fetch"]["result"]["note"])

    def test_import_is_idempotent(self):
        self._legacy_tables()
        conn = db.connect()
        try:
            first = store.import_legacy(conn, force=True)
            second = store.import_legacy(conn, force=True)
            counts_after = store.counts(conn)
        finally:
            conn.close()
        self.assertEqual(second["radar_runs"], 0, "重复导入不得再造运行记录")
        self.assertEqual(second["paper_runs"], 0)
        again = self.api_ok("/api/monitor/runs", window="all")
        self.assertEqual(again["total"], counts_after["runs"])
        self.assertGreaterEqual(first["radar_runs"], 1)

    def test_import_records_real_error_logs(self):
        self._legacy_tables()
        conn = db.connect()
        try:
            store.import_legacy(conn, force=True)
        finally:
            conn.close()
        payload = self.api_ok("/api/monitor/logs", window="all", level="ERROR")
        self.assertTrue(payload["total"] >= 1)
        messages = " ".join(log["message"] for log in payload["logs"])
        self.assertIn("database is locked", messages)

    def test_timeline_marks_runs_without_start_time(self):
        # 历史日报没有开始时间：只出现在列表里，不画在时间轴上
        run = monitor.run("github_daily", trigger="import")
        run.start()
        run.conn.execute("UPDATE monitor_runs SET started_at=NULL WHERE run_id=?", (run.run_id,))
        run.conn.commit()
        run.finish(status="ok")
        payload = self.api_ok("/api/monitor/timeline", window="all")
        self.assertEqual(payload["unknown_start"], 1)
        self.assertIn("没有记录开始时间", payload["unknown_start_note"])
        self.assertEqual(payload["total"], 1)


class TestApiFailure(MonitorTestBase):
    def test_store_failure_returns_json_error_and_no_traceback(self):
        def boom(*args, **kwargs):
            raise RuntimeError("假装数据库坏了")

        original_fn = store.overview
        store.overview = boom
        try:
            status, payload = self.api("/api/monitor/overview")
        finally:
            store.overview = original_fn
        self.assertEqual(status, 500)
        self.assertIn("error", payload)
        self.assertIn("假装数据库坏了", payload["error"])
        self.assertIn("hint", payload, "失败时要给出可诊断的提示")

    def test_unknown_endpoint_is_404(self):
        status, payload = self.api("/api/monitor/whatever")
        self.assertEqual(status, 404)
        self.assertIn("not found", payload["error"])


class TestMonitorPageContract(unittest.TestCase):
    """监控页的静态契约：入口、DOM id、只读调用、安全渲染、轮询与样式。

    这里不做「字符串像不像」的作文检查，只钉死可验证的约束：脚本引用的每个 id 都存在、
    没有外部依赖、没有把不可信数据拼进 HTML 的入口、自动刷新在页面不可见时停、日志增量
    加载不把历史顶回顶部、样式只有一份且用 .monitor-* 前缀。
    """

    WEB = Path(__file__).resolve().parent.parent / "web"
    # 这些 id 由脚本自己渲染进抽屉/分区，静态 HTML 里当然没有（与 test_web.py 同一约定）
    DYNAMIC_IDS = {"drawerItemAction", "drawerItemQuery", "drawerLogLevel", "drawerSourceDetail"}

    @classmethod
    def setUpClass(cls):
        cls.html = (cls.WEB / "monitor.html").read_text(encoding="utf-8")
        cls.js = (cls.WEB / "monitor.js").read_text(encoding="utf-8")
        cls.css = (cls.WEB / "styles.css").read_text(encoding="utf-8")

    def test_page_entry_uses_the_shared_shell(self):
        for marker in ('href="/styles.css"', 'src="/shell.js"', 'src="/monitor.js"',
                       'class="sidebar"', 'class="topbar"', 'id="sidebarToggle"',
                       'id="themeToggle"', 'id="navToggle"', 'id="sectionNav"'):
            self.assertIn(marker, self.html, "监控页缺少 %s" % marker)
        # 主题在首屏前生效，避免闪一下白底
        head = self.html.split("</head>")[0]
        self.assertIn("data-theme", head)
        self.assertIn("fb.theme", head)

    def test_navigation_entry_exists_on_every_page_and_status_page_is_kept(self):
        pages = sorted(p.name for p in self.WEB.glob("*.html"))
        self.assertIn("monitor.html", pages)
        for page in pages:
            text = (self.WEB / page).read_text(encoding="utf-8")
            self.assertIn('href="/monitor.html"', text, "%s 缺少采集监控入口" % page)
        # 系统状态页继续保留，两页职责不同
        self.assertTrue((self.WEB / "status.html").is_file())
        self.assertIn('href="/status.html"', self.html, "监控页仍要能回到系统状态页")
        self.assertIn("status.js", (self.WEB / "status.html").read_text(encoding="utf-8"))

    def test_every_dom_id_used_by_monitor_js_exists(self):
        used = set(re.findall(r'\$\("#([A-Za-z][\w-]*)"\)', self.js))
        self.assertTrue(used, "monitor.js 应当绑定真实的 DOM id")
        missing = sorted(i for i in used
                         if i not in self.DYNAMIC_IDS and ('id="%s"' % i) not in self.html)
        self.assertEqual(missing, [], "monitor.js 需要但 monitor.html 缺失的 id：%s" % missing)

    def test_page_has_no_external_dependency(self):
        for match in re.findall(r'(?:src|href)="(https?://[^"]+)"', self.html):
            self.fail("监控页引用了外部资源：%s" % match)
        for match in re.findall(r'(?:src|href)="(//[^"]+)"', self.html):
            self.fail("监控页引用了协议相对的外部资源：%s" % match)
        self.assertNotIn("cdn.", self.html)

    def test_page_only_calls_the_readonly_monitor_api(self):
        calls = set(re.findall(r'apiGet\("([^"]+)"', self.js))
        self.assertTrue(calls)
        for path in calls:
            self.assertTrue(path.startswith("/"), path)
        self.assertIn("/overview", calls)
        self.assertIn("/runs", calls)
        self.assertIn("/run", calls)
        self.assertIn("/sources", calls)
        self.assertIn("/logs", calls)
        self.assertEqual(self.js.count("fetch("), 1, "只应有一处 fetch，且统一走 apiGet")
        # 只读：不得出现任何写请求或第三方地址
        for marker in ('method: "POST"', "method: 'POST'", "XMLHttpRequest"):
            self.assertNotIn(marker, self.js, "监控页必须是只读的：%s" % marker)
        self.assertEqual(sorted(set(re.findall(r'https?://[^\s"\']{0,40}', self.js))), [])

    def test_untrusted_text_has_no_html_sink(self):
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                     "eval(", "new Function"):
            self.assertNotIn(sink, self.js, "monitor.js 不得用 %s 渲染数据" % sink)
        self.assertIn("textContent", self.js)
        self.assertIn("createElement", self.js)
        # 渲染函数统一走 createElement + textContent
        self.assertIn("node.textContent = value", self.js)

    def test_links_are_protocol_checked_and_opened_safely(self):
        self.assertIn("function safeLink(", self.js)
        guard = self.js.split("function safeLink(", 1)[1].split("function ", 1)[0]
        self.assertIn('"http:"', guard)
        self.assertIn('"https:"', guard)
        self.assertIn("hostname", guard)
        render = self.js.split("function linkOrText(", 1)[1].split("\n  }", 1)[0]
        self.assertIn('target: "_blank"', render)
        self.assertIn('rel: "noopener noreferrer"', render)
        # 没有通过 safeLink 校验的地址只能当纯文本
        self.assertIn("safeLink(url)", render)

    def test_auto_refresh_pauses_when_the_page_is_hidden(self):
        self.assertIn("document.hidden", self.js)
        self.assertIn("visibilitychange", self.js)
        self.assertIn("clearInterval", self.js)
        # 两个轮询回调都必须先看可见性
        self.assertGreaterEqual(self.js.count("if (document.hidden) return;"), 2,
                                "总览轮询与日志轮询都要在页面不可见时暂停")
        # 恢复可见时补一次轻量刷新（不动日志面板的阅读位置）
        watcher = self.js.split('document.addEventListener("visibilitychange"', 1)[1]
        self.assertIn("refreshLight()", watcher)
        self.assertIn("pollLogs()", watcher)

    def test_live_status_dot_has_failure_colours(self):
        """顶栏圆点必须能表示失败：曾经只有绿色，接口挂了还是绿点配「读取失败」。"""
        self.assertIn("live-dot is-", self.js)
        for kind in ("is-ok", "is-info", "is-warn", "is-bad"):
            self.assertIn(".live-dot.%s" % kind, self.css,
                          "缺少 .live-dot.%s 的配色，状态圆点会永远显示成功色" % kind)

    def test_log_scroller_is_the_element_that_actually_scrolls(self):
        """滚动容器是 .monitor-logwrap；读 #logsList.scrollTop 恒为 0，滚动位置会失效。"""
        self.assertIn("function logsScroller(", self.js)
        self.assertIn('closest(".monitor-logwrap")', self.js)
        self.assertIn(".monitor-logwrap", self.css)
        # 真正的滚动容器：有高度上限并且竖向可滚动（否则 scrollTop 永远是 0）
        scroller_rule = self.css.split(".monitor-logwrap { max-height", 1)[1].split("}", 1)[0]
        self.assertIn("overflow-y: auto", scroller_rule)

    def test_log_auto_refresh_is_incremental_and_keeps_scroll_anchor(self):
        poll = self.js.split("async function pollLogs()", 1)[1].split("async function openRun(", 1)[0]
        self.assertIn("since_id", poll, "日志增量加载要按 since_id 拉取，而不是整页重取")
        self.assertIn("anchor", poll, "增量插入后要保住滚动位置，历史不能被顶回顶部")
        self.assertIn("scrollTop", poll)
        self.assertNotIn("renderLogs(", poll, "自动刷新不得整页重渲染，否则阅读位置会被顶回顶部")
        self.assertEqual(self.js.count("setInterval"), 2, "总览与日志各一个轮询，不多不少")

    def test_older_logs_are_appended_instead_of_replacing_the_page(self):
        """「加载更早」必须把更旧的一页接在下面；曾经它直接替换整页，阅读位置全丢。"""
        self.assertIn('logsAppend: "1"', self.js)
        self.assertIn('dataset: { logsPage: String(payload.page + 1), logsAppend: "1" }', self.js)
        render = self.js.split("function renderLogs(payload, options)", 1)[1].split("\n  async function", 1)[0]
        self.assertIn("if (opts.append)", render)
        self.assertIn("concat(payload.logs)", render)
        self.assertIn("has_more", render, "没有更多时必须禁用「加载更早」")

    def test_drawer_tabs_expose_tabpanel_semantics(self):
        for panel in ("drawerOverview", "drawerStages", "drawerSources", "drawerItems", "drawerLogs"):
            self.assertIn('id="%s"' % panel, self.html)
        self.assertIn('role="tabpanel"', self.html)
        self.assertIn("aria-controls=", self.html)
        self.assertIn("aria-labelledby=", self.html)

    def test_missing_overview_sections_render_unknown_not_zero(self):
        """接口没返回「未解决错误 / 长期未更新来源」时必须是未知，不能被 0 顶替。"""
        self.assertIn("payload.open_errors || null", self.js)
        self.assertIn("payload.stale_sources || null", self.js)
        self.assertIn("numberOrUnknown", self.js)
        self.assertIn("接口没有返回这一项，暂时无法判断", self.js)
        # 阈值小时数来自接口，不能写死
        self.assertIn("超过 ${show(stale.threshold_hours)} 小时", self.js)
        self.assertNotIn("threshold_hours || 48", self.js)

    def test_timeline_ticks_are_snapped_to_round_local_hours(self):
        """刻度要落在整点（1/2/3/6/12/24 小时或整天），而不是窗口等分点。"""
        render = self.js.split("function renderTimeline(payload)", 1)[1].split("\n  function ", 1)[0]
        self.assertIn("function tickStep(", render)
        self.assertIn("function alignedTicks(", render)
        self.assertIn("getFullYear(), start.getMonth(), start.getDate()", render)
        self.assertIn("monitor-tick is-start", render)
        self.assertIn("monitor-tick is-end", render)
        # 七类任务一辆车都不能少
        self.assertNotIn("payload.groups.length > 6", render)
        self.assertIn("is-empty", render)

    def test_stage_numbers_follow_execution_order(self):
        """阶段序号按接口返回顺序编号：直接用历史导入的 seq 会出现 1 2 3 4 2 3 7 8。"""
        render = self.js.split("function renderDrawerStages(payload)", 1)[1].split("\n  function ", 1)[0]
        self.assertIn("payload.stages.forEach((stage, index)", render)
        self.assertIn("`${index + 1}. ", render)

    def test_drawer_returns_focus_and_matches_close_animation(self):
        closer = self.js.split("function closeDrawer()", 1)[1].split("\n  function ", 1)[0]
        self.assertIn("drawerOpener", closer)
        self.assertIn(".focus()", closer)
        self.assertIn("260", closer, "关抽屉的延时要和 --fb-dur-slow 一致，否则动画被切断")
        # 抽屉里的来源行也要能用键盘打开
        self.assertIn("[data-drawer-source]", self.js)

    def test_per_source_items_and_logs_are_rendered(self):
        """5C：点来源能看到它的候选条目与来源日志。"""
        render = self.js.split("function renderSourceDetail(payload)", 1)[1].split("\n  /* ", 1)[0]
        self.assertIn("payload.results", render)
        self.assertIn("payload.logs", render)
        self.assertIn("来源详情", render)

    def test_keyboard_navigation_is_wired(self):
        self.assertIn("tabindex", self.js, "可点击的行要能聚焦")
        self.assertIn('event.key !== "Enter" && event.key !== " "', self.js)
        self.assertIn("event.preventDefault()", self.js)
        # Esc 由外壳广播，监控页负责关抽屉
        self.assertIn('addEventListener("fb:escape", closeDrawer)', self.js)
        shell = (self.WEB / "shell.js").read_text(encoding="utf-8")
        self.assertIn('new CustomEvent("fb:escape")', shell)
        self.assertIn("role=\"tablist\"", self.html)

    def test_empty_and_error_states_are_real(self):
        self.assertIn("function emptyState(", self.js)
        self.assertIn("function errorState(", self.js)
        # 首屏只放「正在读取…」占位，不放任何假数字/假日志
        placeholders = re.findall(r'<p class="pending">([^<]*)</p>', self.html)
        self.assertTrue(placeholders)
        for text in placeholders:
            self.assertTrue(text.endswith("…"), "占位文案只能是加载中：%s" % text)
        self.assertNotIn("monitor-sample", self.html)

    def test_monitor_styles_use_prefix_tokens_and_one_stylesheet(self):
        for name in ("monitor-cards", "monitor-card", "monitor-table", "monitor-drawer",
                     "monitor-timeline-axis", "monitor-lane", "monitor-log", "monitor-chip",
                     "monitor-pager", "monitor-filters", "monitor-empty", "monitor-notice"):
            self.assertIn("." + name, self.css, "styles.css 缺少 .%s" % name)
        # 只用现有设计令牌，不自造硬编码配色文件
        monitor_block = self.css.split(".monitor-", 1)[1]
        self.assertIn("var(--fb-", monitor_block)
        css_files = sorted(p.name for p in self.WEB.glob("*.css"))
        self.assertEqual(css_files, ["styles.css"], "不允许新增 CSS 文件")

    def test_responsive_and_reduced_motion_cover_the_monitor_page(self):
        self.assertIn("prefers-reduced-motion", self.css)
        mobile = [block for block in re.split(r"@media[^{]*\{", self.css)
                  if "monitor-" in block and "max-width: 860px" in block]
        self.assertTrue(mobile or re.search(
            r"@media[^{]*max-width:\s*860px[^{]*\{(?:[^{}]|\{[^{}]*\})*\.monitor-", self.css),
            "移动端断点里要针对 .monitor-* 改成单列")
        self.assertIn("grid-template-columns: 1fr", self.css)


if __name__ == "__main__":
    unittest.main(verbosity=2)