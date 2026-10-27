"""Official performance mode: page gate and management-route trimming."""

from pathlib import Path
from types import SimpleNamespace
import json
import unittest

from astrbot.api.web import PluginRequest, bind_request_context
from astrbot_host.multi_api import MultiHostWebAPI
from astrbot_host.performance_mode import performance_enabled, performance_status
from astrbot_host.role_paths import HOST_PREFIX


class PerformanceModeTests(unittest.TestCase):
    def test_official_schema_exposes_performance_switch_first(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(next(iter(schema)), "performance")
        self.assertEqual(schema["performance"]["items"]["enabled"]["type"], "bool")
        self.assertFalse(schema["performance"]["items"]["enabled"]["default"])

    def test_performance_setting_accepts_saved_bool_and_legacy_text(self):
        self.assertFalse(performance_enabled({}))
        self.assertFalse(performance_enabled({"performance": {"enabled": False}}))
        for value in (True, 1, "true", " yes ", "on", "enabled"):
            self.assertTrue(performance_enabled({"performance": {"enabled": value}}), value)
        for value in (False, 0, "false", "off", "", None):
            self.assertFalse(performance_enabled({"performance": {"enabled": value}}), value)

    def test_status_describes_the_same_effective_profile(self):
        lite = performance_status({"performance": {"enabled": True}})
        self.assertTrue(lite["enabled"])
        self.assertFalse(lite["cottage_enabled"])
        self.assertTrue(lite["official_page_only"])
        self.assertTrue(lite["lite_page"])
        self.assertFalse(lite["heavy_management_routes"])
        self.assertTrue(lite["requires_reload"])
        standard = performance_status({"performance": {"enabled": False}})
        self.assertFalse(standard["enabled"])
        self.assertTrue(standard["cottage_enabled"])
        self.assertTrue(standard["heavy_management_routes"])
        # A saved toggle does not silently mix a full page with trimmed routes.
        self.assertTrue(performance_status(
            {"performance": {"enabled": False}}, enabled=True)["lite_page"])
        self.assertFalse(performance_status(
            {"performance": {"enabled": True}}, enabled=False)["lite_page"])

    def test_lite_registration_keeps_only_status_and_performance_routes(self):
        class Context:
            def __init__(self):
                self.registered_web_apis = []

            def register_web_api(self, route, handler, methods, description):
                self.registered_web_apis.append((route, handler, methods, description))

        controller = SimpleNamespace(config={"performance": {"enabled": True}}, context=Context())
        api = MultiHostWebAPI(controller)
        api.register()
        self.assertEqual([row[0] for row in controller.context.registered_web_apis], [
            HOST_PREFIX + "/status",
            HOST_PREFIX + "/performance",
        ])
        self.assertEqual(len(api._registered_routes), 2)




class PerformanceEndpointTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def request(username):
        raw = SimpleNamespace(method="GET", url=SimpleNamespace(path="/host/performance"),
                              headers={}, cookies={}, client=None,
                              query_params=SimpleNamespace(multi_items=lambda: ()))
        return PluginRequest(raw, username=username)

    async def test_endpoint_reports_startup_effective_mode_and_requires_admin(self):
        controller = SimpleNamespace(config={"performance": {"enabled": False}},
                                     _performance_mode_enabled=True)
        api = MultiHostWebAPI(controller)
        with bind_request_context(self.request("admin")):
            lite = await api.api_performance()
        self.assertEqual(lite["status"], "ok")
        self.assertTrue(lite["data"]["enabled"])
        self.assertTrue(lite["data"]["lite_page"])
        self.assertEqual(lite["data"]["mode"], "performance")

        with bind_request_context(self.request(None)):
            denied = await api.api_performance()
        self.assertEqual(denied["message"], "dashboard_identity_required")


if __name__ == "__main__":
    unittest.main()
