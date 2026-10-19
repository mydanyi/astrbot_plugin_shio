"""Black-box behaviour tests for the Shio settings web API (ticket T01).

The tests drive the real ``ShioPlugin``, a real ``astrbot.api.AstrBotConfig``
seeded from the shipped ``_conf_schema.json`` and the real ``astrbot.api.web``
request plumbing (``PluginRequest`` + ``bind_request_context``).  The AstrBot
context is reduced to the three capabilities the plugin may rely on: web-route
registration, star metadata lookup and provider enumeration.

Nothing here inspects source text, imports pre-existing fake API fixtures or
asserts on the absence of a route: a missing route simply yields 404 so the
old implementation fails on the observable response.
"""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from starlette.requests import Request

from astrbot.api import AstrBotConfig
from astrbot.api.web import PluginRequest, bind_request_context

from astrbot_plugin_shio.main import ShioPlugin


PLUGIN_NAME = "astrbot_plugin_shio"
SETTINGS_ROUTE = f"/{PLUGIN_NAME}/settings"
MODELS_ROUTE = f"/{PLUGIN_NAME}/models"
PROB_PATH = "sys001.group.natural_participation_probability"

TEXT_TYPES = frozenset({"string", "text"})
MISSING = object()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def locate_schema() -> Path:
    """Locate the shipped ``_conf_schema.json`` without asserting on its text."""
    root = Path(__file__).resolve().parents[2]
    for candidate in (root / "_conf_schema.json", Path.cwd() / "_conf_schema.json"):
        if candidate.is_file():
            return candidate
    return root / "_conf_schema.json"


def flatten(value, prefix=""):
    """Flatten nested mappings into ``{"a.b.c": value}`` form."""
    flat = {}
    if isinstance(value, Mapping) or hasattr(value, "items"):
        for key, item in value.items():
            flat.update(flatten(item, f"{prefix}.{key}" if prefix else str(key)))
    else:
        flat[prefix] = value
    return flat


def lookup(data, dotted):
    """Read a dotted path from nested or flat configuration representations."""
    current = data
    try:
        for part in dotted.split("."):
            current = current[part]
        return current
    except (KeyError, TypeError, IndexError):
        try:
            return data[dotted]
        except (KeyError, TypeError):
            return MISSING


def schema_leaves(schema):
    """Return ``(dotted_path, node)`` for every scalar field in a schema tree."""
    leaves = []

    def walk(node, prefix):
        if (
            isinstance(node, dict)
            and node.get("type") == "object"
            and isinstance(node.get("items"), dict)
        ):
            for key, child in node["items"].items():
                walk(child, f"{prefix}.{key}" if prefix else str(key))
            return
        leaves.append((prefix, node))

    for key, node in schema.items():
        walk(node, key)
    return leaves


def pick_value(current, *candidates):
    """Pick an integer candidate that differs from ``current``."""
    for candidate in candidates:
        if current != candidate:
            return candidate
    return int(current) + 1


class RouteTable:
    """Minimal stand-in for the context route registry (replace semantics)."""

    def __init__(self):
        self._routes = {}

    @staticmethod
    def _key(route, method):
        return (str(route), str(method).upper())

    def register(self, route, handler, methods=None, desc=None):
        if isinstance(methods, str):
            methods = [methods]
        for method in (methods or ["GET"]):
            self._routes[self._key(route, method)] = handler

    def handler(self, route, method="GET"):
        return self._routes.get(self._key(route, method))

    def methods_for(self, route):
        return sorted(method for (path, method) in self._routes if path == str(route))


