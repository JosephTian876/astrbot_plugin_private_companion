"""Round-3 F-9: one authoritative tool-capability decision, no second route read.

Round 2 introduced two implementations of the same decision:

* ``LocalReplyGenerator._round_tool_catalog()`` predicted the round with
  ``router.selected_route(...)`` and, when the prediction said "no tools",
  withheld the catalog *before* opening the session;
* ``LocalModelGateway.open_session()`` decided again from the authoritative
  ``plan.providers`` returned by ``router.resolve(...)``.

Both currently converge, but the prediction reads the whole configuration
snapshot a second time (``routing.py:129-135``) and sees a different provider
chain than the plan that actually executes.  A future routing rule (or a stale
prediction) can therefore make the audit and the executed envelope disagree.

Round 3 removes the prediction.  ``generation`` passes the catalog unchanged and
the gateway is the single source of truth: it drops the catalog, records the
``provider_without_tool_use`` filter, and returns the effective catalog in the
session.  These tests use a deliberate lie to prove the prediction is gone:

* ``selected_route`` is patched to return a tool-incapable chain while the real
  plan is tool-capable.  A build that consults the prediction loses the catalog;
  the fixed build never calls it.

The tests use the real ``LocalReplyGenerator`` + ``ModelRouter`` +
``LocalModelGateway`` from round 2's fixture, with only the provider
``generate`` and ``selected_route`` replaced.  All offline.
"""

from __future__ import annotations

from dataclasses import replace
import unittest
from unittest.mock import patch

from companion_v2.execution.models import digest
from tests.test_r2_text_runtime import TextRuntimeFixture, draft


class ToolCapabilitySingleSourceTests(TextRuntimeFixture, unittest.IsolatedAsyncioTestCase):
    async def test_generation_never_makes_a_second_route_prediction(self):
        self.stack(("text", "tools"))
        self.responses["primary"] = draft("single source.")
        calls = []

        def stale_prediction(*args, **kwargs):
            calls.append((args, kwargs))
            provider = replace(self.providers[0], capabilities=("text",))
            return (provider.binding.provider, provider.model, (provider,))

        with patch.object(self.router, "selected_route", side_effect=stale_prediction):
            batch, run = self.manual_run()
            result = await self.generator.generate(run)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual([part["text"] for part in self.parts(batch)], ["single source."])
        # The fixed build decides from the resolved plan only.
        self.assertEqual(calls, [])
        self.assertIn("source.read", self.offered_actions(self.called[0][1]))
        audit = self.audit(run.task.task_id)
        self.assertNotIn("provider_without_tool_use",
                         [item.get("reason") for item in audit["tool_filters"]])

    async def test_a_stale_prediction_cannot_remove_tools_from_the_executed_plan(self):
        self.stack(("text", "tools"))
        self.responses["primary"] = draft("tools stayed.")

        def stale_prediction(*args, **kwargs):
            provider = replace(self.providers[0], capabilities=("text",))
            return (provider.binding.provider, provider.model, (provider,))

        with patch.object(self.router, "selected_route", side_effect=stale_prediction):
            batch, run = self.manual_run()
            await self.generator.generate(run)

        effective = self.called[0][1].tools
        self.assertIn("source.read", tuple(tool.action for tool in effective))
        audit = self.audit(run.task.task_id)
        self.assertEqual(audit["tool_actions"], [tool.action for tool in effective])
        self.assertEqual(audit["tool_catalog_hash"], digest(effective))
        self.assertEqual(self.parts(batch)[0]["text"], "tools stayed.")

    async def test_the_reply_profile_is_read_once(self):
        self.stack(("text", "tools"))
        self.responses["primary"] = draft("one read.")
        original = self.configuration.read

        with patch.object(self.configuration, "read", wraps=original) as read:
            batch, run = self.manual_run()
            await self.generator.generate(run)

        groups = [call.kwargs.get("group_id") for call in read.call_args_list]
        self.assertEqual(groups.count(self.reply_profile.group), 1, groups)

    async def test_audit_and_envelope_share_the_same_tool_truth(self):
        self.stack(("text", "tools"))
        self.responses["primary"] = draft("no tools needed this round.")
        batch, run = self.manual_run()

        await self.generator.generate(run)

        envelope = self.called[0][1]
        audit = self.audit(run.task.task_id)
        self.assertEqual(audit["tool_actions"], [tool.action for tool in envelope.tools])
        self.assertEqual(audit["tool_catalog_hash"], digest(envelope.tools))
        self.assertEqual(self.parts(batch)[0]["text"], "no tools needed this round.")

    async def test_a_withheld_round_is_still_decided_by_the_gateway_and_replies(self):
        self.stack(("text",))
        self.responses["primary"] = draft("still delivered.")
        batch, run = self.manual_run()

        result = await self.generator.generate(run)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(self.called[0][1].tools, ())
        self.assertEqual(self.parts(batch)[0]["text"], "still delivered.")
        audit = self.audit(run.task.task_id)
        self.assertEqual(audit["tool_actions"], [])
        self.assertEqual(audit["tool_catalog_hash"], digest(()))
        self.assertEqual(audit["tool_filters"][-1],
                         {"action": "*", "reason": "provider_without_tool_use"})
        self.assertEqual(self.tool_rows(batch), ())

    async def test_gateway_open_session_remains_directly_authoritative(self):
        self.stack(("text",))
        catalog, filters = self.messages.tools.catalog_for_with_diagnostics(self.context, None)
        self.assertTrue(catalog)
        batch = self.accept(self.event("direct-input"))
        self.messages.seal(self.context, batch, deadline_at=self.deadline).unwrap()
        claim = self.tasks.claim(self.tasks.registrations["conversation.reply"].definition.lane).unwrap().claim
        self.tasks.begin(claim).unwrap()
        task = self.tasks.read(self.context, self.messages.batch(batch)["generation_task_id"]).unwrap()
        from companion_v2.execution.runner import TaskRun
        run = TaskRun(self.tasks, claim, task)
        request = self.messages.request(run)

        session = self.gateway.open_session(run, request, tools=catalog, tool_filters=filters)

        self.assertEqual(session.tools, ())
        audit = self.audit(run.task.task_id)
        self.assertEqual(audit["tool_actions"], [])
        self.assertEqual(audit["tool_filters"][-1],
                         {"action": "*", "reason": "provider_without_tool_use"})


if __name__ == "__main__":
    unittest.main()
