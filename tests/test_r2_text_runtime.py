"""第 2 轮修复（R2-D2 运行时闭环）：工具能力是**每一轮**的门，不是整轮任务的门。

修复前的事实链（本文件每条断言都对应其中一环）：

* ``companion_v2/conversation/generation.py:485-499`` 无条件把
  ``messages.tools.catalog_for_with_diagnostics(...)`` 的结果传给
  ``gateway.open_session(tools=...)``；
* ``companion_v2/model_gateway/service.py:75-77`` 在“有目录 + 任一 provider 无
  ``tools``”时抛 ``TaskError("model_capability_mismatch", ...)``，整条文本回复失败；
* 私聊随后走 ``generation.py:500-505`` 的本地兜底，但 ``:645`` 使用了只在模型循环
  （``:529``/``:614``）里赋值的 ``current`` → ``UnboundLocalError``，用户收到的是沉默；
* 群聊 ``generation.py:263-264`` 不给兜底 → 整轮失败、没有任何提示；
* 宿主（AstrBot 4.28.1 / 4.29.1 ``tool_loop_agent_runner.py`` 的
  ``modalities and "tool_use" not in modalities``）在同样情形下只是把本轮 ``tools``
  清空，仍然照常回复。

因此本文件锁定四组契约（全部走真实 ``LocalReplyGenerator`` + 真实 ``ModelRouter``
+ 真实 ``LocalModelGateway``，只有 provider 的 ``generate`` 是内存假实现）：

1. ``WithheldCatalogTests``：provider 没有 ``tools`` 能力 → 本轮不向 gateway 传目录，
   私聊/群聊都仍然发出真实回复；降级原因进 ``model_requests.plan_json`` 审计；
2. ``DeclaredToolUseTests``：provider 有 ``tools`` 能力 → 目录照旧、工具照旧执行，
   不得“顺手”削弱正常轮次；
3. ``ModalityReplyMatrixTests``：12 组宿主 ``modalities`` + 未知值，经
   ``host_capabilities()`` 推出能力后逐步跑真实 route/generation；
4. ``LocalFailureNoticeTests``：``open_session`` 失败时私聊兜底文案必须真的发出
   （不再是 ``UnboundLocalError``），且非 ``TaskError`` 仍然原样抛出（不做 blanket catch）。

测试全部离线：没有网络、没有真实模型、没有 QQ，也不碰任何宿主进程。

关于群聊：``ConversationMessages`` 的群装配还需要宿主侧的 group authority
（缺它会得到 ``group_task_audience_required`` / ``scope_not_granted``），那属于
``tests/test_group_participation.py`` 一类 host 套件的职责；而本轮修复的
“每轮工具门”在 ``generation``/``service`` 里都不按 scope 分支，所以下面的私聊
矩阵覆盖的是同一段代码。群聊在真实 multi-host 装配里的端到端结果见报告 §4。
"""

from __future__ import annotations

from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from companion_v2.adapters.astrbot.provider import REPLY_TASK_CAPABILITIES, host_capabilities
from companion_v2.conversation.generation import MODEL_FAILURE_NOTICE
from companion_v2.conversation.messages import REPLY_KIND
from companion_v2.conversation.tool_loop import LocalRoleTools
from companion_v2.execution.models import Lane, TaskResult, digest, encode
from companion_v2.execution.runner import TaskRun
from companion_v2.model_gateway.models import Amount, ModelDraft, ProviderResponse, ToolCall
from companion_v2.model_gateway.routing import ModelRouter
from companion_v2.model_gateway.service import LocalModelGateway
from tests.test_conversation_messages import MessageFixture


CONSUMER = "model_gateway"


def draft(text="我只用文字回复。"):
    return ProviderResponse("generated", ModelDraft(text, ()), Amount(15, 40), "reply-receipt")


def tool_round(*calls):
    return ProviderResponse("generated", usage=Amount(15, 40), receipt_ref="tool-receipt", tool_calls=calls)


