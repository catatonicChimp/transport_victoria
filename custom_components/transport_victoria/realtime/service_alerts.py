"""Parse GTFS Realtime service alerts feed."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from ..models import Alert

_LOGGER = logging.getLogger(__name__)

# Protobuf enum → human-readable string mappings
_CAUSE_MAP = {
    0: "UNKNOWN_CAUSE",
    1: "OTHER_CAUSE",
    2: "TECHNICAL_PROBLEM",
    3: "STRIKE",
    4: "DEMONSTRATION",
    5: "ACCIDENT",
    6: "HOLIDAY",
    7: "WEATHER",
    8: "MAINTENANCE",
    9: "CONSTRUCTION",
    10: "POLICE_ACTIVITY",
    11: "MEDICAL_EMERGENCY",
}

_EFFECT_MAP = {
    0: "UNKNOWN_EFFECT",
    1: "NO_SERVICE",
    2: "REDUCED_SERVICE",
    3: "SIGNIFICANT_DELAYS",
    4: "DETOUR",
    5: "ADDITIONAL_SERVICE",
    6: "MODIFIED_SERVICE",
    7: "OTHER_EFFECT",
    8: "STOP_MOVED",
    9: "NO_EFFECT",
    10: "ACCESSIBILITY_ISSUE",
}


@dataclass(slots=True, frozen=True)
class ActivePeriod:
    """One active_period window from a service alert (start inclusive, end
    exclusive per GTFS-RT; either bound may be absent)."""

    start: datetime | None
    end: datetime | None


@dataclass(slots=True, frozen=True)
class SelectorRecord:
    """One ``informed_entity`` selector, with its populated fields preserved.

    GTFS-RT semantics: the populated fields WITHIN one selector are ANDed;
    separate selectors on an alert are ORed.  ``None`` means "not scoped by this
    field" (the field was absent), so ``direction_id`` / ``route_type_id`` use
    key presence — a value of ``0`` is a real scope, not "unset".
    """

    agency_id: str | None = None
    route_type_id: int | None = None
    route_id: str | None = None
    direction_id: int | None = None
    stop_id: str | None = None
    trip_id: str | None = None
    trip_start_date: str | None = None
    trip_start_time: str | None = None

    def is_unscoped(self) -> bool:
        """True when no matchable field is populated (matches everything)."""
        return not (
            self.route_type_id is not None
            or self.route_id is not None
            or self.direction_id is not None
            or self.stop_id is not None
            or self.trip_id is not None
        )


@dataclass(slots=True, frozen=True)
class ServiceAlert:
    """A service alert with its full informed_entity selector list (T9).

    Unlike :class:`~custom_components.transport_victoria.models.Alert`, this
    preserves the *per-selector* structure (AND-within / OR-across), the trip
    and direction selectors, and the agency/route_type scope.  Matching is by
    selector only — ``cause`` is informational and MUST NOT be used for
    matching (the ``_CAUSE_MAP`` numbering is fixed separately in T13).
    """

    alert_id: str
    header: str
    description: str
    cause: str
    effect: str
    active_periods: tuple[ActivePeriod, ...]
    selectors: tuple[SelectorRecord, ...]
    # The realtime mode whose feed this alert came from (implicit agency scope
    # in the single-feed-per-mode model).  Set by the fetch helper; None when
    # parsed standalone.
    feed_mode: str | None = None


def is_alert_active(alert: ServiceAlert, now: datetime) -> bool:
    """Return True if ``alert`` is active at ``now`` (UTC-aware).

    An alert with no active_periods is always active.  Otherwise it is active
    when any period satisfies ``start <= now < end`` (start and end each
    optional).  ``now`` must be an aware UTC datetime matching the period
    timestamps.
    """
    if not alert.active_periods:
        return True
    for period in alert.active_periods:
        start_ok = period.start is None or period.start <= now
        end_ok = period.end is None or now < period.end
        if start_ok and end_ok:
            return True
    return False


def parse_service_alerts(feed: object, feed_mode: str | None = None) -> list[ServiceAlert]:
    """Parse a service-alerts FeedMessage into selector-rich ServiceAlert rows.

    Returns *all* alerts with their per-``informed_entity`` :class:`SelectorRecord`
    list and active periods, WITHOUT any now-based filtering — callers apply
    :func:`is_alert_active` with their own clock so matching is testable.

    Args:
        feed: A decoded ``gtfs_realtime_pb2.FeedMessage`` instance.
        feed_mode: The realtime mode this feed belongs to (implicit agency
            scope), or None when unknown.

    Returns:
        List of :class:`ServiceAlert` (empty on a malformed/empty feed).
    """
    alerts: list[ServiceAlert] = []

    try:
        entities = feed.entity  # type: ignore[attr-defined]
    except AttributeError:
        _LOGGER.warning(
            "parse_service_alerts received unexpected feed type: %s", type(feed)
        )
        return alerts

    for entity in entities:
        if not entity.HasField("alert"):
            continue
        raw = entity.alert

        header = _first_translation(raw.header_text)
        if not header:
            continue

        periods = tuple(
            ActivePeriod(
                start=(
                    datetime.fromtimestamp(p.start, tz=timezone.utc)
                    if p.start
                    else None
                ),
                end=(
                    datetime.fromtimestamp(p.end, tz=timezone.utc) if p.end else None
                ),
            )
            for p in raw.active_period  # type: ignore[attr-defined]
        )

        selectors = tuple(_selector(ie) for ie in raw.informed_entity)

        alerts.append(
            ServiceAlert(
                alert_id=entity.id,
                header=header,
                description=_first_translation(raw.description_text),
                cause=_CAUSE_MAP.get(raw.cause, "UNKNOWN_CAUSE"),
                effect=_EFFECT_MAP.get(raw.effect, "UNKNOWN_EFFECT"),
                active_periods=periods,
                selectors=selectors,
                feed_mode=feed_mode,
            )
        )

    return alerts


def _selector(ie: object) -> SelectorRecord:
    """Build one :class:`SelectorRecord` from an informed_entity message."""
    trip = ie.trip if _has_msg(ie, "trip") else None
    return SelectorRecord(
        agency_id=_string_field(ie, "agency_id"),
        route_type_id=(int(ie.route_type) if _has_int(ie, "route_type") else None),
        route_id=_string_field(ie, "route_id"),
        direction_id=(
            int(trip.direction_id)
            if trip is not None and _has_int(trip, "direction_id")
            else None
        ),
        stop_id=_string_field(ie, "stop_id"),
        trip_id=(_string_field(trip, "trip_id") if trip is not None else None),
        trip_start_date=(
            _string_field(trip, "start_date") if trip is not None else None
        ),
        trip_start_time=(
            _string_field(trip, "start_time") if trip is not None else None
        ),
    )


def _has_msg(obj: object, name: str) -> bool:
    has = getattr(obj, "HasField", None)
    if has is None:
        return False
    try:
        return bool(has(name))
    except (ValueError, TypeError):
        return False


def _has_int(obj: object, name: str) -> bool:
    """Key presence for a scalar protobuf field (``0`` counts as set)."""
    return _has_msg(obj, name)


def _string_field(obj: object, name: str) -> str | None:
    try:
        value = getattr(obj, name)
    except AttributeError:
        return None
    return value or None


def parse_alerts(
    feed: object,
    stop_id: str | None = None,
    route_id: str | None = None,
) -> list[Alert]:
    """Parse a GTFS Realtime FeedMessage and return active Alert objects.

    Optionally filters to alerts that specifically mention a stop or route.
    If neither stop_id nor route_id is provided, all alerts are returned.

    Args:
        feed: A decoded gtfs_realtime_pb2.FeedMessage.
        stop_id: If provided, only return alerts that mention this stop.
        route_id: If provided, only return alerts that mention this route.

    Returns:
        List of Alert objects, newest first.
    """
    alerts: list[Alert] = []
    now_ts = datetime.now(tz=timezone.utc).timestamp()

    for entity in feed.entity:  # type: ignore[attr-defined]
        if not entity.HasField("alert"):
            continue

        raw = entity.alert

        # Filter by informed_entity if a scope is provided
        if stop_id or route_id:
            if not _entity_matches(raw, stop_id=stop_id, route_id=route_id):
                continue

        # Check whether the alert is currently active
        if raw.active_period and not _is_active(raw.active_period, now_ts):
            continue

        header = _first_translation(raw.header_text)
        description = _first_translation(raw.description_text)

        if not header:
            continue

        affected_routes = [
            ie.route_id
            for ie in raw.informed_entity
            if ie.route_id
        ]
        affected_stops = [
            ie.stop_id
            for ie in raw.informed_entity
            if ie.stop_id
        ]

        active_from = None
        active_until = None
        if raw.active_period:
            period = raw.active_period[0]
            if period.start:
                active_from = datetime.fromtimestamp(period.start, tz=timezone.utc)
            if period.end:
                active_until = datetime.fromtimestamp(period.end, tz=timezone.utc)

        alerts.append(
            Alert(
                alert_id=entity.id,
                header=header,
                description=description,
                cause=_CAUSE_MAP.get(raw.cause, "UNKNOWN_CAUSE"),
                effect=_EFFECT_MAP.get(raw.effect, "UNKNOWN_EFFECT"),
                active_from=active_from,
                active_until=active_until,
                affected_route_ids=affected_routes,
                affected_stop_ids=affected_stops,
            )
        )

    return alerts


def _entity_matches(
    alert: object,
    stop_id: str | None,
    route_id: str | None,
) -> bool:
    """Return True if the alert's informed_entity list mentions the given stop/route."""
    for ie in alert.informed_entity:  # type: ignore[attr-defined]
        if stop_id and ie.stop_id == stop_id:
            return True
        if route_id and ie.route_id == route_id:
            return True
        # An alert scoped only to a trip or agency — include it if no specific filter
        if not stop_id and not route_id:
            return True
    return False


def _is_active(active_periods: object, now_ts: float) -> bool:
    """Return True if any active period covers the current time."""
    for period in active_periods:  # type: ignore[attr-defined]
        start = period.start or 0
        end = period.end or float("inf")
        if start <= now_ts <= end:
            return True
    return False


def _first_translation(translated_string: object) -> str:
    """Return the first English (or any) translation from a TranslatedString."""
    translations = translated_string.translation  # type: ignore[attr-defined]
    if not translations:
        return ""
    # Prefer English
    for t in translations:
        if t.language in ("en", "en-AU", ""):
            return str(t.text)
    return str(translations[0].text)
