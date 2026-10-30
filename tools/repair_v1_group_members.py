"""Repair existing V1 group imports that only captured generic observation rows.

The 7.0.0 group importer read ``group_members`` but dropped the curated
account-wide ``members`` returned by :func:`map_legacy_worldbook`.  Re-running
the migration page now includes them; this tool fixes installations that were
already imported without forcing a full re-review.

It is intentionally conservative:

* only active ``legacy_v1_imported`` group sources are touched;
* only curated profiles whose account is a recorded member of that V1 group are
  added (global profiles do not leak into unrelated groups);
* the existing entries are preserved and the source code/digest are updated
  through :class:`KnowledgeCatalog`, so the normal reader and audit trail apply.

Usage (stop the plugin first, or accept a very short SQLite write lock):

    python tools/repair_v1_group_members.py \
        --v1-root "C:/Users/.../plugin_data/astrbot_plugin_private_companion" \
        --data-root "C:/Users/.../companion-7/installations/main/runtime" \
        --installation-id "astrbot-private-companion-v2:multi:main" --apply
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _backup_sqlite(database: Path, destination: Path) -> None:
    source = sqlite3.connect(str(database))
    target = sqlite3.connect(str(destination))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _recorded_members(raw: dict, group_id: str) -> set[str]:
    group = (raw.get("groups") or {}).get(str(group_id)) or {}
    members = group.get("members") if isinstance(group, dict) else None
    return {str(key) for key in (members or {}) if str(key).strip()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v1-root", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--installation-id", required=True)
    parser.add_argument("--apply", action="store_true",
                        help="write the repair; without it only a report is printed")
    parser.add_argument("--backup", default=None, type=Path,
                        help="optional SQLite backup path (defaults to a timestamped sibling)")
    args = parser.parse_args()

    raw_path = args.v1_root / "companions.json"
    database = args.data_root / "companion-v2.sqlite3"
    if not raw_path.is_file():
        print(f"missing V1 data: {raw_path}", file=sys.stderr)
        return 2
    if not database.is_file():
        print(f"missing V2 database: {database}", file=sys.stderr)
        return 2
    raw = json.loads(raw_path.read_text(encoding="utf-8"))

    from companion_v2.execution.models import digest
    from companion_v2.foundation.refs import ScopeKind
    from companion_v2.knowledge.catalog import KnowledgeCatalog
    from companion_v2.legacy.data_migration import build_data_plan, map_legacy_worldbook
    from companion_v2.storage.runtime import StorageRuntime
    from astrbot_host.legacy_group_data_api import LegacyGroupDataWebAPI

    if args.apply:
        backup = args.backup or database.with_name(
            database.name + ".before-v1-member-repair-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        _backup_sqlite(database, Path(backup))
        print(f"backup: {backup}")

    plan = build_data_plan(args.v1_root, source_persona_id="")
    runtime = StorageRuntime.open(data_root=args.data_root, installation_id=args.installation_id)
    try:
        catalog = KnowledgeCatalog(runtime)
        versions = [version for version in catalog.list(args.installation_id, limit=64)
                    if version.active and version.code == "legacy_v1_imported"
                    and version.binding.scope_kind is ScopeKind.GROUP
                    and version.binding.native_scope_ids]
        if not versions:
            print("no active legacy group knowledge sources found")
            return 1
        for version in versions:
            group_id = str(version.binding.native_scope_ids[0])
            _, base_members, _, group_members = map_legacy_worldbook(
                plan, source_group_id=group_id, confirm_member_ids=True, include_group=True)
            group_members = LegacyGroupDataWebAPI._remap_group_members(
                group_members, group_id, group_id)
            recorded = _recorded_members(raw, group_id)
            curated = tuple(member for member in base_members
                            if any(account in recorded for account in member.declared_account_ids))
            merged = LegacyGroupDataWebAPI._merge_group_members(
                curated, group_members, group_id, recorded_accounts=recorded)
            content_digest = digest({"entries": [item.document for item in version.entries],
                                     "members": [item.document for item in merged]})
            changed = content_digest != version.digest
            print(f"group {group_id}: entries={len(version.entries)} members={len(group_members)} "
                  f"curated={len(curated)} merged={len(merged)} changed={changed}")
            if not changed or not args.apply:
                continue
            catalog.save(version.binding, source_id=version.source_id, locator=version.locator,
                         format=version.format, content_digest=content_digest,
                         entries=version.entries, members=merged, issues=(),
                         at=datetime.now(timezone.utc), code="legacy_v1_imported")
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
