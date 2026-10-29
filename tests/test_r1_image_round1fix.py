"""Round-1 follow-up contracts: GPT Image capabilities and the live image budget.

Two adversarial findings are pinned here:

* P1-3: 7.0.7 saved ``quality=standard`` on every endpoint (its unconditional
  default).  The official GPT Image protocol rejects that value and the whole
  ``response_format`` field, while a deliberate dall-e ``standard`` must still
  reach the wire.  The contract is decided by backend/model capability, not by
  rewriting every saved value.
* P2-5: a saved endpoint timeout must reach the next tool task without
  invalidating the frozen catalog identity of a running/queued invocation.
  The image manifest therefore declares the documented hard ceiling and the
  provider snapshots the configured budget when a task starts.

Offline only: ``aiohttp`` is replaced by recording stand-ins and configuration
tests use temporary files.  No real network or model call is made.
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest import mock

from astrbot_host.native_extensions import NativeExtensionRegistry
from astrbot_host.native_extensions.builtins import BundledNativeLease
from astrbot_host.native_extensions.image_native import adapter as image_adapter
from astrbot_host.native_extensions.image_native.adapter import PLUGIN_ID, PROVIDER_ID, SCHEMA, NativeImageProvider
from astrbot_host.native_extensions.image_native.extension import ImageConfig, ImageExtensionAPI
from astrbot_host.native_extensions.image_native.output_configuration import (
    MAX_TASK_TIMEOUT_SECONDS, save_configuration, settings as image_settings)
from astrbot_host.native_extensions.image_native.types import fingerprint
from companion_v2.capabilities.models import FurnitureDescriptor
from companion_v2.execution.models import encode
from companion_v2.product import PRODUCT_VERSION
from tests.test_r1_image_protocol import HTTPBackedTest, b64_reply, endpoint, reference


def saved_endpoint(**overrides):
    """One endpoint document shaped exactly like a 7.0.7 save."""
    value = {"id": "primary", "name": "Primary", "enabled": True, "platform": "openai",
             "model": "gpt-image-1", "width": 1024, "height": 1024,
             "base_url": "https://api.example.test/v1", "api_key": "test-key",
             "timeout_seconds": 300, "quality": "standard",
             "max_reference_images": 1, "size_label": "1024x1024"}
    value.update(overrides)
    return value


def saved_document(*endpoints, routes=None):
    """The 7.0.7 on-disk shape (a saved ``standard`` default, no response format)."""
    endpoints = list(endpoints) or [saved_endpoint()]
    routes = routes or {"generate": ["online:" + endpoints[0]["id"]], "edit": []}
    return {"native_v2": {"enabled": True, "backend": "auto", "output": {
        "enabled": True, "backend": "online_api", "sd_webui": {}, "comfyui": {},
        "endpoints": endpoints, "operation_routes": routes,
    }}}


class LegacyConfigurationFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def open_api(self, document, name="image-config.json"):
        path = self.root / name
        path.write_text(json.dumps(document), encoding="utf-8")
        api = ImageExtensionAPI(data_root=self.root / (name + "-data"), config_path=path,
                                version=PRODUCT_VERSION)
        self.addCleanup(api.close)
        return api, path

    @staticmethod
    def raw_endpoints(path):
        return json.loads(path.read_text(encoding="utf-8"))["native_v2"]["output"]["endpoints"]


class GptImageWireContractTests(HTTPBackedTest):
    """The request body is decided by the model's protocol, not the saved value."""

    async def test_upgraded_707_standard_is_not_sent_to_gpt_image(self):
        self.http.reply("POST", lambda url, kwargs: b64_reply())

        await self.execute(endpoint(model="gpt-image-1", quality="standard"))

        body = self.request_json()
        self.assertNotIn("quality", body)
        self.assertNotIn("response_format", body)

    async def test_chatgpt_image_ignores_an_explicit_legacy_opt_in(self):
        self.http.reply("POST", lambda url, kwargs: b64_reply())

        await self.execute(endpoint(model="chatgpt-image-latest", quality="hd",
                                    response_format="b64_json"))

        body = self.request_json()
        self.assertNotIn("quality", body)
        self.assertNotIn("response_format", body)

    async def test_gpt_image_keeps_the_supported_quality_values(self):
        for quality in ("auto", "low", "medium", "high"):
            with self.subTest(quality=quality):
                self.http.reply("POST", lambda url, kwargs: b64_reply())
                self.http.calls.clear()
                await self.execute(endpoint(model="gpt-image-1", quality=quality))
                self.assertEqual(self.request_json()["quality"], quality)

    async def test_provider_prefixed_gpt_image_model_keeps_the_same_contract(self):
        for model in ("openai/gpt-image-1", "azure:gpt-image-1", "gpt-image-1:free"):
            with self.subTest(model=model):
                self.http.reply("POST", lambda url, kwargs: b64_reply())
                self.http.calls.clear()
                await self.execute(endpoint(model=model, quality="standard"))
                self.assertNotIn("quality", self.request_json())

    async def test_dall_e_keeps_a_deliberate_standard_and_b64_json(self):
        self.http.reply("POST", lambda url, kwargs: b64_reply())

        await self.execute(endpoint(model="dall-e-3", quality="standard", response_format="b64_json"))

        body = self.request_json()
        self.assertEqual(body["quality"], "standard")
        self.assertEqual(body["response_format"], "b64_json")

    async def test_unknown_compatible_model_keeps_its_explicit_legacy_choices(self):
        # This release cannot prove an arbitrary compatible model's vocabulary,
        # so it must not silently rewrite that model's saved choices.
        self.http.reply("POST", lambda url, kwargs: b64_reply())

        await self.execute(endpoint(model="image-v1", quality="standard", response_format="b64_json"))

        body = self.request_json()
        self.assertEqual(body["quality"], "standard")
        self.assertEqual(body["response_format"], "b64_json")

    async def test_gpt_image_edit_multipart_omits_the_legacy_fields(self):
        self.http.reply("POST", lambda url, kwargs: b64_reply())

        await self.execute(endpoint(model="gpt-image-1", quality="standard", response_format="b64_json"),
                           operation="edit", references=[reference()])

        self.assertEqual(self.posted_form().field_names(), ["model", "prompt", "n", "size", "image"])

    async def test_dall_e_edit_multipart_keeps_the_explicit_opt_in(self):
        self.http.reply("POST", lambda url, kwargs: b64_reply())

        await self.execute(endpoint(model="dall-e-3", quality="standard", response_format="b64_json"),
                           operation="edit", references=[reference()])

        self.assertEqual(self.posted_form().field_names(),
                         ["model", "prompt", "n", "size", "response_format", "quality", "image"])


