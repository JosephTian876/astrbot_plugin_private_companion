"""生图输出迁移的重跑边界：V2 已有的设置不能被 V1 迁移再写一遍。

V1 迁移可以重复试算、重复执行，所以它必然落在「新版界面可能已经改过」的
``native_v2.output`` 上。两个方向都要钉住，缺一个就会出问题：

* V2 只在新版界面配了下载代理（还没有端点队列）时，V1 端点必须照常迁入；
* V2 已保存的下载代理（以及整份已迁入的配置）不能被第二遍迁移改写。

保存走真实的 ``save_configuration``，因此断言的是落盘后的配置，而不是一份
手写的近似快照。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest

from astrbot_host.native_extensions.image_native.output_configuration import (
    public_configuration,
    save_configuration,
)
from companion_v2.legacy.image_configuration_migration import (
    convert_image_configuration,
    plan_image_output_changes,
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


def v1_photo_action_config():
    """一份只配了在线生图队列与下载代理的 V1 配置。"""
    return {"photo_action_config": {
        "photo_generation_backend": "external",
        "external_image_api_endpoints": [{"name": "Legacy", "platform": "openai",
            "base_url": "https://api.example.test/v1", "api_key": "legacy-key",
            "model": "image-v1"}],
        "external_image_download_proxy": "http://v1-proxy:8080",
    }}


class ImageOutputMigrationRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.plugin = SimpleNamespace(
            data_dir=Path(temporary.name),
            _native_v2_provider=_Provider(),
            # 与 ImageConfig 给新装的默认值一致：还没有人在这台机器上迁过生图。
            config=_Config({"native_v2": {
                "enabled": True,
                "backend": "auto",
                "output": {"enabled": False, "backend": "auto", "endpoints": [],
                           "sd_webui": {}, "comfyui": {}},
            }}),
        )

    def migrated(self):
        return convert_image_configuration(v1_photo_action_config())

    def settings(self):
        return public_configuration(self.plugin)["settings"]

    def stored(self):
        return self.plugin.config["native_v2"]["output"]

    def save(self, changes):
        return save_configuration(self.plugin, public_configuration(self.plugin)["revision"], changes)

    def test_empty_v2_output_receives_every_migrated_group(self):
        migrated = self.migrated()

        changes, differences = plan_image_output_changes(self.settings(), migrated.output_changes)

        self.assertEqual(set(changes), set(migrated.output_changes))
        self.assertEqual(differences, ())

    def test_v2_download_proxy_alone_does_not_block_the_endpoint_migration(self):
        self.save({"network": {"download_proxy": "http://v2-proxy:3128"}})

        migrated = self.migrated()
        changes, differences = plan_image_output_changes(self.settings(), migrated.output_changes)

        self.assertEqual(set(changes), set(migrated.output_changes) - {"network"})
        self.assertIn("endpoints", changes)
        self.assertEqual([item["kind"] for item in differences], ["preserved_v2"])
        self.assertEqual([item["field"] for item in differences], ["image.output.network"])

    def test_saved_v2_download_proxy_survives_the_endpoint_migration(self):
        self.save({"network": {"download_proxy": "http://v2-proxy:3128"}})

        changes, _ = plan_image_output_changes(self.settings(), self.migrated().output_changes)
        self.save(changes)

        self.assertEqual(self.stored()["network"]["download_proxy"], "http://v2-proxy:3128")
        self.assertEqual([item["base_url"] for item in self.stored()["endpoints"]],
                         ["https://api.example.test/v1"])

    def test_cleared_v2_download_proxy_is_not_restored_by_a_rerun(self):
        self.save({"network": {"download_proxy": "http://v2-proxy:3128"}})
        self.save({"network": {"clear_download_proxy": True}})
        # 已清空的组与从未改过的组都投影成 download_proxy_set=False；只有显式
        # 保存过才会带 use_environment_proxy。
        self.assertEqual(self.settings()["network"],
                         {"download_proxy_set": False, "use_environment_proxy": False})

        changes, differences = plan_image_output_changes(self.settings(), self.migrated().output_changes)

        self.assertNotIn("network", changes)
        self.assertIn("endpoints", changes)
        self.assertEqual([item["field"] for item in differences], ["image.output.network"])
        self.save(changes)
        self.assertEqual(self.stored()["network"],
                         {"download_proxy": "", "use_environment_proxy": False})
        self.assertEqual([item["base_url"] for item in self.stored()["endpoints"]],
                         ["https://api.example.test/v1"])

    def test_second_migration_over_a_populated_output_preserves_it_and_writes_nothing(self):
        first = self.migrated()
        self.save(first.output_changes)
        before = public_configuration(self.plugin)

        changes, differences = plan_image_output_changes(before["settings"], first.output_changes)

        self.assertEqual(changes, {})
        self.assertEqual([item["kind"] for item in differences], ["preserved_v2"])
        self.assertEqual(before["revision"], public_configuration(self.plugin)["revision"])
        self.assertEqual(self.plugin.config.saves, 1)


if __name__ == "__main__":
    unittest.main()
