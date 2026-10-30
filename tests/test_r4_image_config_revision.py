"""Regression coverage for the image output configuration revision contract."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot_host.native_extensions.image_native.output_configuration import (
    public_configuration,
    save_configuration,
)


class _Config(dict):
    def __init__(self, value):
        super().__init__(value)
        self.saves = 0

    def save_config(self):
        self.saves += 1


class _Provider:
    def __init__(self):
        self.owner = SimpleNamespace(_tasks=[], backend=None)
        self.wait_budget_seconds = 0
        self._closed = False

    def task_timeout_seconds(self):
        return 300


class ImageConfigurationRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.provider = _Provider()
        self.plugin = SimpleNamespace(
            data_dir=Path(self.temp.name),
            _native_v2_provider=self.provider,
            config=_Config({
                "native_v2": {
                    "enabled": False,
                    "backend": "sd_webui",
                    "output": {
                        "enabled": False,
                        "backend": "sd_webui",
                        "endpoints": [],
                        "sd_webui": {},
                        "comfyui": {},
                        "network": {},
                        "legacy_backend": "",
                        "legacy_options": {},
                    },
                },
            }),
        )

    def test_backend_change_does_not_trip_the_derived_route_revision(self):
        published = public_configuration(self.plugin)
        self.assertEqual(published["settings"]["operation_routes"]["generate"], ["sd_webui"])

        saved = save_configuration(self.plugin, published["revision"], {"backend": "comfyui"})

        self.assertEqual(saved["settings"]["backend"], "comfyui")
        self.assertEqual(saved["settings"]["operation_routes"]["generate"], ["comfyui"])
        self.assertEqual(self.plugin.config.saves, 1)

    def test_real_source_change_still_rejects_a_stale_revision(self):
        published = public_configuration(self.plugin)
        output = self.plugin.config["native_v2"]["output"]
        output["legacy_backend"] = "changed-outside-the-page"

        with self.assertRaisesRegex(Exception, "configuration_conflict"):
            save_configuration(self.plugin, published["revision"], {"backend": "comfyui"})


if __name__ == "__main__":
    unittest.main()