class TextRuntimeFixture(MessageFixture):
    """真实消息/回复装配，provider 能力按宿主 ``modalities`` 推出。"""

    def setUp(self):
        super().setUp()
        # 主回复 profile 用生产常量构造：这正是 provider/staging/multi 修好之后
        # 的真实形状（"text" 必需，工具不进门）。这样本文件不会替那里写死一个
        # 已过时的 ("text", "tools")。
        self.reply_profile = replace(next(profile for profile in self.profiles if profile.kind == REPLY_KIND),
                                     capabilities=REPLY_TASK_CAPABILITIES)
        self.profiles = tuple(self.reply_profile if profile.kind == REPLY_KIND else profile
                              for profile in self.profiles)
        self.tool_owner = LocalRoleTools(self.messages)
        # 默认按宿主最常见的一种合法配置装配：只勾“文本”，没有 tool_use。
        self.stack(host_capabilities({"modalities": ["text"]}))

    # --- 真实模型栈 -------------------------------------------------------
    def stack(self, capabilities):
        """按给定 provider 能力重建真实 router/gateway（配置、registry、任务都不动）。"""
        self.capabilities = tuple(capabilities)
        self.providers = tuple(replace(provider, capabilities=self.capabilities)
                               for provider in self.providers)
        router = ModelRouter(self.configuration, self.profiles, self.providers)
        router.start(expected_generation=self.consumer_generation())
        gateway = LocalModelGateway(router, self.ledger, self.tasks)
        self.router, self.gateway = router, gateway
        self.generator.gateway = gateway
        return gateway

    def consumer_generation(self):
        return self.runtime.read_one(
            "SELECT generation FROM configuration_consumers WHERE consumer_id = ?", (CONSUMER,))["generation"]

    # --- 真实一轮 ---------------------------------------------------------
    def manual_run(self, *, context=None, identifier="input-1"):
        batch = self.accept(self.event(identifier), context=context)
        task = self.messages.seal(context or self.context, batch, deadline_at=self.deadline).unwrap()
        claim = self.tasks.claim(Lane.FOREGROUND).unwrap().claim
        self.tasks.begin(claim).unwrap()
        return batch, TaskRun(self.tasks, claim, task)

    def audit(self, task_id):
        row = self.runtime.read_one("SELECT plan_json FROM model_requests WHERE task_id = ?", (task_id,))
        self.assertIsNotNone(row, "the model plan must be recorded for this task")
        return json.loads(row["plan_json"])

    def offered_actions(self, envelope):
        return tuple(tool.action for tool in envelope.tools)

    def tool_rows(self, batch):
        return self.runtime.read_all(
            "SELECT * FROM conversation_tool_calls WHERE batch_id = ? ORDER BY sequence", (batch,))

    def settle(self, run, result=None):
        """Close the claimed task so a failing case cannot poison the next subTest."""
        self.tasks.finish(run.claim, result or TaskResult("failed")).unwrap()


class WithheldCatalogTests(TextRuntimeFixture, unittest.IsolatedAsyncioTestCase):
    """没有 ``tools`` 能力的 provider：本轮不给目录，但回复必须照常发出。"""

    def setUp(self):
        super().setUp()
        # host_capabilities({"modalities": ["text"]}) -> ("text",)
        self.assertEqual(self.capabilities, ("text",))
        # 真实工具目录：至少 source.read（与宿主装配一致，目录从来不是空的）。
        catalog, _ = self.messages.tools.catalog_for_with_diagnostics(self.context, None)
        self.assertIn("source.read", {tool.action for tool in catalog})

    async def test_private_reply_is_delivered_without_a_tool_catalog(self):
        self.responses["primary"] = draft("好，我直接说就好。")
        batch, run = self.manual_run()

        result = await self.generator.generate(run)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual([part["text"] for part in self.parts(batch)], ["好，我直接说就好。"])
        # 本轮真的没有把目录交给 gateway/模型。
        self.assertEqual(self.called[0][1].tools, ())
        audit = self.audit(run.task.task_id)
        self.assertEqual(audit["tool_actions"], [])
        # 目录里本来就会记录“这一轮没给哪些 action”（catalog_diagnostics），
        # 降级原因追加在末尾，便于观测面区分“授权过滤”和“模型没有工具能力”。
        self.assertEqual(audit["tool_filters"][-1], {"action": "*", "reason": "provider_without_tool_use"})
        self.assertEqual(self.tool_rows(batch), ())

    async def test_the_withheld_round_is_visible_in_the_plan_audit(self):
        self.responses["primary"] = draft()
        _, run = self.manual_run()
        await self.generator.generate(run)
        audit = self.audit(run.task.task_id)
        self.assertEqual(audit["tool_catalog_hash"], digest(()))
        self.assertEqual(audit["tool_actions"], [])
        self.assertEqual([item["reason"] for item in audit["tool_filters"]][-1:], ["provider_without_tool_use"])


