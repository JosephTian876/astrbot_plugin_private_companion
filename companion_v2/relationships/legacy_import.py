"""Fill V2's operator-managed relationship profile from reviewed V1 records.

V1 kept three pieces of relationship data on each user record and the other four
public profile fields in the old "user profile" prompt.  The prompt is parsed by
``legacy.relationship_profiles.config_relationship_profile`` and passed in as a
``seed`` for the one person it names; there is deliberately no second field map
here, both inputs use the public ``FIELD_LIMITS`` contract.

A relationship profile belongs to one role and one person, so the person is
resolved through the same trusted host bindings the private-contact importer
uses; a missing or conflicting account is reported as skipped instead of being
guessed.

Idempotency rule: an existing non-empty V2 value always wins. V1 fills a field
only while that field is empty, and the target revision is checked inside the
same transaction as the write. Running the importer again therefore reports
``unchanged`` and never overwrites something the operator edited later.
"""

from __future__ import annotations

from companion_v2.configuration.models import (
    ConfigLayer,
    ConfigTarget,
    ConfigurationError,
)
from companion_v2.execution.models import TaskError
from companion_v2.foundation.refs import SubjectKind, SubjectRef
from companion_v2.memory.person.access import resolve_subject

from .profile import (
    FIELD_LIMITS,
    GROUP,
    RelationshipProfileConfigurationOwner,
    configuration_fields,
)


SOURCE_ID = "private.core_profiles.users"
# V1 用户库只有 nickname / level / note 三项；性别、生日、职业、相处方式在旧版
# 配置的「用户档案」提示词里，由 legacy.relationship_profiles.config_relationship_profile
# 读出后作为 seed 合进对应用户，别再把它们当成 V1 没有的字段。
MAPPED_FIELDS = {
    "nickname": "default_nickname",
    "role_relation": "role_relation",
    "extra": "extra",
}
# Reading the whole public contract is what makes the prompt-only fields both
# readable and writable; deriving this from MAPPED_FIELDS would silently skip
# user_gender/user_birthday/user_occupation/interaction again.
PROFILE_FIELDS = tuple(FIELD_LIMITS)


def relationship_target(context, subject_id):
    scope = context.scope
    return ConfigTarget(
        scope.installation_id,
        ConfigLayer.RELATIONSHIP,
        context.role_binding.role,
        bot_id=scope.bot_id,
        subject=SubjectRef(scope.installation_id, SubjectKind.HUMAN, subject_id),
    )


def _mapped_changes(profile):
    """Return (changes, dropped) bounded by the V2 field contract, never truncated."""
    changes, dropped = {}, []
    for source, name in MAPPED_FIELDS.items():
        value = profile.get(source) or ""
        if not value:
            continue
        if len(value) > FIELD_LIMITS[name]:
            dropped.append(name)
            continue
        changes[name] = value
    return changes, dropped


def _read_values(owner, fields, target):
    snapshot = owner.read(target, fields)
    return snapshot, {
        name: snapshot.fields[f"{GROUP}.{name}"].value for name in PROFILE_FIELDS
    }


def _entry(profile):
    return {
        "source_id": SOURCE_ID,
        "user_id": profile["user_id"],
        "subject_id": "",
        "state": "skipped",
        "reason": "",
        "source_fields": list(profile.get("source_fields") or ()),
        "warnings": list(profile.get("warnings") or ()),
        "changes": {},
        "preserved": [],
        "dropped": [],
    }


def _seed_values(seed):
    """Validate the parsed prompt against the same public field contract.

    ``config_relationship_profile`` already returns V2 field names, so there is
    no second source-to-target table here: unknown names are reported (not
    guessed), over-limit values stay dropped (never truncated), and empty values
    are ignored exactly like the V1 user-record fields.
    """
    if not isinstance(seed, dict):
        return "", {}, [], []
    user_id = str(seed.get("user_id") or "").strip()
    raw_fields = seed.get("fields")
    fields, dropped, ignored = {}, [], []
    if isinstance(raw_fields, dict):
        for name, value in raw_fields.items():
            if name not in FIELD_LIMITS or not isinstance(value, str):
                ignored.append(str(name))
                continue
            text = value.strip()
            if not text:
                continue
            if len(text) > FIELD_LIMITS[name]:
                dropped.append(name)
                continue
            fields[name] = text
    # The prompt parser already dropped over-limit fields. Keep those reportable
    # for the matching person instead of losing why a field is absent.
    for name in seed.get("dropped") or ():
        if name in FIELD_LIMITS and name not in dropped:
            dropped.append(name)
    return user_id, fields, dropped, ignored


