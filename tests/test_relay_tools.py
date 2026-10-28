from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from astrbot_host.private_notice import send_group_notice
from astrbot_host.relay_pending import claim_group, claim_private_receipts
from astrbot_host.relay_tools import RelayToolPort
from companion_v2.conversation.external_tools import ExternalToolInvocation
from companion_v2.execution.models import TaskError, encode
from companion_v2.foundation.refs import AudienceRef, ScopeKind, ScopeRef, SubjectKind, SubjectRef
from companion_v2.identity_policy.models import AuthenticatedHostPrincipal, ResolvedIdentity, RoleBinding
from companion_v2.storage.repositories import OperationRepository
from companion_v2.storage.runtime import StorageRuntime
from companion_v2.conversation.relay_tools import SEND_ACTION, tool_descriptors


NOW = datetime(2026, 10, 19, 4, 0, tzinfo=timezone.utc)
SETTINGS = {"enabled": True, "require_confirm": True, "max_text_chars": 500,
            "core_target_id": "1001", "allowed_targets": ["2002", "2003"],
            "multi_target_limit": 5, "default_style": "persona",
            "allowed_groups": ["5005"]}


class FakeReverseApi:
    def __init__(self, response=None, by_action=None):
        self.response = response if response is not None else {"message_id": 789}
        self.by_action = dict(by_action or {})
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action in self.by_action:
            return self.by_action[action]
        return self.response


class RelayToolPortTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory(prefix="companion-v2-relay-")
        self.addCleanup(self.temporary.cleanup)
        self.runtime = StorageRuntime.open(data_root=Path(self.temporary.name) / "v2-data",
                                           installation_id="installation-a")
        self.addCleanup(self.runtime.close)
        scope = ScopeRef("installation-a", "onebot", ScopeKind.PRIVATE, "1001",
                         "bot-a", "account-a", "conversation-1001", "session-1001")
        subject = SubjectRef("installation-a", SubjectKind.HUMAN, "owner")
        role = SubjectRef("installation-a", SubjectKind.BOT, "companion")
        principal = AuthenticatedHostPrincipal("installation-a", "astrbot", "onebot", "1001",
                                               "bot-a", "account-a")
        actor = ResolvedIdentity(principal, subject, "binding-a", 1, 1)
        self.context = SimpleNamespace(
            scope=scope,
            actor=actor,
            role_binding=RoleBinding(scope, role, 1),
            audience=AudienceRef(scope, (subject,)),
            key="relay-test-context",
        )
        self.api = FakeReverseApi()
        client = SimpleNamespace(_api=SimpleNamespace(_wsr_api=self.api),
                                 _wsr_api_clients={"account-a": self.api})
        bridge = SimpleNamespace(target_for_scope=lambda _scope: None,
                                 client=lambda _scope: client)
        self.port = RelayToolPort(bridge, native_types=SimpleNamespace(reverse_api=FakeReverseApi))
        self.port.attach(SimpleNamespace(
            state="running", runtime=self.runtime,
            relay_settings=SimpleNamespace(read=lambda _context, _at: dict(SETTINGS)),
            now=lambda: NOW,
            messages=SimpleNamespace(group_network=SimpleNamespace(read_owner=lambda _owner: None)),
        ))
        self.current_input = {"event_id": "proposal-event", "observed_at": NOW.isoformat()}
        self.current_text = "帮我转告：今晚不去了"
        self.port._input = lambda _invocation: (self.current_input, self.current_text)
        self.text = "今晚不去了"
        self.operation_id = None

    def invocation(self, action, arguments, identifier):
        descriptor = next(item for item in tool_descriptors() if item.tool.action == action)
        return ExternalToolInvocation(identifier, "core-reply:relay-test", "call-" + identifier,
                                      self.context, descriptor, encode(arguments), NOW + timedelta(minutes=2))

    def propose(self):
        invocation = self.invocation("relay_message", {"destination_id": "2002", "text": self.text}, "proposal-1")
        result = self.port._propose(invocation, json.loads(invocation.arguments_json))
        self.operation_id = json.loads(result.source.payload)["operation_id"]
        self.assertEqual(json.loads(result.source.payload)["state"], "awaiting_confirmation")
        self.assertEqual(self.api.calls, [])

    async def send(self):
        arguments = {"operation_id": self.operation_id, "destination_id": "2002",
                     "text": self.text, "confirmed": True}
        invocation = self.invocation(SEND_ACTION, arguments, "send-1")
        return await self.port._send(invocation, arguments)

    async def test_confirmation_must_be_a_later_message_and_delivery_receipt_is_audited(self):
        self.propose()
        same_turn = await self.send()
        self.assertEqual(same_turn.error_code, "relay_confirmation_required")
        self.assertEqual(self.api.calls, [])

        self.current_input = {"event_id": "confirmation-event",
                              "observed_at": (NOW + timedelta(seconds=1)).isoformat()}
        self.current_text = "确认发送"
        sent = await self.send()
        self.assertEqual(sent.state, "succeeded")
        self.assertEqual(self.api.calls[0][1]["user_id"], 2002)
        self.assertEqual(self.api.calls[0][1]["self_id"], "account-a")
        outbox = OperationRepository(self.runtime).outbox(
            self.operation_id, "private:2002").unwrap()
        self.assertEqual((outbox.state, outbox.receipt_ref),
                         ("accepted", "onebot-message:789"))

        replay = await self.send()
        self.assertEqual(replay.receipt_ref, "onebot-message:789")
        self.assertEqual(len(self.api.calls), 1)

    async def test_group_name_resolves_inside_configured_whitelist(self):
        self.api.by_action["get_group_list"] = [
            {"group_id": "5005", "group_name": "家人群"},
            {"group_id": "9999", "group_name": "陌生群"},
        ]
        invocation = self.invocation("relay_message", {
            "destination_kind": "group", "destination_id": "家人群",
            "text": self.text}, "group-name")
        arguments = await self.port._prepare_propose_mention(
            invocation, json.loads(invocation.arguments_json))
        self.assertEqual(arguments["destination_id"], "5005")
        self.assertEqual(self.api.calls[0][0], "get_group_list")
        self.api.calls.clear()
        await self.port._prepare_propose_mention(
            invocation, json.loads(invocation.arguments_json))
        self.assertEqual(self.api.calls, [])

    async def test_group_member_alias_falls_back_to_relationship_names(self):
        self.api.by_action["get_group_member_list"] = [
            {"user_id": "7777", "nickname": "现在的新名片", "card": ""},
        ]
        self.port.application.messages.group_network.read_owner = lambda _owner: {
            "members": {"7777": {"aliases": [{"value": "以前的群名片"}]}}
        }
        invocation = self.invocation("relay_message", {
            "destination_kind": "group", "destination_id": "5005",
            "text": self.text, "mention": "以前的群名片"}, "group-alias")
        arguments = await self.port._prepare_propose_mention(
            invocation, json.loads(invocation.arguments_json))
        self.assertEqual(arguments["mention"], "7777")
        self.api.calls.clear()
        await self.port._prepare_propose_mention(
            invocation, json.loads(invocation.arguments_json))
        self.assertEqual(self.api.calls, [])

    async def test_group_relay_requires_confirmation_and_sends_at_mention(self):
        invocation = self.invocation("relay_message", {
            "destination_kind": "group", "destination_id": "5005",
            "text": self.text, "mention": "7777"}, "group-proposal")
        prepared = self.port._propose(invocation, json.loads(invocation.arguments_json))
        payload = json.loads(prepared.source.payload)
        self.assertEqual(payload["state"], "awaiting_confirmation")
        self.assertEqual(payload["destination_kind"], "group")
        self.assertEqual(payload["mention_id"], "7777")
        operation_id = payload["operation_id"]
        self.assertEqual(self.api.calls, [])

        self.current_input = {"event_id": "group-confirmation",
                              "observed_at": (NOW + timedelta(seconds=1)).isoformat()}
        self.current_text = "确认发送"
        arguments = {"operation_id": operation_id, "destination_kind": "group",
                     "destination_id": "5005", "text": self.text,
                     "mention": "7777", "confirmed": True}
        send = self.invocation(SEND_ACTION, arguments, "group-send")
        sent = await self.port._send(send, arguments)
        self.assertEqual(sent.state, "succeeded")
        action, kwargs = self.api.calls[0]
        self.assertEqual(action, "send_group_msg")
        self.assertEqual(kwargs["group_id"], 5005)
        self.assertEqual(kwargs["message"][0], {"type": "at", "data": {"qq": "7777"}})
        self.assertEqual(kwargs["message"][-1], {"type": "text", "data": {"text": self.text}})
        outbox = OperationRepository(self.runtime).outbox(operation_id, "group:5005").unwrap()
        self.assertEqual(outbox.state, "accepted")

    async def test_multi_target_limit_is_enforced(self):
        invocation = self.invocation("relay_message_many", {
            "destination_kind": "private",
            "destination_ids": "2002,2003,2004,2005,2006,2007",
            "text": self.text}, "multi-over-limit")
        with self.assertRaises(TaskError) as caught:
            await self.port._prepare_propose_many(invocation, json.loads(invocation.arguments_json))
        self.assertEqual(caught.exception.code, "relay_targets_limit")

    async def test_style_is_part_of_the_pending_operation(self):
        invocation = self.invocation("relay_message", {
            "destination_id": "2002", "text": "委婉一点的内容", "style": "soft"},
            "style-proposal")
        prepared = self.port._propose(invocation, json.loads(invocation.arguments_json))
        payload = json.loads(prepared.source.payload)
        self.assertEqual(payload["style"], "soft")
        self.current_input = {"event_id": "style-confirmation",
                              "observed_at": (NOW + timedelta(seconds=1)).isoformat()}
        self.current_text = "确认发送"
        arguments = {"operation_id": payload["operation_id"], "destination_id": "2002",
                     "text": "委婉一点的内容", "style": "persona", "confirmed": True}
        with self.assertRaises(TaskError):
            await self.port._send(self.invocation(SEND_ACTION, arguments, "style-mismatch"), arguments)

    async def test_delay_until_seen_schedules_without_sending(self):
        invocation = self.invocation("relay_message", {
            "destination_kind": "group", "destination_id": "5005",
            "text": self.text, "mention": "7777", "delay_until_seen": True},
            "delay-proposal")
        prepared = self.port._propose(invocation, json.loads(invocation.arguments_json))
        operation_id = json.loads(prepared.source.payload)["operation_id"]
        self.current_input = {"event_id": "delay-confirmation",
                              "observed_at": (NOW + timedelta(seconds=1)).isoformat()}
        self.current_text = "确认发送"
        arguments = {"operation_id": operation_id, "destination_kind": "group",
                     "destination_id": "5005", "text": self.text, "mention": "7777",
                     "delay_until_seen": True, "confirmed": True}
        result = await self.port._send(self.invocation(SEND_ACTION, arguments, "delay-send"), arguments)
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(json.loads(result.source.payload)["state"], "scheduled")
        self.assertEqual(self.api.calls, [])
        tasks = claim_group(self.runtime, "5005", "7777")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["text"], self.text)

    async def test_private_receipt_task_is_recorded_after_send(self):
        invocation = self.invocation("relay_message", {
            "destination_id": "2002", "text": self.text, "need_receipt": True},
            "receipt-proposal")
        prepared = self.port._propose(invocation, json.loads(invocation.arguments_json))
        operation_id = json.loads(prepared.source.payload)["operation_id"]
        self.current_input = {"event_id": "receipt-confirmation",
                              "observed_at": (NOW + timedelta(seconds=1)).isoformat()}
        self.current_text = "确认发送"
        arguments = {"operation_id": operation_id, "destination_id": "2002",
                     "text": self.text, "need_receipt": True, "confirmed": True}
        result = await self.port._send(self.invocation(SEND_ACTION, arguments, "receipt-send"), arguments)
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(self.api.calls[0][0], "send_private_msg")
        tasks = claim_private_receipts(self.runtime, "2002")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["question"], self.text)

    async def test_multi_target_proposal_and_batch_send(self):
        invocation = self.invocation("relay_message_many", {
            "destination_kind": "private", "destination_ids": "2002,2003",
            "text": self.text}, "multi-proposal")
        prepared = await self.port._prepare_propose_many(
            invocation, json.loads(invocation.arguments_json))
        result = self.port._propose_many(invocation, prepared)
        payload = json.loads(result.source.payload)
        self.assertEqual(payload["state"], "awaiting_confirmation")
        self.assertEqual([item["destination_id"] for item in payload["targets"]], ["2002", "2003"])

        self.current_input = {"event_id": "multi-confirmation",
                              "observed_at": (NOW + timedelta(seconds=1)).isoformat()}
        self.current_text = "确认发送"
        operations = []
        for item in payload["targets"]:
            operations.append({
                "operation_id": item["operation_id"], "destination_kind": "private",
                "destination_id": item["destination_id"], "text": self.text,
            })
        response = await self.port._send_many(
            self.invocation("relay_send_many", {"operations": operations, "confirmed": True},
                            "multi-send"),
            {"operations": operations, "confirmed": True})
        self.assertEqual(response.state, "succeeded")
        self.assertEqual(len(self.api.calls), 2)
        self.assertEqual(sorted(call[1]["user_id"] for call in self.api.calls), [2002, 2003])

    async def test_group_relay_rejects_group_outside_whitelist(self):
        invocation = self.invocation("relay_message", {
            "destination_kind": "group", "destination_id": "9999", "text": self.text}, "group-denied")
        result = self.port._propose(invocation, json.loads(invocation.arguments_json))
        self.assertEqual(json.loads(result.source.payload)["code"], "relay_group_not_allowed")
        self.assertEqual(self.api.calls, [])

    async def test_missing_platform_receipt_stays_unknown_and_is_never_replayed(self):
        self.propose()
        self.current_input = {"event_id": "confirmation-event",
                              "observed_at": (NOW + timedelta(seconds=1)).isoformat()}
        self.current_text = "确认发送"
        self.api.response = {"status": "ok"}

        result = await self.send()
        self.assertEqual(result.state, "unknown")
        outbox = OperationRepository(self.runtime).outbox(
            self.operation_id, "private:2002").unwrap()
        self.assertEqual((outbox.state, outbox.receipt_ref), ("unknown", None))

        with self.assertRaises(TaskError) as raised:
            await self.send()
        self.assertEqual(raised.exception.code, "relay_result_unknown")
        self.assertEqual(len(self.api.calls), 1)


class NativeGroupTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_platform_group_transport_keeps_mention_and_text_order(self):
        class Platform:
            def __init__(self):
                self.sent = []

            async def send_by_session(self, session, chain):
                self.sent.append((session, chain))

        platform = Platform()
        route = SimpleNamespace(kind=ScopeKind.GROUP, platform_id="qq-official",
                                login="account", peer="opaque-group", platform=platform,
                                platform_name="qq_official")
        result = await send_group_notice(None, route, text="群里说一声", mention_id="7777")
        self.assertEqual(result["state"], "delivered")
        session, chain = platform.sent[0]
        self.assertEqual(session.session_id, "opaque-group")
        self.assertEqual([type(item).__name__ for item in chain.chain],
                         ["At", "Plain", "Plain"])
        self.assertEqual(chain.chain[-1].text, "群里说一声")


if __name__ == "__main__":
    unittest.main()
