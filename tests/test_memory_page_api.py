"""Formal Memory Page API compatibility for the installed memory companion."""

from __future__ import annotations

import hashlib
import inspect
import json
import unittest

from astrbot_host.integration_registry import UnifiedCompanionExtensionAPI
from astrbot_host.memory_page_api import (
    MEMORY_PAGE_API_FAMILY,
    MEMORY_PAGE_API_VERSION,
    MEMORY_PAGE_CAPABILITIES,
    MEMORY_PAGE_SNAPSHOT_VERSION,
    MemoryPageAPI,
    MemoryPageUnavailable,
)


class MemoryPageAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = MemoryPageAPI(None)

    def test_descriptor_is_exact_and_declares_every_required_capability(self):
        descriptor = self.adapter.capabilities()
        self.assertEqual(tuple(descriptor), (
            "plugin_id", "instance_generation", "api_family", "api_version",
            "supported_task_versions", "capabilities", "lifecycle_state",
            "degraded_reasons"))
        self.assertEqual(descriptor["plugin_id"], "astrbot_plugin_private_companion")
        self.assertRegex(descriptor["instance_generation"], r"^[0-9a-f]{32}$")
        self.assertEqual(descriptor["api_family"], MEMORY_PAGE_API_FAMILY)
        self.assertEqual(descriptor["api_version"], MEMORY_PAGE_API_VERSION)
        self.assertEqual(descriptor["supported_task_versions"], [
            MEMORY_PAGE_SNAPSHOT_VERSION, "companion.memory-page-photo.v1"])
        self.assertEqual(set(MEMORY_PAGE_CAPABILITIES).issubset(descriptor["capabilities"]), True)
        self.assertEqual((descriptor["lifecycle_state"], descriptor["degraded_reasons"]),
                         ("ready", []))

    async def test_snapshot_is_sealed_path_free_and_matches_the_declared_date(self):
        snapshot = await self.adapter.export_snapshot(
            target_plugin_id="astrbot_plugin_memory_companion",
            selected_date="2026-10-28")
        self.assertEqual(tuple(snapshot), (
            "version", "source_plugin_id", "instance_generation", "selected_date",
            "available_dates", "features", "coordination", "day", "snapshot_id",
            "snapshot_sha256"))
        self.assertEqual(snapshot["version"], MEMORY_PAGE_SNAPSHOT_VERSION)
        self.assertEqual(snapshot["source_plugin_id"], "astrbot_plugin_private_companion")
        self.assertEqual(snapshot["instance_generation"], self.adapter.instance_generation)
        self.assertEqual(snapshot["selected_date"], "2026-10-28")
        self.assertEqual(snapshot["available_dates"], ["2026-10-28"])
        self.assertEqual(snapshot["coordination"], {
            "available": True, "state": "ready", "reason_code": ""})
        self.assertEqual(snapshot["day"]["date"], "2026-10-28")
        self.assertEqual(snapshot["day"]["plan"], {
            "date": "", "source": "none", "items": []})
        self.assertEqual((snapshot["day"]["details"], snapshot["day"]["photos"],
                          snapshot["day"]["diaries"]), ([], [], []))
        hashed = {field: snapshot[field] for field in (
            "version", "source_plugin_id", "instance_generation", "selected_date",
            "available_dates", "features", "coordination", "day")}
        digest = hashlib.sha256(json.dumps(hashed, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(snapshot["snapshot_sha256"], digest)
        self.assertEqual(snapshot["snapshot_id"], "memorypagesnap_" + digest)
        encoded = json.dumps(snapshot, ensure_ascii=False).lower()
        for forbidden in ("/users/", "c:\\\\", "session_id", "provider_id", "token"):
            self.assertNotIn(forbidden, encoded)

    async def test_photo_requests_fail_with_a_stable_code_instead_of_host_data(self):
        with self.assertRaises(MemoryPageUnavailable) as raised:
            await self.adapter.read_photo(
                target_plugin_id="astrbot_plugin_memory_companion",
                photo_ref="mphoto_0123456789ab_abcdefghijklmnopqrstuv")
        self.assertEqual(raised.exception.code, "memory_page_photo_unavailable")

    async def test_public_facade_exposes_the_three_formal_methods(self):
        api = object.__new__(UnifiedCompanionExtensionAPI)
        api._memory_page = self.adapter
        self.assertTrue(callable(api.memory_page_capabilities))
        self.assertTrue(inspect.iscoroutinefunction(api.export_memory_page_snapshot))
        self.assertTrue(inspect.iscoroutinefunction(api.read_memory_page_photo))
        self.assertEqual(api.memory_page_capabilities()["lifecycle_state"], "ready")
        snapshot = await api.export_memory_page_snapshot(
            target_plugin_id="astrbot_plugin_memory_companion",
            selected_date="2026-10-28")
        self.assertEqual(snapshot["selected_date"], "2026-10-28")


if __name__ == "__main__":
    unittest.main()