class DeclaredToolUseTests(TextRuntimeFixture, unittest.IsolatedAsyncioTestCase):
    """有 ``tools`` 能力：目录与工具执行都必须保持原样（不得静默削弱）。"""

    def setUp(self):
        super().setUp()
        self.stack(host_capabilities({"modalities": ["text", "tool_use"]}))
        self.assertEqual(self.capabilities, ("text", "tools"))

    async def test_a_declared_tool_round_still_offers_the_catalog_and_runs_the_tool(self):
        async def respond(envelope):
            if not envelope.tool_history:
                return tool_round(ToolCall("save-1", "memo.create",
                                           encode({"title": "Runtime", "body": "Catalog kept"})))
            return draft("记下来了。")

        self.responses["primary"] = respond
        batch, run = self.manual_run()

        result = await self.generator.generate(run)

        self.assertEqual(result.status, "succeeded")
        actions = self.offered_actions(self.called[0][1])
        self.assertIn("source.read", actions)
        self.assertIn("memo.create", actions)
        self.assertEqual([row["action"] for row in self.tool_rows(batch)], ["memo.create"])
        self.assertEqual(self.parts(batch)[0]["text"], "记下来了。")
        self.assertEqual(self.audit(run.task.task_id)["tool_actions"], list(actions))
        self.assertNotIn("provider_without_tool_use",
                         [item.get("reason") for item in self.audit(run.task.task_id)["tool_filters"]])

    async def test_a_text_only_round_of_a_tool_capable_provider_keeps_the_catalog(self):
        self.responses["primary"] = draft("这轮不需要工具。")
        batch, run = self.manual_run()
        await self.generator.generate(run)
        self.assertIn("source.read", self.offered_actions(self.called[0][1]))
        self.assertEqual(self.tool_rows(batch), ())
        self.assertEqual(self.parts(batch)[0]["text"], "这轮不需要工具。")


#: 12 组宿主 ``modalities``（AstrBot 面板的四种可勾选项两两/三三组合里合法的 12 组）。
MODALITY_MATRIX = (
    ("text",),
    ("text", "image"),
    ("text", "audio"),
    ("text", "tool_use"),
    ("text", "image", "audio"),
    ("text", "image", "tool_use"),
    ("text", "audio", "tool_use"),
    ("text", "image", "audio", "tool_use"),
    ("image",),
    ("image", "tool_use"),
    ("audio",),
    ("audio", "tool_use"),
)

#: 宿主未知/非列表值：只可能来自手改配置或宿主未来新增模态。
UNKNOWN_MODALITY_SHAPES = (
    (["video"], ("text",)),
    (["text", "video"], ("text",)),
    (["video", "image"], ("text", "image")),
    ([1, None, "text"], ("text",)),
    (["TEXT"], ("text",)),
    ("image", ("text", "tools", "image", "audio")),
    ([], ("text", "tools", "image", "audio")),
)


class ModalityReplyMatrixTests(TextRuntimeFixture, unittest.IsolatedAsyncioTestCase):
    """宿主 ``modalities`` → 能力 → 真实 route/generation 的主回复端到端矩阵。"""

    async def test_twelve_host_modality_shapes_each_produce_a_reply(self):
        for index, modalities in enumerate(MODALITY_MATRIX):
            with self.subTest(modalities=modalities):
                capabilities = host_capabilities({"modalities": list(modalities)})
                self.assertIn("text", capabilities)
                self.stack(capabilities)
                self.responses["primary"] = draft("按 %s 配置回复。" % ",".join(modalities))
                batch, run = self.manual_run(identifier="modality-%d" % index)

                try:
                    result = await self.generator.generate(run)
                except BaseException:
                    self.settle(run)
                    raise

                self.assertEqual(result.status, "succeeded")
                self.assertEqual(self.parts(batch)[0]["text"], "按 %s 配置回复。" % ",".join(modalities))
                offered = self.offered_actions(self.called[-1][1])
                if "tools" in capabilities:
                    self.assertIn("source.read", offered)
                else:
                    self.assertEqual(offered, ())
                self.assertEqual(self.tool_rows(batch), ())
                self.settle(run, result)

    async def test_unknown_modality_values_never_prevent_a_reply(self):
        for index, (modalities, expected) in enumerate(UNKNOWN_MODALITY_SHAPES):
            with self.subTest(modalities=modalities):
                capabilities = host_capabilities({"modalities": modalities})
                self.assertEqual(capabilities, expected)
                self.stack(capabilities)
                self.responses["primary"] = draft("未知模态也能回复 %d。" % index)
                batch, run = self.manual_run(identifier="unknown-%d" % index)

                try:
                    result = await self.generator.generate(run)
                except BaseException:
                    self.settle(run)
                    raise

                self.assertEqual(result.status, "succeeded")
                self.assertEqual(self.parts(batch)[0]["text"], "未知模态也能回复 %d。" % index)
                self.settle(run, result)