class GptImageProjectionTests(LegacyConfigurationFixture):
    """The dashboard sees the effective value; the file is not rewritten on read."""

    def test_707_standard_is_projected_as_the_server_default_without_rewriting_the_file(self):
        api, path = self.open_api(saved_document())

        projected = api.output_configuration()["settings"]["endpoints"][0]

        self.assertEqual(projected["quality"], "")
        self.assertEqual(projected["response_format"], "")
        self.assertTrue(projected["legacy_protocol_defaults"],
                        "the page needs a read-only migration hint for the old default")
        self.assertEqual(self.raw_endpoints(path)[0]["quality"], "standard",
                         "reading a saved configuration must not rewrite the file")
        self.assertNotIn("legacy_protocol_defaults", self.raw_endpoints(path)[0])

    def test_saving_the_projection_migrates_only_the_gpt_image_endpoint(self):
        gpt = saved_endpoint(id="gpt", name="GPT")
        dalle = saved_endpoint(id="dalle", name="DALL-E", model="dall-e-3",
                               quality="standard", response_format="b64_json")
        api, path = self.open_api(saved_document(gpt, dalle, routes={
            "generate": ["online:gpt", "online:dalle"], "edit": []}))

        current = api.output_configuration()
        saved = api.save_output_configuration(current["revision"], current["settings"])

        raw = {item["id"]: item for item in self.raw_endpoints(path)}
        self.assertEqual(raw["gpt"]["quality"], "")
        self.assertEqual(raw["gpt"]["response_format"], "")
        self.assertNotIn("legacy_protocol_defaults", raw["gpt"],
                         "the read-only projection flag must never be persisted")
        self.assertEqual(raw["dalle"]["quality"], "standard")
        self.assertEqual(raw["dalle"]["response_format"], "b64_json")
        projected = {item["id"]: item for item in saved["settings"]["endpoints"]}
        self.assertEqual(projected["dalle"]["quality"], "standard")
        self.assertFalse(projected["gpt"]["legacy_protocol_defaults"])

    def test_explicit_dall_e_quality_survives_projection_and_save(self):
        api, path = self.open_api(saved_document(saved_endpoint(model="dall-e-3")))

        projected = api.output_configuration()["settings"]["endpoints"][0]
        self.assertEqual(projected["quality"], "standard")

        current = api.output_configuration()
        api.save_output_configuration(current["revision"], current["settings"])

        self.assertEqual(self.raw_endpoints(path)[0]["quality"], "standard")

    def test_projection_keeps_an_unknown_compatible_model_unchanged(self):
        api, _path = self.open_api(saved_document(saved_endpoint(model="image-v1")))

        projected = api.output_configuration()["settings"]["endpoints"][0]

        self.assertEqual(projected["quality"], "standard")
        self.assertEqual(projected["response_format"], "")
        self.assertFalse(projected["legacy_protocol_defaults"])

    def test_projection_recognizes_a_provider_prefixed_gpt_image_model(self):
        api, _path = self.open_api(saved_document(saved_endpoint(model="azure:gpt-image-1")))

        projected = api.output_configuration()["settings"]["endpoints"][0]

        self.assertEqual(projected["quality"], "")
        self.assertTrue(projected["legacy_protocol_defaults"])

    def test_projection_flag_is_off_when_nothing_legacy_was_saved(self):
        missing_quality = saved_endpoint(quality="", response_format="")
        missing_quality.pop("quality")
        api, _path = self.open_api(saved_document(missing_quality))

        projected = api.output_configuration()["settings"]["endpoints"][0]

        self.assertEqual(projected["quality"], "")
        self.assertFalse(projected["legacy_protocol_defaults"])

    def test_a_new_gpt_image_endpoint_from_a_page_draft_stays_on_server_defaults(self):
        api, _path = self.open_api({"native_v2": {"enabled": True, "backend": "auto", "output": {
            "enabled": False, "backend": "auto", "sd_webui": {}, "comfyui": {}, "endpoints": [],
        }}})
        draft = {key: value for key, value in saved_endpoint().items()
                 if key not in {"quality", "response_format"}}

        current = api.output_configuration()
        saved = api.save_output_configuration(current["revision"], {
            "enabled": False, "backend": "online_api", "endpoints": [draft],
        })

        self.assertEqual(saved["settings"]["endpoints"][0]["quality"], "")
        self.assertEqual(saved["settings"]["endpoints"][0]["response_format"], "")


