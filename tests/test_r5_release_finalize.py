"""R5 release-finalize contracts (local additions, not from upstream).

Covers the two round-5 decisions that changed observable behaviour/copy:

* ``legacy_nothing_to_import`` in a batch migration is a **review** receipt, not a
  failed import (round-4 review R4-03): nothing broke, there was simply nothing
  in that V1 group to write.
* every other group-level error code keeps its ``failed`` receipt, so the
  reclassification cannot hide a real failure.
"""

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from astrbot_host.legacy_api import LegacyMigrationWebAPI, batch_group_receipt_state
from companion_v2.foundation.outcome import Outcome


CLASSIFIED = {
    "legacy_nothing_to_import": "review",
    "legacy_group_not_found": "failed",
    "legacy_group_mapping_confirmation_required": "failed",
    "legacy_plan_stale": "failed",
}


class _GroupImportStub:
    """Stand-in for ``LegacyGroupDataWebAPI``; only the batch's three calls matter."""

    response: dict = {}

    def __init__(self, *args, **kwargs):
        self.arguments = []

    def _target(self):
        return ("target", "session")

    def _plan(self, root, persona_id, group_id, target, session):
        # An expression-only group: zero network counters, no knowledge.
        return {"plan_token": "plan-token", "group_expression": {"available": False},
                "group_worldbook": {"entries": 0, "members": 0},
                "source_group_network": {"member_count": 0, "edge_count": 0}}

    def apply_import(self, arguments, root, target, session):
        type(self).arguments.append(arguments)
        return type(self).response


class BatchReceiptStateTests(unittest.IsolatedAsyncioTestCase):
    def api(self):
        api = LegacyMigrationWebAPI.__new__(LegacyMigrationWebAPI)
        scope = {"token": "scope-token", "personas": [], "groups": [
            {"persona_id": "persona-a", "source_id": "group-1", "auto": True, "target_id": "target-a"}]}
        api._scope = lambda *args: Outcome.success(scope)
        api.controller = SimpleNamespace(role_id="role-a")
        return api

    async def batch(self, response):
        fake = type("FakeGroupImportAPI", (_GroupImportStub,), {"response": response, "arguments": []})
        api = self.api()
        with patch("astrbot_host.legacy_group_data_api.LegacyGroupDataWebAPI", fake):
            result = await api._batch_apply(
                object(), Path("."), None,
                {"token": "scope-token", "personas": [],
                 "groups": [{"persona_id": "persona-a", "group_id": "group-1"}]})
        self.assertTrue(result.is_success, result.failure if not result.is_success else "")
        return result.unwrap(), fake

    def test_receipt_state_mapping_is_explicit(self):
        for code, state in CLASSIFIED.items():
            with self.subTest(code=code):
                self.assertEqual(batch_group_receipt_state(code), state)

    async def test_group_with_nothing_to_import_is_review_not_failed(self):
        receipt, fake = await self.batch({
            "status": "error", "message": "legacy_nothing_to_import",
            "reason": "select group relationships, knowledge or expression.",
            "data": {"code": "legacy_nothing_to_import"}})
        self.assertEqual([item["state"] for item in receipt["items"]], ["review"])
        self.assertEqual(receipt["items"][0]["reason"], "legacy_nothing_to_import")
        self.assertEqual(receipt["counts"], {"imported": 0, "review": 1, "failed": 0})
        # The reviewed plan really had nothing to ask for: the request carried no
        # import flags, which is exactly why the importer answered that way.
        self.assertFalse(fake.arguments[0]["import_group_network"])
        self.assertFalse(fake.arguments[0]["import_group_worldbook"])
        self.assertFalse(fake.arguments[0]["import_group_expression"])

    async def test_real_group_error_still_counts_as_failed(self):
        receipt, _ = await self.batch({
            "status": "error", "message": "legacy_group_mapping_confirmation_required",
            "data": {"code": "legacy_group_mapping_confirmation_required"}})
        self.assertEqual([item["state"] for item in receipt["items"]], ["failed"])
        self.assertEqual(receipt["counts"], {"imported": 0, "review": 0, "failed": 1})

    async def test_successful_group_import_is_still_imported(self):
        receipt, _ = await self.batch({"status": "ok", "message": "", "data": {"members_added": 0}})
        self.assertEqual([item["state"] for item in receipt["items"]], ["imported"])
        self.assertEqual(receipt["counts"], {"imported": 1, "review": 0, "failed": 0})


if __name__ == "__main__":
    unittest.main()
