"""收藏夹模块测试。离线：只用本地 SQLite。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401,E402  (isolated DB + project root on sys.path)

from core import bookmarks, db  # noqa: E402


def _item(full_name="acme/tool", **overrides):
    item = {
        "full_name": full_name,
        "owner": full_name.split("/")[0],
        "name": full_name.split("/")[-1],
        "url": "https://github.com/%s" % full_name,
        "description": "一个用来处理日常任务的本地工具",
        "language": "Python",
        "stars": 1234,
        "topics": ["cli", "automation"],
        "avatar": "https://github.com/acme.png?size=96",
    }
    item.update(overrides)
    return item


class Base(unittest.TestCase):
    def setUp(self):
        db.init()
        self.conn = db.connect()
        bookmarks.init(self.conn)
        self.conn.execute("DELETE FROM bookmarks")
        self.conn.execute("DELETE FROM bookmark_folders")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()


class TestBookmarkItems(Base):
    def test_add_stores_a_metadata_snapshot(self):
        bookmarks.add(self.conn, "acme/tool", _item())
        rows = bookmarks.items(self.conn)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["full_name"], "acme/tool")
        self.assertEqual(row["description"], "一个用来处理日常任务的本地工具")
        self.assertEqual(row["language"], "Python")
        self.assertEqual(row["stars"], 1234)
        self.assertEqual(row["topics"], ["cli", "automation"])
        self.assertEqual(row["owner"], "acme")
        self.assertEqual(row["name"], "tool")
        self.assertIn("acme.png", row["avatar"])

    def test_snapshot_survives_without_any_item_data(self):
        """收藏页不依赖 repos 表，也不依赖当日日报。"""
        bookmarks.add(self.conn, "ghost/repo")
        rows = bookmarks.items(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["full_name"], "ghost/repo")
        self.assertEqual(rows[0]["stars"], 0)
        self.assertEqual(rows[0]["topics"], [])

    def test_toggle_adds_then_removes(self):
        self.assertTrue(bookmarks.toggle(self.conn, "acme/tool", _item()))
        self.assertTrue(bookmarks.exists(self.conn, "acme/tool"))
        self.assertFalse(bookmarks.toggle(self.conn, "acme/tool"))
        self.assertFalse(bookmarks.exists(self.conn, "acme/tool"))
        self.assertEqual(bookmarks.items(self.conn), [])

    def test_remove_is_idempotent(self):
        bookmarks.remove(self.conn, "nope/none")
        self.assertEqual(bookmarks.counts(self.conn)["total"], 0)

    def test_re_add_keeps_the_better_metadata(self):
        bookmarks.add(self.conn, "acme/tool", _item(stars=10))
        # 再收一次但这次没有描述：旧值不能被空值覆盖
        bookmarks.add(self.conn, "acme/tool", _item(description="", stars=999))
        row = bookmarks.items(self.conn)[0]
        self.assertEqual(row["description"], "一个用来处理日常任务的本地工具")
        self.assertEqual(row["stars"], 999)  # MAX(旧, 新)

    def test_counts(self):
        bookmarks.add(self.conn, "a/one", _item("a/one"))
        bookmarks.add(self.conn, "a/two", _item("a/two"))
        folder = bookmarks.create_folder(self.conn, "工具")
        bookmarks.move(self.conn, "a/one", folder["id"])
        counts = bookmarks.counts(self.conn)
        self.assertEqual(counts["total"], 2)
        self.assertEqual(counts["unfiled"], 1)
        self.assertEqual(counts["folders"], 1)


class TestFolders(Base):
    def test_create_rename_delete(self):
        folder = bookmarks.create_folder(self.conn, "AI 工具")
        self.assertEqual(folder["name"], "AI 工具")
        bookmarks.rename_folder(self.conn, folder["id"], "AI 与大模型")
        self.assertEqual(bookmarks.folders(self.conn)[0]["name"], "AI 与大模型")
        bookmarks.delete_folder(self.conn, folder["id"])
        self.assertEqual(bookmarks.folders(self.conn), [])

    def test_folder_name_is_required_and_capped(self):
        with self.assertRaises(ValueError):
            bookmarks.create_folder(self.conn, "   ")
        long_name = "长" * 80
        folder = bookmarks.create_folder(self.conn, long_name)
        self.assertLessEqual(len(folder["name"]), bookmarks.MAX_FOLDER_NAME)

    def test_duplicate_name_rejected_case_insensitively(self):
        bookmarks.create_folder(self.conn, "Tools")
        with self.assertRaises(ValueError):
            bookmarks.create_folder(self.conn, "tools")

    def test_folder_count_is_capped(self):
        for i in range(bookmarks.MAX_FOLDERS):
            bookmarks.create_folder(self.conn, "文件夹 %d" % i)
        with self.assertRaises(ValueError):
            bookmarks.create_folder(self.conn, "再来一个")

    def test_deleting_a_folder_keeps_its_bookmarks(self):
        folder = bookmarks.create_folder(self.conn, "临时")
        bookmarks.add(self.conn, "a/one", _item("a/one"), folder_id=folder["id"])
        bookmarks.delete_folder(self.conn, folder["id"])
        rows = bookmarks.items(self.conn)
        self.assertEqual(len(rows), 1)          # 收藏没被删
        self.assertIsNone(rows[0]["folder_id"])  # 回到未分类

    def test_rename_refuses_unknown_and_duplicate(self):
        first = bookmarks.create_folder(self.conn, "A")
        second = bookmarks.create_folder(self.conn, "B")
        with self.assertRaises(ValueError):
            bookmarks.rename_folder(self.conn, second["id"], "a")
        with self.assertRaises(ValueError):
            bookmarks.rename_folder(self.conn, 9999, "C")
        self.assertEqual(bookmarks.folders(self.conn)[0]["id"], first["id"])

    def test_delete_unknown_folder_raises(self):
        with self.assertRaises(ValueError):
            bookmarks.delete_folder(self.conn, 4242)


class TestFolderFiltering(Base):
    def setUp(self):
        super().setUp()
        self.tools = bookmarks.create_folder(self.conn, "工具")
        self.papers = bookmarks.create_folder(self.conn, "论文")
        bookmarks.add(self.conn, "a/inbox", _item("a/inbox"))
        bookmarks.add(self.conn, "a/wrench", _item("a/wrench"), folder_id=self.tools["id"])
        bookmarks.add(self.conn, "a/paper", _item("a/paper"), folder_id=self.papers["id"])

    def test_filter_by_folder(self):
        self.assertEqual(len(bookmarks.items(self.conn, "all")), 3)
        self.assertEqual(len(bookmarks.items(self.conn, self.tools["id"])), 1)
        self.assertEqual(bookmarks.items(self.conn, self.tools["id"])[0]["full_name"], "a/wrench")

    def test_filter_unfiled(self):
        rows = bookmarks.items(self.conn, bookmarks.UNFILED)
        self.assertEqual([r["full_name"] for r in rows], ["a/inbox"])

    def test_unknown_folder_falls_back_to_unfiled(self):
        rows = bookmarks.items(self.conn, 999999)
        self.assertEqual([r["full_name"] for r in rows], ["a/inbox"])

    def test_move_between_folders(self):
        self.assertTrue(bookmarks.move(self.conn, "a/wrench", self.papers["id"]))
        self.assertEqual(len(bookmarks.items(self.conn, self.tools["id"])), 0)
        self.assertEqual(len(bookmarks.items(self.conn, self.papers["id"])), 2)

    def test_move_to_unfiled_with_empty_value(self):
        self.assertTrue(bookmarks.move(self.conn, "a/wrench", None))
        row = [r for r in bookmarks.items(self.conn) if r["full_name"] == "a/wrench"][0]
        self.assertIsNone(row["folder_id"])

    def test_move_unknown_repo_returns_false(self):
        self.assertFalse(bookmarks.move(self.conn, "no/such", self.tools["id"]))

    def test_invalid_folder_id_on_add_lands_in_unfiled(self):
        bookmarks.add(self.conn, "a/odd", _item("a/odd"), folder_id="not-an-id")
        row = [r for r in bookmarks.items(self.conn) if r["full_name"] == "a/odd"][0]
        self.assertIsNone(row["folder_id"])

    def test_folder_name_is_exposed_for_display(self):
        row = bookmarks.items(self.conn, self.tools["id"])[0]
        self.assertEqual(row["folder_name"], "工具")


class TestSearch(Base):
    def setUp(self):
        super().setUp()
        bookmarks.add(self.conn, "acme/crawler", _item(
            "acme/crawler", description="批量抓取网页并整理成结构化数据", language="Python",
            topics=["scraping", "crawler"]))
        bookmarks.add(self.conn, "acme/render", _item(
            "acme/render", description="把视图渲染成图片", language="Rust", topics=["image"]))

    def test_search_matches_name(self):
        rows = bookmarks.items(self.conn, "all", "crawler")
        self.assertEqual([r["full_name"] for r in rows], ["acme/crawler"])

    def test_search_matches_description(self):
        rows = bookmarks.items(self.conn, "all", "结构化数据")
        self.assertEqual([r["full_name"] for r in rows], ["acme/crawler"])

    def test_search_matches_language_and_topics(self):
        self.assertEqual(len(bookmarks.items(self.conn, "all", "Rust")), 1)
        self.assertEqual(len(bookmarks.items(self.conn, "all", "scraping")), 1)

    def test_search_is_case_insensitive(self):
        self.assertEqual(len(bookmarks.items(self.conn, "all", "CRAWLER")), 1)

    def test_search_does_not_treat_wildcards_as_wildcards(self):
        """用户输入 % 时不该匹配一切。"""
        self.assertEqual(bookmarks.items(self.conn, "all", "%"), [])
        self.assertEqual(bookmarks.items(self.conn, "all", "_"), [])

    def test_search_combines_with_folder_filter(self):
        folder = bookmarks.create_folder(self.conn, "抓取")
        bookmarks.move(self.conn, "acme/crawler", folder["id"])
        self.assertEqual(len(bookmarks.items(self.conn, folder["id"], "crawl")), 1)
        self.assertEqual(bookmarks.items(self.conn, folder["id"], "render"), [])

    def test_empty_search_returns_everything(self):
        self.assertEqual(len(bookmarks.items(self.conn, "all", "   ")), 2)


class TestState(Base):
    def test_state_carries_folders_items_and_counts(self):
        folder = bookmarks.create_folder(self.conn, "工具")
        bookmarks.add(self.conn, "a/one", _item("a/one"), folder_id=folder["id"])
        state = bookmarks.state(self.conn, folder["id"], "one")
        self.assertEqual(state["folder"], folder["id"])
        self.assertEqual(state["query"], "one")
        self.assertEqual(len(state["folders"]), 1)
        self.assertEqual(state["folders"][0]["count"], 1)
        self.assertEqual(len(state["items"]), 1)
        self.assertEqual(state["counts"], {"total": 1, "unfiled": 0, "folders": 1})

    def test_state_defaults_to_all(self):
        bookmarks.add(self.conn, "a/one", _item("a/one"))
        state = bookmarks.state(self.conn)
        self.assertEqual(state["folder"], "all")
        self.assertEqual(state["query"], "")
        self.assertEqual(len(state["items"]), 1)


class TestMigration(Base):
    def test_init_adds_columns_to_a_legacy_table(self):
        """旧库只有 full_name + created_at，init 必须能加上新列。"""
        self.conn.execute("DROP TABLE bookmarks")
        self.conn.execute("CREATE TABLE bookmarks (full_name TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
        self.conn.execute("INSERT INTO bookmarks(full_name, created_at) VALUES('old/repo', '2026-01-01T00:00:00+08:00')")
        self.conn.commit()
        bookmarks.init(self.conn)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(bookmarks)")}
        for name in ("folder_id", "url", "description", "language", "stars", "topics", "avatar", "meta_at"):
            self.assertIn(name, cols)
        rows = bookmarks.items(self.conn)
        self.assertEqual(rows[0]["full_name"], "old/repo")

    def test_init_backfills_metadata_from_repos(self):
        self.conn.execute("DROP TABLE bookmarks")
        self.conn.execute("CREATE TABLE bookmarks (full_name TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
        db.save_repo(self.conn, {
            "full_name": "old/repo", "owner": "old", "name": "repo", "description": "老收藏的简介",
            "url": "https://github.com/old/repo", "language": "Go", "stars": 88, "forks": 3,
            "topics": ["legacy"], "avatar": "https://github.com/old.png", "homepage": "",
            "license": "", "created_at": "2025-01-01T00:00:00Z", "pushed_at": "", "meta_at": "",
        })
        self.conn.execute("INSERT INTO bookmarks(full_name, created_at) VALUES('old/repo', '2026-01-01T00:00:00+08:00')")
        self.conn.commit()
        bookmarks.init(self.conn)
        row = bookmarks.items(self.conn)[0]
        self.assertEqual(row["description"], "老收藏的简介")
        self.assertEqual(row["language"], "Go")
        self.assertEqual(row["stars"], 88)
        self.assertEqual(row["topics"], ["legacy"])
        self.assertEqual(row["url"], "https://github.com/old/repo")

    def test_init_is_idempotent(self):
        bookmarks.init(self.conn)
        bookmarks.init(self.conn)
        bookmarks.add(self.conn, "a/one", _item("a/one"))
        self.assertEqual(bookmarks.counts(self.conn)["total"], 1)


class TestBrokenMetadata(Base):
    def test_corrupt_topics_json_is_tolerated(self):
        bookmarks.add(self.conn, "a/one", _item("a/one"))
        self.conn.execute("UPDATE bookmarks SET topics='{not json' WHERE full_name='a/one'")
        self.conn.commit()
        self.assertEqual(bookmarks.items(self.conn)[0]["topics"], [])


class TestRoutesDeclared(unittest.TestCase):
    """接口被误删时能立刻发现。"""

    def test_app_declares_the_new_endpoints(self):
        source = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
        for route in ("/api/bookmarks", "/api/bookmarks/folder", "/api/bookmarks/move",
                      "/api/follows/search"):
            self.assertIn(route, source, "缺少接口：%s" % route)

    def test_settings_api_exposes_and_saves_the_push_switch(self):
        from core import ai_api, ai_settings

        source = (Path(__file__).resolve().parent.parent / "core" / "ai_api.py").read_text(encoding="utf-8")
        self.assertIn("push", ai_settings.DEFAULTS)
        self.assertIn('saved["push_enabled"]', source)


if __name__ == "__main__":
    unittest.main()