class FakeContext:
    """Context proxy exposing only the surface the plugin is allowed to use."""

    def __init__(self):
        self.routes = RouteTable()
        self.stars = {}
        self.providers = []

    def register_web_api(self, route, handler, methods=None, desc=None):
        self.routes.register(route, handler, methods, desc)

    def get_registered_star(self, name):
        return self.stars.get(name)

    def get_all_providers(self):
        return list(self.providers)

    def set_star(self, plugin, config, activated=True):
        metadata = SimpleNamespace(
            name=PLUGIN_NAME,
            activated=activated,
            config=config,
            star_cls=plugin,
        )
        self.stars[PLUGIN_NAME] = metadata
        return metadata


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
class SettingsPageApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp_path = Path(self._tmpdir.name)

        self.schema = json.loads(locate_schema().read_text(encoding="utf-8"))
        self.config_path = self.tmp_path / "astrbot_plugin_shio_config.json"
        self.context = FakeContext()
        self.config = self._new_config()
        self.plugin = await self._boot(self.config)

    # -- plumbing ---------------------------------------------------------- #
    def _new_config(self):
        return AstrBotConfig(config_path=str(self.config_path), schema=self.schema)

    async def _boot(self, config):
        plugin = ShioPlugin(self.context, config)
        self.context.set_star(plugin, config)
        return plugin

    def _request(self, method, path, payload=None):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "root_path": "",
            "query_string": b"",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
            "client": ("127.0.0.1", 4711),
            "server": ("testserver", 80),
        }
        delivered = False

        async def receive():
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        return Request(scope, receive)

    async def _invoke(self, handler, method, path, payload=None):
        if handler is None:
            return 404, {"detail": "no handler registered"}

        plugin_request = PluginRequest(
            self._request(method, path, payload),
            plugin_name=PLUGIN_NAME,
            username="test-admin",
        )
        positional = [
            parameter
            for parameter in inspect.signature(handler).parameters.values()
            if parameter.kind
            in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        ]
        args = () if not positional else (plugin_request,)

        with bind_request_context(plugin_request):
            response = handler(*args)
            if inspect.isawaitable(response):
                response = await response

        status = getattr(response, "status_code", None)
        raw = getattr(response, "body", b"") or b""
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {"raw": raw.decode("utf-8", "replace")}
        return status, data

    async def _call(self, method, path, payload=None):
        handler = self.context.routes.handler(path, method)
        return await self._invoke(handler, method, path, payload)

    async def _get_settings(self):
        return await self._call("GET", SETTINGS_ROUTE)

    async def _post_settings(self, payload):
        return await self._call("POST", SETTINGS_ROUTE, payload)

    @staticmethod
    def _schema_of(body):
        schema = body.get("schema", {})
        if isinstance(schema, str):
            schema = json.loads(schema)
        return schema

    def _stored_json(self):
        return json.loads(self.config_path.read_text(encoding="utf-8-sig"))

    # -- 1 ---------------------------------------------------------------- #
    async def test_get_settings_returns_config_schema_and_revision(self):
        status, body = await self._get_settings()
        self.assertEqual(status, 200, f"GET settings is not served: {body!r}")
        for key in ("config", "schema", "revision"):
            self.assertIn(key, body, f"GET settings payload lacks {key!r}: {body!r}")
        self.assertIsInstance(body["config"], dict)
        self.assertIsInstance(body["revision"], str)
        self.assertTrue(body["revision"], "revision must be a non-empty string")

    # -- 2 ---------------------------------------------------------------- #
    async def test_schema_exposes_every_configurable_leaf(self):
        status, body = await self._get_settings()
        self.assertEqual(status, 200, f"GET settings is not served: {body!r}")
        schema = self._schema_of(body)
        self.assertIsInstance(schema, dict)
        self.assertIn("sys001", schema)

        leaves = schema_leaves(schema)
        paths = [path for path, _ in leaves]
        self.assertEqual(len(paths), len(set(paths)), "schema exposes duplicate leaf paths")
        self.assertIn(PROB_PATH, paths, "natural participation field is not reachable")
        self.assertEqual(
            len(leaves),
            70,
            f"schema should expose 70 configurable leaves, got {len(leaves)}",
        )

    # -- 3 ---------------------------------------------------------------- #
    async def test_get_models_lists_only_id_and_model(self):
        self.context.providers = [
            SimpleNamespace(
                meta=lambda: SimpleNamespace(id="existing/chat"),
                get_model=lambda: "chat-v1",
                provider_config={"api_key": "secret-value"},
            )
        ]
        status, body = await self._call("GET", MODELS_ROUTE)
        self.assertEqual(status, 200, f"GET models is not served: {body!r}")
        providers = body.get("providers")
        self.assertIsInstance(providers, list, f"providers must be a list: {body!r}")
        self.assertEqual(len(providers), 1)
        entry = providers[0]
        self.assertEqual(set(entry), {"id", "model"}, f"unexpected provider keys: {entry!r}")
        self.assertEqual(entry["id"], "existing/chat")
        self.assertEqual(entry["model"], "chat-v1")

        rendered = json.dumps(body)
        self.assertNotIn("secret-value", rendered, "provider credentials leaked")
        self.assertNotIn("api_key", rendered, "provider credentials leaked")

    # -- 4 ---------------------------------------------------------------- #
    async def test_post_settings_persists_a_single_field_change(self):
        status, before = await self._get_settings()
        self.assertEqual(status, 200, f"GET settings is not served: {before!r}")
        baseline = flatten(before["config"])
        self.assertIn(PROB_PATH, baseline, "natural participation field missing from config")
        old_revision = before["revision"]
        new_value = pick_value(baseline[PROB_PATH], 35, 7, 91)

        status, after = await self._post_settings(
            {"revision": old_revision, "changes": {PROB_PATH: new_value}}
        )
        self.assertEqual(status, 200, f"valid save rejected: {after!r}")
        self.assertEqual(lookup(after["config"], PROB_PATH), new_value)
        self.assertNotEqual(after["revision"], old_revision, "revision did not advance")

        updated = flatten(after["config"])
        changed = {
            key for key in set(baseline) | set(updated) if baseline.get(key) != updated.get(key)
        }
        self.assertEqual(changed, {PROB_PATH}, "a single change must touch a single field")

        reloaded = self._new_config()
        self.assertEqual(lookup(reloaded, PROB_PATH), new_value, "change was not persisted")

    # -- 5 ---------------------------------------------------------------- #
    async def test_post_settings_rejects_unknown_field_without_writing(self):
        status, before = await self._get_settings()
        self.assertEqual(status, 200, f"GET settings is not served: {before!r}")
        baseline = flatten(before["config"])

        status, body = await self._post_settings(
            {"revision": before["revision"], "changes": {"sys001.not_a_real_field": 1}}
        )
        self.assertEqual(status, 400, f"unknown field accepted: {body!r}")

        status, after = await self._get_settings()
        self.assertEqual(status, 200)
        self.assertEqual(flatten(after["config"]), baseline, "rejected save mutated the config")
        self.assertNotIn("not_a_real_field", self.config_path.read_text(encoding="utf-8"))

    # -- 6 ---------------------------------------------------------------- #
    async def test_post_settings_rejects_wrong_value_type_without_writing(self):
        status, before = await self._get_settings()
        self.assertEqual(status, 200, f"GET settings is not served: {before!r}")
        baseline = flatten(before["config"])

        status, body = await self._post_settings(
            {"revision": before["revision"], "changes": {PROB_PATH: "not-a-number"}}
        )
        self.assertEqual(status, 400, f"invalid value type accepted: {body!r}")

        status, after = await self._get_settings()
        self.assertEqual(status, 200)
        self.assertEqual(flatten(after["config"]), baseline, "rejected save mutated the config")

    # -- 7 ---------------------------------------------------------------- #
    async def test_post_settings_conflict_keeps_the_newer_revision(self):
        status, before = await self._get_settings()
        self.assertEqual(status, 200, f"GET settings is not served: {before!r}")
        stale_revision = before["revision"]
        current = flatten(before["config"])[PROB_PATH]
        first = pick_value(current, 7, 19, 61)
        second = first + 3

        status, body = await self._post_settings(
            {"revision": stale_revision, "changes": {PROB_PATH: first}}
        )
        self.assertEqual(status, 200, f"initial save rejected: {body!r}")
        fresh_revision = body["revision"]
        self.assertNotEqual(fresh_revision, stale_revision)

        status, body = await self._post_settings(
            {"revision": stale_revision, "changes": {PROB_PATH: second}}
        )
        self.assertEqual(status, 409, f"stale revision was accepted: {body!r}")

        status, after = await self._get_settings()
        self.assertEqual(status, 200)
        self.assertEqual(lookup(after["config"], PROB_PATH), first, "conflicting write was applied")
        self.assertEqual(after["revision"], fresh_revision, "conflict must not bump the revision")

    # -- 8 ---------------------------------------------------------------- #
    async def test_storage_failure_is_not_reported_as_success(self):
        status, before = await self._get_settings()
        self.assertEqual(status, 200, f"GET settings is not served: {before!r}")
        baseline = flatten(before["config"])
        new_value = pick_value(baseline[PROB_PATH], 11, 13, 97)

        def failing_save(config, replace_config=None, **kwargs):
            if replace_config:
                config.update(replace_config)
            raise OSError("simulated disk failure")

        patcher = mock.patch.object(
            AstrBotConfig, "save_config", new=failing_save
        )

        with patcher:
            try:
                status, body = await self._post_settings(
                    {"revision": before["revision"], "changes": {PROB_PATH: new_value}}
                )
            except Exception as exc:  # the request may surface the failure directly
                status, body = None, {"error": repr(exc)}

        self.assertFalse(
            status is not None and 200 <= int(status) < 300,
            f"failed persistence was reported as success: {status} {body!r}",
        )

        status, after = await self._get_settings()
        self.assertEqual(status, 200)
        self.assertEqual(
            flatten(after["config"]),
            baseline,
            "in-memory config was not restored after the storage failure",
        )

    # -- 9 ---------------------------------------------------------------- #
    async def test_disabled_plugin_rejects_reads_and_writes(self):
        status, before = await self._get_settings()
        self.assertEqual(status, 200, f"GET settings is not served: {before!r}")
        baseline = flatten(before["config"])
        new_value = pick_value(baseline[PROB_PATH], 21, 23, 89)
        stale_handler = self.context.routes.handler(SETTINGS_ROUTE, "POST")
        self.assertIsNotNone(stale_handler, "POST settings route was never registered")

        self.context.stars[PLUGIN_NAME].activated = False

        status, _ = await self._call("GET", SETTINGS_ROUTE)
        self.assertIn(status, (403, 404), f"disabled plugin served settings: {status}")

        status, body = await self._invoke(
            stale_handler,
            "POST",
            SETTINGS_ROUTE,
            {"revision": before["revision"], "changes": {PROB_PATH: new_value}},
        )
        self.assertIn(status, (403, 404), f"disabled plugin served a write: {status} {body!r}")
        self.assertNotEqual(
            lookup(self._stored_json(), PROB_PATH),
            new_value,
            "disabled plugin still persisted a change",
        )

    # -- 10 --------------------------------------------------------------- #
    async def test_reload_replaces_route_and_stale_handler_never_uses_old_config(self):
        status, before = await self._get_settings()
        self.assertEqual(status, 200, f"GET settings is not served: {before!r}")
        stale_get = self.context.routes.handler(SETTINGS_ROUTE, "GET")
        self.assertIsNotNone(stale_get, "GET settings route was never registered")

        reloaded_config = self._new_config()
        reloaded_plugin = await self._boot(reloaded_config)
        self.assertIsNot(reloaded_plugin, self.plugin)
        self.assertEqual(
            self.context.routes.methods_for(SETTINGS_ROUTE),
            ["GET", "POST"],
            "reload must replace the route instead of proliferating handlers",
        )

        current = flatten(before["config"])[PROB_PATH]
        new_value = pick_value(current, 31, 37, 83)
        status, saved = await self._post_settings(
            {"revision": before["revision"], "changes": {PROB_PATH: new_value}}
        )
        self.assertEqual(status, 200, f"reloaded plugin cannot save: {saved!r}")

        status, body = await self._invoke(stale_get, "GET", SETTINGS_ROUTE)
        self.assertIn(status, (200, 403, 404), f"stale handler returned {status}: {body!r}")
        if status == 200:
            self.assertEqual(
                lookup(body["config"], PROB_PATH),
                new_value,
                "stale handler served the outdated config object",
            )

    # -- 11 --------------------------------------------------------------- #
    async def test_unknown_configuration_survives_known_field_update(self):
        self.config["__legacy_marker__"] = {"keep": "intact"}
        self.config.save_config()

        status, before = await self._get_settings()
        self.assertEqual(status, 200, f"GET settings is not served: {before!r}")
        baseline = flatten(before["config"])
        self.assertIn(PROB_PATH, baseline)
        new_value = pick_value(baseline[PROB_PATH], 41, 43, 79)

        status, body = await self._post_settings(
            {"revision": before["revision"], "changes": {PROB_PATH: new_value}}
        )
        self.assertEqual(status, 200, f"save rejected: {body!r}")

        stored = self._stored_json()
        self.assertEqual(
            stored.get("__legacy_marker__"),
            {"keep": "intact"},
            "unknown/legacy configuration was dropped while saving known fields",
        )
        self.assertEqual(lookup(stored, PROB_PATH), new_value, "known field was not persisted")

    # -- 12 --------------------------------------------------------------- #
    async def test_custom_rule_text_and_empty_allow_list_round_trip(self):
        status, before = await self._get_settings()
        self.assertEqual(status, 200, f"GET settings is not served: {before!r}")
        leaves = schema_leaves(self._schema_of(before))

        text_paths = [
            path
            for path, node in leaves
            if isinstance(node, dict)
            and node.get("type") in TEXT_TYPES
            and not node.get("options")
        ]
        list_paths = [
            path
            for path, node in leaves
            if isinstance(node, dict) and node.get("type") == "list"
        ]
        self.assertTrue(text_paths, "schema exposes no free-form text field")
        self.assertTrue(list_paths, "schema exposes no list field")

        text_path = next(
            (path for path in text_paths if "rule" in path.lower() or "prompt" in path.lower()),
            text_paths[0],
        )
        list_path = next(
            (path for path in list_paths if "allow" in path.lower() or "white" in path.lower()),
            list_paths[0],
        )

        custom_text = "自定义规则 -- keep me 42"
        baseline = flatten(before["config"])
        self.assertNotEqual(baseline.get(text_path), custom_text)

        status, body = await self._post_settings(
            {
                "revision": before["revision"],
                "changes": {text_path: custom_text, list_path: []},
            }
        )
        self.assertEqual(status, 200, f"custom rule text / empty list rejected: {body!r}")
        self.assertEqual(lookup(body["config"], text_path), custom_text)
        self.assertEqual(lookup(body["config"], list_path), [])

        reloaded = self._new_config()
        self.assertEqual(
            lookup(reloaded, text_path),
            custom_text,
            "custom rule text was replaced by a schema default",
        )
        self.assertEqual(lookup(reloaded, list_path), [], "empty allow list is not legal")


if __name__ == "__main__":
    unittest.main()