def relationship_seed_state(seed):
    """JSON-safe identity of the parsed prompt for the migration plan token.

    ``relationship_profile_token`` hashes field *names* only, so a value edited
    between preview and apply would otherwise validate against the same token.
    Including the normalized seed keeps both stages on the same parsed input.
    """
    user_id, fields, dropped, ignored = _seed_values(seed)
    if not user_id or not (fields or dropped or ignored):
        return {}
    return {
        "user_id": user_id,
        "fields": fields,
        "dropped": sorted(dropped),
        "ignored": sorted(ignored),
    }


def _seed_matches(profile, seed_user_id):
    """这条档案记录是否就是配置里写到的主要陪伴对象。"""
    if not seed_user_id:
        return False
    known = {str(profile.get("user_id") or ""),
             *(str(item) for item in profile.get("user_ids") or ())}
    return seed_user_id in known


def relationship_profile_entries(runtime, context, profiles, *, seed=None):
    """Resolve every V1 candidate against this target without writing anything.

    ``seed`` 是旧版配置里那份「用户档案」提示词的解析结果；它只并入对应用户，
    不新建用户、也不改变 identity 判定。
    """
    scope = context.scope
    if getattr(scope.kind, "value", scope.kind) != "private":
        return ()
    owner = RelationshipProfileConfigurationOwner(runtime)
    fields = configuration_fields()
    seed_user_id, seed_fields, seed_dropped, seed_ignored = _seed_values(seed)
    seed_source_fields = (list(seed.get("source_fields") or ())
                          if isinstance(seed, dict) else [])
    entries = []
    for profile in profiles:
        entry = _entry(profile)
        entries.append(entry)
        issue = next(iter(profile.get("issues") or ()), "")
        if issue:
            entry["reason"] = issue
            continue
        if (profile.get("platform"), profile.get("account_id")) != (scope.platform, scope.account_id):
            entry["reason"] = "different_account"
            continue
        try:
            subject_id = resolve_subject(runtime, context, profile["native_account_id"])
        except TaskError:
            entry["reason"] = "identity_not_registered"
            continue
        entry["subject_id"] = subject_id
        mapped, dropped = _mapped_changes(profile)
        if _seed_matches(profile, seed_user_id):
            for name, value in seed_fields.items():
                mapped.setdefault(name, value)
            entry["source_fields"].extend(seed_source_fields)
            dropped = list(dict.fromkeys((*dropped, *seed_dropped)))
            if seed_ignored:
                entry["warnings"].append("relationship_profile_seed_field_ignored")
        entry["dropped"] = dropped
        if not mapped:
            entry["reason"] = ("relationship_profile_field_too_long" if dropped
                               else "relationship_profile_empty")
            continue
        try:
            snapshot, current = _read_values(owner, fields, relationship_target(context, subject_id))
        except (ConfigurationError, ValueError, KeyError) as error:
            entry["reason"] = getattr(error, "code", "relationship_profile_target_invalid")
            continue
        changes = {name: value for name, value in mapped.items() if not current[name].strip()}
        entry["preserved"] = [name for name in mapped if current[name].strip()]
        if changes:
            entry["state"] = "ready"
            entry["changes"] = changes
        elif dropped:
            entry["reason"] = "relationship_profile_field_too_long"
        else:
            entry["state"] = "unchanged"
    return tuple(entries)