class ImageTaskBudgetTests(unittest.IsolatedAsyncioTestCase):
    """P2-5: stable catalog identity, live per-task budget.

    The declaration timeout is the documented ceiling so a save can never make
    an already-registered tool shorter than an allowed endpoint.  The effective
    budget comes from the saved configuration and is snapshotted when each task
    starts, so a save changes the next task but never a running one.
    """

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.api = ImageExtensionAPI(data_root=self.root / "image", config_path=self.root / "config.json",
                                     version=PRODUCT_VERSION)
        self.addCleanup(self.api.close)
        lease = BundledNativeLease(PLUGIN_ID, self.api.companion_v2_native(), api=self.api,
                                   furniture=FurnitureDescriptor("image", "image", "画面工作室", "image"))
        self.registry = NativeExtensionRegistry(
            SimpleNamespace(get_all_stars=lambda: ()), bundled_extensions=(lease,))
        self.provider = self.api.companion_v2_native()

    def save_timeout(self, timeout):
        current = self.api.output_configuration()
        endpoints = [dict(item) for item in current["settings"]["endpoints"]]
        if not endpoints:
            endpoints = [dict(saved_endpoint(), quality="", response_format="", timeout_seconds=timeout)]
        for item in endpoints:
            item["timeout_seconds"] = timeout
        return self.api.save_output_configuration(current["revision"], {
            "enabled": True, "backend": "online_api", "endpoints": endpoints,
        })

    def declarations(self):
        return {item.host_tool_id: item for item in self.registry.declarations()}

    def test_declared_timeout_is_a_stable_ceiling_that_covers_any_saved_budget(self):
        before = self.declarations()

        self.save_timeout(900)

        after = self.declarations()
        self.assertEqual(set(before), set(after))
        for _host_tool_id, declaration in after.items():
            self.assertEqual(declaration.descriptor.definition().timeout_seconds,
                             MAX_TASK_TIMEOUT_SECONDS,
                             "the catalog must not freeze a stale configured timeout")
            self.assertGreaterEqual(declaration.descriptor.definition().timeout_seconds,
                                    self.provider.task_timeout_seconds())
        self.assertEqual(self.provider.task_timeout_seconds(), 900)
        self.assertEqual(self.provider.wait_budget_seconds, 900)

    def test_saving_does_not_change_the_declaration_identity_of_a_running_task(self):
        before = self.declarations()

        self.save_timeout(1200)

        after = self.declarations()
        for host_tool_id, declaration in before.items():
            # Encoded descriptors are persisted with each invocation and compared
            # on replay/adoption; a save must not invalidate them.
            self.assertEqual(encode(declaration.descriptor), encode(after[host_tool_id].descriptor))
            self.assertIs(declaration, after[host_tool_id])

    def test_manifest_timeouts_are_the_documented_ceiling_before_and_after_save(self):
        before = self.provider.manifest()["tools"]

        self.save_timeout(300)

        after = self.provider.manifest()["tools"]
        for tool in (*before, *after):
            self.assertEqual(tool["timeout_seconds"], MAX_TASK_TIMEOUT_SECONDS)

    async def test_next_task_uses_the_saved_budget_and_a_running_task_keeps_its_snapshot(self):
        provider = stub_provider(timeout=600)
        self.addAsyncCleanup(provider.close)
        plugin = BudgetPlugin(self.root / "budget" / "config.json", provider,
                              saved_endpoint(quality="", response_format="", timeout_seconds=600))
        budgets = []

        def save_budget(target, timeout):
            current = image_settings(target)
            endpoints = [dict(item) for item in current["endpoints"]]
            endpoints[0]["timeout_seconds"] = timeout
            save_configuration(target, fingerprint(current), {"endpoints": endpoints})

        async def fake_wait(owner, operation, *, authorize, deadline, budget_seconds,
                            interval_seconds, now, monotonic, sleep):
            budgets.append(budget_seconds)
            if len(budgets) == 3:
                # This task is already running (it is in its polling wait); a
                # dashboard save is allowed because the owner has no open call.
                save_budget(plugin, 1200)
            return operation

        async def noop():
            return None

        with mock.patch.object(image_adapter, "wait_for_output", fake_wait):
            await provider.execute(request(provider), before_dispatch=noop)
            save_budget(plugin, 900)
            await provider.execute(request(provider, "inv-2"), before_dispatch=noop)
            await provider.execute(request(provider, "inv-3"), before_dispatch=noop)

        self.assertEqual(budgets, [600, 900, 900])
        self.assertEqual(provider.task_timeout_seconds(), 1200)

    async def test_wait_loop_stops_at_the_configured_budget_not_the_ceiling(self):
        # The declaration ceiling (1800) must not keep a task waiting longer
        # than the saved configuration; the earlier of the task deadline and the
        # saved budget ends the polling loop.
        from astrbot_host.native_extensions.image_native.waiting import wait_for_output

        class Owner:
            def __init__(self):
                self.calls = []

            async def recover(self, scope, operation_id, *, authorize):
                self.calls.append(operation_id)
                return {"state": "pending", "scope": scope, "id": operation_id, "receipt": {}}

        async def authorize():
            return None

        async def run(deadline, budget):
            clock = {"t": 0.0}

            async def sleep(seconds):
                clock["t"] += seconds

            operation = {"state": "pending", "scope": "scope-1", "id": "operation-1", "receipt": {}}
            result = await wait_for_output(
                Owner(), operation, authorize=authorize, deadline=deadline, budget_seconds=budget,
                interval_seconds=50.0, now=lambda: clock["t"], monotonic=lambda: clock["t"], sleep=sleep)
            return result, clock["t"]

        result, elapsed = await run(1800.0, 600.0)
        self.assertEqual(result["state"], "pending")
        self.assertEqual(elapsed, 600.0)
        _result, elapsed = await run(1800.0, 900.0)
        self.assertEqual(elapsed, 900.0)
        _result, elapsed = await run(400.0, 600.0)
        self.assertEqual(elapsed, 400.0, "the request deadline still bounds the wait")

    def test_saving_while_a_task_is_dispatching_is_refused(self):
        current = self.api.output_configuration()
        self.provider.owner._tasks.add(object())

        with self.assertRaises(Exception) as error:
            self.api.save_output_configuration(current["revision"], {
                "enabled": True, "backend": "online_api",
                "endpoints": [dict(saved_endpoint(), quality="", response_format="",
                                   timeout_seconds=900)],
            })

        self.assertEqual(getattr(error.exception, "code", ""), "configuration_busy")


