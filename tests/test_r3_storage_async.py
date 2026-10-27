"""Round-3: storage Dashboard I/O must leave the event loop and stay serialized.

The storage endpoints (inventory, retention preview/cleanup, backup-directory
relocation) are synchronous filesystem walks/copies.  Before this round they ran
inside the ``async`` route while holding ``controller._control_lock``: a large
installation blocked every AstrBot task for the duration and also stalled message
delivery authorization.  These tests prove, with synthetic directories only:

* the event loop keeps ticking during inventory and relocation;
* inventory/cleanup run on a worker thread, never on the loop thread;
* the control lock is not held across the disk work;
* a cleanup cannot interleave with a backup/restore operation (shared
  ``backup_operation_lock``) and two storage requests never cross;
* the backup-directory copy waits for a running backup operation too;
* a snapshot a staged restore still depends on survives a confirmed cleanup;
* the store the host opens on a worker thread stays usable from the loop
  (``check_same_thread=False`` + ``RLock``), which is what makes this legal.

The tests are offline and use temporary directories / SQLite files.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from astrbot_host import storage_api
from astrbot_host.backup_api import backup_operation_lock
from astrbot_host.storage_api import StorageWebAPI
from companion_v2.storage.backup import LEGACY_BACKUP_FORMAT, PENDING_FILENAME

# The routes import ``astrbot_host.web_api`` lazily on their first call.  The host
# itself imports that module when it registers routes, before any storage request;
# importing it here reproduces that warmed state.  Without it the tick probe below
# would measure roughly three seconds of module imports (first call only) and call
# it "blocked event loop", which is neither the disk work under test nor a state
# production ever reaches.
import astrbot_host.web_api  # noqa: E402,F401 - warm the route dependency

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
INSTALLATION = "r3-storage-async-installation"


def _write_v1_snapshot(directory: Path, identifier: str, created_at: datetime,
                       installation_id: str = INSTALLATION) -> Path:
    """Write a real 7.0.7 snapshot pair: one flat SQLite database plus its sidecar."""
    from companion_v2.storage.schema import SCHEMA_VERSION

    directory.mkdir(parents=True, exist_ok=True)
    database = directory / f"companion-v2-{identifier}.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE runtime_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany("INSERT INTO runtime_metadata (key, value) VALUES (?, ?)",
                               (("installation_id", installation_id),
                                ("schema_version", SCHEMA_VERSION)))
        connection.commit()
    finally:
        connection.close()
    (directory / f"companion-v2-{identifier}.json").write_text(json.dumps({
        "format": LEGACY_BACKUP_FORMAT, "snapshot_id": identifier,
        "created_at": created_at.isoformat(), "installation_id": installation_id,
        "schema_version": SCHEMA_VERSION, "byte_size": database.stat().st_size,
        "content_hash": hashlib.sha256(database.read_bytes()).hexdigest(), "note": "",
    }), encoding="utf-8")
    return database


class Request:
    username = "dashboard-admin"
    query = {}
    payload = b""

    async def body(self):
        return self.payload


class Ticker:
    """Count loop iterations and record the worst scheduling gap."""

    def __init__(self, interval: float = 0.01):
        self.interval = interval
        self.ticks = 0
        self.max_gap = 0.0
        self.failure: BaseException | None = None
        self._task: asyncio.Task | None = None

    async def _run(self):
        while True:
            started = time.monotonic()
            await asyncio.sleep(self.interval)
            gap = time.monotonic() - started
            self.max_gap = max(self.max_gap, gap)
            self.ticks += 1

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task


class StorageAsyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="r3-storage-async-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.default_root = self.root / "plugin-data" / "astrbot_plugin_private_companion"
        self.default_root.mkdir(parents=True)
        self.profile_root = self.default_root / "installations" / "main"
        self.runtime_root = self.profile_root / "runtime"
        self.config = {"storage": {"path": "", "backup_path": "",
                                   "retention_count": 10, "retention_days": 90}}
        self.controller = NS(
            data_root=self.default_root, default_data_root=self.default_root,
            config=self.config, runtime=None, _control_lock=asyncio.Lock(),
            profile_root=self.profile_root,
            settings=NS(installation_id=INSTALLATION), configuration=None,
            save_plugin_config=self.save, now=lambda: datetime.now(timezone.utc))
        self.api = StorageWebAPI(self.controller)
        self.request = Request()

    def save(self, candidate):
        self.config.clear()
        self.config.update(candidate)

    async def test_inventory_runs_off_the_event_loop(self):
        """``inventory`` used to run inside the route: no ticks for its duration."""
        original = storage_api.inventory
        threads: list[threading.Thread] = []

        def slow_inventory(*args, **kwargs):
            threads.append(threading.current_thread())
            time.sleep(0.35)
            return original(*args, **kwargs)

        ticker = Ticker()
        ticker.start()
        try:
            with patch.object(storage_api, "inventory", new=slow_inventory):
                with patch("astrbot_host.storage_api.request", self.request):
                    response = await self.api.status()
        finally:
            await ticker.stop()

        self.assertEqual(response["status"], "ok", response)
        self.assertTrue(threads, "inventory was never called")
        self.assertTrue(all(thread is not threading.main_thread() for thread in threads),
                        "inventory still ran on the event-loop thread")
        self.assertGreater(ticker.ticks, 10,
                           f"the event loop only ran {ticker.ticks} ticks during a 0.35 s inventory")
        self.assertLess(ticker.max_gap, 0.25,
                        f"the event loop stalled for {ticker.max_gap:.2f}s")

    async def test_status_does_not_hold_the_control_lock_across_disk_work(self):
        """The control lock is shared with delivery authorization; disk work must not.

        Timing alone cannot prove this: on the old code the synchronous route blocks
        the very ``sleep`` a probe would use, so the lock looks free afterwards.
        Observe the lock at the moment the disk walk actually starts instead.
        """
        original = self.api._status
        observed: dict = {}

        def status_probe():
            observed["locked"] = self.controller._control_lock.locked()
            observed["thread"] = threading.current_thread()
            return original()

        with patch.object(self.api, "_status", new=status_probe):
            with patch("astrbot_host.storage_api.request", self.request):
                response = await self.api.status()
        self.assertEqual(response["status"], "ok", response)
        self.assertIn("locked", observed, "_status was never called")
        self.assertFalse(observed["locked"],
                         "the storage route held the control lock while walking the disk")
        self.assertIsNot(observed["thread"], threading.main_thread())

    async def test_cleanup_waits_for_a_running_backup_operation(self):
        """Cleanup deletes snapshot pairs; it must not cross a snapshot/restore."""
        started = threading.Event()
        release = threading.Event()

        def cleanup_stub(*args, **kwargs):
            started.set()
            release.wait(10)
            return {"snapshot_count": 0, "remove_count": 0, "remove_bytes": 0,
                    "snapshot_ids": [], "incomplete_count": 0, "incomplete_ids": [],
                    "incomplete_reasons": [], "removed_count": 0, "removed_bytes": 0}

        self.request.payload = b'{"action":"clean"}'
        lock = backup_operation_lock(self.controller)
        self.assertTrue(lock.acquire(timeout=1))
        try:
            with patch.object(storage_api, "cleanup_backups", new=cleanup_stub):
                with patch("astrbot_host.storage_api.request", self.request):
                    task = asyncio.create_task(self.api.update())
                    await asyncio.sleep(0.25)
                    self.assertFalse(started.is_set(),
                                     "cleanup ran while a backup held the operation lock")
                    release.set()
                    lock.release()
                    lock_held = False
                    response = await asyncio.wait_for(task, timeout=10)
        finally:
            if not release.is_set():
                release.set()
            if lock.locked():
                lock.release()
        self.assertEqual(response["status"], "ok", response)

    async def test_two_concurrent_status_requests_are_serialized(self):
        """Inventory walks are read-only, but two requests must not cross."""
        original = storage_api.inventory
        active = 0
        max_active = 0
        guard = threading.Lock()

        def counted(*args, **kwargs):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.15)
                return original(*args, **kwargs)
            finally:
                with guard:
                    active -= 1

        with patch.object(storage_api, "inventory", new=counted):
            with patch("astrbot_host.storage_api.request", self.request):
                first, second = await asyncio.gather(self.api.status(), self.api.status())
        self.assertEqual(first["status"], "ok", first)
        self.assertEqual(second["status"], "ok", second)
        self.assertEqual(max_active, 1, "two storage requests crossed on the same controller")

    async def test_two_cleanup_requests_never_overlap(self):
        """Two confirmed cleanups must not both delete the same pair."""
        active = 0
        max_active = 0
        calls = 0

        def cleanup_stub(*args, **kwargs):
            nonlocal active, max_active, calls
            calls += 1
            active += 1
            max_active = max(max_active, active)
            try:
                time.sleep(0.2)
                return {"snapshot_count": 0, "remove_count": 0, "remove_bytes": 0,
                        "snapshot_ids": [], "incomplete_count": 0, "incomplete_ids": [],
                        "incomplete_reasons": [], "removed_count": 0, "removed_bytes": 0}
            finally:
                active -= 1

        self.request.payload = b'{"action":"clean"}'
        with patch.object(storage_api, "cleanup_backups", new=cleanup_stub):
            with patch("astrbot_host.storage_api.request", self.request):
                first, second = await asyncio.gather(self.api.update(), self.api.update())
        self.assertEqual(first["status"], "ok", first)
        self.assertEqual(second["status"], "ok", second)
        self.assertEqual(calls, 2)
        self.assertEqual(max_active, 1, "two cleanups crossed on the same controller")

    async def test_relocation_runs_off_the_event_loop(self):
        """Saving a new backup path copies every snapshot: it used to block the loop."""
        old = self.runtime_root / "backups"
        database = _write_v1_snapshot(old, "1" * 32, NOW)
        new = self.root / "external-snapshots"
        original = storage_api.relocate_backup_storage
        threads: list[threading.Thread] = []

        def slow_relocate(*args, **kwargs):
            threads.append(threading.current_thread())
            time.sleep(0.35)
            return original(*args, **kwargs)

        self.request.payload = json.dumps({
            "action": "save", "path": "", "backup_path": str(new),
            "retention_count": 10, "retention_days": 90}).encode()
        ticker = Ticker()
        ticker.start()
        try:
            with patch.object(storage_api, "relocate_backup_storage", new=slow_relocate):
                with patch("astrbot_host.storage_api.request", self.request):
                    response = await self.api.update()
        finally:
            await ticker.stop()

        self.assertEqual(response["status"], "ok", response)
        self.assertTrue(threads, "relocation was never called")
        self.assertTrue(all(thread is not threading.main_thread() for thread in threads),
                        "the relocation copy still ran on the event-loop thread")
        self.assertGreater(ticker.ticks, 10,
                           f"the event loop only ran {ticker.ticks} ticks during a 0.35 s relocation")
        self.assertLess(ticker.max_gap, 0.25)
        installation_key = hashlib.sha256(INSTALLATION.encode("utf-8")).hexdigest()
        destination = new / "installations" / installation_key / "backups"
        self.assertTrue((destination / database.name).is_file())
        self.assertFalse(database.exists())

    async def test_a_snapshot_a_staged_restore_depends_on_survives_cleanup(self):
        """The async cleanup path must still protect the staged request's source."""
        backup_dir = self.runtime_root / "backups"
        older = "2" * 32
        newer = "3" * 32
        _write_v1_snapshot(backup_dir, older, NOW)
        _write_v1_snapshot(backup_dir, newer, NOW.replace(hour=10))
        (self.runtime_root / PENDING_FILENAME).write_text(json.dumps({
            "format": "companion-v2.restore-request.v1", "snapshot_id": older,
            "content_hash": "0" * 64, "requested_at": NOW.isoformat()}))
        self.config["storage"]["retention_count"] = 1
        self.request.payload = b'{"action":"clean"}'
        with patch("astrbot_host.storage_api.request", self.request):
            response = await self.api.update()
        self.assertEqual(response["status"], "ok", response)
        # The policy would delete the older snapshot, but the staged request still
        # needs it as rollback material, so both pairs must remain.
        self.assertTrue((backup_dir / f"companion-v2-{older}.sqlite3").is_file())
        self.assertTrue((backup_dir / f"companion-v2-{newer}.sqlite3").is_file())

    async def test_storage_request_lock_is_one_lock_per_controller(self):
        other = NS()
        self.assertIs(storage_api.storage_request_lock(self.controller),
                      storage_api.storage_request_lock(self.controller))
        self.assertIsNot(storage_api.storage_request_lock(self.controller),
                         storage_api.storage_request_lock(other))

        class Slotted:
            __slots__ = ()

        # A controller that cannot carry the attribute still has to share one lock:
        # a lock that only lives for the length of a single call serializes nothing.
        self.assertIs(storage_api.storage_request_lock(Slotted()),
                      storage_api.storage_request_lock(Slotted()))

    async def test_relocation_waits_for_a_running_backup_operation(self):
        """Saving a new backup path copies pairs; it must not cross a snapshot."""
        started = threading.Event()
        release = threading.Event()

        def relocate_stub(*args, **kwargs):
            started.set()
            release.wait(10)
            return ()

        new = self.root / "external-snapshots"
        self.request.payload = json.dumps({
            "action": "save", "path": "", "backup_path": str(new),
            "retention_count": 10, "retention_days": 90}).encode()
        lock = backup_operation_lock(self.controller)
        self.assertTrue(lock.acquire(timeout=1))
        try:
            with patch.object(storage_api, "relocate_backup_storage", new=relocate_stub):
                with patch("astrbot_host.storage_api.request", self.request):
                    task = asyncio.create_task(self.api.update())
                    await asyncio.sleep(0.25)
                    self.assertFalse(started.is_set(),
                                     "the copy ran while a backup held the operation lock")
                    release.set()
                    lock.release()
                    response = await asyncio.wait_for(task, timeout=10)
        finally:
            if not release.is_set():
                release.set()
            if lock.locked():
                lock.release()
        self.assertEqual(response["status"], "ok", response)
        self.assertEqual(self.config["storage"]["backup_path"], str(new))

    async def test_saving_a_new_backup_path_does_not_hold_the_control_lock(self):
        """The copy may take minutes; delivery authorization must keep working."""
        old = self.runtime_root / "backups"
        _write_v1_snapshot(old, "6" * 32, NOW)
        new = self.root / "external-snapshots"
        original = storage_api.relocate_backup_storage
        observed: dict = {}

        def copy_probe(*args, **kwargs):
            observed["locked"] = self.controller._control_lock.locked()
            observed["thread"] = threading.current_thread()
            return original(*args, **kwargs)

        self.request.payload = json.dumps({
            "action": "save", "path": "", "backup_path": str(new),
            "retention_count": 10, "retention_days": 90}).encode()
        with patch.object(storage_api, "relocate_backup_storage", new=copy_probe):
            with patch("astrbot_host.storage_api.request", self.request):
                response = await self.api.update()
        self.assertEqual(response["status"], "ok", response)
        self.assertIn("locked", observed, "the backup copy was never called")
        self.assertFalse(observed["locked"],
                         "the save held the control lock while copying every snapshot")
        self.assertIsNot(observed["thread"], threading.main_thread())

    async def test_the_store_opened_on_a_worker_thread_stays_usable_from_the_loop(self):
        """Pin the SQLite threading contract these routes now rely on.

        ``_open_async`` opens the store with ``asyncio.to_thread``; the storage route
        writes to it from the loop thread (``dashboard_operator``) while backup work
        runs in other workers. That is only legal because ``StorageRuntime`` connects
        with ``check_same_thread=False`` and serializes every use with an ``RLock``.
        Nothing in inventory/retention/relocation opens SQLite itself, so the worker
        threads those routes use never share a connection at all.
        """
        from companion_v2.storage.runtime import StorageRuntime

        root = self.root / "sqlite-thread"
        opener: list[threading.Thread] = []

        def open_store():
            opener.append(threading.current_thread())
            return StorageRuntime.open(data_root=root, installation_id=INSTALLATION)

        runtime = await asyncio.to_thread(open_store)
        self.assertIsNot(opener[0], threading.main_thread(),
                         "the store must be opened off the loop, like the host does it")
        try:
            with runtime.transaction() as connection:  # the loop thread
                connection.execute(
                    "INSERT OR REPLACE INTO runtime_metadata(key, value) VALUES('r3-thread', 'loop')")
            row = await asyncio.to_thread(  # a worker thread
                runtime.read_one, "SELECT value FROM runtime_metadata WHERE key = ?", ("r3-thread",))
            self.assertEqual(row["value"], "loop")
            total = await asyncio.to_thread(
                lambda: runtime.read_one("SELECT COUNT(*) AS n FROM runtime_metadata", ())["n"])
            self.assertGreaterEqual(total, 1)
        finally:
            await asyncio.to_thread(runtime.close)
        self.assertTrue((root / StorageRuntime.DATABASE_FILENAME).is_file())


if __name__ == "__main__":
    unittest.main()
