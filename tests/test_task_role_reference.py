"""The self page is the role reference for background tasks, not conversation."""

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
import unittest

from companion_v2.model_gateway.service import LocalModelGateway, TASK_ROLE_REFERENCE_KINDS


class TaskRoleReferenceTests(unittest.TestCase):
    @dataclass(frozen=True)
    class Request:
        role_instruction: str
        instruction: str = "本轮任务"

    def request(self):
        return self.Request(role_instruction="AstrBot 人格原文")

    def gateway(self, reference):
        gateway = object.__new__(LocalModelGateway)
        gateway.role_reference = SimpleNamespace(instruction=lambda context, at: reference)
        gateway.tasks = SimpleNamespace(now=lambda: 0)
        return gateway

    def make_run(self, kind, context=None):
        return SimpleNamespace(task=SimpleNamespace(spec=SimpleNamespace(kind=kind, context=context or object())))

    def test_background_task_replaces_persona_with_the_self_page(self):
        gateway = self.gateway("【角色稳定档案】\n- 姓名：沈知夏")
        request = gateway._task_role_request(self.make_run("role.journal.diary"), self.request())
        self.assertEqual(request.role_instruction, "【角色稳定档案】\n- 姓名：沈知夏")
        self.assertNotIn("AstrBot 人格原文", request.role_instruction)

    def test_empty_self_page_falls_back_to_captured_persona(self):
        gateway = self.gateway("")
        request = gateway._task_role_request(self.make_run("calendar.plan"), self.request())
        self.assertEqual(request.role_instruction, "AstrBot 人格原文")

    def test_conversation_and_image_safety_keep_their_original_instruction(self):
        gateway = self.gateway("【角色稳定档案】")
        for kind in ("conversation.reply", "conversation.image_review"):
            with self.subTest(kind=kind):
                request = gateway._task_role_request(self.make_run(kind), self.request())
                self.assertEqual(request.role_instruction, "AstrBot 人格原文")

    def test_background_allowlist_does_not_include_conversation(self):
        self.assertIn("conversation.poke_reply", TASK_ROLE_REFERENCE_KINDS)
        self.assertIn("role.journal.diary", TASK_ROLE_REFERENCE_KINDS)
        self.assertNotIn("conversation.reply", TASK_ROLE_REFERENCE_KINDS)
        self.assertNotIn("conversation.image_review", TASK_ROLE_REFERENCE_KINDS)



class GatewayGenerateBoundaryTests(unittest.TestCase):
    @dataclass(frozen=True)
    class Request:
        role_instruction: str

    def test_generate_continues_with_the_request_frozen_into_the_plan(self):
        captured = []

        class Session:
            def __init__(self, original):
                self.plan = SimpleNamespace(envelope=SimpleNamespace(original=original))

            async def generate(self, request):
                captured.append(request)
                return "completion"

        original = self.Request("captured persona")
        planned = self.Request("self page role reference")
        session = Session(planned)
        gateway = object.__new__(LocalModelGateway)
        gateway.open_session = lambda run, request: session

        result = asyncio.run(gateway.generate(object(), original))

        self.assertEqual(result, "completion")
        self.assertEqual(captured, [planned])
        self.assertIs(captured[0], session.plan.envelope.original)


if __name__ == "__main__":
    unittest.main()
