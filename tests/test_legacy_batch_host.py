"""End-to-end: preview a role scope, then import it through the batch endpoint."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import unittest

from astrbot_host.identity_derivation import derive_role_id
from astrbot_host.legacy_api import LegacyMigrationWebAPI
from astrbot_host.multi_controller import AstrBotMultiController
from companion_v2.adapters.astrbot.staging import NativeAdapterTypes
from companion_v2.foundation.clock import DeterministicClock
from companion_v2.legacy.migration import LegacyMigration
from tests.multi_host_support import MultiNativeContext, multi_config
from tests.native_adapter_support import FakeEvent, FakeProvider, FakeReverseApi
from tests.test_management_access import NOW


class LegacyBatchHostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.source = TemporaryDirectory(prefix="companion-v2-batch-v1-")
        self.target = TemporaryDirectory(prefix="companion-v2-batch-v2-")
        self.addCleanup(self.source.cleanup)
        self.addCleanup(self.target.cleanup)
        self.v1_root = Path(self.source.name)
        (self.v1_root / "companions.json").write_text("{}\n", encoding="utf-8", newline="\n")
        image_directory = self.v1_root / "photo_reference_images"
        image_directory.mkdir()
        from io import BytesIO
        from PIL import Image
        payload = BytesIO()
        Image.new("RGB", (12, 12), (24, 80, 140)).save(payload, format="PNG")
        image_directory.joinpath("scarf.png").write_bytes(payload.getvalue())
        self.migration = LegacyMigration(v1_root=self.v1_root, v1_configuration={"wardrobe_items": [{
            "id": "red-scarf", "name": "红围巾", "description": "羊毛，冬季佩戴",
            "tags": ["冬季"], "source": "photo_reference_images/scarf.png"}]})
        native = MultiNativeContext()
        native_types = NativeAdapterTypes("4.27.2", FakeEvent, FakeProvider, SimpleNamespace,
            SimpleNamespace, FakeReverseApi)
        self.clock = DeterministicClock.at(NOW)
        self.controller = AstrBotMultiController(native, multi_config(), Path(self.target.name) / "data",
            native_types=native_types, now=self.clock.now)
        (await self.controller.initialize_profile()).unwrap()
        (await self.controller.start()).unwrap()
        self.addAsyncCleanup(self.controller.terminate)
        self.role_id = "role-a"
        context = self.controller.session.authority.context("first", self.role_id)
        self.session = SimpleNamespace(profile=SimpleNamespace(target=context),
                                       application=self.controller.session.application)

    def api(self):
        api = LegacyMigrationWebAPI.__new__(LegacyMigrationWebAPI)
        api.controller = SimpleNamespace(runtime=self.controller.runtime, now=self.clock.now,
            data_root=Path(self.target.name) / "data", web_api=self.controller.web_api,
            config=self.controller.config, role_id=self.role_id)
        from astrbot_host.management_identity import dashboard_operator
        api._operator = lambda: dashboard_operator(
            self.controller.runtime, "migration-test-admin", at=self.clock.now())
        return api

    async def test_preview_then_batch_imports_the_same_window(self):
        api = self.api()
        scope = api._scope(self.migration, self.v1_root, self.session)
        self.assertTrue(scope.is_success, scope.failure if not scope.is_success else "")
        payload = scope.unwrap()
        self.assertGreaterEqual(payload["counts"]["personas"], 1)
        self.assertTrue(any(item["auto"] for item in payload["personas"]))
        self.assertEqual(payload["groups"], [], "no V1 group data means no group rows")

        personas = [item["source_id"] for item in payload["personas"] if item["auto"]]
        result = await api._batch_apply(self.migration, self.v1_root, self.session,
            {"token": payload["token"], "personas": personas, "groups": []})
        self.assertTrue(result.is_success, result.failure if not result.is_success else "")
        receipt = result.unwrap()
        self.assertEqual(receipt["counts"]["failed"], 0, receipt["items"][0].get("reason"))
        self.assertTrue(receipt["memory_database_skipped"])
        self.assertEqual([item["state"] for item in receipt["items"]], ["imported"])

        application = self.controller.session.application
        context = self.session.profile.target
        items = application.references.assets.list_items(application.references.owner(context))
        self.assertEqual([item.name for item in items], ["红围巾"])

    async def test_stale_preview_writes_nothing(self):
        api = self.api()
        with patch.object(LegacyMigrationWebAPI, "_data_apply") as apply:
            result = await api._batch_apply(self.migration, self.v1_root, self.session,
                {"token": "stale-token", "personas": [""], "groups": []})
        self.assertFalse(result.is_success)
        self.assertEqual(result.failure.code, "legacy_scope_stale")
        apply.assert_not_called()


    def test_group_import_skips_empty_worldbook(self):
        plan = {"plan_token": "token", "group_expression": {"available": False},
                "group_worldbook": {"entries": 0, "members": 0}}
        arguments = LegacyMigrationWebAPI._group_import_arguments(
            self.v1_root, ("persona", "group"), plan)
        self.assertFalse(arguments["import_group_worldbook"])
        plan["group_worldbook"]["entries"] = 1
        arguments = LegacyMigrationWebAPI._group_import_arguments(
            self.v1_root, ("persona", "group"), plan)
        self.assertTrue(arguments["import_group_worldbook"])

    def test_group_import_rejects_malformed_plan_shape(self):
        for plan in ({}, {"plan_token": "token"},
                     {"plan_token": "token", "group_expression": {"available": "yes"}},
                     {"plan_token": "token", "group_expression": {"available": False},
                      "group_worldbook": []}):
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                LegacyMigrationWebAPI._group_import_arguments(
                    self.v1_root, ("persona", "group"), plan)


if __name__ == "__main__":
    unittest.main()
