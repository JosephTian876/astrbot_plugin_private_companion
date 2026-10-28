"""A reply retained across reload must use its current owner's result contract."""

from contextlib import contextmanager, ExitStack
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from astrbot_host import core_reply
from companion_v2.conversation.voice_reply import VoiceReplyPolicy
from companion_v2.execution.models import TaskContext, TaskError, encode
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import AudienceRef, ScopeKind, ScopeRef, SubjectKind, SubjectRef
from companion_v2.identity_policy.models import AuthenticatedHostPrincipal, ResolvedIdentity, RoleBinding
from tests.test_astrbot_core_reply import FakeEvent


@contextmanager
def reloaded_reply_module():
    """Load fresh classes without disrupting the other tests' live import graph."""
    root = Path(__file__).resolve().parents[1]
    with ExitStack() as stack:
        for name in ("companion_v2.foundation.outcome", "companion_v2.execution.models",
                     "companion_v2.conversation.voice_reply"):
            spec = importlib.util.spec_from_file_location(name, root / (name.replace(".", "/") + ".py"))
            module = importlib.util.module_from_spec(spec)
            stack.enter_context(patch.dict(sys.modules, {name: module}))
            spec.loader.exec_module(module)
        name = "astrbot_host._core_reply_reload_fixture"
        spec = importlib.util.spec_from_file_location(name, root / "astrbot_host/core_reply.py")
        reply_module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {name: reply_module}):
            spec.loader.exec_module(reply_module)
            yield reply_module


class CoreReplyReloadTests(unittest.IsolatedAsyncioTestCase):
    def prepare(self):
        event = FakeEvent()
        core_reply.prepare_core_reply_event(event, {"channel": "private"}, owner=object(),
            authorize=AsyncMock(return_value=Outcome.success(True)))
        core_reply.activate_core_reply_delivery_guard(event)
        return event

    async def test_reloaded_owner_can_authorize_the_existing_send_wrapper(self):
        event = self.prepare()
        original_send = event.send
        with reloaded_reply_module() as current:
            self.assertIsNot(current.Outcome, Outcome)
            authorize = AsyncMock(return_value=current.Outcome.success(True))
            self.assertTrue(current.adopt_core_reply_event(event, owner=object(), authorize=authorize))
            self.assertIs(event.send, original_send)
            await event.send("重载后继续这轮回复")
        authorize.assert_awaited_once()
        self.assertEqual(event.sent, [(("重载后继续这轮回复",), {})])

    async def test_reloaded_owner_denial_keeps_its_reason_and_never_sends(self):
        event = self.prepare()
        with reloaded_reply_module() as current:
            authorize = AsyncMock(return_value=current.Outcome.failed("permission_revoked", "revoked"))
            current.adopt_core_reply_event(event, owner=object(), authorize=authorize)
            with self.assertRaises(current.TaskError) as raised:
                await event.send("不得发送")
        self.assertEqual(raised.exception.code, "permission_revoked")
        self.assertEqual(event.sent, [])

    async def test_reload_restores_captured_identity_and_voice_without_reselecting(self):
        scope = ScopeRef("reload-test", "qq", ScopeKind.PRIVATE, "200", "bot", "100", "conversation", "200")
        user = SubjectRef(scope.installation_id, SubjectKind.HUMAN, "user")
        role = SubjectRef(scope.installation_id, SubjectKind.BOT, "role")
        actor = ResolvedIdentity(AuthenticatedHostPrincipal(scope.installation_id, "host", "qq", "200", "bot", "100"),
                                 user, "binding", 3, 5)
        context = TaskContext(actor, RoleBinding(scope, role, 2), AudienceRef(scope, (user,)))
        policy = VoiceReplyPolicy(language="ja", segment_mode="each")
        prepared = {"context": context, "voice_reply_policy": policy, "configured_voice_reply_policy": policy,
                    "instruction": "本轮已捕获的人格", "event_id": "original-turn"}
        event = self.prepare()
        with reloaded_reply_module() as current:
            def authorize(restored):
                self.assertIsInstance(restored["context"], current.TaskContext)
                self.assertIsNot(restored["context"], context)
                self.assertEqual(restored["context"].key, context.key)
                self.assertEqual(encode(restored), encode(prepared))
                self.assertIsNot(type(restored["voice_reply_policy"]), type(policy))
                self.assertEqual(restored["voice_reply_policy"].language, "ja")
                return current.Outcome.success(True)

            current.adopt_core_reply_event(event, owner=object(), prepared=prepared,
                                           authorize=AsyncMock(side_effect=authorize))
            await event.get_extra(core_reply.CORE_REPLY_CONTEXT_EXTRA)["_authorize_delivery"]()

    async def test_reloaded_owner_cannot_accept_an_untyped_authorization(self):
        event = self.prepare()
        with reloaded_reply_module() as current:
            authorize = AsyncMock(return_value=SimpleNamespace(value=True, failure=None))
            current.adopt_core_reply_event(event, owner=object(), authorize=authorize)
            with self.assertRaisesRegex(TypeError, "must return an Outcome"):
                await event.send("不得发送")
        self.assertEqual(event.sent, [])

    async def test_reload_does_not_replay_an_unknown_platform_send(self):
        event = self.prepare()
        state = event.get_extra(core_reply.CORE_REPLY_CONTEXT_EXTRA)
        state["send_attempts"], state["send_unknown"] = 1, 1
        with reloaded_reply_module() as current:
            authorize = AsyncMock(return_value=current.Outcome.success(True))
            current.adopt_core_reply_event(event, owner=object(), authorize=authorize)
            with self.assertRaises(TaskError) as raised:
                await event.send("不得重发")
        self.assertEqual(raised.exception.code, "core_reply_delivery_unknown")
        authorize.assert_not_awaited()
        self.assertEqual(event.sent, [])