def request(provider, invocation_id="inv-1", deadline_seconds=MAX_TASK_TIMEOUT_SECONDS):
    deadline = datetime.now(timezone.utc) + timedelta(seconds=deadline_seconds)
    return {
        "schema": SCHEMA,
        "invocation_id": invocation_id,
        "parent_task_id": "parent-1",
        "tool_call_id": "call-1",
        "action": "image.generate",
        "origin_key": "origin-1",
        "context": {"actor": {"subject_id": "subject-1"}, "scope": "private"},
        "binding": {"provider": PROVIDER_ID, "version": provider.version,
                    "generation": provider.owner.generation, "capability": "image.generate"},
        "arguments": {"prompt": "A blue cup on a white table."},
        "arguments_hash": "a" * 64,
        "deadline_at": deadline.isoformat(),
    }


class StubBackend:
    operations = ("generate", "edit")

    def __init__(self, config):
        self.config = config


class StubOwner:
    """The pending-wait shape: the transport call returned a remote locator."""

    def __init__(self, config):
        self.backend = StubBackend(config)
        self.generation = "generation-1"
        self._tasks = set()
        self._closed = False

    def _live(self):
        return None

    async def execute(self, invocation, *, references=(), reference_offers=(), resolve_references=None,
                      before_dispatch):
        return {"state": "pending", "id": "operation-1", "scope": invocation.scope_key,
                "receipt": {}}

    async def close(self):
        self._closed = True


class BudgetPlugin:
    """Minimal plugin surface used by ``save_configuration`` in this test only."""

    def __init__(self, config_path, provider, endpoint_document):
        config_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_dir = config_path.parent
        self.config = ImageConfig(config_path)
        self.config["native_v2"]["output"] = {
            "enabled": True, "backend": "online_api", "sd_webui": {}, "comfyui": {},
            "endpoints": [endpoint_document],
            "operation_routes": {"generate": ["online:primary"], "edit": []},
        }
        self._native_v2_provider = provider


def stub_provider(*, timeout):
    config = {"enabled": True, "backend": "online_api", "sd_webui": {}, "comfyui": {}, "network": {},
              "endpoints": [saved_endpoint(quality="", response_format="", timeout_seconds=timeout)],
              "operation_routes": {"generate": ["online:primary"], "edit": []}}
    return NativeImageProvider(StubOwner(config), package_version=PRODUCT_VERSION,
                              artifact_hash="test-image-package")


if __name__ == "__main__":
    unittest.main()
