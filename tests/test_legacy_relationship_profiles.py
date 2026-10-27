"""V1 relationship details become the manual V2 profile without guessing identity."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import unittest

from astrbot_host.legacy_api import LegacyMigrationWebAPI
from companion_v2.foundation.outcome import Outcome
from companion_v2.legacy.migration import LegacyMigration
from companion_v2.legacy.private_contacts import private_contacts
from companion_v2.legacy.relationship_profiles import relationship_profiles
from companion_v2.relationships.profile import (
    FIELD_LIMITS,
    GROUP,
    RelationshipProfileConfigurationOwner,
    configuration_fields,
    target_for,
)
from tests.multi_host_support import MultiFixture


PROFILE_FIELDS = ("default_nickname", "role_relation", "extra")
ALL_PROFILE_FIELDS = tuple(FIELD_LIMITS)


def old_user(participant="200", **changes):
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


class RelationshipProfileMappingTests(unittest.TestCase):
    def test_maps_only_the_fields_v1_recorded(self):
        items, rejected = relationship_profiles({"users": {
            "200": old_user(),
            "201": old_user("201", nickname="", persona_relationship={"level": "熟悉"}),
        }})
        self.assertEqual(rejected, ())
        self.assertEqual([item["user_id"] for item in items], ["200", "201"])
        item = items[0]
        self.assertEqual((item["nickname"], item["role_relation"], item["extra"]),
                         ("小舟", "亲近", "喜欢一起看晚霞"))
        self.assertEqual(item["source_fields"],
                         ["nickname", "persona_relationship.level", "persona_relationship.note"])
        self.assertEqual((item["platform"], item["account_id"], item["native_account_id"]),
                         ("qq-main", "100", "200"))
        self.assertEqual(item["issues"], [])
        # Fields V1 never had are simply absent, never invented from other keys.
        self.assertNotIn("user_gender", item)
        self.assertNotIn("user_birthday", item)
        self.assertNotIn("user_occupation", item)
        self.assertNotIn("interaction", item)
        self.assertEqual((items[1]["nickname"], items[1]["role_relation"], items[1]["extra"]),
                         ("", "熟悉", ""))
        self.assertEqual(items[1]["source_fields"], ["persona_relationship.level"])

    def test_generic_nickname_and_missing_identity_need_confirmation(self):
        items, rejected = relationship_profiles({"users": {
            "200": old_user(nickname="主人"),
            "201": old_user("201", identity_bot_id=""),
            "202": {"nickname": "主人"},
        }})
        by_id = {item["user_id"]: item for item in items}
        self.assertEqual(by_id["200"]["nickname"], "")
        self.assertEqual(by_id["200"]["role_relation"], "亲近")
        self.assertIsNone(by_id["201"]["platform"])
        self.assertIsNone(by_id["201"]["account_id"])
        self.assertEqual(by_id["201"]["issues"], ["private_identity_unverified"])
        self.assertIn({"source_key": "202", "reason": "relationship_fields_empty"}, rejected)

    def test_alias_records_with_one_identity_merge_instead_of_duplicating(self):
        items, _ = relationship_profiles({"users": {
            "200": old_user(nickname=""),
            "alias": old_user("200", nickname="小舟", persona_relationship={"note": ""}),
        }})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["user_id"], "200")
        self.assertEqual(items[0]["nickname"], "小舟")
        self.assertEqual(items[0]["extra"], "喜欢一起看晚霞")

    def test_private_contacts_still_use_the_shared_identity_rule(self):
        contacts, rejected = private_contacts({"users": {
            "200": old_user(), "201": old_user("201", identity_platform_kind="qq_official")}})
        self.assertEqual([item["native_account_id"] for item in contacts], ["200"])
        self.assertEqual([item["reason"] for item in rejected], ["private_identity_unverified"])


class RelationshipProfileMigrationTests(MultiFixture, unittest.IsolatedAsyncioTestCase):
    def api(self):
        api = LegacyMigrationWebAPI.__new__(LegacyMigrationWebAPI)
        api.controller = NS(runtime=self.controller.runtime, now=self.clock.now,
            data_root=self.root, web_api=self.controller.web_api,
            config=self.controller.config, role_id="role-a")
        return api

    def migration(self, users):
        temporary = TemporaryDirectory(prefix="companion-v2-relationship-v1-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "companions.json").write_text(
            json.dumps({"users": users}, ensure_ascii=False), encoding="utf-8", newline="\n")
        return LegacyMigration(v1_root=root), root

    def host_configuration(self, prompt, user_id="200"):
        temporary = TemporaryDirectory(prefix="companion-v2-host-config-")
        self.addCleanup(temporary.cleanup)
        host = Path(temporary.name)
        (host / "config").mkdir()
        (host / "plugin_data" / "astrbot_plugin_private_companion").mkdir(parents=True)
        (host / "config" / "astrbot_plugin_private_companion_config.json").write_text(
            json.dumps({"basic_config": {"target_user_ids": [user_id]},
                        "life_schedule_config": {"roleplay_user_profile_prompt": prompt}},
                       ensure_ascii=False), encoding="utf-8", newline="\n")
        return host / "plugin_data" / "astrbot_plugin_private_companion"

    def profile_values(self, context, fields=PROFILE_FIELDS):
        owner = RelationshipProfileConfigurationOwner(self.controller.runtime)
        snapshot = owner.read(target_for(context), configuration_fields())
        return {name: snapshot.fields[f"{GROUP}.{name}"].value for name in fields}

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

    async def test_preview_then_import_fills_only_relationship_fields_and_is_idempotent(self):
        app = await self.start_host()
        session = NS(profile=NS(target=self.controller.session.authority.context("first", "role-a")),
                     application=app)
        context = session.profile.target
        api = self.api()
        migration, root = self.migration({
            "200": old_user(),
            "777": old_user("777"),
            "other-account": old_user("200", identity_bot_id="101"),
            "no-identity": {"user_id": "no-identity", "nickname": "旧称呼",
                            "persona_relationship": {"level": "熟悉"}},
        })

        preview = api._data_plan(migration, root, session).unwrap()
        relationships = preview["relationship_profiles"]
        self.assertEqual((relationships["total"], relationships["ready"],
                          relationships["unchanged"], relationships["skipped"]), (4, 1, 0, 3))
        self.assertEqual([item["user_id"] for item in relationships["samples"]], ["200"])
        self.assertEqual({item["reason"] for item in relationships["skipped_items"]},
                         {"identity_not_registered", "different_account",
                          "private_identity_unverified"})
        self.assertEqual(relationships["samples"][0]["changes"],
                         {"default_nickname": "小舟", "role_relation": "亲近",
                          "extra": "喜欢一起看晚霞"})

        first = api._data_apply_sync(migration, root, session,
            plan_token=preview["plan_token"], **self.apply_kwargs()).unwrap()
        self.assertEqual((first["relationship_profiles"]["imported"],
                          first["relationship_profiles"]["unchanged"],
                          first["relationship_profiles"]["skipped"]), (1, 0, 3))
        self.assertEqual(first["relationship_profiles"]["filled_fields"], 3)
        self.assertEqual(self.profile_values(context),
                         {"default_nickname": "小舟", "role_relation": "亲近",
                          "extra": "喜欢一起看晚霞"})
        self.assertEqual(len(self.stored_values()), 3, "one person's fields only")

        refreshed = api._data_plan(migration, root, session).unwrap()
        self.assertEqual((refreshed["relationship_profiles"]["ready"],
                          refreshed["relationship_profiles"]["unchanged"],
                          refreshed["relationship_profiles"]["skipped"]), (0, 1, 3))
        again = api._data_apply_sync(migration, root, session,
            plan_token=refreshed["plan_token"], **self.apply_kwargs()).unwrap()
        self.assertEqual((again["relationship_profiles"]["imported"],
                          again["relationship_profiles"]["unchanged"],
                          again["relationship_profiles"]["skipped"]), (0, 1, 3))
        self.assertEqual(self.profile_values(context),
                         {"default_nickname": "小舟", "role_relation": "亲近",
                          "extra": "喜欢一起看晚霞"})

    async def test_profile_prompt_seed_imports_all_profile_fields(self):
        app = await self.start_host()
        session = NS(profile=NS(target=self.controller.session.authority.context("first", "role-a")),
                     application=app)
        context = session.profile.target
        api = self.api()
        api.controller.default_data_root = str(self.host_configuration(
            "对用户的称呼：小舟\n用户性别：男\n用户生日：2002-10-24\n用户职业：开发者\n"
            "与角色的相处方式：喜欢一起看晚霞，偶尔拌嘴。\n其他补充信息：喜欢一起看晚霞"))
        migration, root = self.migration({"200": old_user()})

        preview = api._data_plan(migration, root, session).unwrap()
        changes = preview["relationship_profiles"]["samples"][0]["changes"]
        for field in ("user_gender", "user_birthday", "user_occupation", "interaction"):
            self.assertIn(field, changes)

        applied = api._data_apply_sync(migration, root, session,
            plan_token=preview["plan_token"], **self.apply_kwargs()).unwrap()
        self.assertEqual(applied["relationship_profiles"]["imported"], 1)
        values = self.profile_values(context, ALL_PROFILE_FIELDS)
        self.assertEqual(values["user_gender"], "男")
        self.assertEqual(values["interaction"], "喜欢一起看晚霞，偶尔拌嘴。")

    async def test_non_empty_v2_profile_is_unchanged(self):
        app = await self.start_host()
        session = NS(profile=NS(target=self.controller.session.authority.context("first", "role-a")),
                     application=app)
        context = session.profile.target
        owner = RelationshipProfileConfigurationOwner(self.controller.runtime)
        fields = configuration_fields()
        target = target_for(context)
        current = owner.read(target, fields)
        owner.write(target, fields, changes={
            f"{GROUP}.default_nickname": "手改称呼",
            f"{GROUP}.role_relation": "手改定位",
            f"{GROUP}.extra": "手改补充",
        }, reset=(), expected_revision=current.revision, observed_at=self.clock.now())

        api = self.api()
        migration, root = self.migration({"200": old_user()})
        preview = api._data_plan(migration, root, session).unwrap()
        self.assertEqual(preview["relationship_profiles"]["unchanged"], 1)
        self.assertEqual(preview["relationship_profiles"]["ready"], 0)
        result = api._data_apply_sync(migration, root, session,
            plan_token=preview["plan_token"], **self.apply_kwargs()).unwrap()
        self.assertEqual((result["relationship_profiles"]["imported"],
                          result["relationship_profiles"]["unchanged"],
                          result["relationship_profiles"]["skipped"]), (0, 1, 0))
        self.assertEqual(self.profile_values(context),
                         {"default_nickname": "手改称呼", "role_relation": "手改定位",
                          "extra": "手改补充"})

    async def test_switch_off_by_default_writes_no_relationship_fields(self):
        app = await self.start_host()
        session = NS(profile=NS(target=self.controller.session.authority.context("first", "role-a")),
                     application=app)
        api = self.api()
        migration, root = self.migration({"200": old_user()})
        preview = api._data_plan(migration, root, session).unwrap()
        result = api._data_apply_sync(migration, root, session,
            plan_token=preview["plan_token"],
            **self.apply_kwargs(import_relationship_profiles=False,
                                import_private_contacts=True)).unwrap()
        self.assertIsNone(result["relationship_profiles"])
        self.assertEqual(self.stored_values(), [])

    async def test_identity_mismatch_writes_nothing(self):
        app = await self.start_host()
        session = NS(profile=NS(target=self.controller.session.authority.context("first", "role-a")),
                     application=app)
        api = self.api()
        migration, root = self.migration({
            "777": old_user("777"),
            "other-account": old_user("200", identity_bot_id="101"),
            "no-identity": {"nickname": "旧称呼"},
        })
        before = self.stored_values()
        preview = api._data_plan(migration, root, session).unwrap()
        self.assertEqual((preview["relationship_profiles"]["ready"],
                          preview["relationship_profiles"]["skipped"]), (0, 3))
        self.assertEqual({item["reason"] for item in preview["relationship_profiles"]["skipped_items"]},
                         {"identity_not_registered", "different_account",
                          "private_identity_unverified"})
        result = api._data_apply_sync(migration, root, session,
            plan_token=preview["plan_token"], **self.apply_kwargs()).unwrap()
        self.assertEqual((result["relationship_profiles"]["imported"],
                          result["relationship_profiles"]["skipped"]), (0, 3))
        self.assertEqual(self.stored_values(), before)


class PersonaBatchReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_persona_batch_receipt_carries_relationship_counts(self):
        api = LegacyMigrationWebAPI.__new__(LegacyMigrationWebAPI)
        api.controller = NS()
        document = {"plan_token": "token", "private_contacts": {},
                    "journals": {}, "reaction_assets": {}, "reference_images": {},
                    "wardrobe_items": {}, "expressions": {}, "worldbook": {},
                    "group_networks": []}
        applied = AsyncMock(return_value=Outcome.success({"relationship_profiles": {
            "imported": 2, "unchanged": 1, "skipped": 3}}))
        with patch.object(LegacyMigrationWebAPI, "_data_plan",
                          return_value=Outcome.success(document)), \
             patch.object(LegacyMigrationWebAPI, "_data_apply", applied):
            item = await api._import_persona(None, None, None, "persona", {})
        self.assertEqual(item["state"], "imported")
        self.assertEqual(item["relationship_profiles"],
                         {"imported": 2, "unchanged": 1, "skipped": 3})
        self.assertTrue(applied.await_args.kwargs["import_relationship_profiles"])


if __name__ == "__main__":
    unittest.main()
