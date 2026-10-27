"""R1: complete, rollback-safe installation backup using only synthetic data.

The fixture owns its temporary root so Windows never tries to delete a directory
while a SQLite connection still holds a file open: ``addCleanup`` runs after the
test body, and the fixture closes the runtime before removing the tree.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from companion_v2.legacy.archive import build_v1_archive_plan, copy_v1_archive, verify_v1_archive
from companion_v2.storage import backup as backup_module
from companion_v2.storage.backup import InstallationBackup, PENDING_FILENAME, apply_pending
from companion_v2.storage.management import (
    cleanup_backups, copy_backup_snapshots, remove_transferred_backup_snapshots,
)
from companion_v2.storage.runtime import StorageRuntime


NOW = datetime(2026, 10, 27, 6, tzinfo=timezone.utc)
INSTALLATION = "r1-synthetic-installation"
AUX_DATABASES = (
    "semantic-catalog.sqlite3",
    "legacy-memory-materials.sqlite3",
    "content_v2/works.sqlite3",
    "extensions/screen/operations.sqlite3",
    "extensions/image/native_v2/native_image.sqlite3",
    "extensions/image/native_v2/operations.sqlite3",
    "extensions/reality/role_snapshot_sources.db",
    "extensions/reality/snapshot_dependencies.db",
    "extensions/together/novel_library/novel_library.sqlite3",
    "extensions/together/native_v2/novel_library/scope-a/novel_library.sqlite3",
)
ORIGINAL = "screen/originals/ab/" + "ab" * 32
IMAGE_ORIGINAL = "extensions/image/native_v2/originals/imageop_" + "cd" * 16 + "_0.image"
NOVEL = "extensions/together/novel_library/books/book-a/source.txt"
SCOPED_NOVEL = "extensions/together/native_v2/novel_library/scope-a/books/book-b/source.md"
BINARY_FILES = (ORIGINAL, IMAGE_ORIGINAL, NOVEL, SCOPED_NOVEL)
CONFIG_FILES = (
    "decision/settings.json",
    "decision/roles/legacy.json",
    "search/settings.json",
    "extensions/image/config.json",
    "extensions/reality/config.json",
    "extensions/together/config.json",
    "extensions/game/config.json",
    "extensions/live/config.json",
    "extensions/reality/reality_companion.json",
    "extensions/together/native_v2/operations.json",
    "extensions/reality/native_v2/operations.json",
    "extensions/live/native_v2/live_state.json",
    "extensions/together/host-state.json",
    "extensions/together/together_companion_token_usage.json",
)
RESET_FILES = (
    "extensions/reality/mihome/auth.json",
    "extensions/reality/mihome/state.json",
    "extensions/game/trusted_browsers.json",
    "extensions/together/native_v2/invitations.json",
)
# Credentials written after the snapshot: a restore keeps them instead of reverting
# them to "nothing", because the snapshot never carried them (review P2-2).
CURRENT_SECRET = "CURRENT-ONLY-SECRET"
CURRENT_TOKEN = "CURRENT-ONLY-TOKEN"
# Owned state that must not survive a restore: a capture taken after the snapshot,
# an image operation material, a derived search index and an endpoint cache.
POST_SNAPSHOT_ORIGINAL = "screen/originals/cd/" + "cd" * 32
POST_SNAPSHOT_IMAGE = "extensions/image/native_v2/originals/imageop_" + "ef" * 16 + "_0.image"
DERIVED_DATABASES = (
    "memory-search.sqlite3",
    "extensions/image/native_v2/endpoint_tests/native_image.sqlite3",
)
TRANSIENT_FILES = ("extensions/together/temp/disposable.bin",)
# A file the operator put in the data root by hand: it is not installation state
# and a restore must never delete it.
UNOWNED_DIRECTORY_FILE = "notes/unowned.keep"
UNRELATED = "unrelated.txt"


def _put(root: Path, relative: str, body: bytes) -> None:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def _make_db(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES (?)", (value,))
        connection.commit()
    finally:
        connection.close()


def _db_value(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("SELECT value FROM marker").fetchone()[0]
    finally:
        connection.close()


class SyntheticInstallation:
    """One synthetic V2 installation inside a temporary root this object owns."""

    def __init__(self, *, external: bool = False):
        self.base = Path(tempfile.mkdtemp(prefix="r1-backup-"))
        self.root = self.base / "data"
        self.external = (self.base / "external" / "snapshots") if external else None
        self.runtime = StorageRuntime.open(data_root=self.root, installation_id=INSTALLATION)
        self._closed = False
        try:
            with self.runtime.transaction() as connection:
                connection.execute("INSERT INTO runtime_metadata(key,value) VALUES('synthetic-marker','before')")
            for relative in AUX_DATABASES:
                _make_db(self.path(relative), "before")
            for index, relative in enumerate(BINARY_FILES):
                _put(self.root, relative, b"before-binary\x00\xff" + bytes([index]))
            for relative in CONFIG_FILES:
                _put(self.root, relative, json.dumps({
                    "enabled": True, "synthetic_marker": "before",
                    "api_key": "SYNTHETIC-SECRET-DO-NOT-PACK",
                    "nested": {"pairing_token": "SYNTHETIC-TOKEN-DO-NOT-PACK"},
                    "custom_headers": {"Authorization": "Bearer SYNTHETIC-DO-NOT-PACK"},
                }).encode())
            for relative in RESET_FILES:
                _put(self.root, relative, b"synthetic-resettable-credential")
            for relative in DERIVED_DATABASES:
                _make_db(self.path(relative), "derived-cache")
            for relative in TRANSIENT_FILES:
                _put(self.root, relative, b"cache")
            _put(self.root, UNOWNED_DIRECTORY_FILE, b"do-not-delete-unknown")
            _put(self.root, UNRELATED, b"unrelated")
            _put(self.root, "tools/bin/executable.bin", b"tool")
            v1_source = self.base / "synthetic-v1-source"
            _put(v1_source, "private/history.bin", b"immutable-v1\x00\xff")
            plan = build_v1_archive_plan(v1_source)
            copy_v1_archive(plan, self.path("legacy-v1-archives"), archive_id="archive-synthetic")
        except BaseException:
            self.close()
            raise

    def path(self, relative: str) -> Path:
        return self.root.joinpath(*relative.split("/"))

    def backup(self, *, now=NOW) -> InstallationBackup:
        return InstallationBackup(self.runtime, now=lambda: now, directory=self.external,
                                  retention_count=20, retention_days=3650)

    def create(self) -> dict:
        return self.backup().create(note="synthetic complete").unwrap()

    def bundle(self, snapshot_id: str) -> Path:
        return self.backup().directory / f"companion-v2-{snapshot_id}.bundle"

    def payload(self, snapshot_id: str, relative: str) -> Path:
        return self.bundle(snapshot_id).joinpath("payload", *_parts(relative))

    def mutate(self) -> None:
        with self.runtime.transaction() as connection:
            connection.execute("UPDATE runtime_metadata SET value='after' WHERE key='synthetic-marker'")
        for relative in AUX_DATABASES:
            connection = sqlite3.connect(self.path(relative))
            try:
                connection.execute("UPDATE marker SET value='after'")
                connection.commit()
            finally:
                connection.close()
        for relative in BINARY_FILES:
            _put(self.root, relative, b"after-private-bytes\x00\xff")
        for relative in CONFIG_FILES:
            document = json.loads(self.path(relative).read_text())
            document["synthetic_marker"] = "after"
            document["api_key"] = CURRENT_SECRET
            document["nested"]["pairing_token"] = CURRENT_TOKEN
            _put(self.root, relative, json.dumps(document).encode())
        for relative in RESET_FILES:
            _put(self.root, relative, b"synthetic-current-credential")
        _put(self.root, POST_SNAPSHOT_ORIGINAL, b"newer-owned-original")
        _put(self.root, POST_SNAPSHOT_IMAGE, b"newer-owned-image")
        _put(self.root, UNRELATED, b"unrelated-after")

    def stage(self, manifest: dict) -> None:
        self.mutate()
        self.backup().restore(snapshot_id=manifest["snapshot_id"],
                              confirmed_hash=manifest["content_hash"]).unwrap()

    def close_runtime(self) -> None:
        try:
            self.runtime.close()
        except Exception:  # pragma: no cover - close is best effort during cleanup
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.close_runtime()
        shutil.rmtree(self.base, ignore_errors=True)

    def assert_restored(self, test: unittest.TestCase) -> None:
        connection = sqlite3.connect(self.path("companion-v2.sqlite3"))
        try:
            test.assertEqual(connection.execute(
                "SELECT value FROM runtime_metadata WHERE key='synthetic-marker'").fetchone()[0], "before")
        finally:
            connection.close()
        for relative in AUX_DATABASES:
            test.assertEqual(_db_value(self.path(relative)), "before", relative)
        for index, relative in enumerate(BINARY_FILES):
            test.assertEqual(self.path(relative).read_bytes(), b"before-binary\x00\xff" + bytes([index]))
        for relative in CONFIG_FILES:
            document = json.loads(self.path(relative).read_text())
            test.assertEqual(document["synthetic_marker"], "before", relative)
            # The snapshot carries no secrets, and a restore must not forget the
            # ones the installation uses today (review P2-2): the current value is
            # merged back instead of being replaced by the redacted document.
            test.assertEqual(document["api_key"], CURRENT_SECRET, relative)
            test.assertEqual(document["nested"]["pairing_token"], CURRENT_TOKEN, relative)
        test.assertFalse(self.path(POST_SNAPSHOT_ORIGINAL).exists())
        test.assertFalse(self.path(POST_SNAPSHOT_IMAGE).exists())
        for relative in DERIVED_DATABASES:
            test.assertFalse(self.path(relative).exists(), relative)
        for relative in RESET_FILES:
            # Credential stores are deliberately kept, not pruned.
            test.assertTrue(self.path(relative).is_file(), relative)
            test.assertEqual(self.path(relative).read_bytes(), b"synthetic-current-credential", relative)
        test.assertEqual(self.path(UNOWNED_DIRECTORY_FILE).read_bytes(), b"do-not-delete-unknown")
        test.assertEqual(self.path(UNRELATED).read_bytes(), b"unrelated-after")
        verify_v1_archive(self.path("legacy-v1-archives/archive-synthetic"))


def _parts(relative: str) -> tuple[str, ...]:
    return tuple(Path(relative).parts)


def _data_bytes(root: Path) -> dict[str, bytes]:
    """Every file a failed restore must put back byte for byte.

    ``*.tmp`` files are skipped for the same reason the backup policy skips them:
    they are in-flight temporary writes, and SQLite itself briefly creates one
    (``<db>-SHM.tmp``) next to a WAL database on this Windows build. They are
    never installation state and comparing them would be a race, not an assertion.
    """
    result = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name.endswith(".tmp"):
            continue
        relative = path.relative_to(root)
        if relative.parts[0] == "backups" or any(
                part.startswith(".restore-") for part in relative.parts):
            continue
        if path.name == PENDING_FILENAME or path.name.startswith("companion-v2.sqlite3.replaced"):
            continue
        result[relative.as_posix()] = path.read_bytes()
    return result


def _assert_data_unchanged(test: unittest.TestCase, root: Path, before: dict[str, bytes]) -> None:
    """Compare every file byte for byte, without a multi-minute diff on failure.

    ``assertEqual`` on two dictionaries of binary bodies sends unittest into
    ``difflib`` over megabyte-long ``bytes`` reprs; naming the changed paths first
    keeps the assertion exactly as strict and makes a regression readable.
    """
    current = _data_bytes(root)
    test.assertEqual(sorted(current), sorted(before), "the set of files changed")
    changed = sorted(relative for relative, body in before.items() if current[relative] != body)
    test.assertEqual(changed, [], "these files changed byte content")


class R1BackupRestoreTests(unittest.TestCase):
    def installation(self, *, external: bool = False) -> SyntheticInstallation:
        fixture = SyntheticInstallation(external=external)
        self.addCleanup(fixture.close)
        return fixture

    def test_complete_external_roundtrip_covers_databases_files_and_redaction(self) -> None:
        fixture = self.installation(external=True)
        manifest = fixture.create()
        self.assertEqual(manifest["format"], "companion-v2.installation-backup.v2")
        self.assertGreaterEqual(len(manifest["artifacts"]), 30)
        self.assertTrue(fixture.bundle(manifest["snapshot_id"]).is_dir())
        packed = b"".join(path.read_bytes() for path in fixture.bundle(manifest["snapshot_id"]).rglob("*")
                          if path.is_file())
        self.assertNotIn(b"DO-NOT-PACK", packed)
        snapshot_id = manifest["snapshot_id"]
        # The main database and every auxiliary store travel in the same bundle.
        self.assertTrue(fixture.payload(snapshot_id, "companion-v2.sqlite3").is_file())
        for relative in AUX_DATABASES:
            self.assertTrue(fixture.payload(snapshot_id, relative).is_file(), relative)
        for relative in BINARY_FILES:
            self.assertTrue(fixture.payload(snapshot_id, relative).is_file(), relative)
        self.assertTrue((fixture.backup().directory / f"companion-v2-{snapshot_id}.json").is_file())
        preview = fixture.backup().preview_restore(snapshot_id=snapshot_id).unwrap()
        self.assertEqual(preview["verified"]["manifest"]["content_hash"], manifest["content_hash"])
        fixture.stage(manifest)
        schema = manifest["schema_version"]
        fixture.close_runtime()
        outcome = apply_pending(fixture.root, installation_id=INSTALLATION,
                                schema_version=schema, backup_directory=fixture.external)
        self.assertTrue(outcome["applied"], outcome)
        fixture.assert_restored(self)
        self.assertFalse((fixture.root / PENDING_FILENAME).exists())
        self.assertTrue((fixture.root / "companion-v2.sqlite3.replaced").is_file())
        # The immutable source bundle is not consumed by the restore.
        for relative in ("companion-v2.sqlite3", *AUX_DATABASES, *BINARY_FILES):
            self.assertTrue(fixture.payload(snapshot_id, relative).is_file(), relative)

    def test_real_auxiliary_wal_is_snapshotted_without_copying_its_sidecar(self) -> None:
        fixture = self.installation()
        path = fixture.path("semantic-catalog.sqlite3")
        writer = sqlite3.connect(path, isolation_level=None)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("CREATE TABLE wal_marker(value TEXT)")
            writer.execute("INSERT INTO wal_marker VALUES('committed-only-in-wal')")
            keepalive = sqlite3.connect(path)
            try:
                self.assertTrue(Path(str(path) + "-wal").is_file())
                manifest = fixture.create()
                snapshot = fixture.payload(manifest["snapshot_id"], "semantic-catalog.sqlite3")
                self.assertEqual(_db_value(snapshot), "before")
                connection = sqlite3.connect(snapshot)
                try:
                    self.assertEqual(connection.execute(
                        "SELECT value FROM wal_marker").fetchone()[0], "committed-only-in-wal")
                finally:
                    connection.close()
                self.assertFalse(Path(str(snapshot) + "-wal").exists())
                self.assertFalse(Path(str(snapshot) + "-shm").exists())
            finally:
                keepalive.close()
        finally:
            writer.close()

    def test_swap_failure_rolls_back_every_byte_and_pending_can_retry(self) -> None:
        fixture = self.installation(external=True)
        manifest = fixture.create()
        fixture.stage(manifest)
        schema = manifest["schema_version"]
        fixture.close_runtime()
        # Preserve a real committed main-database WAL triad across the synthetic restart.
        main = fixture.path("companion-v2.sqlite3")
        connection = sqlite3.connect(main, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("INSERT INTO runtime_metadata(key,value) VALUES('wal-preserved','yes')")
            triad = {suffix: Path(str(main) + suffix).read_bytes() for suffix in ("", "-wal", "-shm")
                     if Path(str(main) + suffix).is_file()}
        finally:
            connection.close()
        for suffix, body in triad.items():
            Path(str(main) + suffix).write_bytes(body)
        _put(fixture.root, "companion-v2.sqlite3.replaced", b"prior-safety-do-not-delete")
        before = _data_bytes(fixture.root)
        target = fixture.path("semantic-catalog.sqlite3")
        real_replace = backup_module.os.replace
        fired = []

        def fail_once(source, destination):
            if Path(destination) == target and not fired:
                fired.append(True)
                raise OSError(18, "synthetic cross-volume swap failure")
            return real_replace(source, destination)

        with patch.object(backup_module.os, "replace", new=fail_once):
            failed = apply_pending(fixture.root, installation_id=INSTALLATION,
                                   schema_version=schema, backup_directory=fixture.external)
        self.assertFalse(failed["applied"], failed)
        self.assertTrue(fired)
        _assert_data_unchanged(self, fixture.root, before)
        self.assertTrue((fixture.root / PENDING_FILENAME).is_file())
        self.assertEqual(fixture.path("companion-v2.sqlite3.replaced").read_bytes(),
                         b"prior-safety-do-not-delete")
        self.assertFalse(fixture.path("companion-v2.sqlite3.replaced-wal").exists())
        self.assertFalse(fixture.path("companion-v2.sqlite3.replaced-shm").exists())
        retried = apply_pending(fixture.root, installation_id=INSTALLATION,
                                schema_version=schema, backup_directory=fixture.external)
        self.assertTrue(retried["applied"], retried)
        fixture.assert_restored(self)

    def test_prepare_copy_failure_never_touches_current_installation(self) -> None:
        fixture = self.installation(external=True)
        manifest = fixture.create()
        fixture.stage(manifest)
        fixture.close_runtime()
        before = _data_bytes(fixture.root)
        real_copy = backup_module._copy_verified
        fired = []

        def fail_copy(source, destination, **kwargs):
            if Path(source).name == "semantic-catalog.sqlite3" and not fired:
                fired.append(True)
                raise OSError(28, "synthetic full destination during copy")
            return real_copy(source, destination, **kwargs)

        with patch.object(backup_module, "_copy_verified", new=fail_copy):
            result = apply_pending(fixture.root, installation_id=INSTALLATION,
                                   schema_version=manifest["schema_version"],
                                   backup_directory=fixture.external)
        self.assertFalse(result["applied"], result)
        self.assertTrue(fired)
        _assert_data_unchanged(self, fixture.root, before)
        self.assertTrue((fixture.root / PENDING_FILENAME).is_file())
        retried = apply_pending(fixture.root, installation_id=INSTALLATION,
                                schema_version=manifest["schema_version"],
                                backup_directory=fixture.external)
        self.assertTrue(retried["applied"], retried)
        fixture.assert_restored(self)

    def test_prune_failure_rolls_back_pruned_post_snapshot_state(self) -> None:
        fixture = self.installation(external=True)
        manifest = fixture.create()
        fixture.stage(manifest)
        fixture.close_runtime()
        before = _data_bytes(fixture.root)
        self.assertIn(POST_SNAPSHOT_ORIGINAL, before)
        real_prune = backup_module._prune_path
        fired = []

        def fail_prune(journal, data_root, path):
            if not fired:
                fired.append(True)
                raise OSError(13, "synthetic delete failure while pruning")
            return real_prune(journal, data_root, path)

        with patch.object(backup_module, "_prune_path", new=fail_prune):
            result = apply_pending(fixture.root, installation_id=INSTALLATION,
                                   schema_version=manifest["schema_version"],
                                   backup_directory=fixture.external)
        self.assertFalse(result["applied"], result)
        self.assertTrue(fired)
        _assert_data_unchanged(self, fixture.root, before)
        self.assertTrue((fixture.root / PENDING_FILENAME).is_file())
        retried = apply_pending(fixture.root, installation_id=INSTALLATION,
                                schema_version=manifest["schema_version"],
                                backup_directory=fixture.external)
        self.assertTrue(retried["applied"], retried)
        fixture.assert_restored(self)

    def test_pending_unlink_failure_is_committed_and_finalization_is_idempotent(self) -> None:
        fixture = self.installation(external=True)
        manifest = fixture.create()
        fixture.stage(manifest)
        fixture.close_runtime()
        pending = fixture.path(PENDING_FILENAME)
        original_unlink = Path.unlink
        fired = []

        def fail_pending_unlink(path, *args, **kwargs):
            if path == pending and not fired:
                fired.append(True)
                raise OSError(13, "synthetic pending finalization failure")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=fail_pending_unlink):
            result = apply_pending(fixture.root, installation_id=INSTALLATION,
                                   schema_version=manifest["schema_version"],
                                   backup_directory=fixture.external)
        self.assertTrue(result["applied"], result)
        self.assertTrue(result.get("pending_finalization_required"), result)
        self.assertTrue(pending.is_file())
        fixture.assert_restored(self)
        finalized = apply_pending(fixture.root, installation_id=INSTALLATION,
                                  schema_version=manifest["schema_version"],
                                  backup_directory=fixture.external)
        self.assertTrue(finalized["applied"], finalized)
        self.assertFalse(pending.exists())
        fixture.assert_restored(self)

    def test_legacy_staged_request_is_refused_without_touching_the_installation(self) -> None:
        fixture = self.installation()
        manifest = fixture.create()
        second = fixture.backup(now=NOW + timedelta(minutes=1)).create(note="second").unwrap()
        (fixture.root / PENDING_FILENAME).write_text(json.dumps({
            "format": "companion-v2.installation-backup.v1", "snapshot_id": manifest["snapshot_id"],
            "content_hash": manifest["content_hash"], "requested_at": NOW.isoformat(),
            "safety_snapshot_id": manifest["snapshot_id"],
        }))
        # A staged request this version cannot apply is never silently replaced.
        refused = fixture.backup().restore(snapshot_id=second["snapshot_id"],
                                           confirmed_hash=second["content_hash"])
        self.assertFalse(refused.is_success)
        self.assertEqual(refused.failure.code, "backup_restore_pending")
        # Capture the bytes only after the live connection is gone: closing it
        # checkpoints the main database and removes its WAL sidecars.
        fixture.close_runtime()
        before = _data_bytes(fixture.root)
        result = apply_pending(fixture.root, installation_id=INSTALLATION,
                               schema_version=manifest["schema_version"])
        self.assertFalse(result["applied"], result)
        self.assertEqual(result["reason"], "backup_pending_legacy")
        _assert_data_unchanged(self, fixture.root, before)
        self.assertTrue((fixture.root / PENDING_FILENAME).is_file())
        self.assertEqual(fixture.backup().pending().unwrap()["restorable"], False)

    def test_retention_never_prunes_the_staged_restore_or_its_safety_copy(self) -> None:
        fixture = self.installation(external=True)
        manifest = fixture.create()
        fixture.stage(manifest)
        pending = json.loads((fixture.root / PENDING_FILENAME).read_text())
        safety_id = pending["safety_snapshot_id"]
        # One snapshot of retention must not remove either copy the restore needs.
        cleanup = cleanup_backups(fixture.root, retention_count=1, retention_days=1,
                                  now=NOW + timedelta(days=365), backup_directory=fixture.external,
                                  installation_id=INSTALLATION)
        self.assertEqual(cleanup["removed_count"], 0, cleanup)
        self.assertTrue(fixture.bundle(manifest["snapshot_id"]).is_dir())
        self.assertTrue(fixture.bundle(safety_id).is_dir())
        fixture.close_runtime()
        applied = apply_pending(fixture.root, installation_id=INSTALLATION,
                                schema_version=manifest["schema_version"],
                                backup_directory=fixture.external)
        self.assertTrue(applied["applied"], applied)
        fixture.assert_restored(self)

    def test_management_copies_and_prunes_only_complete_bundles(self) -> None:
        fixture = self.installation()
        first = fixture.create()
        second = fixture.backup(now=NOW + timedelta(minutes=1)).create(note="second").unwrap()
        external = fixture.base / "external"
        pairs = copy_backup_snapshots(fixture.backup().directory, external)
        self.assertEqual(len(pairs), 2)
        self.assertTrue((external / f"companion-v2-{first['snapshot_id']}.bundle").is_dir())
        self.assertTrue((external / f"companion-v2-{second['snapshot_id']}.bundle").is_dir())
        other = InstallationBackup(fixture.runtime, directory=external)
        self.assertTrue(other.inspect(snapshot_id=first["snapshot_id"]).is_success)
        self.assertTrue(other.inspect(snapshot_id=second["snapshot_id"]).is_success)
        self.assertEqual(remove_transferred_backup_snapshots(pairs, external), 2)
        self.assertFalse(fixture.bundle(first["snapshot_id"]).exists())
        self.assertFalse(fixture.bundle(second["snapshot_id"]).exists())
        cleanup_backups(fixture.root, retention_count=1, retention_days=3650,
                        now=NOW + timedelta(minutes=2), backup_directory=external,
                        installation_id=INSTALLATION)
        self.assertFalse((external / f"companion-v2-{first['snapshot_id']}.bundle").exists())
        self.assertTrue(other.inspect(snapshot_id=second["snapshot_id"]).is_success)

    def test_partial_bundle_is_not_copied_or_deleted(self) -> None:
        fixture = self.installation()
        manifest = fixture.create()
        snapshot_id = manifest["snapshot_id"]
        fixture.payload(snapshot_id, "semantic-catalog.sqlite3").unlink()
        destination = fixture.base / "destination"
        with self.assertRaises(Exception):
            copy_backup_snapshots(fixture.backup().directory, destination)
        self.assertFalse(destination.exists() and any(destination.iterdir()))
        self.assertTrue(fixture.bundle(snapshot_id).is_dir())
        cleanup_backups(fixture.root, retention_count=1, retention_days=1,
                        now=NOW + timedelta(days=30), backup_directory=fixture.backup().directory,
                        installation_id=INSTALLATION)
        self.assertTrue(fixture.bundle(snapshot_id).is_dir())
        self.assertFalse(fixture.backup().inspect(snapshot_id=snapshot_id).is_success)

    def test_v1_remains_listable_and_restores_only_its_main_database(self) -> None:
        """A 7.0.7 snapshot is a main-database-only restore, never a silent partial.

        The review rejected the previous behaviour (listable but unrestorable) as a
        compatibility regression. It is now restorable as exactly what 7.0.7 could
        do, and the scope has to be confirmed: the engine refuses a plain restore so
        no caller can pretend the result is a whole installation.
        """
        fixture = self.installation()
        current = fixture.create()
        directory = fixture.backup().directory
        snapshot_id = "a" * 32
        old_main = directory / f"companion-v2-{snapshot_id}.sqlite3"
        shutil.copy2(fixture.payload(current["snapshot_id"], "companion-v2.sqlite3"), old_main)
        (directory / f"companion-v2-{snapshot_id}.json").write_text(json.dumps({
            "format": "companion-v2.installation-backup.v1", "snapshot_id": snapshot_id,
            "created_at": NOW.isoformat(), "installation_id": INSTALLATION,
            "schema_version": current["schema_version"], "byte_size": old_main.stat().st_size,
            "content_hash": hashlib.sha256(old_main.read_bytes()).hexdigest(), "note": "legacy",
        }))
        backup = fixture.backup()
        listed = backup.listing().unwrap()["snapshots"]
        self.assertEqual({item["format"] for item in listed},
                         {"companion-v2.installation-backup.v1", "companion-v2.installation-backup.v2"})
        legacy_item = next(item for item in listed if item["snapshot_id"] == snapshot_id)
        self.assertTrue(legacy_item["restorable"])
        self.assertEqual(legacy_item["restore_scope"], "main_database_only")
        legacy = backup.inspect(snapshot_id=snapshot_id)
        self.assertTrue(legacy.is_success, legacy.failure)
        self.assertEqual(legacy.unwrap()["restore_scope"], "main_database_only")
        preview = backup.preview_restore(snapshot_id=snapshot_id).unwrap()
        self.assertIn("legacy_main_database_only", preview["warnings"])
        # Without the scope acknowledgement the request is refused, not silently staged.
        with self.assertRaises(Exception):
            backup.restore(snapshot_id=snapshot_id,
                           confirmed_hash=hashlib.sha256(old_main.read_bytes()).hexdigest()).unwrap()
        self.assertFalse(backup.pending_path.exists())
        staged = backup.restore(snapshot_id=snapshot_id,
                                confirmed_hash=hashlib.sha256(old_main.read_bytes()).hexdigest(),
                                confirmed_scope="main_database_only")
        self.assertTrue(staged.is_success, staged.failure)

    def test_manifest_traversal_extra_payload_and_auxiliary_tamper_fail_closed(self) -> None:
        fixture = self.installation()
        manifest = fixture.create()
        backup = fixture.backup()
        sidecar = backup.directory / f"companion-v2-{manifest['snapshot_id']}.json"
        document = json.loads(sidecar.read_text())
        document["artifacts"][0]["path"] = "../../outside"
        sidecar.write_text(json.dumps(document))
        self.assertFalse(backup.inspect(snapshot_id=manifest["snapshot_id"]).is_success)

        fixture = self.installation()
        manifest = fixture.create()
        backup = fixture.backup()
        sidecar = backup.directory / f"companion-v2-{manifest['snapshot_id']}.json"
        document = json.loads(sidecar.read_text())
        # A manifest cannot direct the restore at the pending request or the
        # displaced copy: only declared installation roots are restorable targets.
        document["artifacts"][0]["path"] = PENDING_FILENAME
        sidecar.write_text(json.dumps(document))
        self.assertFalse(backup.inspect(snapshot_id=manifest["snapshot_id"]).is_success)

        fixture = self.installation()
        manifest = fixture.create()
        backup = fixture.backup()
        _put(fixture.bundle(manifest["snapshot_id"]), "payload/unexpected.txt", b"unexpected")
        self.assertFalse(backup.inspect(snapshot_id=manifest["snapshot_id"]).is_success)

        fixture = self.installation()
        manifest = fixture.create()
        backup = fixture.backup()
        fixture.payload(manifest["snapshot_id"], "semantic-catalog.sqlite3").write_bytes(b"not sqlite")
        self.assertFalse(backup.inspect(snapshot_id=manifest["snapshot_id"]).is_success)
        with self.assertRaises(Exception):
            backup.restore(snapshot_id=manifest["snapshot_id"],
                           confirmed_hash=manifest["content_hash"]).unwrap()
        self.assertFalse(backup.pending_path.exists())

        fixture = self.installation()
        manifest = fixture.create()
        backup = fixture.backup()
        missing = fixture.payload(manifest["snapshot_id"], "content_v2/works.sqlite3")
        missing.unlink()
        self.assertFalse(backup.inspect(snapshot_id=manifest["snapshot_id"]).is_success)

    def test_host_api_contract_matches_the_engine_without_importing_the_sdk(self) -> None:
        """``backup_api``/host call sites cannot change here, so pin the contract.

        The AstrBot SDK is not installed in this isolated environment, so the host
        modules cannot be imported; reading them as source is what keeps the
        unchangeable call sites honest.
        """
        import ast
        import inspect

        host_root = Path(__file__).resolve().parent.parent
        # ``astrbot_host/multi_controller.py`` already carries a UTF-8 BOM in the
        # frozen baseline, so host sources are read tolerantly here.
        def host_tree(relative: str):
            return ast.parse((host_root / relative).read_text(encoding="utf-8-sig"))

        tree = host_tree("astrbot_host/backup_api.py")
        imported = {alias.name for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.module == "companion_v2.storage.backup"
                    for alias in node.names}
        self.assertTrue(imported)
        for name in imported:
            self.assertTrue(hasattr(backup_module, name), name)
        for parameter in ("installation_id", "schema_version", "backup_directory"):
            self.assertIn(parameter, inspect.signature(backup_module.apply_pending).parameters)
        for method, keywords in (("listing", ()), ("create", ("note",)), ("inspect", ("snapshot_id",)),
                                 ("preview_restore", ("snapshot_id",)),
                                 ("restore", ("snapshot_id", "confirmed_hash")), ("pending", ())):
            parameters = inspect.signature(getattr(InstallationBackup, method)).parameters
            for keyword in keywords:
                self.assertIn(keyword, parameters, method)
        for keyword in ("now", "retention_count", "retention_days", "directory"):
            self.assertIn(keyword, inspect.signature(InstallationBackup.__init__).parameters)
        # The multi-target open path now calls the startup gate, which owns the
        # ``apply_staged_restore`` call (and refuses to open before a pending
        # rollback is finished). The single-target controller still calls the
        # engine function directly.
        gate_calls = [node for node in ast.walk(host_tree("astrbot_host/multi_controller.py"))
                      if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "guard_restore_startup"]
        self.assertTrue(gate_calls, "astrbot_host/multi_controller.py must gate the open")
        for call in gate_calls:
            keywords = {keyword.arg for keyword in call.keywords}
            self.assertIn("installation_id", keywords, "astrbot_host/multi_controller.py")
            self.assertIn("backup_directory", keywords, "astrbot_host/multi_controller.py")
        for relative in ("astrbot_host/backup_api.py", "astrbot_host/controller.py"):
            calls = [node for node in ast.walk(host_tree(relative))
                     if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "apply_staged_restore"]
            self.assertTrue(calls, relative)
            for call in calls:
                keywords = {keyword.arg for keyword in call.keywords}
                self.assertIn("installation_id", keywords, relative)
                self.assertIn("backup_directory", keywords, relative)

    def test_backup_is_idempotent_and_rejects_linked_owned_source(self) -> None:
        fixture = self.installation()
        first = fixture.create()
        target = fixture.payload(first["snapshot_id"], "semantic-catalog.sqlite3")
        before = target.stat().st_mtime_ns
        second = fixture.create()
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(before, target.stat().st_mtime_ns)
        outside = fixture.base / "outside.bin"
        outside.write_bytes(b"outside")
        link = fixture.path(ORIGINAL)
        try:
            link.unlink()
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable on this host")
        result = fixture.backup(now=NOW + timedelta(minutes=2)).create(note="linked")
        self.assertFalse(result.is_success)
        self.assertEqual(outside.read_bytes(), b"outside")


if __name__ == "__main__":
    unittest.main()
