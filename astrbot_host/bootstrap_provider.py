"""Resolve an automatic shared text provider without changing user config."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BootstrapProvider:
    provider_id: str
    name: str = ""
    source: str = "unavailable"  # configured | auto | fallback | unavailable


def _meta(provider):
    getter = getattr(provider, "meta", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            return None
    return None


def provider_identifier(provider) -> str:
    meta = _meta(provider)
    identifier = getattr(meta, "id", None)
    if not isinstance(identifier, str) or not identifier.strip():
        config = getattr(provider, "provider_config", None)
        identifier = config.get("id") if isinstance(config, dict) else None
    if not isinstance(identifier, str):
        identifier = getattr(provider, "id", None)
    return identifier.strip() if isinstance(identifier, str) else ""


def provider_name(provider, identifier: str = "") -> str:
    meta = _meta(provider)
    name = getattr(meta, "name", None)
    if not isinstance(name, str) or not name.strip():
        name = getattr(provider, "name", None)
    return (name.strip() if isinstance(name, str) and name.strip() else identifier)[:120]


def _supported(provider, expected_type) -> bool:
    """``expected_type`` 可以是类、类元组，或 native_types 的文本 provider 判定器。"""
    if expected_type is None:
        return True
    if callable(expected_type) and not isinstance(expected_type, type):
        return bool(expected_type(provider))
    return isinstance(provider, expected_type)


def supported_text_provider(provider, expected_type) -> bool:
    """Public form used by setup routes that check a chosen provider."""
    return _supported(provider, expected_type)


def text_provider_predicate(native_types):
    """The registered classes plus any provider exposing the text surface."""
    checker = getattr(native_types, "is_text_provider", None)
    return checker if callable(checker) else getattr(native_types, "provider_types", None)


def text_provider_catalog(context, expected_type) -> list[dict]:
    """Return loaded providers that the V2 text transport can wrap."""
    try:
        inventory = list(context.get_all_providers() or [])
    except Exception:
        inventory = []
    found = {}
    for provider in inventory:
        try:
            if not _supported(provider, expected_type):
                continue
            identifier = provider_identifier(provider)
            if not identifier:
                continue
            found[identifier] = {"id": identifier, "name": provider_name(provider, identifier)}
        except (AttributeError, TypeError, ValueError):
            continue
    return sorted(found.values(), key=lambda item: (item["name"].lower(), item["id"]))


def resolve_bootstrap_provider(context, expected_type, configured_id: str = "") -> BootstrapProvider:
    """Prefer the reviewed ID, then AstrBot's current/default provider.

    The fallback is intentionally read-only.  The account page can show it as
    an automatic choice and the host can start without asking the user to save
    a second copy of AstrBot's already-selected model.
    """
    configured = str(configured_id or "").strip()
    catalog = text_provider_catalog(context, expected_type)
    by_id = {item["id"]: item for item in catalog}
    if configured:
        provider = None
        try:
            provider = context.get_provider_by_id(configured)
        except Exception:
            provider = None
        if provider is not None and _supported(provider, expected_type):
            return BootstrapProvider(configured, by_id.get(configured, {}).get(
                "name") or provider_name(provider, configured), "configured")
        if configured in by_id:
            return BootstrapProvider(configured, by_id[configured]["name"], "configured")

    active = None
    try:
        getter = getattr(context, "get_using_provider", None)
        if callable(getter):
            active = getter()
    except Exception:
        active = None
    if active is not None and _supported(active, expected_type):
        identifier = provider_identifier(active)
        if identifier:
            return BootstrapProvider(identifier, by_id.get(identifier, {}).get(
                "name") or provider_name(active, identifier), "fallback" if configured else "auto")

    if catalog:
        first = catalog[0]
        return BootstrapProvider(first["id"], first["name"], "fallback" if configured else "auto")
    return BootstrapProvider("", "", "unavailable")
