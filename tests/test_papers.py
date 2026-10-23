"""学术论文模块测试：分类、去重、AI 概况、数据源统计、API 参数。

默认全部离线：不触碰外网，来源用假数据；真实连通性测试见
``python app.py radar papers test-sources``。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401,E402

from core import db  # noqa: E402
from core.papers import ai as paper_ai  # noqa: E402
from core.papers import api as paper_api  # noqa: E402
from core.papers import classify, sources, store  # noqa: E402


def _paper(**overrides):
    base = {
        "source": "crossref",
        "source_id": "10.1000/xyz",
        "title": "A Study of Quantum Transport in Graphene",
        "abstract": "We measure quantum transport in graphene devices at low temperature.",
        "url": "https://doi.org/10.1000/xyz",
        "published_at": "2026-10-20",
        "doi": "10.1000/xyz",
        "pmid": "",
        "arxiv_id": "",
        "venue": "Physical Review Letters",
        "subjects": [],
        "raw": {},
    }
    base.update(overrides)
    return base


class Base(unittest.TestCase):
    def setUp(self):
        store.init()
        conn = db.connect()
        try:
            conn.execute("DELETE FROM paper_items")
            conn.execute("DELETE FROM paper_ai_overviews")
            conn.execute("DELETE FROM paper_source_runs")
            conn.commit()
        finally:
            conn.close()

    def conn(self):
        return db.connect()


class TestClassification(unittest.TestCase):
    def test_physics_by_journal_and_keywords(self):
        discs = classify.classify("crossref", "Quantum transport in graphene",
                                  "We study quantum transport and condensed matter effects.",
                                  "Physical Review Letters")
        self.assertIn("physics", discs)

    def test_medicine_by_source(self):
        discs = classify.classify("pubmed", "A randomized clinical trial of therapy",
                                  "Patients received treatment; mortality was measured.")
        self.assertIn("medicine", discs)

    def test_cs_by_huggingface_source(self):
        discs = classify.classify("huggingface", "A new transformer for code",
                                  "We train a large language model.")
        self.assertIn("cs", discs)

    def test_single_common_word_is_not_enough(self):
        # 只出现一个"market"，不足以判定金融
        discs = classify.classify("crossref", "Market day at the village",
                                  "A short note about vegetables.")
        self.assertNotIn("finance", discs)

    def test_multi_discipline_allowed(self):
        discs = classify.classify(
            "crossref", "Machine learning for protein structure prediction",
            "Deep learning models predict protein folding from genomic data. "
            "We train neural networks on gene sequences.",
            "Nature Biotechnology")
        self.assertIn("biology", discs)
        self.assertIn("cs", discs)

    def test_unknown_returns_empty(self):
        self.assertEqual(classify.classify("crossref", "Notes on a Tuesday",
                                           "Nothing specific here."), [])

    def test_normalize_discipline(self):
        self.assertEqual(classify.normalize_discipline("CS"), "cs")
        self.assertEqual(classify.normalize_discipline("nonsense"), "all")
        self.assertEqual(classify.normalize_discipline(""), "all")


class TestDedupe(Base):
    def test_same_doi_merges(self):
        conn = self.conn()
        try:
            store.upsert_papers(conn, [_paper()])
            store.upsert_papers(conn, [_paper(title="Same DOI, new title")])
            self.assertEqual(store.count_papers(conn, "all"), 1)
        finally:
            conn.close()

    def test_cross_source_same_paper_merges_and_tracks_sources(self):
        conn = self.conn()
        try:
            store.upsert_papers(conn, [_paper(disciplines=["physics"])])
            store.upsert_papers(conn, [_paper(source="openalex", source_id="W123",
                                              disciplines=["physics"])])
            papers = store.list_papers(conn, "all", 5)
            self.assertEqual(len(papers), 1)
            self.assertEqual({s["source"] for s in papers[0]["sources"]},
                             {"crossref", "openalex"})
        finally:
            conn.close()

    def test_arxiv_version_suffix_collapses(self):
        conn = self.conn()
        try:
            store.upsert_papers(conn, [_paper(doi="", source="huggingface", arxiv_id="2410.12345v1")])
            store.upsert_papers(conn, [_paper(doi="", source="huggingface", arxiv_id="2410.12345v3")])
            self.assertEqual(store.count_papers(conn, "all"), 1)
        finally:
            conn.close()

    def test_no_identifier_falls_back_to_title(self):
        conn = self.conn()
        try:
            store.upsert_papers(conn, [_paper(doi="", pmid="", arxiv_id="")])
            store.upsert_papers(conn, [_paper(doi="", pmid="", arxiv_id="",
                                              title="A study of   QUANTUM transport in graphene!")])
            self.assertEqual(store.count_papers(conn, "all"), 1)
        finally:
            conn.close()

    def test_empty_abstract_does_not_overwrite_existing(self):
        conn = self.conn()
        try:
            store.upsert_papers(conn, [_paper()])
            store.upsert_papers(conn, [_paper(abstract="")])
            paper = store.list_papers(conn, "all", 1)[0]
            self.assertIn("quantum transport", paper["abstract"].lower())
        finally:
            conn.close()

    def test_missing_date_and_link_still_stored(self):
        conn = self.conn()
        try:
            counts = store.upsert_papers(conn, [_paper(published_at="", url="", doi="")])
            self.assertEqual(counts["inserted"], 1)
            paper = store.list_papers(conn, "all", 1)[0]
            self.assertEqual(paper["published_at"], "")
        finally:
            conn.close()

    def test_empty_title_is_skipped(self):
        conn = self.conn()
        try:
            counts = store.upsert_papers(conn, [_paper(title="   ")])
            self.assertEqual(counts["skipped"], 1)
        finally:
            conn.close()


class TestDisciplineFilter(Base):
    def test_filter_returns_only_matching(self):
        conn = self.conn()
        try:
            store.upsert_papers(conn, [
                _paper(doi="10.1/a", title="Quantum thing", disciplines=["physics"]),
                _paper(doi="10.1/b", title="Clinical trial", disciplines=["medicine"]),
                _paper(doi="10.1/c", title="Mixed", disciplines=["physics", "math"]),
            ])
            physics = store.list_papers(conn, "physics", 10)
            self.assertEqual({p["doi"] for p in physics}, {"10.1/a", "10.1/c"})
            self.assertEqual(len(store.list_papers(conn, "all", 10)), 3)
        finally:
            conn.close()


class TestOverviewStore(Base):
    def test_save_and_read_overview(self):
        conn = self.conn()
        try:
            store.upsert_papers(conn, [_paper()])
            paper = store.list_papers(conn, "all", 1)[0]
            store.save_overview(conn, paper["id"], "done",
                                {"research": "研究 X", "method": "方法 Y"}, "model-a", 1,
                                content_hash_value=store.content_hash(paper))
            saved = store.get_overview(conn, paper["id"])
            self.assertEqual(saved["status"], "done")
            self.assertEqual(saved["model"], "model-a")
            self.assertEqual(saved["overview"]["research"], "研究 X")
        finally:
            conn.close()

    def test_changed_abstract_becomes_pending_again(self):
        conn = self.conn()
        try:
            store.upsert_papers(conn, [_paper()])
            paper = store.list_papers(conn, "all", 1)[0]
            store.save_overview(conn, paper["id"], "done", {"research": "旧"},
                                "m", 1, content_hash_value="stale-hash")
            pending = store.pending_papers(conn, 10, 1)
            self.assertEqual([p["id"] for p in pending], [paper["id"]])
        finally:
            conn.close()

    def test_fresh_done_is_not_pending(self):
        conn = self.conn()
        try:
            store.upsert_papers(conn, [_paper()])
            paper = store.list_papers(conn, "all", 1)[0]
            store.save_overview(conn, paper["id"], "done", {"research": "x"},
                                "m", 1, content_hash_value=store.content_hash(paper))
            self.assertEqual(store.pending_papers(conn, 10, 1), [])
        finally:
            conn.close()


class TestPaperAi(unittest.TestCase):
    def test_normalize_requires_research(self):
        self.assertIsNone(paper_ai.normalize({"method": "m"}))
        self.assertIsNone(paper_ai.normalize("not a dict"))
        ok = paper_ai.normalize({"research": "研究", "method": "方法"})
        self.assertEqual(ok["research"], "研究")

    def test_normalize_trims_long_values(self):
        data = paper_ai.normalize({"research": "x" * 500})
        self.assertLessEqual(len(data["research"]), 200)

    def test_disabled_model_reports_reason(self):
        overview, error, model = paper_ai.analyze(_paper(), {"llm": {"enabled": False}})
        self.assertIsNone(overview)
        self.assertIn("未启用", error)

    def test_paper_without_abstract_is_not_sent_to_model(self):
        overview, error, _model = paper_ai.analyze(
            _paper(abstract=""), {"llm": {"enabled": True, "model": "m"}})
        self.assertIsNone(overview)
        self.assertIn("没有摘要", error)

    def test_render_text_keeps_labels(self):
        text = paper_ai.render_text({"research": "研究", "limits": "无法判断"})
        self.assertIn("研究内容", text)
        self.assertIn("信息局限", text)


class TestIsoDate(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(sources.iso_date("2026-Oct-21"), "2026-10-21")
        self.assertEqual(sources.iso_date("2026-10-21"), "2026-10-21")
        self.assertEqual(sources.iso_date("2026/9/3"), "2026-09-03")
        self.assertEqual(sources.iso_date("2026-Oct"), "2026-10-01")
        self.assertEqual(sources.iso_date(""), "")


class TestApiValidation(Base):
    def test_unknown_discipline_falls_back_to_all(self):
        conn = self.conn()
        try:
            store.upsert_papers(conn, [_paper(disciplines=["physics"])])
        finally:
            conn.close()
        status, payload = paper_api.handle_get("/api/radar/papers",
                                               {"discipline": "astrology", "limit": "5"}, {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["discipline"], "all")
        self.assertEqual(len(payload["papers"]), 1)

    def test_limit_is_clamped(self):
        status, payload = paper_api.handle_get("/api/radar/papers", {"limit": "999"}, {})
        self.assertEqual(status, 200)
        self.assertIn("papers", payload)

    def test_retry_requires_id(self):
        status, payload = paper_api.handle_post("/api/radar/papers/retry", {}, {})
        self.assertEqual(status, 400)

    def test_unknown_path_404(self):
        status, _payload = paper_api.handle_get("/api/radar/papers/nope", {}, {})
        self.assertEqual(status, 404)


class TestAiQueueWithoutLimits(Base):
    """论文模块不再单独限制 AI 次数，成本统一由全局 token 限额控制。"""

    def _make_papers(self, count):
        conn = self.conn()
        try:
            items = [_paper(doi="10.9/%d" % i, title="Paper %d" % i,
                            abstract="Abstract body number %d with enough words." % i)
                     for i in range(count)]
            store.upsert_papers(conn, items)
        finally:
            conn.close()

    def _drain(self):
        from core.papers import service

        while service.queue_size():
            service._QUEUE.get_nowait()
            service._QUEUE.task_done()

    def test_all_pending_papers_are_queued(self):
        from core.papers import service

        self._make_papers(5)
        cfg = {"papers": {"ai": {"enabled": True, "concurrency": 1}}}
        self.assertEqual(service.analyze_pending(cfg), 5)
        self._drain()

    def test_disabled_ai_queues_nothing(self):
        from core.papers import service

        self._make_papers(2)
        self.assertEqual(service.analyze_pending({"papers": {"ai": {"enabled": False}}}), 0)

    def test_status_reports_ai_state_not_budget(self):
        from core.papers import service

        status = service.status({"papers": {"ai": {"enabled": True}}})
        self.assertIn("ai", status)
        self.assertNotIn("ai_budget", status)
        self.assertIn("paused", status["ai"])
        self.assertEqual(status["schedule"]["interval_hours"], 24)

    def test_retry_does_not_write_budget(self):
        from core.papers import service

        self._make_papers(1)
        conn = self.conn()
        try:
            paper_id = store.list_papers(conn, "all", 1)[0]["id"]
        finally:
            conn.close()
        self.assertTrue(service.retry_one(paper_id))
        self._drain()


if __name__ == "__main__":
    unittest.main(verbosity=2)
