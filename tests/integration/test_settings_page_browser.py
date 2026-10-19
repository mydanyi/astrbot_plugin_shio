"""Browser-behaviour tests for pages/settings (UI contract only).

The page is exercised in Chromium against a test-only bridge boundary.
No product code is imported; the page talks to an injected test-only bridge.
"""
import json
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVED = ROOT / "pages" / "settings"
SCHEMA_FILE = ROOT / "_conf_schema.json"
PROB_PATH = "sys001.group.natural_participation_probability"
PROVIDER_PATH = "sys001.group.natural_decision_provider_id"

try:
    from playwright.sync_api import expect, sync_playwright

    HAVE_PLAYWRIGHT = True
except ImportError:  # pragma: no cover - environment without playwright
    expect = sync_playwright = None
    HAVE_PLAYWRIGHT = False


BRIDGE_JS = """
(() => {
  const f = (window.__uiFixture = __FIXTURE__);
  window.AstrBotPluginPage = {
    ready: async () => ({ pluginName: 'astrbot_plugin_shio', isDark: false }),
    getContext: () => ({ isDark: false }),
    onContext: () => () => {},
    apiGet: async (endpoint) => {
      if (endpoint === 'settings') {
        if (f.failLoad) throw new Error('load failed');
        return { schema: f.schema, config: structuredClone(f.config), revision: f.revision, version: '0.5.20' };
      }
      if (endpoint === 'models') return { providers: f.providers };
      throw new Error('unexpected endpoint');
    },
    apiPost: async (endpoint, payload) => {
      if (endpoint !== 'settings') throw new Error('unexpected endpoint');
      if (f.failSave) throw new Error('save failed');
      f.saves.push(payload);
      for (const [path, value] of Object.entries(payload.changes)) {
        const ps = path.split('.');
        let n = f.config;
        for (const k of ps.slice(0, -1)) n = n[k];
        n[ps.at(-1)] = value;
      }
      f.revision = 'r2';
      return { config: structuredClone(f.config), revision: f.revision };
    },
  };
})();
"""


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def _expand(node):
    """Turn one schema field into its default value (test fixture only)."""
    if not isinstance(node, dict):
        return {}
    if node.get("type") == "object" or ("type" not in node and isinstance(node.get("items"), dict)):
        return {key: _expand(sub) for key, sub in node.get("items", {}).items()}
    if "default" in node:
        return json.loads(json.dumps(node["default"]))
    return {}


def build_defaults(schema):
    return {key: _expand(field) for key, field in schema.items()}


