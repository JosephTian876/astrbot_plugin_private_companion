"""Display preferences survive the sandboxed plugin iframe through the host bridge."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from astrbot_host.page_api import DISPLAY_PREFERENCES_FILE, PageWebAPI, PREFIX


PLUGIN_NAME = "astrbot_plugin_private_companion"
PREFERENCES = {
    "appearance": {"theme": "walnut", "density": "roomy", "radius": "round",
                   "typeScale": "l", "motion": "low"},
    "followLight": True,
    "largeText": True,
}


class DisplayPreferencesApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="companion-display-preferences-")
        self.controller = NS(data_root=Path(self.directory.name), _terminated=False)
        self.api = PageWebAPI(self.controller)

    def tearDown(self):
        self.directory.cleanup()

    async def call(self, handler, body=None, *, username="administrator", plugin_name=PLUGIN_NAME,
                   query=None):
        payload = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        request = NS(username=username, plugin_name=plugin_name, query=query or {},
                     body=AsyncMock(return_value=payload))
        with patch("astrbot_host.page_api.request", request):
            return await handler()

    async def test_preferences_round_trip_to_plugin_data_by_dashboard_account(self):
        saved = await self.call(self.api.save_display_preferences, PREFERENCES)
        self.assertEqual(saved["status"], "ok", saved)
        self.assertEqual(saved["data"]["preferences"], PREFERENCES)

        # A new API instance proves the value came from disk, as it will after a
        # normal browser reload creates a fresh iframe.
        restored = await self.call(PageWebAPI(self.controller).display_preferences)
        self.assertEqual(restored["data"]["preferences"], PREFERENCES)
        path = Path(self.directory.name) / DISPLAY_PREFERENCES_FILE
        self.assertFalse(path.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertFalse(path.read_bytes().endswith(b"\r\n"))

    async def test_other_dashboard_accounts_do_not_share_preferences(self):
        await self.call(self.api.save_display_preferences, PREFERENCES)
        other = await self.call(self.api.display_preferences, username="another-admin")
        self.assertIsNone(other["data"]["preferences"])

    async def test_requires_dashboard_identity_and_this_plugins_page_context(self):
        for username, plugin_name, query in (
            ("api_key:synthetic", PLUGIN_NAME, {}),
            ("administrator", "another_plugin", {}),
            ("administrator", PLUGIN_NAME, {"user": "other"}),
        ):
            response = await self.call(self.api.display_preferences, username=username,
                                       plugin_name=plugin_name, query=query)
            self.assertEqual(response["status"], "error")

    async def test_rejects_invalid_preferences_without_overwriting_saved_values(self):
        await self.call(self.api.save_display_preferences, PREFERENCES)
        invalid = {**PREFERENCES, "appearance": {**PREFERENCES["appearance"], "theme": "unknown"}}
        rejected = await self.call(self.api.save_display_preferences, invalid)
        self.assertEqual(rejected["data"]["code"], "display_preferences_invalid")

        restored = await self.call(self.api.display_preferences)
        self.assertEqual(restored["data"]["preferences"], PREFERENCES)


if __name__ == "__main__":
    unittest.main()
