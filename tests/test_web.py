"""Web shell regression tests.

These guard the front-end contract: every DOM id the scripts bind to must exist in the
matching page, and the pages must stay dependency-free (no CDN, no build step).
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401,E402

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


def _read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def _ids_used_in(js_name: str) -> set:
    """Every `$("#id")` the script binds to or writes into."""
    text = _read(js_name)
    return set(re.findall(r'\$\("#([A-Za-z][\w-]*)"\)', text))


class TestPagesExist(unittest.TestCase):
    def test_all_pages_present(self):
        for name in ("index.html", "radar.html", "status.html", "styles.css", "shell.js",
                     "app.js", "radar.js", "status.js"):
            self.assertTrue((WEB / name).is_file(), "缺少前端文件：%s" % name)

    def test_removed_stylesheet_is_gone(self):
        self.assertFalse((WEB / "radar.css").exists(), "radar.css 应已合并进 styles.css")
        for name in ("index.html", "radar.html", "status.html"):
            self.assertNotIn("radar.css", _read(name))


class TestSharedShell(unittest.TestCase):
    def test_every_page_uses_the_shared_shell(self):
        for name in ("index.html", "radar.html", "status.html"):
            html = _read(name)
            self.assertIn('href="/styles.css"', html, name)
            self.assertIn('src="/shell.js"', html, name)
            self.assertIn('class="sidebar"', html, name)
            self.assertIn('class="topbar"', html, name)
            self.assertIn('id="sidebarToggle"', html, name)
            self.assertIn('id="themeToggle"', html, name)
            self.assertIn('id="navToggle"', html, name)

    def test_navigation_links_all_three_modules(self):
        html = _read("index.html")
        self.assertIn('href="/"', html)
        self.assertIn('href="/radar.html"', html)
        self.assertIn('href="/status.html"', html)

    def test_no_external_assets(self):
        """核心界面不能依赖外部 CDN。"""
        for name in ("index.html", "radar.html", "status.html"):
            html = _read(name)
            for match in re.findall(r'(?:src|href)="(https?://[^"]+)"', html):
                self.fail("%s 引用了外部资源：%s" % (name, match))

    def test_theme_bootstrap_runs_before_paint(self):
        for name in ("index.html", "radar.html", "status.html"):
            html = _read(name)
            head = html.split("</head>")[0]
            self.assertIn("data-theme", head, name)
            self.assertIn("fb.theme", head, name)


class TestScriptContracts(unittest.TestCase):
    # 这些 id 由脚本自己渲染进容器，静态 HTML 里当然不存在
    DYNAMIC_IDS = {
        "saveSettings", "setDailyTime", "setTopEvents", "setMinHeat", "setTopics",
        "setKeywords", "setExclude", "setPushEnabled", "setPushItems", "setAlertThreshold",
        "wVelocity", "wCross", "wEngage", "wVerify", "wInterest",
    }

    def test_index_has_every_id_app_js_uses(self):
        html = _read("index.html")
        missing = sorted(i for i in _ids_used_in("app.js")
                         if i not in self.DYNAMIC_IDS and f'id="{i}"' not in html)
        self.assertEqual(missing, [], "app.js 需要但 index.html 缺失的 id：%s" % missing)

    def test_radar_has_every_id_radar_js_uses(self):
        html = _read("radar.html")
        missing = sorted(i for i in _ids_used_in("radar.js")
                         if i not in self.DYNAMIC_IDS and f'id="{i}"' not in html)
        self.assertEqual(missing, [], "radar.js 需要但 radar.html 缺失的 id：%s" % missing)

    def test_dynamic_settings_ids_are_rendered_by_radar_js(self):
        js = _read("radar.js")
        for element_id in ("saveSettings", "setDailyTime", "setPushEnabled", "setTopics"):
            self.assertIn('id="%s"' % element_id, js,
                          "设置表单字段 %s 应由 radar.js 渲染" % element_id)

    def test_status_js_only_reads_known_endpoints(self):
        text = _read("status.js")
        for endpoint in re.findall(r'get\("(/api/[^"]+)"\)', text):
            self.assertTrue(endpoint.startswith("/api/"), endpoint)
        self.assertNotIn('method: "POST"', text, "状态页必须是只读的")


class TestDesignTokens(unittest.TestCase):
    def test_core_tokens_defined(self):
        css = _read("styles.css")
        for token in ("--fb-accent", "--fb-bg", "--fb-surface", "--fb-text", "--fb-border",
                      "--fb-success", "--fb-warn", "--fb-danger", "--fb-info",
                      "--fb-r-md", "--fb-shadow-1", "--fb-dur", "--fb-max-w"):
            self.assertIn(token + ":", css, "缺少设计令牌 %s" % token)

    def test_dark_theme_has_its_own_token_block(self):
        css = _read("styles.css")
        self.assertIn(':root[data-theme="dark"]', css)
        dark = css.split(':root[data-theme="dark"]', 1)[1].split("}", 1)[0]
        for token in ("--fb-bg", "--fb-surface", "--fb-text", "--fb-border", "--fb-accent"):
            self.assertIn(token + ":", dark, "深色主题缺少 %s" % token)

    def test_reduced_motion_is_respected(self):
        self.assertIn("prefers-reduced-motion", _read("styles.css"))

    def test_responsive_breakpoints_exist(self):
        css = _read("styles.css")
        for width in ("1240px", "1080px", "860px", "560px"):
            self.assertIn(width, css, "缺少断点 %s" % width)

    def test_css_is_not_duplicated_across_files(self):
        """只有一份样式表，避免旧样式与新样式互相污染。"""
        css_files = sorted(p.name for p in WEB.glob("*.css"))
        self.assertEqual(css_files, ["styles.css"])


class TestShellBehaviour(unittest.TestCase):
    def test_theme_cycle_covers_three_modes(self):
        js = _read("shell.js")
        self.assertIn('["light", "dark", "auto"]', js)
        self.assertIn("prefers-color-scheme", js)

    def test_view_toggle_is_ui_only_and_persisted(self):
        js = _read("shell.js")
        self.assertIn("fb.view", js)
        self.assertIn("is-list", js)

    def test_shell_does_not_call_business_endpoints(self):
        js = _read("shell.js")
        self.assertNotIn("/api/", js, "外壳层不应直接请求业务接口")


if __name__ == "__main__":
    unittest.main(verbosity=2)
