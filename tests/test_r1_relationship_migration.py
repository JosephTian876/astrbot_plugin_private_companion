"""Round-1 regression tests for the #309 relationship seed and group batch flags.

Covers D3/D4 of ``reports/private-companion-v7-review/round-1/02-data-migration-audit.md``
with synthetic V1 directories and temporary V2 roots only. No production data or
network is used.

The contract these tests pin down:

* preview and apply feed the *same* parsed V1 "user profile" prompt into
  ``relationship_profile_entries``; the parsed seed is part of the plan token,
  so a prompt edited between the two stages is reported as stale instead of
  silently writing a different value;
* all seven public relationship fields (``FIELD_LIMITS``) are read, filled and
  reported; a non-empty V2 value stays authoritative and is shown as preserved;
* a group target only requests ``import_group_worldbook`` when V1 actually has
  group-scoped entries/members, and asks for network/expression only from the
  source that exists, so a network-only or expression-only group migrates;
* one failing group becomes one failed receipt and never aborts the later ones.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from astrbot_host.legacy_api import LegacyMigrationWebAPI
from companion_v2.execution.models import TaskError
from companion_v2.foundation.outcome import Outcome
from companion_v2.legacy.migration import LegacyMigration
from companion_v2.relationships.profile import (
    FIELD_LIMITS,
    GROUP,
    RelationshipProfileConfigurationOwner,
    configuration_fields,
    target_for,
)
from tests.group_host_support import GroupFixture
from tests.multi_host_support import MultiFixture


PROMPT = """对用户的称呼：小舟
用户性别：女
用户生日：2002-10-24
用户职业：开发者
是角色的XX：最信任的人
与角色的相处方式：晚上一起看晚霞，累了就安静待着。
其他补充信息：喜欢热可可。
"""

EXPECTED_SEED = {
    "default_nickname": "小舟",
    "user_gender": "女",
    "user_birthday": "2002-10-24",
    "user_occupation": "开发者",
    "role_relation": "最信任的人",
    "interaction": "晚上一起看晚霞，累了就安静待着。",
    "extra": "喜欢热可可。",
}

# The V1 user record stays authoritative for the three fields it recorded
# (``mapped.setdefault``); the prompt only fills the fields the record lacks.
EXPECTED_MERGED = {
    **EXPECTED_SEED,
    "role_relation": "亲近",
    "extra": "喜欢一起看晚霞",
}

GROUP_RULE = {
    "id": "old-group-rule",
    "kind": "grammar",
    "situation": "群话题收束",
    "pattern": "简短地结束群聊回合",
    "instruction": "保留轻巧的句末停顿",
    "channels": ["group", "proactive"],
    "relationship_stages": ["familiar", "any"],
    "evidence_examples": ["好，晚点聊。"],
}


def old_user(participant="200", **changes):
    """One verified V1 private user that the multi-target fixture can resolve."""
    return {
        "user_id": participant,
        "identity_subject_id": participant,
        "identity_adapter_instance_id": "qq-main",
        "identity_bot_id": "100",
        "identity_platform_kind": "onebot",
        "umo": f"qq-main:FriendMessage:{participant}",
        "private_inbound_count": 12,
        "manual_enabled": True,
        "nickname": "小舟",
        "persona_relationship": {
            "level": "亲近",
            "preference": "可轻分享",
            "note": "喜欢一起看晚霞",
            "score": 85,
            "updated_at": "2026-10-24T00:00:00+00:00",
        },
        "relationship_role": "owner",
        "relationship_phase_key": "deeply_bonded",
        **changes,
    }


def host_configuration(prompt=PROMPT, *, primary_user_id="200"):
    return {
        "basic_config": {"target_user_ids": [primary_user_id]},
        "life_schedule_config": {"roleplay_user_profile_prompt": prompt},
    }


class RelationshipSeedMigrationTests(MultiFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        temporary = TemporaryDirectory(prefix="companion-v2-r1-host-config-")
        self.addCleanup(temporary.cleanup)
        # A normal AstrBot layout: <data>/plugin_data/<plugin> with V1 settings
        # beside it in <data>/config. The API resolves its host snapshot from
        # here, so preview and apply read the same prompt the migration does.
        self.host_data_root = (Path(temporary.name) / "data" / "plugin_data"
                               / "astrbot_plugin_private_companion")
        self.host_data_root.mkdir(parents=True)
        self.write_host_config()

    def write_host_config(self, prompt=PROMPT, *, primary_user_id="200"):
        path = (self.host_data_root.parents[1] / "config"
                / "astrbot_plugin_private_companion_config.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(host_configuration(prompt, primary_user_id=primary_user_id),
                                   ensure_ascii=False), encoding="utf-8", newline="\n")

    def api(self):
        api = LegacyMigrationWebAPI.__new__(LegacyMigrationWebAPI)
        api.controller = NS(runtime=self.controller.runtime, now=self.clock.now,
            data_root=self.root, default_data_root=self.host_data_root,
            web_api=self.controller.web_api,
            config=self.controller.config, role_id="role-a")
        return api

    def source(self, users):
        temporary = TemporaryDirectory(prefix="companion-v2-r1-relationship-v1-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "companions.json").write_text(
            json.dumps({"users": users}, ensure_ascii=False), encoding="utf-8", newline="\n")
        return root

    def migration_at(self, root, *, prompt=PROMPT, primary_user_id="200"):
        return LegacyMigration(
            v1_root=root, v1_configuration=host_configuration(prompt,
                                                              primary_user_id=primary_user_id))

    def migration(self, users, *, prompt=PROMPT, primary_user_id="200"):
        root = self.source(users)
        return self.migration_at(root, prompt=prompt, primary_user_id=primary_user_id), root

    async def session(self):
        app = await self.start_host()
        return NS(profile=NS(target=self.controller.session.authority.context("first", "role-a")),
                  application=app)

    def profile_values(self, context):
        owner = RelationshipProfileConfigurationOwner(self.controller.runtime)
        snapshot = owner.read(target_for(context), configuration_fields())
        return {name: snapshot.fields[f"{GROUP}.{name}"].value for name in FIELD_LIMITS}

    def stored_values(self):
        return [dict(row) for row in self.controller.runtime.read_all(
            "SELECT target_key, field_id, value_json FROM configuration_values "
            "WHERE field_id LIKE 'relationship.profile.%' ORDER BY field_id", ())]

    @staticmethod
    def apply_kwargs(**changes):
        options = dict(source_group_id="", source_persona_id="", import_reactions=False,
            import_group_network=False, confirm_member_ids=False, import_journals=False,
            confirm_persona_mapping=False, import_worldbook=False, import_private_contacts=False,
            import_relationship_profiles=True)
        options.update(changes)
        return options

    async def test_preview_and_apply_use_the_same_seven_field_seed(self):
        session = await self.session()
        context = session.profile.target
        api = self.api()
        migration, root = self.migration({"200": old_user()})

        preview = api._data_plan(migration, root, session).unwrap()
        relationships = preview["relationship_profiles"]
        self.assertEqual((relationships["total"], relationships["ready"],
                          relationships["unchanged"], relationships["skipped"]), (1, 1, 0, 0))
        sample = relationships["samples"][0]
        # The old code raised KeyError on the four prompt-only fields, so this
        # assertion is also the "no KeyError, no silent drop" guarantee.
        self.assertEqual(sample["changes"], EXPECTED_MERGED)
        self.assertEqual(set(sample["changes"]), set(FIELD_LIMITS))
        self.assertTrue(any("用户性别" in item for item in sample["source_fields"]))
        self.assertTrue(any("与角色的相处方式" in item for item in sample["source_fields"]))

        result = api._data_apply_sync(migration, root, session,
            plan_token=preview["plan_token"], **self.apply_kwargs()).unwrap()
        self.assertEqual((result["relationship_profiles"]["imported"],
                          result["relationship_profiles"]["unchanged"],
                          result["relationship_profiles"]["skipped"]), (1, 0, 0))
        self.assertEqual(result["relationship_profiles"]["filled_fields"], 7)
        self.assertEqual(result["relationship_profiles"]["dropped_fields"], 0)
        self.assertEqual(self.profile_values(context), EXPECTED_MERGED)
        self.assertEqual(len(self.stored_values()), 7, "one person's seven fields only")

    async def test_replay_is_idempotent_and_v2_conflicts_are_preserved(self):
        session = await self.session()
        context = session.profile.target
        owner = RelationshipProfileConfigurationOwner(self.controller.runtime)
        fields = configuration_fields()
        target = target_for(context)
        current = owner.read(target, fields)
        owner.write(target, fields, changes={
            f"{GROUP}.user_gender": "手改性别",
            f"{GROUP}.interaction": "手改相处方式",
        }, reset=(), expected_revision=current.revision, observed_at=self.clock.now())

        api = self.api()
        migration, root = self.migration({"200": old_user()})
        preview = api._data_plan(migration, root, session).unwrap()
        sample = preview["relationship_profiles"]["samples"][0]
        self.assertEqual(preview["relationship_profiles"]["ready"], 1)
        self.assertEqual(set(sample["preserved"]), {"user_gender", "interaction"})
        self.assertNotIn("user_gender", sample["changes"])
        self.assertNotIn("interaction", sample["changes"])
        self.assertEqual(len(sample["changes"]), 5)

        result = api._data_apply_sync(migration, root, session,
            plan_token=preview["plan_token"], **self.apply_kwargs()).unwrap()
        self.assertEqual((result["relationship_profiles"]["imported"],
                          result["relationship_profiles"]["unchanged"],
                          result["relationship_profiles"]["skipped"]), (1, 0, 0))
        self.assertEqual(result["relationship_profiles"]["filled_fields"], 5)
        self.assertEqual(result["relationship_profiles"]["preserved_fields"], 2)
        self.assertEqual(self.profile_values(context), {
            **EXPECTED_MERGED,
            "user_gender": "手改性别",
            "interaction": "手改相处方式",
        })

        refreshed = api._data_plan(migration, root, session).unwrap()
        self.assertEqual((refreshed["relationship_profiles"]["ready"],
                          refreshed["relationship_profiles"]["unchanged"],
                          refreshed["relationship_profiles"]["skipped"]), (0, 1, 0))
        again = api._data_apply_sync(migration, root, session,
            plan_token=refreshed["plan_token"], **self.apply_kwargs()).unwrap()
        self.assertEqual((again["relationship_profiles"]["imported"],
                          again["relationship_profiles"]["unchanged"],
                          again["relationship_profiles"]["skipped"]), (0, 1, 0))
        self.assertEqual(self.profile_values(context), {
            **EXPECTED_MERGED,
            "user_gender": "手改性别",
            "interaction": "手改相处方式",
        })

    async def test_seed_edit_between_preview_and_apply_is_stale(self):
        session = await self.session()
        context = session.profile.target
        owner = RelationshipProfileConfigurationOwner(self.controller.runtime)
        fields = configuration_fields()
        target = target_for(context)
        current = owner.read(target, fields)
        owner.write(target, fields, changes={f"{GROUP}.interaction": "手改相处方式"},
                    reset=(), expected_revision=current.revision, observed_at=self.clock.now())

        api = self.api()
        root = self.source({"200": old_user()})
        migration = self.migration_at(root)
        preview = api._data_plan(migration, root, session).unwrap()
        before = self.profile_values(context)

        changed_prompt = PROMPT.replace(
            "与角色的相处方式：晚上一起看晚霞，累了就安静待着。",
            "与角色的相处方式：预览之后改成了完全不同的说法。")
        # `interaction` is preserved by the non-empty V2 value, so the entry
        # state and changed-field names are identical; only the seed itself
        # differs. The plan token must still notice it.
        self.write_host_config(changed_prompt)
        changed = self.migration_at(root, prompt=changed_prompt)
        changed_preview = api._data_plan(changed, root, session).unwrap()
        self.assertNotEqual(changed_preview["plan_token"], preview["plan_token"])
        self.assertEqual(changed_preview["relationship_profiles"]["samples"][0]["changes"],
                         preview["relationship_profiles"]["samples"][0]["changes"])

        stale = api._data_apply_sync(changed, root, session,
            plan_token=preview["plan_token"], **self.apply_kwargs())
        self.assertFalse(stale.is_success)
        self.assertEqual(stale.failure.code, "legacy_plan_stale")
        self.assertEqual(self.profile_values(context), before)

    async def test_seed_for_another_user_never_fills_this_profile(self):
        session = await self.session()
        context = session.profile.target
        api = self.api()
        self.write_host_config(primary_user_id="999")
        migration, root = self.migration({"200": old_user()}, primary_user_id="999")

        preview = api._data_plan(migration, root, session).unwrap()
        sample = preview["relationship_profiles"]["samples"][0]
        self.assertEqual(set(sample["changes"]), {"default_nickname", "role_relation", "extra"})
        result = api._data_apply_sync(migration, root, session,
            plan_token=preview["plan_token"], **self.apply_kwargs()).unwrap()
        self.assertEqual(result["relationship_profiles"]["filled_fields"], 3)
        values = self.profile_values(context)
        self.assertEqual(values["user_gender"], "")
        self.assertEqual(values["user_birthday"], "")
        self.assertEqual(values["user_occupation"], "")
        self.assertEqual(values["interaction"], "")

    async def test_overlong_seed_field_is_reported_instead_of_truncated(self):
        session = await self.session()
        api = self.api()
        long_gender = "女" * (FIELD_LIMITS["user_gender"] + 1)
        prompt = f"用户性别：{long_gender}\n用户职业：开发者\n"
        self.write_host_config(prompt)
        migration, root = self.migration({"200": old_user()}, prompt=prompt)

        preview = api._data_plan(migration, root, session).unwrap()
        sample = preview["relationship_profiles"]["samples"][0]
        self.assertIn("user_gender", sample["dropped"])
        self.assertNotIn("user_gender", sample["changes"])
        self.assertEqual(sample["changes"]["user_occupation"], "开发者")
        result = api._data_apply_sync(migration, root, session,
            plan_token=preview["plan_token"], **self.apply_kwargs()).unwrap()
        self.assertEqual(result["relationship_profiles"]["dropped_fields"], 1)
        self.assertEqual(result["relationship_profiles"]["samples"][0]["dropped"], ["user_gender"])
        self.assertEqual(self.profile_values(session.profile.target)["user_gender"], "")

    async def test_relationship_write_failure_becomes_a_skipped_receipt(self):
        session = await self.session()
        api = self.api()
        migration, root = self.migration({"200": old_user()})
        preview = api._data_plan(migration, root, session).unwrap()

        with patch.object(RelationshipProfileConfigurationOwner, "write",
                          side_effect=TaskError("relationship_profile_target_invalid", "boom")):
            result = api._data_apply_sync(migration, root, session,
                plan_token=preview["plan_token"], **self.apply_kwargs()).unwrap()
        receipt = result["relationship_profiles"]
        self.assertEqual((receipt["imported"], receipt["unchanged"], receipt["skipped"]),
                         (0, 0, 1))
        self.assertEqual(receipt["skipped_items"][0]["reason"],
                         "relationship_profile_target_invalid")


class GroupBatchMigrationTests(GroupFixture, unittest.IsolatedAsyncioTestCase):
    def api(self):
        api = LegacyMigrationWebAPI.__new__(LegacyMigrationWebAPI)
        # Group import reaches the real controller through LegacyGroupDataWebAPI,
        # so the view must carry its settings/session as well as the role context
        # the private migration APIs use.
        api.controller = NS(runtime=self.controller.runtime, now=self.clock.now,
            data_root=self.root, web_api=self.controller.web_api,
            config=self.controller.config, role_id="role-a",
            settings=self.controller.settings, session=self.controller.session,
            last_session=self.controller.last_session, _terminated=False)
        return api

    def source_root(self, *, groups=None, worldbook_entries=(), member_profiles=None):
        temporary = TemporaryDirectory(prefix="companion-v2-r1-group-v1-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        payload = {"groups": groups or {}}
        if worldbook_entries:
            payload["worldbook_entries"] = list(worldbook_entries)
        if member_profiles:
            payload["worldbook_member_profiles"] = member_profiles
        (root / "companions.json").write_text(
            json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        return root

    async def session_and_api(self):
        app = await self.start_host()
        context = self.controller.session.authority.context("first", "role-a")
        return NS(profile=NS(target=context), application=app), self.api()

    async def batch_for_all_groups(self, api, root, session, *, groups=None):
        migration = LegacyMigration(v1_root=root)
        scope = api._scope(migration, root, session)
        self.assertTrue(scope.is_success, scope.failure if not scope.is_success else "")
        payload = scope.unwrap()
        requests = groups if groups is not None else [
            {"persona_id": item["persona_id"], "group_id": item["source_id"]}
            for item in payload["groups"] if item["auto"]]
        result = await api._batch_apply(migration, root, session,
            {"token": payload["token"], "personas": [], "groups": requests})
        return payload, requests, result

    @staticmethod
    def network_group(group_id="300"):
        return {"members": {"200": {"name": "甲群成员", "count": 8},
                            "201": {"name": "乙群成员", "count": 4}},
                "relationship_edges": {"200|201": {"a": "200", "b": "201", "count": 2,
                                                   "tone": "玩笑"}}}

    @staticmethod
    def expression_group():
        return {"expression_profile": {"learned_rules": [dict(GROUP_RULE)]}}

    async def test_network_only_group_with_empty_worldbook_imports(self):
        session, api = await self.session_and_api()
        root = self.source_root(groups={"300": self.network_group()})
        scope, requests, result = await self.batch_for_all_groups(api, root, session)
        self.assertEqual([item["source_id"] for item in scope["groups"]], ["300"])
        self.assertEqual(requests, [{"persona_id": "", "group_id": "300"}])

        self.assertTrue(result.is_success, result.failure if not result.is_success else "")
        receipt = result.unwrap()
        self.assertEqual(receipt["counts"], {"imported": 1, "review": 0, "failed": 0})
        item = receipt["items"][0]
        self.assertEqual(item["state"], "imported", item.get("reason"))
        # An empty V1 worldbook must not be requested, so apply reports None
        # instead of failing with legacy_group_worldbook_empty.
        self.assertIsNone(item["result"]["group_worldbook"])
        self.assertTrue(item["result"]["group_network"]["imported"])

    async def test_expression_only_group_without_network_or_worldbook_imports(self):
        session, api = await self.session_and_api()
        root = self.source_root(groups={"300": self.expression_group()})
        scope, requests, result = await self.batch_for_all_groups(api, root, session)
        self.assertEqual([item["source_id"] for item in scope["groups"]], ["300"])
        self.assertEqual(requests, [{"persona_id": "", "group_id": "300"}])

        self.assertTrue(result.is_success, result.failure if not result.is_success else "")
        receipt = result.unwrap()
        self.assertEqual(receipt["counts"], {"imported": 1, "review": 0, "failed": 0})
        item = receipt["items"][0]
        self.assertEqual(item["state"], "imported", item.get("reason"))
        self.assertIsNone(item["result"]["group_network"])
        self.assertIsNone(item["result"]["group_worldbook"])
        self.assertEqual(item["result"]["group_expression"]["created"], 1)

        replay_scope, _, replay = await self.batch_for_all_groups(api, root, session)
        self.assertTrue(replay.is_success, replay.failure if not replay.is_success else "")
        replay_item = replay.unwrap()["items"][0]
        self.assertEqual(replay_item["state"], "imported", replay_item.get("reason"))
        self.assertEqual(replay_item["result"]["group_expression"]["created"], 0)

    async def test_group_with_network_and_worldbook_imports_both(self):
        session, api = await self.session_and_api()
        root = self.source_root(
            groups={"300": self.network_group()},
            worldbook_entries=[{
                "id": "group-entry", "name": "甲群资料",
                "content": "只有明确映射到甲群后才可注入的合成资料。",
                "__template_key": "group", "scope": ["default:groupmessage:300"]}])
        scope, requests, result = await self.batch_for_all_groups(api, root, session)
        self.assertEqual([item["source_id"] for item in scope["groups"]], ["300"])
        self.assertEqual(requests, [{"persona_id": "", "group_id": "300"}])

        self.assertTrue(result.is_success, result.failure if not result.is_success else "")
        item = result.unwrap()["items"][0]
        self.assertEqual(item["state"], "imported", item.get("reason"))
        self.assertEqual(item["result"]["group_worldbook"],
                         {"entries_imported": 1, "members_imported": 0, "state": "imported"})
        self.assertTrue(item["result"]["group_network"]["imported"])

    async def test_group_with_only_member_profiles_imports_worldbook_members(self):
        session, api = await self.session_and_api()
        root = self.source_root(
            groups={},
            member_profiles={"200": {
                "user_id": "200", "name": "甲群成员",
                "content": "甲群合成成员线索。", "projection_kind": "group_observation",
                "group_observation_scope_ids": ["default:groupmessage:300"]}})
        scope, requests, result = await self.batch_for_all_groups(api, root, session)
        self.assertEqual([item["source_id"] for item in scope["groups"]], ["300"])
        self.assertEqual(requests, [{"persona_id": "", "group_id": "300"}])

        self.assertTrue(result.is_success, result.failure if not result.is_success else "")
        item = result.unwrap()["items"][0]
        self.assertEqual(item["state"], "imported", item.get("reason"))
        # A member-profile-only group has worldbook members but no entries and
        # no relationship network; the member list alone must request the import.
        self.assertEqual(item["result"]["group_worldbook"],
                         {"entries_imported": 0, "members_imported": 1, "state": "imported"})

    async def test_worldbook_only_group_without_network_imports_entries(self):
        session, api = await self.session_and_api()
        root = self.source_root(
            groups={},
            worldbook_entries=[{
                "id": "group-entry", "name": "甲群资料",
                "content": "只有明确映射到甲群后才可注入的合成资料。",
                "__template_key": "group", "scope": ["default:groupmessage:300"]}])
        scope, requests, result = await self.batch_for_all_groups(api, root, session)
        self.assertEqual([item["source_id"] for item in scope["groups"]], ["300"])
        self.assertEqual(requests, [{"persona_id": "", "group_id": "300"}])

        self.assertTrue(result.is_success, result.failure if not result.is_success else "")
        item = result.unwrap()["items"][0]
        self.assertEqual(item["state"], "imported", item.get("reason"))
        self.assertEqual(item["result"]["group_worldbook"]["entries_imported"], 1)
        # No relationship network in V1, so nothing is claimed about members.
        self.assertIsNone(item["result"]["group_network"])

    async def test_one_failing_group_does_not_stop_later_groups(self):
        api = LegacyMigrationWebAPI.__new__(LegacyMigrationWebAPI)
        api.controller = NS(role_id="role-a")
        scope = {"token": "scope-token", "personas": [], "groups": [
            {"persona_id": "", "source_id": "300", "target_id": "group-a",
             "auto": True, "reason": "same_platform_account_and_id"},
            {"persona_id": "", "source_id": "301", "target_id": "group-b",
             "auto": True, "reason": "same_platform_account_and_id"},
            {"persona_id": "", "source_id": "302", "target_id": "group-c",
             "auto": True, "reason": "same_platform_account_and_id"},
        ]}

        calls = []

        class FakeGroupAPI:
            def __init__(self, controller, target_id, role_id):
                self.target_id, self.role_id = target_id, role_id

            def _target(self):
                return NS(target_id=self.target_id), object()

            def _plan(self, root, persona_id, group_id, target, session):
                calls.append(("plan", group_id))
                if group_id == "300":
                    raise TaskError("legacy_group_not_found", "no source row")
                return {"plan_token": "plan-" + group_id,
                        "source_group_network": {"source_group_id": group_id,
                                                 "member_count": 1, "edge_count": 0},
                        "group_worldbook": {"entries": 1, "members": 0},
                        "group_expression": {"available": False}}

            def apply_import(self, arguments, root, target, session):
                calls.append(("apply", arguments["source_group_id"],
                              arguments["import_group_network"],
                              arguments["import_group_worldbook"],
                              arguments["import_group_expression"]))
                if arguments["source_group_id"] == "301":
                    raise TaskError("legacy_worldbook_target_conflict", "owned elsewhere")
                return {"status": "ok", "data": {"idempotent": True}}

        with patch.object(LegacyMigrationWebAPI, "_scope",
                          return_value=Outcome.success(scope)), \
             patch("astrbot_host.legacy_group_data_api.LegacyGroupDataWebAPI", FakeGroupAPI):
            result = await api._batch_apply(None, None, None, {
                "token": "scope-token", "personas": [],
                "groups": [{"persona_id": "", "group_id": group_id}
                           for group_id in ("300", "301", "302")]})

        self.assertTrue(result.is_success, result.failure if not result.is_success else "")
        receipt = result.unwrap()
        self.assertEqual(receipt["counts"], {"imported": 1, "review": 0, "failed": 2})
        self.assertEqual([item["state"] for item in receipt["items"]],
                         ["failed", "failed", "imported"])
        self.assertEqual(receipt["items"][0]["reason"], "legacy_group_not_found")
        self.assertEqual(receipt["items"][1]["reason"], "legacy_worldbook_target_conflict")
        # The last group still ran with source-derived flags: its plan has one
        # network member and one worldbook entry, and no expression.
        self.assertEqual(calls[0], ("plan", "300"))
        self.assertEqual(calls[-1], ("apply", "302", True, True, False))


if __name__ == "__main__":
    unittest.main()