def relationship_profile_plan(entries):
    """JSON-safe preview summary; no runtime objects and no secret paths."""
    samples, skipped = [], []
    for entry in entries:
        item = {
            "source_id": entry["source_id"],
            "user_id": entry["user_id"],
            "subject_id": entry["subject_id"],
            "state": entry["state"],
            "reason": entry["reason"],
            "source_fields": list(entry["source_fields"]),
            "warnings": list(entry["warnings"]),
            "changes": dict(entry["changes"]),
            "preserved": list(entry["preserved"]),
            "dropped": list(entry["dropped"]),
        }
        (skipped if entry["state"] == "skipped" else samples).append(item)
    return {
        "total": len(entries),
        "ready": sum(entry["state"] == "ready" for entry in entries),
        "unchanged": sum(entry["state"] == "unchanged" for entry in entries),
        "skipped": len(skipped),
        "samples": samples[:20],
        "skipped_items": skipped[:50],
    }


def relationship_profile_token(entries):
    """Compact, JSON-safe state that makes a preview stale when a target changes.

    Values are deliberately excluded: the importer never overwrites a non-empty
    V2 field, so only identity and empty/non-empty states can change the work.
    """
    return {
        "total": len(entries),
        "entries": [{
            "user_id": entry["user_id"],
            "subject_id": entry["subject_id"],
            "state": entry["state"],
            "reason": entry["reason"],
            "changes": sorted(entry["changes"]),
            "preserved": list(entry["preserved"]),
            "dropped": list(entry["dropped"]),
        } for entry in entries],
    }


def import_relationship_profiles(runtime, context, entries, at):
    """Write the ready entries one person at a time; one failure never stops the rest."""
    owner = RelationshipProfileConfigurationOwner(runtime)
    fields = configuration_fields()
    result = {"imported": 0, "unchanged": 0, "skipped": 0, "filled_fields": 0,
              "preserved_fields": 0, "dropped_fields": 0,
              "samples": [], "skipped_items": []}
    for entry in entries:
        result["dropped_fields"] += len(entry["dropped"])
        if entry["state"] == "skipped":
            result["skipped"] += 1
            if len(result["skipped_items"]) < 50:
                result["skipped_items"].append({
                    "user_id": entry["user_id"],
                    "reason": entry["reason"] or "relationship_profile_not_ready",
                    "source_fields": list(entry["source_fields"]),
                })
            continue
        if entry["state"] == "unchanged":
            result["unchanged"] += 1
            result["preserved_fields"] += len(entry["preserved"])
            if len(result["samples"]) < 50:
                result["samples"].append({
                    "user_id": entry["user_id"], "subject_id": entry["subject_id"],
                    "state": "unchanged", "filled": [],
                    "preserved": list(entry["preserved"]), "dropped": list(entry["dropped"]),
                })
            continue
        try:
            with runtime.transaction():
                target = relationship_target(context, entry["subject_id"])
                snapshot, current = _read_values(owner, fields, target)
                changes = {f"{GROUP}.{name}": value for name, value in entry["changes"].items()
                           if not current[name].strip()}
                # Report what the preview already promised to keep, plus any
                # field another writer filled between preview and this write.
                preserved = list(dict.fromkeys(
                    name for name in (*entry["preserved"], *entry["changes"])
                    if current[name].strip()))
                if changes:
                    written = owner.write(
                        target, fields, changes=changes, reset=(),
                        expected_revision=snapshot.revision, observed_at=at,
                    )
                    result["imported"] += 1
                    result["filled_fields"] += len(written.changed_fields)
                    state = "imported"
                else:
                    result["unchanged"] += 1
                    state = "unchanged"
                result["preserved_fields"] += len(preserved)
                if len(result["samples"]) < 50:
                    result["samples"].append({
                        "user_id": entry["user_id"],
                        "subject_id": entry["subject_id"],
                        "state": state,
                        "filled": sorted(name.removeprefix(GROUP + ".") for name in changes),
                        "preserved": preserved,
                        "dropped": list(entry["dropped"]),
                    })
        except Exception as error:  # noqa: BLE001 - one person must not stop the batch
            result["skipped"] += 1
            if len(result["skipped_items"]) < 50:
                result["skipped_items"].append({
                    "user_id": entry["user_id"],
                    "reason": getattr(error, "code", type(error).__name__),
                    "source_fields": list(entry["source_fields"]),
                })
    return result
