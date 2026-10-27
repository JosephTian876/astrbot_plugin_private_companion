"""Round 1 regression: a private refusal belongs to its own event, never a shared slot.

Every event here carries the same synthetic participant on two independently
configured Bot accounts.  Platform clients are in-memory doubles, so these tests
prove routing and context ownership without any real delivery.
"""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

from companion_v2.foundation.outcome import Failure
from tests.multi_host_support import MultiFixture, multi_config


class _TargetNotice:
    """Per-account refusal copy and cooldown, mirroring the real settings reader."""

    def __init__(self, values):
        self.values = values

    def defaults(self, context):
        return dict(self.values[context.scope.account_id])


class PrivateRefusalRoutingTests(MultiFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.controller = self.make_controller(multi_config(delivery_mode="onebot_private"))
        for platform in self.native.platforms.values():
            platform.client._api._wsr_api.calls.clear()

    def event(self, target="first", identifier="event-1", text="在吗"):
        configured = self.controller.settings.target(target)
        event = MultiFixture.event(self, target=target, identifier=identifier, text=text)
        login, peer = int(configured.account_id), int(configured.participant_id)
        # A production private event carries the raw OneBot identity used by the
        # notice route authenticator; derive it from this target, never the first.
        event.message_obj.raw_message = {
            "post_type": "message", "message_type": "private", "sub_type": "friend",
            "self_id": login, "user_id": peer, "message_id": identifier,
            "sender": {"user_id": peer},
            "message": [{"type": "text", "data": {"text": text}}],
        }
        return event

    def records(self):
        calls = []
        for platform in self.native.platforms.values():
            calls.extend(platform.client._api._wsr_api.calls)
        return calls

    def sent_private(self):
        sent = []
        for action, params in self.records():
            if action != "send_private_msg":
                continue
            text = "".join(part.get("data", {}).get("text", "") for part in params.get("message", ())
                           if part.get("type") == "text")
            sent.append((params.get("self_id"), params.get("user_id"), text))
        return sent

    def use_notices(self, app, values):
        app.private_notice.settings = _TargetNotice(values)

    def revoke_dialogue(self, target_id):
        context = self.controller.authority.context(target_id, "role-a")
        identities = self.controller.authority.identities
        identities.revoke_grant(
            self.controller.authority.grant_id(context.scope, "conversation.reply"),
            revoked_at=self.clock.now(), expected_revision=identities.revision)
        return context

    async def denied(self, target, identifier):
        event = self.event(target, identifier)
        result = await self.controller.prepare_core_reply(event)
        self.assertFalse(result.is_success)
        self.assertEqual(result.failure.code, "private_access_dialogue_denied")
        self.assertIsNotNone(result.refusal)
        return event, result

    async def test_interleaved_refusals_keep_each_bots_context_and_send_both(self):
        app = await self.start_host()
        self.use_notices(app, {
            "100": {"denied_reply": "甲", "notice_cooldown_seconds": 0},
            "101": {"denied_reply": "乙", "notice_cooldown_seconds": 0},
        })
        self.revoke_dialogue("first")
        self.revoke_dialogue("second")

        event_a, first = await self.denied("first", "interleave-a")
        event_b, second = await self.denied("second", "interleave-b")
        self.assertEqual(first.refusal.scope.account_id, "100")
        self.assertEqual(second.refusal.scope.account_id, "101")
        self.assertIsNot(first.refusal, second.refusal)
        self.assertFalse(hasattr(self.controller.session, "last_refusal"),
                         "the multi-host session must not carry a shared refusal slot")

        # Complete B first, then A, exactly the interleaving that used to lose A.
        receipt_b = await self.controller.refuse_core_reply(event_b, second.refusal)
        receipt_a = await self.controller.refuse_core_reply(event_a, first.refusal)

        self.assertEqual(receipt_b["state"], "delivered")
        self.assertEqual(receipt_a["state"], "delivered")
        self.assertEqual(sorted(self.sent_private()), [("100", 200, "甲"), ("101", 200, "乙")])
        self.assertIsNotNone(app.private_notice.document(first.refusal))
        self.assertIsNotNone(app.private_notice.document(second.refusal))
        self.assertEqual(app.runtime.read_all("SELECT batch_id FROM conversation_batches", ()), ())
        self.assertEqual(self.native.provider.client.state.calls, [])

    async def test_each_target_keeps_its_own_line_and_cooldown(self):
        app = await self.start_host()
        self.use_notices(app, {
            "100": {"denied_reply": "甲", "notice_cooldown_seconds": 0},
            "101": {"denied_reply": "乙", "notice_cooldown_seconds": 3600},
        })
        self.revoke_dialogue("first")
        self.revoke_dialogue("second")

        for index in range(2):
            event_a, first = await self.denied("first", "cooldown-a-" + str(index))
            await self.controller.refuse_core_reply(event_a, first.refusal)
            event_b, second = await self.denied("second", "cooldown-b-" + str(index))
            await self.controller.refuse_core_reply(event_b, second.refusal)

        sent = self.sent_private()
        self.assertEqual([text for login, _user, text in sent if login == "100"], ["甲", "甲"])
        self.assertEqual([text for login, _user, text in sent if login == "101"], ["乙"])
        self.assertNotIn("乙", [text for login, _user, text in sent if login == "100"])
        self.assertNotIn("甲", [text for login, _user, text in sent if login == "101"])

    async def test_prepare_core_reply_context_returns_event_bound_refusal(self):
        app = await self.start_host()
        self.use_notices(app, {
            "100": {"denied_reply": "甲", "notice_cooldown_seconds": 0},
            "101": {"denied_reply": "乙", "notice_cooldown_seconds": 0},
        })
        self.revoke_dialogue("first")
        self.revoke_dialogue("second")

        event_a = self.event("first", "context-a")
        first = await self.controller.prepare_core_reply_context(event_a)
        event_b = self.event("second", "context-b")
        second = await self.controller.prepare_core_reply_context(event_b)

        self.assertEqual(first.failure.code, "private_access_dialogue_denied")
        self.assertEqual(second.failure.code, "private_access_dialogue_denied")
        self.assertEqual(first.refusal.scope.account_id, "100")
        self.assertEqual(second.refusal.scope.account_id, "101")
        self.assertFalse(hasattr(self.controller.session, "last_refusal"))

        # Preparing B must not steal A's line, even though A is refused afterwards.
        await self.controller.refuse_core_reply(event_a, first.refusal)
        self.assertEqual(self.sent_private(), [("100", 200, "甲")])

    async def test_concurrent_receive_claimed_does_not_lose_either_refusal(self):
        app = await self.start_host()
        self.use_notices(app, {
            "100": {"denied_reply": "甲", "notice_cooldown_seconds": 0},
            "101": {"denied_reply": "乙", "notice_cooldown_seconds": 0},
        })
        self.revoke_dialogue("first")
        self.revoke_dialogue("second")
        event_a = self.event("first", "receive-a")
        event_b = self.event("second", "receive-b")

        original = self.controller._refuse_private
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(session, event, *args, **kwargs):
            if event is event_a:
                entered.set()
                await release.wait()
            return await original(session, event, *args, **kwargs)

        self.controller._refuse_private = delayed
        try:
            task_a = asyncio.create_task(self.controller.receive_claimed(event_a))
            await asyncio.wait_for(entered.wait(), 5)
            result_b = await self.controller.receive_claimed(event_b)
            release.set()
            result_a = await asyncio.wait_for(task_a, 5)
        finally:
            release.set()
            self.controller._refuse_private = original

        self.assertEqual(result_a.failure.code, "private_access_dialogue_denied")
        self.assertEqual(result_b.failure.code, "private_access_dialogue_denied")
        self.assertEqual(sorted(self.sent_private()), [("100", 200, "甲"), ("101", 200, "乙")])
        self.assertIsNotNone(app.private_notice.document(
            self.controller.authority.context("first", "role-a")))
        self.assertIsNotNone(app.private_notice.document(
            self.controller.authority.context("second", "role-a")))

    async def test_refusal_context_is_not_reused_for_another_events_route(self):
        app = await self.start_host()
        self.use_notices(app, {
            "100": {"denied_reply": "甲", "notice_cooldown_seconds": 0},
            "101": {"denied_reply": "乙", "notice_cooldown_seconds": 0},
        })
        self.revoke_dialogue("first")
        self.revoke_dialogue("second")
        event_a, first = await self.denied("first", "wrong-context-a")
        _event_b, second = await self.denied("second", "wrong-context-b")

        self.assertIsNone(await self.controller.refuse_core_reply(event_a, second.refusal))
        self.assertIsNone(await self.controller.refuse_core_reply(event_a, None))
        self.assertEqual(self.sent_private(), [])
        self.assertIsNone(app.private_notice.document(first.refusal))
        self.assertIsNone(app.private_notice.document(second.refusal))

        receipt = await self.controller.refuse_core_reply(event_a, first.refusal)
        self.assertEqual(receipt["state"], "delivered")
        self.assertEqual(self.sent_private(), [("100", 200, "甲")])


class MainRefusalWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_forwards_the_failed_preparations_explicit_refusal_context(self):
        from companion_v2.storage.runtime import StorageRuntime
        from tests.test_main_no_reply import ENTRY
        from tests.test_management_access import INSTALLATION, NOW

        temporary = TemporaryDirectory(prefix="companion-r1-refusal-entry-")
        self.addCleanup(temporary.cleanup)
        runtime = StorageRuntime.open(data_root=Path(temporary.name), installation_id=INSTALLATION)
        self.addCleanup(runtime.close)
        plugin = ENTRY.PrivateCompanionV2StagingPlugin.__new__(ENTRY.PrivateCompanionV2StagingPlugin)
        target = NS(key="configured-role", target_id="first")
        refusal = object()
        staging = NS(
            runtime=runtime,
            now=lambda: NOW,
            claims=lambda _event: True,
            target_for_event=lambda _event: target,
            settings=NS(enabled=True),
            settings_error=None,
            _accepting=True,
            _terminated=False,
            session=NS(profile=NS(target=target)),
            session_topology=NS(reply_enabled=AsyncMock(return_value=True),
                                schedule_observe=lambda event: None),
            persona_mapping_for_event=AsyncMock(return_value=NS(role_id="registered-role")),
            prepare_core_reply=AsyncMock(return_value=NS(
                failure=Failure("private_access_dialogue_denied", "denied"), refusal=refusal)),
            refuse_core_reply=AsyncMock(return_value={"state": "delivered"}),
        )
        plugin.staging = staging
        event = NS(
            platform_meta=NS(name="aiocqhttp"),
            message_obj=NS(type=NS(value="FriendMessage"), raw_message={"post_type": "message"}),
            stopped=False,
        )
        event.stop_event = lambda: setattr(event, "stopped", True)

        await plugin.receive_companion_message(event)

        staging.refuse_core_reply.assert_awaited_once_with(event, refusal)
        self.assertTrue(event.call_llm)


if __name__ == "__main__":
    unittest.main()