class LocalFailureNoticeTests(TextRuntimeFixture, unittest.IsolatedAsyncioTestCase):
    """``open_session`` 失败：私聊兜底必须真的发出去，且不做 blanket catch。"""

    async def test_private_notice_is_delivered_when_the_provider_cannot_start_the_round(self):
        # 没有 text 能力的 provider：主回复 profile 在 route 阶段就拒绝这一轮。
        self.stack(("image",))
        batch, run = self.manual_run()

        result = await self.generator.generate(run)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual([part["text"] for part in self.parts(batch)], [MODEL_FAILURE_NOTICE])
        self.assertEqual(self.called, [])
        self.assertEqual(self.runtime.read_all("SELECT * FROM model_calls", ()), ())
        saved = self.messages.batch(batch)
        self.assertIsNone(saved["call_id"])
        self.assertEqual(saved["part_count"], 1)

    async def test_an_unexpected_gateway_failure_is_not_hidden(self):
        self.responses["primary"] = draft()
        batch, run = self.manual_run()

        def broken(*_args, **_kwargs):
            raise ValueError("synthetic broken gateway")

        with patch.object(self.gateway, "open_session", broken):
            with self.assertRaises(ValueError):
                await self.generator.generate(run)
        self.assertEqual(self.parts(batch), ())


class ServiceDegradationTests(TextRuntimeFixture, unittest.IsolatedAsyncioTestCase):
    """service 侧兜底：目录到了无工具能力的 provider 手上时，记录并丢弃而不是失败。"""

    def setUp(self):
        super().setUp()
        self.stack(("text",))
        self.catalog, self.filters = self.messages.tools.catalog_for_with_diagnostics(self.context, None)
        self.assertTrue(self.catalog)

    def prepare(self):
        batch = self.accept(self.event("service-input"))
        self.messages.seal(self.context, batch, deadline_at=self.deadline).unwrap()
        return batch

    async def open(self, batch):
        claim = self.tasks.claim(Lane.FOREGROUND).unwrap().claim
        self.tasks.begin(claim).unwrap()
        task = self.tasks.read(self.context, self.messages.batch(batch)["generation_task_id"]).unwrap()
        run = TaskRun(self.tasks, claim, task)
        request = self.messages.request(run)
        session = self.gateway.open_session(run, request, tools=self.catalog, tool_filters=self.filters)
        return run, request, session

    async def test_a_catalog_that_cannot_be_offered_is_recorded_and_dropped(self):
        batch = self.prepare()
        run, request, session = await self.open(batch)

        completion = await session.generate(request)

        # 该 provider 走的是 fixture 的默认假回复；重点是“没有失败、也没有工具轮”。
        self.assertEqual(completion.draft.text, "First part.\nSecond part.")
        self.assertEqual(self.called[0][1].tools, ())
        self.assertEqual(completion.tool_calls, ())
        audit = self.audit(run.task.task_id)
        self.assertEqual(audit["tool_actions"], [])
        self.assertEqual(audit["tool_filters"][-1], {"action": "*", "reason": "provider_without_tool_use"})

    async def test_a_tool_capable_provider_keeps_the_offered_catalog(self):
        self.stack(("text", "tools"))
        batch = self.prepare()
        run, request, session = await self.open(batch)

        await session.generate(request)

        self.assertEqual(self.offered_actions(self.called[0][1]), tuple(tool.action for tool in self.catalog))
        self.assertEqual(self.audit(run.task.task_id)["tool_actions"],
                         [tool.action for tool in self.catalog])


if __name__ == "__main__":
    unittest.main()
