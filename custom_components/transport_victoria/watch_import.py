"""T11 watch→board import compatibility mapper.

Pure module (**no** ``homeassistant`` imports) that maps a WATCH config entry's
``data`` into a trip-group board *pin* candidate, so the config-flow import step
can offer an OPT-IN, non-destructive "import my watches as a board" path.

Nothing here creates, deletes, or mutates a source entry.  It only *reads* watch
data + the local GTFS generation and returns a classification + a derived
:class:`~.board.PinData` (built through the SAME derivation path T8 uses —
:func:`~.board.pin_from_route_all` / :func:`~.board.pin_from_line` — so labels
and identity match manual creation).

Compatibility rules (see :func:`map_watch_to_pin`):

* **Trackers** (vehicle / route) → incompatible (``REASON_TRACKER``).
* **Transfer journeys** (``CONF_JOURNEY_MODE``) → incompatible (``REASON_JOURNEY``).
* A watch with **no line filter** that spans more than one route at its stop →
  incompatible (``REASON_NO_ROUTE``); a single-route stop resolves 1:1.
* **Missing / NULL keys** (platform, colour override, via hint, direction) map
  to explicit ``None`` and are recorded in ``WatchMapping.notes`` — never
  silently dropped.
* **Stale references** (route / stop / destination ids no longer present in the
  current generation) are mapped with a ``stale`` flag + ``stale_fields`` and
  are still imported — never silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .board import PinData, pin_from_line, pin_from_route_all
from .const import (
    CONF_ALL_DESTINATIONS,
    CONF_DESTINATION_STOP_ID,
    CONF_DESTINATION_STOP_NAME,
    CONF_DIRECTION,
    CONF_ENTRY_TYPE,
    CONF_JOURNEY_MODE,
    CONF_MODE,
    CONF_PLATFORM_FILTER,
    CONF_ROUTE_ID_FILTER,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    ENTRY_TYPE_ROUTE_TRACKER,
    ENTRY_TYPE_VEHICLE_TRACKER,
    ENTRY_TYPE_WATCH,
    ROUTE_TYPE_MAP,
)
from .gtfs.repository import (
    LineCandidate,
    RouteResult,
    get_lines_between,
    get_routes_for_stop,
    get_stop_lat_lon,
)

# ---------------------------------------------------------------------------
# Status / reason codes
# ---------------------------------------------------------------------------

COMPATIBLE = "compatible"
INCOMPATIBLE = "incompatible"

# Incompatibility reason codes (reported verbatim in the import summary).
REASON_JOURNEY = "journey"          # transfer journeys stay as-is
REASON_TRACKER = "tracker"          # vehicle/route trackers stay as-is
REASON_NO_ROUTE = "no_route"        # all-lines watch spans several routes
REASON_MISSING_STOP = "missing_stop"  # watch carries no boarding stop

# Human labels for each reason (used by the config-flow summary step).
REASON_LABELS: dict[str, str] = {
    REASON_JOURNEY: "transfer journey (stays as-is)",
    REASON_TRACKER: "vehicle/route tracker (stays as-is)",
    REASON_NO_ROUTE: "all-lines watch (no single line; stays as-is)",
    REASON_MISSING_STOP: "no boarding stop (stays as-is)",
}

# Stale-reference field names (set on ``WatchMapping.stale_fields``).
STALE_ROUTE = "route_id"
STALE_STOP = "stop_id"
STALE_DESTINATION = "destination_stop_id"

STALE_LABELS: dict[str, str] = {
    STALE_ROUTE: "line no longer in the current timetable",
    STALE_STOP: "stop no longer in the current timetable",
    STALE_DESTINATION: "destination no longer reachable in the current timetable",
}


@dataclass(slots=True)
class WatchMapping:
    """Outcome of mapping one watch entry's data onto the pin schema."""

    compatible: bool
    reason: str | None = None          # set when incompatible (REASON_*)
    pin: PinData | None = None         # set when compatible
    stale: bool = False                # any stale reference flagged
    stale_fields: list[str] = field(default_factory=list)  # STALE_* names
    notes: list[str] = field(default_factory=list)         # missing/NULL notes


def _match_route(routes: list[RouteResult], route_id: str) -> RouteResult | None:
    """Return the route in ``routes`` whose id is ``route_id``, else None."""
    return next((r for r in routes if r.route_id == route_id), None)


def _match_line(lines: list[LineCandidate], direction: int | None) -> LineCandidate:
    """Pick the line matching ``direction`` (or the first, deterministically).

    The resolver sorts candidates, so the fallback is stable.  A watch stores a
    single ``direction_id`` but not a ``via_hint``; when several routings share
    the direction we pick the first (footscray-style) via deterministically and
    note the ambiguity downstream via the pin's own ``via_hint``.
    """
    if direction is None:
        return lines[0]
    return next((l for l in lines if l.direction_id == direction), lines[0])


