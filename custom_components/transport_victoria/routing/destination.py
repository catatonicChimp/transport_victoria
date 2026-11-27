"""Destination selection and direction derivation (T7).

The resolver here is the single API underpinning watches (and, later, T8 trip
groups): it maps ``(route_id, boarding_stop_id, destination_stop_id)`` onto one
or more direction candidates derived from actual trip evidence, rather than an
explicit ``direction_id`` step.

Label/value helpers are pure functions so the config flow and the T8 group flow
share one formatting scheme.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..gtfs.repository import (
    Destination,
    LineCandidate,
    collapse_direction_candidates,
    get_terminals_for_route,
    load_trip_shapes,
    reaching_trip_ids,
)


@dataclass(slots=True)
class Resolution:
    """Outcome of resolving a destination to one or more direction candidates.

    ``candidates`` is empty when the pair is impossible, in which case
    ``reason`` carries a stable, translatable code (``"no_direct_service"``).
    """

    candidates: list[Destination] = field(default_factory=list)
    reason: str | None = None

    @property
    def resolved(self) -> Destination | None:
        """Return the single candidate when unambiguous, else None."""
        return self.candidates[0] if len(self.candidates) == 1 else None

    @property
    def ambiguous(self) -> bool:
        """True when more than one direction candidate matches."""
        return len(self.candidates) > 1


def resolve_destination(
    db_path: Path,
    route_id: str,
    boarding_stop_id: str,
    destination_stop_id: str | None = None,
) -> Resolution:
    """Derive the direction(s) for a destination from trip evidence.

    Args:
        db_path: Logical DB anchor (resolved to the current generation).
        route_id: GTFS route_id of the line being watched.
        boarding_stop_id: The origin stop the user boards at.
        destination_stop_id: The chosen terminal stop, or None for "all
            destinations" (in which case every terminus of the route is
            returned as a candidate).

    Returns:
        A :class:`Resolution` with zero (impossible pair + reason), one
        (unambiguous direction) or several (loop / both-directions ambiguity)
        candidates.
    """
    if destination_stop_id is None:
        return Resolution(candidates=get_terminals_for_route(db_path, route_id))

    trip_ids = reaching_trip_ids(
        db_path, boarding_stop_id, destination_stop_id, route_id=route_id
    )
    if not trip_ids:
        return Resolution(candidates=[], reason="no_direct_service")

    trips = load_trip_shapes(db_path, trip_ids=trip_ids)
    return Resolution(candidates=collapse_direction_candidates(trips, destination_stop_id))


def destination_label(destination: Destination) -> str:
    """Human label for a destination candidate: name, plus "via X" when needed."""
    if destination.via_hint:
        return f"{destination.terminal_stop_name} via {destination.via_hint}"
    return destination.terminal_stop_name


def destination_value(destination: Destination) -> str:
    """Stable, unambiguous selector value for a destination candidate.

    Encodes (terminal, direction, via) so two candidates that share a terminus
    (e.g. "City via Footscray" vs "City via Kensington") are distinct values.
    """
    return f"{destination.terminal_stop_id}::{destination.direction_id}::{destination.via_hint or ''}"


def line_label(line: LineCandidate) -> str:
    """Human label for a line candidate used by the T8 group multi-select."""
    name = line.route_short_name or line.route_long_name or line.route_id
    if line.via_hint:
        return f"{name} \u2192 {line.terminal_stop_name} via {line.via_hint}"
    return f"{name} \u2192 {line.terminal_stop_name}"
