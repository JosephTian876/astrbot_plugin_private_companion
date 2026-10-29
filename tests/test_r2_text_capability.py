"""第 2 轮修复（R2-D2）：AstrBot ``modalities`` 不得门控文本能力。

宿主事实（AstrBot 4.28.1 与 4.29.1 逐行一致，见报告 §D2.1）：

* 文本是**本底能力**。宿主全仓没有 ``"text" in modalities`` 这类判定；
  ``text`` 只是 Dashboard 上的惰性复选框，文本生成本身永远不因
  ``modalities`` 关闭。
* ``[]`` 是宿主自己的迁移默认值（``astrbot/core/utils/migra_helper.py``
  在键缺失时无条件补空列表），语义是“未配置、不限制”；上下文净化路径
  （``tool_loop_agent_runner.py``）把 ``not modalities``（含缺键）同样
  视为全支持。
* ``tool_use`` 缺失只表示**这一轮不给工具**（同一文件把 ``tools`` 置空），
  不是“这个任务不可用”。缺失 ``image``/``audio`` 才让宿主改选其它 provider。
* `Dashboard 只有 ``text`` / ``image`` / ``audio`` / ``tool_use`` 四项；
  未知值只可能来自手改配置或宿主未来新增模态，适配器不该因此崩溃。

因此本文件锁定三组契约：

1. ``HostModalityCapabilityTests``：宿主 12 组模态 → V2 能力集，走真实
   ``AstrBotTextProvider``。
2. ``MultiHostReplyProfileTests`` / ``StagingHostReplyProfileTests``：两个
   真实宿主装配里，主回复 profile 只要求 ``text``（工具不进门）。
3. ``ReplyCapabilityRoutingTests``：真实 ``ModelRouter`` + 真实形状的
   主回复 profile。5 种宿主合法配置都能通过任务级路由；缺 ``tool_use``
   退化为**每轮**的运行时门（``model_gateway/service.py:75-77``），
   而不是整轮任务失败。
"""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest

# AstrBot 在 import 期解析数据目录。先重定向再导入宿主模块，保证本文件单独
# 运行时也不会在仓库里造出 data/ 树（与 tests/test_astrbot_staging_host.py
# 的既有做法一致）。
_previous_astrbot_root = os.environ.get("ASTRBOT_ROOT")
_astrbot_import_root = None
if _previous_astrbot_root is None:
    _astrbot_import_root = TemporaryDirectory(prefix="companion-v2-text-import-")
    os.environ["ASTRBOT_ROOT"] = _astrbot_import_root.name


def tearDownModule():
    if _astrbot_import_root is not None:
        os.environ.pop("ASTRBOT_ROOT", None)
        _astrbot_import_root.cleanup()


from astrbot_host.controller import AstrBotStagingController

from companion_v2.adapters.astrbot.staging import NativeAdapterTypes
from companion_v2.configuration.registry import FieldRegistry
from companion_v2.configuration.service import ConfigurationService
from companion_v2.conversation.messages import REPLY_KIND
from companion_v2.execution.models import (
    Lane,
    ProviderBinding,
    TaskError,
    TaskRegistration,
    TaskResult,
)
from companion_v2.model_gateway.budget import BudgetLedger
from companion_v2.model_gateway.models import (
    Amount,
    LocalProvider,
    ModelDraft,
    ModelTaskProfile,
    ProviderResponse,
    ToolSpec,
)
from companion_v2.model_gateway.routing import ModelRouter, configuration_fields
from companion_v2.model_gateway.service import LocalModelGateway
from tests.multi_host_support import MultiFixture
from tests.native_adapter_support import FakeEvent, FakeProvider, FakeReverseApi, NativeFixture
from tests.test_astrbot_staging_host import WebContext, host_config
from tests.test_management_access import NOW
from tests.test_model_gateway import ModelFixture


#: 宿主缺省/空列表时 V2 授予的完整能力集（既有兼容断言，见
#: tests/test_astrbot_model_wire.py:215-223）。
FULL = ("text", "tools", "image", "audio")

#: 宿主的四种可勾选模态；``text`` 不在其中作为“可关闭项”。
HOST_LEGAL_MODALITIES = (
    ("text",),
    ("text", "image"),
    ("image",),
    ("audio",),
    ("image", "tool_use"),
)

MISSING = object()


