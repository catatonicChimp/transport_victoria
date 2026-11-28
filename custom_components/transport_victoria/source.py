"""Source (hub) entry helpers — the ownership/readiness contract (T18.1).

One ``entry_type: "source"`` config entry owns the integration's credential
(optional — see scheduled-only) and schedule lifecycle.  Feature entries
(watch/board/journey/route-tracker) are *associated* with it via
``source_entry_id`` in their data and no longer store credentials themselves.

These helpers are pure lookups shared by the coordinators, platforms, config
flow, and ``__init__``.  They are deliberately defensive (``getattr`` guards) so
they also work against the light-env test doubles, which may lack a
``config_entries`` manager or a ``hass.data`` tree.

Nothing here imports ``__init__`` (which imports the coordinators), so there is
no circular-import risk for coordinator/platform callers.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from .const import (
    CONF_API_KEY,
    CONF_ENTRY_TYPE,
    CONF_REALTIME_ENABLED,
    CONF_SOURCE_ENTRY_ID,
    DOMAIN,
    ENTRY_TYPE_SOURCE,
    MODE_BUNDLE_MAP,
    READINESS_CONFIGURED,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

# Serializes source-hub creation (migration + terminal flow creation) so two
# concurrent flows cannot both observe "no hub" and create two hubs.  Created at
# import time (Python 3.10+ allows this — the lock does not bind to a loop until
# first contended); once contended it is bound to that loop, which is fine for a
# single-runner HA process.
_SOURCE_LOCK = asyncio.Lock()


def source_lock() -> "asyncio.Lock":
    """Return the module-level source-creation lock (F2 singleton serialization)."""
    return _SOURCE_LOCK


def is_source_entry(entry: "ConfigEntry") -> bool:
    """Return True when ``entry`` is the source (hub) entry."""
    return entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SOURCE


def get_source_entry(hass: "HomeAssistant") -> "ConfigEntry | None":
    """Return the single live source entry, or None when none exists."""
    config_entries = getattr(hass, "config_entries", None)
    if config_entries is None:
        return None
    for entry in config_entries.async_entries(DOMAIN):
        if is_source_entry(entry):
            return entry
    return None


def get_source_entry_id(hass: "HomeAssistant") -> str | None:
    """Return the source entry's id, or None when no hub exists."""
    entry = get_source_entry(hass)
    return entry.entry_id if entry is not None else None


def get_source_api_key(hass: "HomeAssistant") -> str | None:
    """Return the source entry's API key, or None (scheduled-only)."""
    entry = get_source_entry(hass)
    if entry is None:
        return None
    key = entry.data.get(CONF_API_KEY)
    return str(key) if key else None


def source_disabled(hass: "HomeAssistant") -> bool:
    """Return True when the source hub is disabled (dependents go unavailable)."""
    hub = get_source_entry(hass)
    return hub is not None and getattr(hub, "disabled_by", None) is not None


def source_is_scheduled_only(hass: "HomeAssistant") -> bool:
    """Return True when a source hub exists but has no credential.

    Intentional scheduled-only mode.  Returns False when there is no source hub
    at all (legacy entries / tests) — absence of a hub is not "keyless".
    """
    hub = get_source_entry(hass)
    if hub is None:
        return False
    return get_source_api_key(hass) is None


def source_for_entry(hass: "HomeAssistant", entry: "ConfigEntry") -> "ConfigEntry | None":
    """Return the source entry associated with ``entry`` (fallback: the hub).

    Prefers the explicit ``source_entry_id`` (set at creation / migration); falls
    back to the single live hub so a legacy entry without the association field
    still resolves to the credential owner.
    """
    sid = entry.data.get(CONF_SOURCE_ENTRY_ID)
    if sid:
        config_entries = getattr(hass, "config_entries", None)
        if config_entries is not None:
            hub = config_entries.async_get_entry(sid)
            if hub is not None:
                return hub
    return get_source_entry(hass)


def source_readiness(hass: "HomeAssistant", hub_id: str) -> str:
    """Return the hub's readiness state (default CONFIGURED)."""
    stored = hass.data.get(DOMAIN, {}).get(hub_id, {})
    return stored.get("readiness", READINESS_CONFIGURED)


def _feature_realtime_requested(entry: "ConfigEntry") -> bool:
    """Return whether the feature entry requests realtime (its own option)."""
    return bool(
        entry.options.get(
            CONF_REALTIME_ENABLED,
            entry.data.get(CONF_REALTIME_ENABLED, True),
        )
    )


def effective_realtime_enabled(hass: "HomeAssistant", entry: "ConfigEntry") -> bool:
    """Return whether realtime is usable for ``entry``.

    Effective capability = the feature *requested* realtime AND a credential is
    configured on the hub AND the hub is not disabled.  Schedule-health (a
    ``SCHEDULE_FAILED`` readiness, a stale/expired generation) does NOT disable
    realtime here — the coordinators already gate on ``schedule_state`` for
    staleness, and a failed *refresh* only pauses the lifecycle, not the current
    (possibly still-usable) data.
    """
    if not _feature_realtime_requested(entry):
        return False
    if source_disabled(hass):
        return False
    if get_source_api_key(hass) is None:
        return False
    return get_source_entry(hass) is not None


def find_dependents(hass: "HomeAssistant", hub_id: str) -> list["ConfigEntry"]:
    """Return every feature entry associated with ``hub_id`` (never the hub)."""
    config_entries = getattr(hass, "config_entries", None)
    if config_entries is None:
        return []
    return [
        entry
        for entry in config_entries.async_entries(DOMAIN)
        if not is_source_entry(entry)
        and entry.data.get(CONF_SOURCE_ENTRY_ID) == hub_id
    ]


def all_mode_bundles() -> list[str]:
    """Return the union of every mode's schedule bundle folders (import-all).

    v1 bundle policy: the source hub imports ALL network bundles at first setup.
    """
    folders: set[str] = set()
    for folders_for_mode in MODE_BUNDLE_MAP.values():
        folders.update(folders_for_mode)
    return sorted(folders)


async def async_reload_dependents(hass: "HomeAssistant", hub_id: str) -> None:
    """Reload every feature entry associated with ``hub_id``.

    Used when the hub's credential changes (reconfigure) so dependents rebuild
    their realtime client with the new key.  Entries that are not yet loaded are
    left alone (their first setup reads the new credential).
    """
    config_entries = getattr(hass, "config_entries", None)
    if config_entries is None:
        return
    for entry in find_dependents(hass, hub_id):
        await config_entries.async_reload(entry.entry_id)


async def async_remove_dependents(hass: "HomeAssistant", hub_id: str) -> list[str]:
    """Remove every feature entry associated with ``hub_id`` (deletion cascade).

    Returns the titles of the entries removed (for the confirm step / logging).
    """
    config_entries = getattr(hass, "config_entries", None)
    if config_entries is None:
        return []
    removed: list[str] = []
    for entry in list(find_dependents(hass, hub_id)):
        title = entry.title
        await config_entries.async_remove(entry.entry_id)
        removed.append(title)
    return removed
