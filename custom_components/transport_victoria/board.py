"""Trip-group board data model (T8).

Pure logic with **no** ``homeassistant`` imports: the light-env unit tests
import this module directly, and the config flow / setup paths reuse the same
functions so the board pin model and (in T10) the entity layer share one source
of truth.

A *board* is a parent config entry that groups several departure *pins* — one
per line combination — into a TripView-style departure board.  Each pin is a HA
subentry whose ``data`` holds the fields in :class:`PinData`.

Identity and ordering contracts:

* A pin's identity is a stable, deterministic key (:func:`pin_key`) computed
  from ``(route_id, boarding_stop_id, destination_stop_id, direction_id,
  platform_filter)``.  It is stored in the subentry's ``data`` **and** used as
  the subentry ``unique_id``, so reloads and re-imports never change it.
* The board's display order is an explicit list of subentry ids stored in the
  parent entry's options (``CONF_SUBENTRY_ORDER``).  Frontend subentry order is
  never trusted — :func:`normalize_order` is the canonical read.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .const import (
    CONF_ALL_DESTINATIONS,
    CONF_BOARDING_STOP_ID,
    CONF_BOARDING_STOP_NAME,
    CONF_COLOUR_OVERRIDE,
    CONF_DESTINATION_STOP_ID,
    CONF_DESTINATION_STOP_NAME,
    CONF_DIRECTION,
    CONF_INCLUDE,
    CONF_LABEL_FROM,
    CONF_LABEL_TO,
    CONF_PIN_KEY,
    CONF_PLATFORM_FILTER,
    CONF_ROUTE_ID,
    CONF_ROUTE_LONG_NAME,
    CONF_ROUTE_SHORT_NAME,
    CONF_ROUTE_TYPE,
    CONF_VIA_HINT,
    ROUTE_TYPE_BUNDLE_MAP,
)
from .gtfs.repository import (
    Destination,
    LineCandidate,
    RouteResult,
    get_lines_between,
    get_routes_for_stop,
)
from .routing.destination import destination_label, resolve_destination

# Reason codes emitted by :func:`validate_pin` when a pin no longer resolves
# against the current generation DB.
VALIDATION_OK = "ok"
VALIDATION_NO_SERVICE = "no_direct_service"   # (route, stop, dest) impossible
VALIDATION_DIRECTION_CHANGED = "direction_changed"  # pair reachable, dir moved


def pin_key(
    route_id: str,
    boarding_stop_id: str,
    destination_stop_id: str | None,
    direction_id: int | None,
    platform_filter: str | None = None,
    via_hint: str | None = None,
) -> str:
    """Return the stable identity key for a pin combination.

    Deterministic (not a UUID) so that re-deriving the same combination —
    across reloads, re-imports, or re-adding the pin in the flow — yields the
    same key.  ``destination_stop_id is None`` means "all destinations".
    ``via_hint`` is part of the identity because the resolver distinguishes two
    routings of the same (route, direction, terminal) as separate combinations.
    """
    raw = "|".join(
        (
            route_id or "",
            boarding_stop_id or "",
            destination_stop_id or "*",
            "*" if direction_id is None else str(direction_id),
            platform_filter or "*",
            via_hint or "*",
        )
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Pin model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PinData:
    """One trip-group pin (the ``data`` of a HA subentry)."""

    pin_key: str
    route_type: int
    route_id: str
    boarding_stop_id: str
    boarding_stop_name: str
    route_short_name: str = ""
    route_long_name: str = ""
    all_destinations: bool = True
    destination_stop_id: str | None = None
    destination_stop_name: str | None = None
    direction_id: int | None = None
    via_hint: str | None = None
    label_from: str = ""
    label_to: str = ""
    platform_filter: str | None = None
    colour_override: str | None = None
    include: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the subentry ``data`` mapping (CONF_* keys)."""
        return {
            CONF_PIN_KEY: self.pin_key,
            CONF_ROUTE_TYPE: self.route_type,
            CONF_ROUTE_ID: self.route_id,
            CONF_BOARDING_STOP_ID: self.boarding_stop_id,
            CONF_BOARDING_STOP_NAME: self.boarding_stop_name,
            CONF_ROUTE_SHORT_NAME: self.route_short_name,
            CONF_ROUTE_LONG_NAME: self.route_long_name,
            CONF_ALL_DESTINATIONS: self.all_destinations,
            CONF_DESTINATION_STOP_ID: self.destination_stop_id,
            CONF_DESTINATION_STOP_NAME: self.destination_stop_name,
            CONF_DIRECTION: self.direction_id,
            CONF_VIA_HINT: self.via_hint,
            CONF_LABEL_FROM: self.label_from,
            CONF_LABEL_TO: self.label_to,
            CONF_PLATFORM_FILTER: self.platform_filter,
            CONF_COLOUR_OVERRIDE: self.colour_override,
            CONF_INCLUDE: self.include,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PinData":
        """Deserialize a subentry ``data`` mapping (tolerant of missing keys)."""
        return cls(
            pin_key=str(data.get(CONF_PIN_KEY, "")),
            route_type=int(data.get(CONF_ROUTE_TYPE, 0)),
            route_id=str(data.get(CONF_ROUTE_ID, "")),
            boarding_stop_id=str(data.get(CONF_BOARDING_STOP_ID, "")),
            boarding_stop_name=str(data.get(CONF_BOARDING_STOP_NAME, "")),
            route_short_name=str(data.get(CONF_ROUTE_SHORT_NAME, "")),
            route_long_name=str(data.get(CONF_ROUTE_LONG_NAME, "")),
            all_destinations=bool(data.get(CONF_ALL_DESTINATIONS, True)),
            destination_stop_id=data.get(CONF_DESTINATION_STOP_ID),
            destination_stop_name=data.get(CONF_DESTINATION_STOP_NAME),
            direction_id=data.get(CONF_DIRECTION),
            via_hint=data.get(CONF_VIA_HINT),
            label_from=str(data.get(CONF_LABEL_FROM, "")),
            label_to=str(data.get(CONF_LABEL_TO, "")),
            platform_filter=data.get(CONF_PLATFORM_FILTER),
            colour_override=data.get(CONF_COLOUR_OVERRIDE),
            include=bool(data.get(CONF_INCLUDE, True)),
        )

    @property
    def title(self) -> str:
        """Short display title for the subentry (e.g. "220 → City")."""
        origin = self.label_from or self.boarding_stop_name or self.route_id
        if self.all_destinations:
            return f"{origin} — all services"
        return f"{origin} → {self.label_to or self.destination_stop_name or ''}"


# ---------------------------------------------------------------------------
# Label helpers (reuse the T7 formatting scheme)
# ---------------------------------------------------------------------------


def route_title(route_short_name: str, route_long_name: str) -> str:
    """Return the display title for a route (short name preferred)."""
    short = (route_short_name or "").strip()
    long = (route_long_name or "").strip()
    return short or long


def line_value(line: LineCandidate) -> str:
    """Stable selector value for a line candidate (mirrors ``destination_value``)."""
    return (
        f"{line.route_id}::{line.direction_id}::{line.terminal_stop_id}"
        f"::{line.via_hint or ''}"
    )


def default_label_from(
    route_short_name: str,
    route_long_name: str,
    stop_name: str,
) -> str:
    """Default origin label: ``"{route title} from {stop name}"``."""
    title = route_title(route_short_name, route_long_name)
    return f"{title} from {stop_name}"


def default_label_to(
    all_destinations: bool,
    terminal_stop_id: str | None,
    terminal_stop_name: str | None,
    via_hint: str | None,
    direction_id: int | None,
) -> str:
    """Default destination label, reusing T7's ``destination_label``."""
    if all_destinations:
        return "All destinations"
    return destination_label(
        Destination(
            direction_id=direction_id,
            terminal_stop_id=terminal_stop_id or "",
            terminal_stop_name=terminal_stop_name or "",
            via_hint=via_hint,
        )
    )


# ---------------------------------------------------------------------------
# Pin derivation (resolver-backed)
# ---------------------------------------------------------------------------


def pin_from_line(
    line: LineCandidate,
    boarding_stop_id: str,
    boarding_stop_name: str,
    *,
    platform_filter: str | None = None,
    colour_override: str | None = None,
) -> PinData:
    """Build a specific-destination pin from a line candidate (T7 resolver)."""
    key = pin_key(
        line.route_id,
        boarding_stop_id,
        line.terminal_stop_id,
        line.direction_id,
        platform_filter,
        line.via_hint,
    )
    return PinData(
        pin_key=key,
        route_type=line.route_type or 0,
        route_id=line.route_id,
        boarding_stop_id=boarding_stop_id,
        boarding_stop_name=boarding_stop_name,
        route_short_name=line.route_short_name,
        route_long_name=line.route_long_name,
        all_destinations=False,
        destination_stop_id=line.terminal_stop_id,
        destination_stop_name=line.terminal_stop_name,
        direction_id=line.direction_id,
        via_hint=line.via_hint,
        label_from=default_label_from(
            line.route_short_name, line.route_long_name, boarding_stop_name
        ),
        label_to=default_label_to(
            False,
            line.terminal_stop_id,
            line.terminal_stop_name,
            line.via_hint,
            line.direction_id,
        ),
        platform_filter=platform_filter,
        colour_override=colour_override,
    )


def pin_from_route_all(
    route: RouteResult,
    boarding_stop_id: str,
    boarding_stop_name: str,
    *,
    platform_filter: str | None = None,
    colour_override: str | None = None,
) -> PinData:
    """Build an "all destinations" pin for a route serving the origin stop."""
    key = pin_key(route.route_id, boarding_stop_id, None, None, platform_filter)
    return PinData(
        pin_key=key,
        route_type=route.route_type or 0,
        route_id=route.route_id,
        boarding_stop_id=boarding_stop_id,
        boarding_stop_name=boarding_stop_name,
        route_short_name=route.route_short_name,
        route_long_name=route.route_long_name,
        all_destinations=True,
        destination_stop_id=None,
        destination_stop_name=None,
        direction_id=None,
        label_from=default_label_from(
            route.route_short_name, route.route_long_name, boarding_stop_name
        ),
        label_to=default_label_to(True, None, None, None, None),
        platform_filter=platform_filter,
        colour_override=colour_override,
    )


def line_pins(
    db_path: Path,
    boarding_stop_id: str,
    boarding_stop_name: str,
    destination_stop_id: str,
    route_types: list[int] | None = None,
) -> list[PinData]:
    """Derive one pin per (route, direction, via) serving the stop pair."""
    lines = get_lines_between(
        db_path, boarding_stop_id, destination_stop_id, route_types=route_types
    )
    return [
        pin_from_line(line, boarding_stop_id, boarding_stop_name) for line in lines
    ]


def all_destination_pins(
    db_path: Path,
    boarding_stop_id: str,
    boarding_stop_name: str,
    route_types: list[int],
) -> list[PinData]:
    """Derive one "all destinations" pin per route serving the origin stop."""
    routes = get_routes_for_stop(db_path, boarding_stop_id, route_types)
    return [
        pin_from_route_all(route, boarding_stop_id, boarding_stop_name)
        for route in routes
    ]


# ---------------------------------------------------------------------------
# Schedule-invalidated validation (revalidate against the current generation)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PinValidation:
    """Outcome of revalidating a pin against the current generation DB."""

    ok: bool
    reason: str | None = None
    direction_id: int | None = None
    candidates: list[Destination] = field(default_factory=list)


def validate_pin(db_path: Path, pin: PinData) -> PinValidation:
    """Revalidate a pin via ``resolve_destination``.

    An "all destinations" pin resolves the route's termini; an empty candidate
    list means the route no longer has servable termini.  A specific-destination
    pin whose (route, stop, destination) pair no longer reaches is invalid
    (``no_direct_service``); a pair that still reaches but whose stored direction
    is no longer among the candidates is flagged ``direction_changed`` so the
    repair path can re-derive it without deleting the pin.
    """
    resolution = resolve_destination(
        db_path, pin.route_id, pin.boarding_stop_id, pin.destination_stop_id
    )
    if not resolution.candidates:
        return PinValidation(
            ok=False,
            reason=resolution.reason or VALIDATION_NO_SERVICE,
        )

    if pin.direction_id is not None:
        directions = {c.direction_id for c in resolution.candidates}
        if pin.direction_id not in directions:
            return PinValidation(
                ok=False,
                reason=VALIDATION_DIRECTION_CHANGED,
                direction_id=resolution.candidates[0].direction_id,
                candidates=resolution.candidates,
            )

    return PinValidation(
        ok=True,
        direction_id=pin.direction_id,
        candidates=resolution.candidates,
    )


# ---------------------------------------------------------------------------
# Consumer-union bundle bookkeeping
# ---------------------------------------------------------------------------


def pin_bundles(pin: PinData) -> frozenset[str]:
    """Return the bundle folders a pin's route_type requires."""
    return ROUTE_TYPE_BUNDLE_MAP.get(pin.route_type, frozenset())


def board_bundles(pins: Iterable[PinData]) -> frozenset[str]:
    """Return the union of bundle folders required by a board's pins."""
    bundles: set[str] = set()
    for pin in pins:
        bundles |= pin_bundles(pin)
    return frozenset(bundles)


# ---------------------------------------------------------------------------
# Ordering (explicit, lives in parent options)
# ---------------------------------------------------------------------------


def normalize_order(
    ordered: Iterable[str],
    all_subentry_ids: Iterable[str],
) -> list[str]:
    """Return the canonical ordered list of subentry ids.

    Preserves the stored order for ids that still exist, then appends any
    subentry ids missing from the stored order (so a pin added out-of-band or a
    partial order never drops a pin from the board).
    """
    all_ids = list(all_subentry_ids)
    present = [sid for sid in ordered if sid in all_ids]
    for sid in all_ids:
        if sid not in present:
            present.append(sid)
    return present


def move_subentry(
    ordered: Iterable[str],
    subentry_id: str,
    direction: int,
) -> list[str]:
    """Return a copy of ``ordered`` with ``subentry_id`` moved by ``direction``.

    ``direction`` is ``-1`` (up) or ``+1`` (down); a no-op at the boundaries.
    """
    result = list(ordered)
    if subentry_id not in result or len(result) < 2:
        return result
    idx = result.index(subentry_id)
    new_idx = idx + direction
    if new_idx < 0 or new_idx >= len(result):
        return result
    result[idx], result[new_idx] = result[new_idx], result[idx]
    return result