def _set(config, dotted, value):
    node = config
    parts = dotted.split(".")
    for key in parts[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = node[key] = {}
        node = nxt
    node[parts[-1]] = value


def make_config(schema):
    config = build_defaults(schema)
    _set(config, "sys001.group.natural_participation_enabled", True)
    _set(config, "sys001.group.natural_group_scopes", ["12345"])
    _set(config, PROB_PATH, 37)
    _set(config, PROVIDER_PATH, "existing/chat")
    _set(config, "sys001.final_review.mode", "combined")
    _set(config, "sys001.final_review.core_prompt", "长规则\n" + ("独立长句。" * 140) + "规则结束标志")
    _set(config, "unknown", {"keep": "unchanged"})
    return config


def field(page, path):
    return page.locator('input[data-path="%s"], select[data-path="%s"]' % (path, path)).first


def provider_select(page):
    return page.locator("#sel-" + PROVIDER_PATH.replace(".", "__"))


@unittest.skipUnless(HAVE_PLAYWRIGHT, "playwright is not installed")
class SettingsPageBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
        cls.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(_QuietHandler, directory=str(SERVED))
        )
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:%d/index.html" % cls.httpd.server_address[1]
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True, args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close()
        finally:
            cls.pw.stop()
            cls.httpd.shutdown()
            cls.httpd.server_close()
            cls.thread.join(timeout=5)

    def setUp(self):
        self.context = None

    def tearDown(self):
        if self.context is not None:
            self.context.close()

    def open(self, config=None, *, fail_load=False, fail_save=False, viewport=(1440, 1000)):
        if self.context is not None:
            self.context.close()
        self.context = self.browser.new_context(
            viewport={"width": viewport[0], "height": viewport[1]}
        )
        page = self.context.new_page()
        fixture = {
            "config": config if config is not None else make_config(self.schema),
            "schema": self.schema,
            "revision": "r1",
            "failLoad": fail_load,
            "failSave": fail_save,
            "saves": [],
            "providers": [
                {"id": "existing/chat", "model": "chat-v1"},
                {"id": "backup/chat", "model": "chat-v2"},
            ],
        }
        page.add_init_script(BRIDGE_JS.replace("__FIXTURE__", json.dumps(fixture, ensure_ascii=False)))
        page.goto(self.url)
        page.locator("#pageRoot").wait_for()
        return page

    def saves(self, page):
        return page.evaluate("() => window.__uiFixture.saves")

    def test_defaults_providers_and_navigation(self):
        page = self.open()
        self.assertEqual(field(page, PROB_PATH).input_value(), "37")
        provider = provider_select(page)
        values = provider.locator("option").evaluate_all("els => els.map(e => e.value)")
        self.assertIn("existing/chat", values)
        self.assertIn("backup/chat", values)
        self.assertEqual(provider.input_value(), "existing/chat")

        navs = page.locator("#navA button[data-page]")
        self.assertEqual(navs.count(), 7)
        for index in range(7):
            navs.nth(index).click()
            heading = page.locator("#pageRoot").get_by_role("heading").first
            self.assertTrue(heading.is_visible())
            self.assertTrue(heading.inner_text().strip())

        config = make_config(self.schema)
        _set(config, PROVIDER_PATH, "unmatched/id")
        page = self.open(config)
        provider = provider_select(page)
        self.assertEqual(provider.input_value(), "unmatched/id")
        self.assertEqual(provider.locator('option[value="unmatched/id"]').count(), 1)

    def test_save_sends_only_changed_path(self):
        page = self.open()
        field(page, PROB_PATH).fill("38")
        page.locator("#saveBtn").click()
        expect(page.locator("#dirtyText")).to_contain_text("已保存")
        expect(page.locator("#saveBtn")).to_be_disabled()
        saves = self.saves(page)
        self.assertEqual(len(saves), 1)
        self.assertEqual(saves[0]["changes"], {PROB_PATH: 38})
        self.assertEqual(
            page.evaluate("() => window.__uiFixture.config.sys001.group.natural_participation_probability"),
            38,
        )
        self.assertEqual(page.evaluate("() => window.__uiFixture.config.unknown"), {"keep": "unchanged"})

    def test_save_failure_then_retry(self):
        page = self.open(fail_save=True)
        field(page, PROB_PATH).fill("38")
        save = page.locator("#saveBtn")
        save.click()
        expect(page.locator("#toastMain")).to_contain_text("失败")
        self.assertTrue(save.is_enabled())
        self.assertEqual(field(page, PROB_PATH).input_value(), "38")
        self.assertEqual(self.saves(page), [])
        page.evaluate("() => { window.__uiFixture.failSave = false; }")
        save.click()
        expect(page.locator("#dirtyText")).to_contain_text("已保存")
        self.assertEqual([item["changes"] for item in self.saves(page)], [{PROB_PATH: 38}])

    def test_rule_dialog_preview_themes_and_narrow_viewport(self):
        page = self.open()
        page.locator('#navA button[data-page="review"]').click()
        edit = page.locator('button[data-path="sys001.final_review.core_prompt"]').filter(has_text="查看").first
        edit.click()
        original = page.locator("#ruleText").input_value()
        self.assertTrue(original.endswith("规则结束标志"))
        page.locator("#ruleText").fill(original + "取消标记")
        page.locator("#ruleCancel").click()
        edit.click()
        self.assertTrue(page.locator("#ruleText").input_value().endswith("规则结束标志"))
        page.locator("#ruleText").fill(original + "追加结束标志")
        page.locator("#ruleApply").click()
        preview = page.locator("#field-sys001__final_review__core_prompt .preview").first
        expect(preview).to_contain_text("追加结束标志")
        clamped = "el => el.scrollHeight - el.clientHeight"
        self.assertLessEqual(preview.evaluate(clamped), 1)
        page.locator("#saveBtn").click()
        expect(page.locator("#saveBtn")).to_be_disabled()
        style = page.locator("#visualStyleSelect")
        for theme in ("native", "mist", "midnight", "cream"):
            style.select_option(theme)
            expect(page.locator("html")).to_have_attribute("data-visual-style", theme)
        page.set_viewport_size({"width": 520, "height": 900})
        self.assertLessEqual(
            page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth"), 1
        )
        self.assertLessEqual(page.locator("#field-sys001__final_review__core_prompt .preview").first.evaluate(clamped), 1)

    def test_load_failure_shows_retry_and_recovers(self):
        page = self.open(fail_load=True)
        self.assertEqual(page.locator("input[data-path], select[data-path]").count(), 0)
        error = page.locator("#loadError")
        expect(error).to_be_visible()
        self.assertEqual(error.get_attribute("role"), "alert")
        self.assertTrue(page.locator("#saveBtn").is_disabled())
        page.evaluate("() => { window.__uiFixture.failLoad = false; }")
        page.locator("#retryLoad").click()
        expect(field(page, PROB_PATH)).to_have_value("37")