def map_watch_to_pin(db_path: Path, data: Mapping[str, Any]) -> WatchMapping:
    """Map one watch entry's ``data`` onto a board pin candidate.

    Args:
        db_path: GTFS generation anchor (resolved via ``store.resolve``).
        data: The watch config entry's ``data`` mapping (CONF_* keys).

    Returns:
        :class:`WatchMapping` — ``compatible`` False (with ``reason``) when the
        watch cannot become a single pin; otherwise ``compatible`` True with a
        derived ``pin`` and (optionally) ``stale`` flags + ``notes``.
    """
    entry_type = data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_WATCH)
    if entry_type in (ENTRY_TYPE_VEHICLE_TRACKER, ENTRY_TYPE_ROUTE_TRACKER):
        return WatchMapping(False, reason=REASON_TRACKER)

    if data.get(CONF_JOURNEY_MODE):
        return WatchMapping(False, reason=REASON_JOURNEY)

    stop_id = data.get(CONF_STOP_ID)
    stop_name = str(data.get(CONF_STOP_NAME, "") or "")
    mode = data.get(CONF_MODE, "")
    route_types = ROUTE_TYPE_MAP.get(mode, [])
    route_filter = data.get(CONF_ROUTE_ID_FILTER)

    if not stop_id:
        return WatchMapping(False, reason=REASON_MISSING_STOP)

    notes: list[str] = []
    stale_fields: list[str] = []

    # --- Stop staleness (independent of route resolution) ------------------
    if get_stop_lat_lon(db_path, str(stop_id)) is None:
        stale_fields.append(STALE_STOP)
        notes.append(f"stop_id {stop_id} not in the current timetable")

    # --- Route resolution ---------------------------------------------------
    routes_at_stop = get_routes_for_stop(db_path, str(stop_id), route_types)
    route: RouteResult | None = None

    if route_filter:
        route = _match_route(routes_at_stop, str(route_filter))
        if route is None:
            # The route no longer serves this stop (or is gone) in the current
            # generation — flag it, but still import (never silently drop).
            stale_fields.append(STALE_ROUTE)
            notes.append(f"route_id {route_filter} no longer serves this stop")
            route = RouteResult(
                route_id=str(route_filter),
                route_short_name="",
                route_long_name="",
                route_color=None,
                route_type=route_types[0] if route_types else None,
            )
    else:
        # No line filter: "all lines" at this stop.
        if len(routes_at_stop) == 1:
            route = routes_at_stop[0]
            notes.append("no line filter; single line at the stop resolved")
        else:
            if not routes_at_stop:
                stale_fields.append(STALE_ROUTE)
                notes.append("no lines found at this stop in the current timetable")
            return WatchMapping(
                False,
                reason=REASON_NO_ROUTE,
                stale=bool(stale_fields),
                stale_fields=stale_fields,
                notes=notes,
            )

    assert route is not None

    # --- Optional field mapping (missing/NULL → explicit None + note) -------
    platform_filter = data.get(CONF_PLATFORM_FILTER)
    if platform_filter is None:
        notes.append("platform_filter missing → None")
    notes.append("colour_override not carried by a watch → None")

    direction = data.get(CONF_DIRECTION)
    all_destinations = bool(data.get(CONF_ALL_DESTINATIONS, True))

    # --- Pin derivation (T8 path) ------------------------------------------
    if all_destinations:
        pin = pin_from_route_all(
            route, str(stop_id), stop_name, platform_filter=platform_filter
        )
        if direction is not None:
            notes.append("stored direction ignored for an all-destinations pin")
    else:
        dest_id = data.get(CONF_DESTINATION_STOP_ID)
        dest_name = str(data.get(CONF_DESTINATION_STOP_NAME, "") or "")
        if not dest_id:
            # Specific destination with no id — map to all-destinations (rule b).
            notes.append("destination_stop_id missing → imported as all destinations")
            pin = pin_from_route_all(
                route, str(stop_id), stop_name, platform_filter=platform_filter
            )
        else:
            lines = get_lines_between(
                db_path, str(stop_id), str(dest_id), route_types=route_types
            )
            if not lines:
                stale_fields.append(STALE_DESTINATION)
                notes.append(
                    f"destination {dest_id} no longer reachable in the current timetable"
                )
                pin = _fallback_specific_pin(
                    route, str(stop_id), stop_name, str(dest_id), dest_name,
                    direction, platform_filter,
                )
            else:
                line = _match_line(lines, direction)
                pin = pin_from_line(
                    line, str(stop_id), stop_name, platform_filter=platform_filter
                )
                if direction is not None and line.direction_id != direction:
                    notes.append(
                        "stored direction differs from the resolver; used the resolver's"
                    )

    return WatchMapping(
        True,
        pin=pin,
        stale=bool(stale_fields),
        stale_fields=stale_fields,
        notes=notes,
    )


def _fallback_specific_pin(
    route: RouteResult,
    stop_id: str,
    stop_name: str,
    dest_id: str,
    dest_name: str,
    direction: int | None,
    platform_filter: str | None,
) -> PinData:
    """Build a specific-destination pin when the resolver returns no lines.

    Used only for a stale destination (flagged, never dropped).  Mirrors
    :func:`~.board.pin_from_line`'s field shape (labels via the same helpers)
    but without a resolver ``LineCandidate``, so ``via_hint`` stays ``None``.
    """
    from .board import default_label_from, default_label_to, pin_key

    key = pin_key(
        route.route_id, stop_id, dest_id, direction, platform_filter, None
    )
    return PinData(
        pin_key=key,
        route_type=route.route_type or 0,
        route_id=route.route_id,
        boarding_stop_id=stop_id,
        boarding_stop_name=stop_name,
        route_short_name=route.route_short_name,
        route_long_name=route.route_long_name,
        all_destinations=False,
        destination_stop_id=dest_id,
        destination_stop_name=dest_name,
        direction_id=direction,
        via_hint=None,
        label_from=default_label_from(
            route.route_short_name, route.route_long_name, stop_name
        ),
        label_to=default_label_to(
            False, dest_id, dest_name, None, direction
        ),
        platform_filter=platform_filter,
        colour_override=None,
    )


def map_watches(
    db_path: Path,
    entries_data: list[tuple[str, Mapping[str, Any]]],
) -> dict[str, WatchMapping]:
    """Map several watch entries' data in one call (offloadable from a flow)."""
    return {
        entry_id: map_watch_to_pin(db_path, data)
        for entry_id, data in entries_data
    }
