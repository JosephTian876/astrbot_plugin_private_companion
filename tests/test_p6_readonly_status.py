"""The memory companion's read-only P6 bridge must see a real V2 registry."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
import unittest

from astrbot_host.integration_registry import UnifiedCompanionExtensionAPI
from companion_v2.diagnostics.readonly_status import (
    P6_READONLY_STATUS_FIELDS,
    P6_READONLY_STATUS_FINGERPRINT,
    P6_READONLY_STATUS_SCHEMA,
    build_p6_readonly_status,
    read_p6_readonly_status,
)


class Cursor:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return (self.value,)


class FakeConnection:
    def __init__(self, values, *, fail=False):
        self.values = iter(values)
        self.fail = fail
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)
        if self.fail:
            raise RuntimeError("database unavailable")
        return Cursor(next(self.values))


class FakeRuntime:
    def __init__(self, values=(460, 428, 31, 360), *, fail=False):
        self.values = values
        self.fail = fail
        self.connection = None

    @contextmanager
    def read_snapshot(self):
        self.connection = FakeConnection(self.values, fail=self.fail)
        yield self.connection


class P6ReadonlyStatusTests(unittest.TestCase):
    def test_contract_is_exact_and_uses_the_memory_companion_fingerprint(self):
        status = build_p6_readonly_status(
            {"profiles": 1, "identity_links": 2, "audit_events": 3, "operations": 4},
            health="ready", reason_code="")

        self.assertEqual(tuple(status), (
            "schema_version", "source_plugin", "contract_fingerprint",
            "health", "reason_code", "counts"))
        self.assertEqual(status["schema_version"], P6_READONLY_STATUS_SCHEMA)
        self.assertEqual(status["source_plugin"], "private_companion")
        self.assertEqual(status["contract_fingerprint"],
                         "a4624f25f2372194f8b3433fa19314f5238a2c6bab480bd11754faf385792600")
        self.assertEqual(tuple(status["counts"]), P6_READONLY_STATUS_FIELDS)
        self.assertEqual(status["health"], "ready")
        self.assertEqual(status["reason_code"], "")
        self.assertEqual(status["counts"], {
            "profiles": 1, "identity_links": 2, "audit_events": 3, "operations": 4})

    def test_live_runtime_counts_are_ready_and_only_numeric(self):
        runtime = FakeRuntime()
        status = read_p6_readonly_status(runtime)

        self.assertEqual(status["health"], "ready", status)
        self.assertEqual(status["counts"], {
            "profiles": 460, "identity_links": 428, "audit_events": 31, "operations": 360})
        self.assertEqual(len(runtime.connection.statements), 4)
        for statement in runtime.connection.statements:
            self.assertIn("SELECT COUNT", statement)

    def test_missing_or_broken_registry_is_unverifiable_not_fake_ready(self):
        missing = read_p6_readonly_status(None)
        broken = read_p6_readonly_status(FakeRuntime(fail=True))

        for status in (missing, broken):
            self.assertEqual(status["health"], "unverifiable")
            self.assertEqual(status["reason_code"], "registry_status_unavailable")
            self.assertEqual(status["counts"], {
                "profiles": 0, "identity_links": 0, "audit_events": 0, "operations": 0})

    def test_counts_are_bounded_integers(self):
        status = build_p6_readonly_status({
            "profiles": -1, "identity_links": 10_000_001,
            "audit_events": True, "operations": "7"})

        self.assertEqual(status["counts"], {
            "profiles": 0, "identity_links": 0, "audit_events": 0, "operations": 0})

    def test_public_facade_uses_the_active_runtime_and_falls_back_to_the_session(self):
        runtime = FakeRuntime()
        api = object.__new__(UnifiedCompanionExtensionAPI)
        api.controller = SimpleNamespace(runtime=runtime, session=None, last_session=None)

        self.assertEqual(api.get_p6_readonly_status()["health"], "ready")
        self.assertIs(api.controller.runtime, runtime)

        fallback = FakeRuntime(values=(12, 13, 14, 15))
        api.controller = SimpleNamespace(
            runtime=None,
            session=None,
            last_session=SimpleNamespace(application=SimpleNamespace(runtime=fallback)),
        )
        status = api.get_p6_readonly_status()
        self.assertEqual(status["health"], "ready", status)
        self.assertEqual(status["counts"]["profiles"], 12)


if __name__ == "__main__":
    unittest.main()