class HostModalityCapabilityTests(NativeFixture):
    """宿主 ``modalities`` → V2 能力集；全部经真实 ``local_provider()``。"""

    def capabilities(self, modalities):
        if modalities is MISSING:
            self.native.provider.provider_config.pop("modalities", None)
        else:
            self.native.provider.provider_config["modalities"] = modalities
        return self.make_model().local_provider().capabilities

    def test_twelve_host_modality_shapes(self):
        """12 组模态形状的精确矩阵；缺少 ``text`` 的组也必须带 ``text``。"""
        cases = (
            (MISSING, FULL),                                      # 键缺失：宽松（宿主迁移不会产出）
            ([], FULL),                                           # 宿主 migra_helper 写入的默认值
            (["text"], ("text",)),
            (["text", "image"], ("text", "image")),
            (["text", "tool_use"], ("text", "tools")),
            (["text", "image", "tool_use"], ("text", "tools", "image")),
            (["text", "image", "audio", "tool_use"], FULL),
            (["image", "tool_use"], ("text", "tools", "image")),   # 宿主合法：没勾“文本”
            (["image"], ("text", "image")),
            (["audio"], ("text", "audio")),
            ("image", FULL),                                      # 非列表不是宿主形状：按未配置处理
            (["video"], ("text",)),                               # 未知值：忽略，不产出空集合
        )
        for modalities, expected in cases:
            with self.subTest(modalities=modalities):
                self.assertEqual(self.capabilities(modalities), expected)

    def test_text_is_never_withheld_from_a_host_legal_provider(self):
        for modalities in HOST_LEGAL_MODALITIES:
            with self.subTest(modalities=modalities):
                self.assertIn("text", self.capabilities(modalities))

    def test_unknown_modalities_are_ignored_instead_of_crashing_assembly(self):
        """``["video"]`` 之类未知值不得构造空能力集（``LocalProvider`` 会抛 ValueError）。"""
        for modalities in (["video"], ["video", "image"], ["text", "video"], [1, None], ["TEXT"]):
            with self.subTest(modalities=modalities):
                capabilities = self.capabilities(modalities)
                self.assertIn("text", capabilities)
                self.assertEqual(len(capabilities), len(set(capabilities)))
                self.assertIsInstance(capabilities, tuple)

    def test_empty_and_missing_modalities_stay_host_compatible(self):
        """守护 D2-1：不得把 ``[]``/缺键改成“只给文本”。"""
        for modalities in (MISSING, []):
            with self.subTest(modalities=modalities):
                self.assertEqual(self.capabilities(modalities), FULL)


class MultiHostReplyProfileTests(MultiFixture, unittest.IsolatedAsyncioTestCase):
    """真实 multi-host 装配：主回复 profile 与 ``modalities`` 的关系。"""

    async def test_main_reply_profile_requires_text_only(self):
        self.native.provider.provider_config["modalities"] = ["text"]
        app = await self.start_host()
        profile = app.router.profiles[REPLY_KIND]
        self.assertEqual(profile.capabilities, ("text",))
        self.assertNotIn("tools", profile.capabilities)
        # provider 侧如实反映宿主声明；工具能力仍在运行时门判定。
        self.assertEqual(app.router.providers["native-model"].capabilities, ("text",))

    async def test_unknown_modality_does_not_break_host_assembly(self):
        self.native.provider.provider_config["modalities"] = ["video"]
        app = await self.start_host()
        self.assertEqual(app.state, "running")
        self.assertEqual(app.router.providers["native-model"].capabilities, ("text",))


class StagingHostReplyProfileTests(unittest.IsolatedAsyncioTestCase):
    """真实 staging 装配：同一份主回复 profile 语义。"""

    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="companion-v2-text-staging-")
        self.root = Path(self.temporary.name)
        self.native = WebContext()
        self.types = NativeAdapterTypes("4.27.2", FakeEvent, FakeProvider, NS, NS, FakeReverseApi)
        self.controller = AstrBotStagingController(
            self.native, host_config(), self.root, native_types=self.types, now=lambda: NOW)

    async def asyncTearDown(self):
        await self.controller.terminate()
        self.temporary.cleanup()

    async def test_main_reply_profile_requires_text_only(self):
        self.native.provider.provider_config["modalities"] = ["text"]
        initialized = await self.controller.initialize_profile()
        self.assertTrue(initialized.is_success, initialized.failure)
        started = await self.controller.start()
        self.assertTrue(started.is_success, started.failure)
        app = self.controller.session.application
        self.assertEqual(app.router.profiles[REPLY_KIND].capabilities, ("text",))
        self.assertEqual(app.router.providers["native-model"].capabilities, ("text",))


