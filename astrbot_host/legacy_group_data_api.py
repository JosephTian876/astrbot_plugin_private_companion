"""Explicit V1 group-data migration routes for configured OneBot group targets."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from astrbot.api.web import request
from companion_v2.execution.models import TaskError, digest
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import ScopeKind, ScopeRef, SubjectKind, SubjectRef
from companion_v2.knowledge.catalog import KnowledgeCatalog, MemberEntry, SourceBinding
from companion_v2.legacy.data_migration import build_data_plan, map_legacy_worldbook
from companion_v2.legacy.migration import LegacyMigration

from .group_settings import GroupTarget
from .management_identity import dashboard_operator
from .web_api import outcome_response


MAX_REQUEST_BYTES = 8192


class LegacyGroupDataWebAPI:
    """Migrate one V1 persona/group pair to one explicitly configured V2 group/role."""

    def __init__(self, controller, target_id, role_id):
        self.controller, self.target_id, self.role_id = controller, target_id, role_id

    def routes(self, prefix):
        return [(prefix + "/plan", self.plan, ["GET"], "Preview one explicit V1-to-V2 group mapping"),
                (prefix + "/apply", self.apply, ["POST"], "Import the reviewed V1 group data")]

    def _target(self):
        controller = self.controller
        session = controller.session or controller.last_session
        if controller._terminated or controller.runtime is None or session is None:
            raise TaskError("staging_not_initialized", "initialize the V2 profile before group migration")
        target = controller.settings.target(self.target_id)
        if (not isinstance(target, GroupTarget) or target.group_id == "*"
                or not any(item.bot_id == target.bot_id and item.role_id == self.role_id
                           for item in controller.settings.personas)):
            raise TaskError("legacy_group_target_required", "select a concrete configured V2 group target and role")
        return target, session

    @staticmethod
    def _root(controller, override):
        from .legacy_api import LegacyMigrationWebAPI
        return LegacyMigrationWebAPI(controller)._v1_root(override)[0]

    @staticmethod
    def _arguments(query, *, body=False):
        allowed = {"v1_root", "source_persona_id", "source_group_id"}
        if body:
            allowed.update({"plan_token", "import_group_network", "import_group_worldbook",
                            "import_group_expression", "confirm_expression_mapping",
                            "confirm_member_ids", "confirm_group_mapping"})
            if not {"plan_token", "source_persona_id", "source_group_id", "import_group_network",
                    "import_group_worldbook", "confirm_member_ids", "confirm_group_mapping"}.issubset(query):
                raise ValueError("required group migration fields are missing")
        if type(query) is not dict or set(query) - allowed:
            raise ValueError("unexpected group migration fields")
        # The shared page query omits empty values. An omitted persona on a
        # read-only plan selects companions.json, as in the other V1 previews.
        # Apply still requires the explicit persona and reviewed plan token.
        if not body:
            query = {"source_persona_id": "", **query}
        if (type(query.get("source_persona_id")) is not str or len(query["source_persona_id"]) > 96
                or type(query.get("source_group_id")) is not str or not query["source_group_id"]
                or len(query["source_group_id"]) > 256):
            raise ValueError("group migration source is invalid")
        if body and (type(query.get("plan_token")) is not str or not query["plan_token"]
                or any(type(query.get(name)) is not bool for name in (
                    "import_group_network", "import_group_worldbook", "confirm_member_ids",
                    "confirm_group_mapping"))
                or any(name in query and type(query[name]) is not bool for name in (
                    "import_group_expression", "confirm_expression_mapping"))):
            raise ValueError("group migration confirmation is invalid")
        root = query.get("v1_root")
        if root is not None and (type(root) is not str or not root.strip()):
            raise ValueError("V1 data path is invalid")
        return {**query, "v1_root": root,
                **({"import_group_expression": query.get("import_group_expression", False),
                    "confirm_expression_mapping": query.get("confirm_expression_mapping", False)}
                   if body else {})}

    def _scope_context(self, target):
        scope = ScopeRef(self.controller.runtime.installation_id, target.platform_id, ScopeKind.GROUP,
                         target.group_id, target.bot_id, target.account_id,
                         target.group_id, target.group_id)
        role = SubjectRef(scope.installation_id, SubjectKind.BOT, self.role_id)
        return SimpleNamespace(scope=scope,
            role_binding=SimpleNamespace(scope=scope, role=role))

    @staticmethod
    def _remap_group_members(members, source_group_id, target_group_id):
        return tuple(MemberEntry(item.entry_id, name=item.name, aliases=item.aliases,
            declared_account_ids=item.declared_account_ids, gender=item.gender, content=item.content,
            priority=item.priority, identity_note=item.identity_note, boundary_note=item.boundary_note,
            important_memories=item.important_memories,
            group_ids=(target_group_id,) if source_group_id in item.group_ids else item.group_ids)
            for item in members)

    @staticmethod
    def _as_group_member(item, target_group_id, *, account_ids=None):
        return MemberEntry(item.entry_id, name=item.name, aliases=item.aliases,
            declared_account_ids=(item.declared_account_ids if account_ids is None else tuple(account_ids)),
            gender=item.gender, content=item.content,
            priority=item.priority, group_ids=(target_group_id,), identity_note=item.identity_note,
            boundary_note=item.boundary_note, important_memories=item.important_memories)

    @staticmethod
    def _member_rank(item):
        """Prefer a curated profile over the generic per-group observation row."""
        return (item.priority, len(item.content), len(item.aliases),
                len(item.identity_note), len(item.boundary_note), len(item.important_memories))

    @staticmethod
    def _recorded_accounts(selected):
        members = (selected or {}).get("members") or {}
        if isinstance(members, dict):
            return {str(account) for account in members}
        if isinstance(members, (list, tuple)):
            return {str(item.get("native_account_id")) for item in members
                    if isinstance(item, dict) and item.get("native_account_id")}
        return set()

    @classmethod
    def _merge_group_members(cls, base_members, group_members, target_group_id, *, recorded_accounts=None):
        """Bring curated account-wide profiles into the selected group.

        V1 kept two layers: generic ``group_observation`` rows scoped to one
        group and hand-maintained profiles without a group scope.  The group
        importer used to read only the first layer, so known people arrived as
        generic observation rows with no relationship material.
        Merge by authenticated account and keep the richer entry; a curated
        profile is never allowed to silently replace a different account.
        ``recorded_accounts`` bounds the merge to people V1 recorded in this
        group, so a global profile cannot leak into an unrelated group.
        """
        merged = {item.entry_id: item for item in group_members}
        owners = {}
        for item in merged.values():
            for account in item.declared_account_ids:
                owners.setdefault(account, set()).add(item.entry_id)
        for item in base_members:
            accounts = tuple(account for account in item.declared_account_ids
                             if recorded_accounts is None or account in recorded_accounts)
            if recorded_accounts is not None and not accounts:
                continue
            matches = set()
            for account in accounts:
                matches.update(owners.get(account, ()))
            # A curated profile that spans multiple group entries is ambiguous;
            # preserve each authenticated account instead of merging people.
            if len(matches) > 1:
                continue
            if not matches:
                if item.entry_id in merged:
                    continue
                enriched = cls._as_group_member(item, target_group_id, account_ids=accounts)
                merged[enriched.entry_id] = enriched
                for account in enriched.declared_account_ids:
                    owners.setdefault(account, set()).add(enriched.entry_id)
                continue
            entry_id = next(iter(matches))
            current = merged.get(entry_id)
            if (current is None or cls._member_rank(item) < cls._member_rank(current)
                    or (item.entry_id in merged and item.entry_id != entry_id)):
                continue
            replacement = cls._as_group_member(item, target_group_id, account_ids=accounts)
            merged.pop(entry_id, None)
            for account in current.declared_account_ids:
                owner_entries = owners.get(account)
                if owner_entries is not None:
                    owner_entries.discard(entry_id)
                    if not owner_entries:
                        owners.pop(account, None)
            merged[replacement.entry_id] = replacement
            for account in replacement.declared_account_ids:
                owners.setdefault(account, set()).add(replacement.entry_id)
        return tuple(merged.values())

    def _plan(self, root, source_persona_id, source_group_id, target, session):
        data = build_data_plan(root, source_persona_id=source_persona_id)
        selected = next((item for item in data.group_networks
                         if item["source_group_id"] == source_group_id), None)
        groups = data.document().get("worldbook", {}).get("groups", [])
        member_groups = data.document().get("worldbook", {}).get("member_profile_groups", [])
        from companion_v2.legacy.expression_data import selected_profile
        from companion_v2.expression.legacy_import import preview as expression_preview
        context = self._scope_context(target)
        expression_profile = selected_profile(data.expression_profiles, context, source_group_id)
        expression = expression_preview(self.controller.runtime, expression_profile, context)
        if selected is None and not any(item.get("source_group_id") == source_group_id
                                        for item in (*groups, *member_groups)) and not expression["available"]:
            raise TaskError("legacy_group_not_found", "the selected V1 persona has no records for this group")
        from companion_v2.relationships.group_network import metadata_key, route_owner_key
        owner_key = route_owner_key(context.scope.installation_id, target.platform_id,
                                    target.bot_id, target.account_id, target.group_id, self.role_id)
        graph_row = self.controller.runtime.read_one(
            "SELECT value FROM runtime_metadata WHERE key = ?", (metadata_key(owner_key),))
        graph_fingerprint = hashlib.sha256((graph_row["value"] if graph_row else "").encode("utf-8")).hexdigest()
        network = session.application.messages.group_network.read_owner(owner_key) or {}
        catalog = KnowledgeCatalog(self.controller.runtime)
        mapped = map_legacy_worldbook(data, source_group_id=source_group_id,
            confirm_member_ids=True, include_group=True)
        group_entries, group_members = mapped[2], mapped[3]
        recorded = self._recorded_accounts(selected)
        group_members = self._merge_group_members(
            mapped[1], self._remap_group_members(group_members, source_group_id, target.group_id),
            target.group_id, recorded_accounts=recorded)
        source_id = self._worldbook_source_id(source_persona_id, source_group_id)
        binding = SourceBinding(context.scope.installation_id, role_id=self.role_id,
            persona_id=self.role_id, native_scope_ids=(target.group_id,), scope_kind=ScopeKind.GROUP,
            scope_route=(target.platform_id, target.bot_id, target.account_id))
        existing = catalog.get(binding, source_id)
        worldbook_digest = self._worldbook_digest(group_entries, group_members)
        token_material = {
            "source_fingerprint": data.source_fingerprint, "persona": source_persona_id,
            "source_group_id": source_group_id, "target": [target.target_id, target.platform_id,
                target.account_id, target.bot_id, target.group_id, self.role_id],
            "graph_fingerprint": graph_fingerprint,
            "expression": expression.get("token"),
            "worldbook": [existing.revision if existing else 0, existing.digest if existing else "",
                          worldbook_digest],
        }
        token = hashlib.sha256(json.dumps(token_material, ensure_ascii=True, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        selected_doc = ({key: value for key, value in selected.items() if key not in ("members", "edges")}
                        if selected else None)
        return {"plan_token": token, "source_fingerprint": data.source_fingerprint,
            "source_persona_id": source_persona_id, "source_group_id": source_group_id,
            "source_group_network": selected_doc,
            "group_expression": expression,
            "target": {"target_id": target.target_id, "platform_id": target.platform_id,
                "account_id": target.account_id, "bot_id": target.bot_id,
                "group_id": target.group_id, "role_id": self.role_id},
            "mapping_policy": {"source_to_targets": "one_to_many_explicit",
                "targets_per_plan": 1, "automatic_fanout": False,
                "source_platform_id": None, "source_platform_known": False},
            "requires_group_mapping_confirmation": True,
            "requires_member_id_confirmation": bool(selected or group_members),
            "network_existing_member_count": len(network.get("members", {})),
            "network_existing_edge_count": len(network.get("edges", {})),
            "group_worldbook": {"entries": len(group_entries), "members": len(group_members),
                "already_present": bool(existing and existing.active and existing.digest == worldbook_digest),
                "conflict": bool(existing and existing.code != "legacy_v1_imported")},
            "warnings": list(data.warnings)}

    @staticmethod
    def _worldbook_source_id(source_persona_id, source_group_id):
        from .legacy_api import LegacyMigrationWebAPI
        return LegacyMigrationWebAPI._worldbook_source_id(source_persona_id, source_group_id)

    @staticmethod
    def _worldbook_digest(entries, members):
        from .legacy_api import LegacyMigrationWebAPI
        return LegacyMigrationWebAPI._worldbook_digest(entries, members)

    def apply_import(self, arguments, root, target, session):
        """Import one reviewed (persona, group) pair without request state."""
        controller = self.controller
        try:
            plan = self._plan(root, arguments["source_persona_id"],
                          arguments["source_group_id"], target, session)
            if arguments["plan_token"] != plan["plan_token"]:
                return outcome_response(Outcome.failed("legacy_plan_stale",
                    "V1 source or V2 group target changed after preview; preview again."))
            if (not arguments["import_group_network"] and not arguments["import_group_worldbook"]
                    and not arguments["import_group_expression"]):
                return outcome_response(Outcome.failed("legacy_nothing_to_import",
                    "select group relationships, knowledge or expression."))
            if arguments["confirm_group_mapping"] is not True:
                return outcome_response(Outcome.failed("legacy_group_mapping_confirmation_required",
                    "confirm that this V1 group ID maps to the selected V2 platform, account and group."))
            if arguments["confirm_member_ids"] is not True and (
                    arguments["import_group_network"] or arguments["import_group_worldbook"]):
                return outcome_response(Outcome.failed("legacy_member_mapping_confirmation_required",
                    "confirm that member account IDs match within the selected V2 platform."))
            if arguments["import_group_expression"] and (
                    not plan["group_expression"]["available"]
                    or arguments["confirm_expression_mapping"] is not True):
                return outcome_response(Outcome.failed("legacy_expression_mapping_confirmation_required",
                    "confirm the same group and role for the imported expression rules."))
    
            data = build_data_plan(root, source_persona_id=arguments["source_persona_id"])
            selected = next((item for item in data.group_networks
                if item["source_group_id"] == arguments["source_group_id"]), None)
            if arguments["import_group_network"] and selected is None:
                return outcome_response(Outcome.failed("legacy_group_not_found",
                    "the selected V1 persona has no group relationship record."))
            group_entries = group_members = ()
            existing_group_source = None
            if arguments["import_group_worldbook"]:
                entries, members, group_entries, group_members = map_legacy_worldbook(
                    data, source_group_id=arguments["source_group_id"],
                    confirm_member_ids=True, include_group=True)
                recorded = self._recorded_accounts(selected)
                group_members = self._merge_group_members(
                    members, self._remap_group_members(
                        group_members, arguments["source_group_id"], target.group_id),
                    target.group_id, recorded_accounts=recorded)
                if not group_entries and not group_members:
                    return outcome_response(Outcome.failed("legacy_group_worldbook_empty",
                        "the selected V1 group has no importable group-scoped knowledge."))
                context = self._scope_context(target)
                source_id = self._worldbook_source_id(arguments["source_persona_id"],
                                                      arguments["source_group_id"])
                binding = SourceBinding(context.scope.installation_id, role_id=self.role_id,
                    persona_id=self.role_id, native_scope_ids=(target.group_id,), scope_kind=ScopeKind.GROUP,
                    scope_route=(target.platform_id, target.bot_id, target.account_id))
                existing_group_source = KnowledgeCatalog(controller.runtime).get(binding, source_id)
                if existing_group_source and existing_group_source.code != "legacy_v1_imported":
                    return outcome_response(Outcome.failed("legacy_worldbook_target_conflict",
                        "the selected V2 group already has a knowledge source with another owner."))
            network_result = None
            worldbook_result = None
            expression_result = None
            # Both destinations share the V2 SQLite runtime. Compose their
            # repository writes so a failure cannot leave a partial mapping.
            with controller.runtime.transaction():
                current_plan = self._plan(root, arguments["source_persona_id"],
                        arguments["source_group_id"], target, session)
                if arguments["plan_token"] != current_plan["plan_token"]:
                    raise TaskError("legacy_plan_stale",
                        "V1 source or V2 group target changed after preview; preview again.")
                current_data = build_data_plan(root, source_persona_id=arguments["source_persona_id"])
                if current_data.source_fingerprint != current_plan["source_fingerprint"]:
                    raise TaskError("legacy_plan_stale", "V1 source changed during import; preview again.")
                current_selected = next((item for item in current_data.group_networks
                    if item["source_group_id"] == arguments["source_group_id"]), None)
                if arguments["import_group_network"] and current_selected is None:
                    raise TaskError("legacy_group_not_found", "the selected V1 persona has no group relationship record")
                if arguments["import_group_worldbook"]:
                    current_entries = map_legacy_worldbook(current_data,
                        source_group_id=arguments["source_group_id"], confirm_member_ids=True,
                        include_group=True)
                    current_group_members = self._merge_group_members(
                        current_entries[1],
                        self._remap_group_members(current_entries[3],
                            arguments["source_group_id"], target.group_id),
                        target.group_id,
                        recorded_accounts=self._recorded_accounts(current_selected))
                    if (self._worldbook_digest(current_entries[2], current_group_members)
                            != self._worldbook_digest(group_entries, group_members)):
                        raise TaskError("legacy_plan_stale", "V1 group knowledge changed during import; preview again.")
                    current_group_source = KnowledgeCatalog(controller.runtime).get(binding, source_id)
                    if current_group_source and current_group_source.code != "legacy_v1_imported":
                        raise TaskError("legacy_worldbook_target_conflict",
                            "the selected V2 group already has a knowledge source with another owner.")
                    existing_group_source = current_group_source
                if arguments["import_group_network"]:
                    selected = current_selected
                    network_result = session.application.messages.group_network.import_legacy_aggregate(
                        self._scope_context(target),
                        source_key=digest(["v1-group-network-import.v2", arguments["source_persona_id"],
                                           arguments["source_group_id"], target.target_id, self.role_id,
                                           selected["members"], selected["edges"]]),
                        members=selected["members"], edges=selected["edges"], at=controller.now())
    
                if arguments["import_group_worldbook"]:
                    catalog = KnowledgeCatalog(controller.runtime)
                    context = self._scope_context(target)
                    source_id = self._worldbook_source_id(arguments["source_persona_id"],
                                                          arguments["source_group_id"])
                    binding = SourceBinding(context.scope.installation_id, role_id=self.role_id,
                        persona_id=self.role_id, native_scope_ids=(target.group_id,), scope_kind=ScopeKind.GROUP,
                        scope_route=(target.platform_id, target.bot_id, target.account_id))
                    fingerprint = self._worldbook_digest(group_entries, group_members)
                    current = existing_group_source
                    if not (current and current.active and current.digest == fingerprint):
                        catalog.save(binding, source_id=source_id,
                            locator="v1-persona:" + arguments["source_persona_id"][:96]
                                + "/group:" + arguments["source_group_id"],
                            format="legacy_worldbook", content_digest=fingerprint,
                            entries=group_entries, members=group_members, issues=(),
                            at=controller.now(), code="legacy_v1_imported")
                    worldbook_result = {"entries_imported": len(group_entries),
                        "members_imported": len(group_members),
                        "state": "unchanged" if current and current.active and current.digest == fingerprint
                            else "imported"}
                if arguments["import_group_expression"]:
                    from companion_v2.legacy.expression_data import selected_profile
                    from companion_v2.expression.legacy_import import import_profile
                    context = self._scope_context(target)
                    profile = selected_profile(current_data.expression_profiles, context,
                                               arguments["source_group_id"])
                    expression_result = import_profile(controller.runtime, profile, context,
                        controller.now(), expected_token=current_plan["group_expression"]["token"])
            return outcome_response(Outcome.success({"source_persona_id": arguments["source_persona_id"],
                "source_group_id": arguments["source_group_id"], "target_id": target.target_id,
                "role_id": self.role_id, "group_network": network_result,
                "group_worldbook": worldbook_result, "group_expression": expression_result,
                "idempotent": True}))
        except TaskError as error:
            return outcome_response(Outcome.failed(error.code, str(error)))
        except (ValueError, TypeError, KeyError, OSError, UnicodeError):
            return outcome_response(Outcome.failed("legacy_request_invalid",
                "invalid group migration request"))

    async def _run(self, arguments, *, apply):
        controller = self.controller
        async with controller._control_lock:
            try:
                if controller._terminated:
                    raise TaskError("staging_host_terminated", "plugin is stopped")
                username = request.username
                from .management_identity import _authenticated_api_key
                if type(username) is not str or not username.strip() or (username.startswith("api_key:") and not _authenticated_api_key(username)):
                    raise TaskError("dashboard_identity_required", "sign in to the AstrBot Dashboard")
                dashboard_operator(controller.runtime, username, at=controller.now())
                target, session = self._target()
                root = self._root(controller, arguments.get("v1_root"))
                migration = LegacyMigration(v1_root=root)
                found = migration.discover()
                if not found.is_success:
                    return outcome_response(found)
                if not apply:
                    plan = self._plan(root, arguments["source_persona_id"],
                                      arguments["source_group_id"], target, session)
                    return outcome_response(Outcome.success(plan))
                return self.apply_import(arguments, root, target, session)
            except TaskError as error:
                return outcome_response(Outcome.failed(error.code, str(error)))
            except (ValueError, TypeError, KeyError, OSError, UnicodeError):
                return outcome_response(Outcome.failed("legacy_request_invalid",
                    "invalid group migration request"))

    async def plan(self):
        try:
            query = request.query
            if (set(query.keys()) - {"v1_root", "source_persona_id", "source_group_id"}
                    or any(len(query.getlist(key)) != 1 for key in query.keys())):
                raise ValueError("unexpected query fields")
            arguments = self._arguments(dict(query))
        except (ValueError, TypeError):
            return outcome_response(Outcome.failed("legacy_request_invalid", "invalid group plan request"))
        return await self._run(arguments, apply=False)

    async def apply(self):
        try:
            raw = await request.body()
            if len(raw) > MAX_REQUEST_BYTES or request.query:
                raise ValueError("bounded JSON body required")

            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate field")
                    result[key] = value
                return result

            body = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
            arguments = self._arguments(body, body=True)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return outcome_response(Outcome.failed("legacy_request_invalid", "invalid group apply request"))
        return await self._run(arguments, apply=True)
