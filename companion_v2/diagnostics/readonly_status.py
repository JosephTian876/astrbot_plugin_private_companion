"""Bounded P6 counts for the memory-companion read-only bridge.

The contract is intentionally numeric-only: no profiles, identity values,
relationship text, session identifiers or operation bodies cross this surface.
The fingerprint is part of the cross-plugin contract and must stay byte-for-
byte compatible with ``astrbot_plugin_memory_companion``'s projection.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


P6_READONLY_STATUS_SCHEMA = "ops.p6.read_only_status.v1"
P6_READONLY_STATUS_FIELDS = ("profiles", "identity_links", "audit_events", "operations")
_HEALTH = frozenset({"ready", "degraded", "unverifiable"})
_MAX_COUNT = 10_000_000


def _fingerprint() -> str:
    payload = {
        "schema_version": P6_READONLY_STATUS_SCHEMA,
        "count_fields": P6_READONLY_STATUS_FIELDS,
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


P6_READONLY_STATUS_FINGERPRINT = _fingerprint()


def _count(value: Any) -> int:
    return value if type(value) is int and 0 <= value <= _MAX_COUNT else 0


def build_p6_readonly_status(
    counts: Any,
    *,
    health: str = "ready",
    reason_code: str = "",
) -> dict[str, Any]:
    """Return the exact six-field P6 projection.

    ``counts`` must be a mapping when the registry is readable. Passing ``None``
    makes the result unverifiable instead of pretending an empty registry is a
    healthy one.
    """

    available = isinstance(counts, Mapping) and all(type(key) is str for key in counts)
    source = counts if available else {}
    normalized_health = health if type(health) is str and health in _HEALTH else "unverifiable"
    normalized_reason = reason_code if type(reason_code) is str and len(reason_code) <= 80 else "invalid_reason_code"
    if not available:
        normalized_health = "unverifiable"
        normalized_reason = "registry_status_unavailable"
    return {
        "schema_version": P6_READONLY_STATUS_SCHEMA,
        "source_plugin": "private_companion",
        "contract_fingerprint": P6_READONLY_STATUS_FINGERPRINT,
        "health": normalized_health,
        "reason_code": normalized_reason,
        "counts": {field: _count(source.get(field)) for field in P6_READONLY_STATUS_FIELDS},
    }


def _count_value(connection: Any, statement: str) -> int:
    row = connection.execute(statement).fetchone()
    return _count(row[0] if row is not None else 0)


def read_p6_readonly_status(runtime: Any) -> dict[str, Any]:
    """Read bounded registry counts from the active V2 storage runtime."""

    if runtime is None:
        return build_p6_readonly_status(None)
    try:
        with runtime.read_snapshot() as connection:
            counts = {
                "profiles": _count_value(
                    connection,
                    "SELECT COUNT(DISTINCT owner_key) FROM memory_views "
                    "WHERE kind = 'person.acquaintance.v1' AND state = 'active'",
                ),
                "identity_links": _count_value(
                    connection,
                    "SELECT COUNT(DISTINCT binding_id) FROM identity_bindings",
                ),
                "audit_events": _count_value(
                    connection,
                    "SELECT COUNT(*) FROM persona_binding_events",
                ),
                "operations": _count_value(
                    connection,
                    "SELECT COUNT(*) FROM operations",
                ),
            }
    except Exception:
        return build_p6_readonly_status(None)
    return build_p6_readonly_status(counts, health="ready", reason_code="")


__all__ = [
    "P6_READONLY_STATUS_FIELDS",
    "P6_READONLY_STATUS_FINGERPRINT",
    "P6_READONLY_STATUS_SCHEMA",
    "build_p6_readonly_status",
    "read_p6_readonly_status",
]
