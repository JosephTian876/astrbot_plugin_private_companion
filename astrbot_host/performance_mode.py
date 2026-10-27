"""Low-resource page mode shared by the plugin host and dashboard page.

Performance mode is deliberately page-focused.  It never changes chat
authorization, memory ownership, or native extension policy; it only removes
the heavy cottage UI and the management routes that UI needs.  A plugin reload
is required because AstrBot discovers plugin pages and web routes at load time.
"""

from __future__ import annotations

from typing import Any


SECTION = "performance"
RELOAD_REQUIRED_MESSAGE = "保存后请重载插件，性能模式才会生效。"


def _enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return False


def performance_enabled(config: Any) -> bool:
    """Return whether the official plugin config enables performance mode."""

    if not isinstance(config, dict):
        return False
    section = config.get(SECTION)
    if not isinstance(section, dict):
        return False
    return _enabled(section.get("enabled", False))


def performance_status(config: Any, *, enabled: bool | None = None) -> dict[str, Any]:
    """Describe the effective page/runtime profile without exposing config.

    ``enabled`` is the startup-effective value recorded when routes were
    registered.  It prevents a saved-but-not-yet-reloaded switch from showing
    the full page while its management routes are still intentionally absent.
    """

    effective = performance_enabled(config) if enabled is None else bool(enabled)
    return {
        "enabled": effective,
        "mode": "performance" if effective else "standard",
        "cottage_enabled": not effective,
        "official_page_only": effective,
        "lite_page": effective,
        "heavy_management_routes": not effective,
        "requires_reload": True,
        "message": RELOAD_REQUIRED_MESSAGE if effective else "",
    }