class ReplyCapabilityRoutingTests(ModelFixture, unittest.IsolatedAsyncioTestCase):
    """真实 ``ModelRouter`` + 主回复 profile 形状（``text`` 必需，工具不必）。"""

    def setUp(self):
        super().setUp()
        self.profiles = (ModelTaskProfile(REPLY_KIND, Lane.FOREGROUND, "primary",
            "只根据已提供的材料回复。", "", 10, capabilities=("text",)),)
        base = tuple(field for field in self.registry.fields.values()
                     if not field.field_id.startswith("model.fixture."))
        self.registry = FieldRegistry(version="models.reply-text.v1",
                                      fields=base + configuration_fields(self.profiles))
        self.configuration = ConfigurationService(self.runtime, self.registry)
        self.registrations = (TaskRegistration(
            replace(self.foreground, kind=REPLY_KIND), self.validate_inputs),)
        self.tasks = self.supervisor()
        (self.tasks.start(expected_generation=self.runtime.read_one(
            "SELECT generation FROM task_supervisor WHERE singleton = 1", ())["generation"])).unwrap()

    def reply_run(self, identifier="task-1"):
        """Claim one running task of the real main reply kind."""
        return self.model_run(identifier, kind=REPLY_KIND)

    def reply_provider(self, capabilities):
        self.called = []

        async def generate(envelope):
            self.called.append(envelope)
            return ProviderResponse("generated", ModelDraft("private draft", ()),
                                    Amount(15, 40), "receipt-primary")

        return LocalProvider(ProviderBinding("model.text", "primary", "1", "local-1"),
                             capabilities, 100, lambda envelope: Amount(30, 100), generate)

    def router_for(self, capabilities):
        router = ModelRouter(self.configuration, self.profiles, (self.reply_provider(capabilities),))
        generation = self.runtime.read_one(
            "SELECT generation FROM configuration_consumers WHERE consumer_id = 'model_gateway'", ())["generation"]
        router.start(expected_generation=generation)
        return router

    async def test_main_reply_profile_routes_for_every_host_legal_configuration(self):
        cases = (
            (("text",), ("text",)),
            (("text", "image"), ("text", "image")),
            (("image",), ("text", "image")),
            (("audio",), ("text", "audio")),
            (("image", "tool_use"), ("text", "tools", "image")),
        )
        profile = self.profiles[0]
        self.assertEqual(profile.capabilities, ("text",))
        for index, (modalities, capabilities) in enumerate(cases):
            with self.subTest(modalities=modalities):
                router = self.router_for(capabilities)
                run = self.reply_run(f"route-{index}")
                plan = router.resolve(run, self.request())
                self.assertEqual([provider.binding.provider for provider in plan.providers], ["primary"])
                self.assertEqual(plan.audit["profile"]["kind"], REPLY_KIND)
                self.tasks.finish(run.claim, TaskResult("succeeded", result_ref=f"route-{index}")).unwrap()

    async def test_missing_tool_use_is_a_round_degradation_not_a_task_failure(self):
        """缺 ``tool_use``：任务级路由通过；带目录的一轮降级为“无工具回复”。

        第 2 轮运行时修复后（``conversation/generation.py`` 的每轮工具决策 +
        ``model_gateway/service.py`` 的记录式降级），目录到了没有工具能力的
        provider 手上会被丢弃并写入 ``tool_filters``，而不是抛
        ``model_capability_mismatch`` 让整条文本回复消失——宿主在
        ``modalities`` 不含 ``tool_use`` 时也只是清空本轮 tools。
        """
        router = self.router_for(("text",))
        request = self.request()
        run = self.reply_run()
        plan = router.resolve(run, request)
        self.assertEqual(plan.providers[0].capabilities, ("text",))

        gateway = LocalModelGateway(router, self.ledger, self.tasks)
        # 没有工具目录的一轮：模型必须能正常回复（不再因“缺 tools”整轮失败）。
        session = gateway.open_session(run, request)
        completion = await session.generate(request)
        self.assertEqual(completion.draft.text, "private draft")
        self.assertEqual(len(self.called), 1)
        calls = len(self.runtime.read_all("SELECT * FROM model_calls", ()))
        self.assertEqual(calls, 1)
        self.tasks.finish(run.claim, TaskResult("succeeded", result_ref=completion.call_id)).unwrap()

        # 同一 provider 被要求提供工具时：目录被记录式丢弃，回复仍然发出，
        # 且没有任何工具被派发（``tool_actions`` 为空）。
        other = self.reply_run("task-2")
        session = gateway.open_session(other, request,
            tools=(ToolSpec("source.read", "读取本轮原始输入", '{"type":"object"}'),))
        await session.generate(request)
        row = self.runtime.read_one("SELECT plan_json FROM model_requests WHERE task_id = 'task-2'", ())
        recorded = json.loads(row["plan_json"])
        self.assertEqual(recorded["tool_actions"], [])
        self.assertEqual(recorded["tool_filters"][-1], {"action": "*", "reason": "provider_without_tool_use"})
        self.assertEqual(self.called[-1].tools, ())
        self.assertEqual(len(self.runtime.read_all("SELECT * FROM model_calls", ())), calls + 1)

    async def test_declared_tool_use_still_allows_a_tool_round(self):
        router = self.router_for(("text", "tools"))
        run = self.reply_run()
        request = self.request()
        gateway = LocalModelGateway(router, self.ledger, self.tasks)
        session = gateway.open_session(run, request,
            tools=(ToolSpec("source.read", "读取本轮原始输入", '{"type":"object"}'),))
        await session.generate(request)
        self.assertEqual(len(self.called), 1)

    async def test_unknown_modality_value_keeps_the_reply_routable(self):
        router = self.router_for(("text",))
        plan = router.resolve(self.reply_run(), self.request())
        self.assertEqual([provider.binding.provider for provider in plan.providers], ["primary"])
        self.assertEqual(plan.providers[0].capabilities, ("text",))
