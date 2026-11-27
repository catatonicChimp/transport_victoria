"""Transport Victoria vehicle tracking — device_tracker entities.

Two tracker surfaces are supported:

1. **Fleet tracking (T16)** — :class:`FleetTrackerCoordinator` /
   :class:`PTVFleetVehicleTracker`.  Attached to a *watch* or *board* entry
   (opt-in via ``CONF_TRACK_VEHICLES``): tracks the **active fleet** on the
   entry's watched routes.  One entity per vehicle (keyed by the feed's
   ``vehicle.id``), added when a vehicle first appears on a watched route and
   removed once it has been absent for ``GRACE_ABSENT_SECONDS``.  This replaces
   the v0.1.0 single-trip tracker, which latched onto one trip's vehicle.

2. **Route tracker** — :class:`RouteVehicleCoordinator` /
   :class:`PTVRouteVehicleTracker`.  Standalone — tracks all active vehicles on
   one route, with optional proximity filtering, into N fixed device_tracker
   slots (like departure sensor slots).

The legacy ``VehicleTrackerCoordinator`` (a separate ``ENTRY_TYPE_VEHICLE_TRACKER``
entry that followed the single vehicle serving the next departure) has been
REMOVED.  Its entities are orphaned on reload; see the T16 report / release
notes.  The fleet path (and the route tracker) fetch through the shared
:class:`~.realtime.client.PTVRealtimeClient`, whose per-feed rate limiter is the
single enforcement point — the old double-throttle ``AsyncRateLimiter(24/60 s)``
is gone from this module.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import aiohttp

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .board import PinData
from .board_aggregation import route_type_to_mode
from .const import (
    CONF_API_KEY,
    CONF_ENTRY_TYPE,
    CONF_MAX_VEHICLES,
    CONF_MODE,
    CONF_PROXIMITY_RADIUS_M,
    CONF_REFERENCE_STOP_ID,
    CONF_REFERENCE_STOP_LAT,
    CONF_REFERENCE_STOP_LON,
    CONF_ROUTE_COLOR,
    CONF_ROUTE_ID,
    CONF_ROUTE_ID_FILTER,
    CONF_ROUTE_LONG_NAME,
    CONF_ROUTE_SHORT_NAME,
    CONF_STOP_ID,
    CONF_TRACK_VEHICLES,
    COORDINATOR_STAGGER_SECONDS,
    DEFAULT_MAX_VEHICLES,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DOMAIN,
    ENTRY_TYPE_BOARD,
    ENTRY_TYPE_ROUTE_TRACKER,
    ENTRY_TYPE_VEHICLE_TRACKER,
    ENTRY_TYPE_WATCH,
    FEED_VEHICLE_POSITIONS,
    MODE_MDI_ICON,
    MODE_PICTOGRAM,
    MODE_TRAM,
    PICTOGRAM_URL_BASE,
)
from .gtfs.repository import route_ids_for_trips, routes_for_stop, trip_terminals
from .realtime.client import FeedError, PTVRealtimeClient
from .realtime.vehicle_positions import (
    VehiclePosition,
    parse_vehicle_positions,
    parse_vehicle_positions_by_vehicle,
)
from .vehicle_fleet import (
    FleetVehicle,
    FleetUpdate,
    MAX_VEHICLES_PER_ENTRY,
    feed_is_stale,
    matched_trip_ids,
    update_fleet,
    vehicle_state,
)

_LOGGER = logging.getLogger(__name__)

# Bounded size of the trip_id → route_id lookup cache (see FleetTrackerCoordinator).
_ROUTE_CACHE_MAX = 2000


# ---------------------------------------------------------------------------
# Platform setup — dispatches on entry type
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up device_tracker entities for this config entry."""
    entry_type = entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_WATCH)

    if entry_type == ENTRY_TYPE_ROUTE_TRACKER:
        _setup_route_tracker(entry, async_add_entities)
        return

    if entry_type == ENTRY_TYPE_VEHICLE_TRACKER:
        # Legacy single-trip tracker entry — deprecated and orphaned (T16).
        _LOGGER.warning(
            "Legacy vehicle tracker entry '%s' is deprecated: its single-trip "
            "tracker has been replaced by dynamic fleet tracking. Enable "
            "'%s' on the watch/board entry instead, then remove this entry.",
            entry.title,
            CONF_TRACK_VEHICLES,
        )
        return

    # Watch / board → dynamic fleet tracking (opt-in).
    if not entry.options.get(CONF_TRACK_VEHICLES, False):
        return

    coordinator = FleetTrackerCoordinator(hass, entry, _db_path_for(hass))
    await coordinator.async_config_entry_first_refresh()

    # Stash the coordinator where the rest of the entry's runtime can find it.
    hass.data[DOMAIN].setdefault(entry.entry_id, {})["fleet_coordinator"] = coordinator

    entities: dict[str, PTVFleetVehicleTracker] = {}

    @callback  # type: ignore[untyped-decorator]
    def _on_fleet_update() -> None:
        update = coordinator.last_fleet_update
        if update is None:
            return
        to_add = [
            PTVFleetVehicleTracker(coordinator, entry, vehicle_id)
            for vehicle_id in update.added
            if vehicle_id not in entities
        ]
        for entity in to_add:
            entities[entity.vehicle_id] = entity
        if to_add:
            async_add_entities(to_add)
        for vehicle_id in update.removed:
            entity = entities.pop(vehicle_id, None)
            if entity is not None:
                entity.async_remove()  # removes from the entity registry too

    entry.async_on_unload(coordinator.async_add_listener(_on_fleet_update))
    _on_fleet_update()


def _db_path_for(hass: HomeAssistant):
    """Return the shared GTFS DB path stored by ``__init__`` at setup."""
    return hass.data[DOMAIN]["db_path"]


# ---------------------------------------------------------------------------
# Fleet tracker (T16)
# ---------------------------------------------------------------------------


class FleetTrackerCoordinator(DataUpdateCoordinator[dict[str, FleetVehicle]]):  # type: ignore[misc]
    """Coordinator tracking the active fleet on an entry's watched routes.

    One fetch per ``(mode, vehicle-positions)`` per TTL through the shared
    client cache (same as T9); the client's per-feed rate limiter is the single
    enforcement point (no separate ``AsyncRateLimiter``).  Parsing and the
    SQLite trip→route lookup run in the executor.

    Route matching precedence (see :func:`~.vehicle_fleet.resolve_vehicle_route`):
    feed ``route_id`` → GTFS trips table → trip-updates descriptor.  This
    coordinator fetches only vehicle-positions; the trips-table lookup (the
    imported schedule contains every current trip) covers the fallback, so the
    trip-updates third precedence is left to callers that already parse that
    feed (it is exercised by the pure-module tests).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        db_path,
    ) -> None:
        self._entry = entry
        self._db_path = db_path
        self._client: PTVRealtimeClient | None = None
        # Trip id → route id lookup cache (bounded; see _route_lookup).
        self._route_cache: dict[str, str] = {}
        # Reconcile state — survives across polls so grace/caps are coherent.
        self._tracked: dict[str, FleetVehicle] = {}
        self._last_update: FleetUpdate | None = None
        self._stale_logged: set[str] = set()
        # Watched scope: explicit routes + modes are derived from the entry; the
        # fallback stop ids are used to resolve routes from the stop when the
        # entry has no explicit route filter (see _resolve_scope).
        self._watched_routes, self._modes, self._stop_ids = _fleet_scope(entry)
        # Executor offload (injectable for tests).
        self._run_sync = hass.async_add_executor_job

        poll_interval = timedelta(seconds=DEFAULT_POLL_INTERVAL_SECONDS)
        super().__init__(
            hass,
            _LOGGER,
            name=f"PTV fleet {entry.title}",
            update_interval=poll_interval,
        )

    @property
    def last_fleet_update(self) -> FleetUpdate | None:
        """The most recent reconcile (added/removed vehicle ids + fleet)."""
        return self._last_update

    @property
    def watched_routes(self) -> frozenset[str]:
        return self._watched_routes

    @property
    def modes(self) -> frozenset[str]:
        return self._modes

    async def _resolve_scope(self) -> frozenset[str]:
        """Return the watched route set, deriving from stops when no filter.

        An entry with an explicit route filter (or route-scoped pins) uses that
        set directly.  An unfiltered watch derives its routes from its stop via
        :func:`~.gtfs.repository.routes_for_stop`, recomputed each reconcile (the
        query is small and stays current with the live schedule).
        """
        if self._watched_routes:
            return self._watched_routes
        routes: set[str] = set()
        for stop_id in self._stop_ids:
            routes |= await self._run_sync(routes_for_stop, self._db_path, stop_id)
        return frozenset(routes)

    async def _async_setup(self) -> None:
        """Initialise the realtime client (once HA's session is available)."""
        api_key = self._entry.data.get(CONF_API_KEY, "")
        session = async_get_clientsession(self.hass)
        self._client = PTVRealtimeClient(api_key, session)

    async def _async_update_data(self) -> dict[str, FleetVehicle]:
        """Fetch vehicle positions and reconcile the fleet (executor-offloaded)."""
        if not self._modes:
            # Tram-only (no vehicle-positions feed) → no fleet.
            return dict(self._tracked)

        watched_routes = await self._resolve_scope()
        if not watched_routes:
            # The watched stop serves no (resolvable) routes → no fleet.
            return dict(self._tracked)

        client = self._client
        if client is None:
            raise UpdateFailed("Fleet tracker client not initialised")

        now = dt_util.utcnow()
        positions: list[VehiclePosition] = []
        failed = False
        for mode in self._modes:
            try:
                feed, meta = await client.fetch_feed_with_meta(
                    mode, FEED_VEHICLE_POSITIONS
                )
            except aiohttp.ClientResponseError as exc:
                if exc.status == 401:
                    raise ConfigEntryAuthFailed("PTV API key rejected (401)") from exc
                _LOGGER.warning(
                    "Vehicle positions fetch failed for %s (%s) — keeping last fleet",
                    mode, exc.status,
                )
                failed = True
                continue
            except aiohttp.ClientError as exc:
                _LOGGER.warning(
                    "Vehicle positions network error for %s: %s — keeping last fleet",
                    mode, exc,
                )
                failed = True
                continue

            # Staleness (T13 header classification): log once, keep tracking.
            if feed_is_stale(meta.header_timestamp, meta.fetched_at, now):
                if mode not in self._stale_logged:
                    self._stale_logged.add(mode)
                    _LOGGER.warning(
                        "Vehicle positions feed for %s is stale — trackers keep "
                        "their last known state", mode,
                    )

            parsed = await self._run_sync(parse_vehicle_positions_by_vehicle, feed)
            positions.extend(parsed.values())

        if failed:
            # A failed fetch means we cannot trust absence — keep last state
            # (no reconcile, no grace-driven removal).  Entities stay available.
            return dict(self._tracked)

        route_by_trip_id = await self._route_lookup(positions)

        # Destination enrichment: fetch terminals only for vehicles that will
        # actually be tracked (their resolved route is watched).
        trip_ids = matched_trip_ids(
            positions, watched_routes, route_by_trip_id, {}
        )
        terminals = await self._terminal_lookup(trip_ids)

        update = update_fleet(
            self._tracked,
            positions,
            watched_routes,
            route_by_trip_id,
            {},  # trip-updates route fallback not fetched here (see docstring)
            now,
            terminals=terminals,
        )
        self._tracked = update.vehicles
        self._last_update = update
        return update.vehicles

    async def _terminal_lookup(self, trip_ids: set[str]) -> dict:
        """Return ``trip_id`` → terminal info for the matched trips (executor).

        Recomputed each reconcile (the query is small — at most the fleet cap of
        30 trips — and stays current with the live schedule).
        """
        if not trip_ids:
            return {}
        return await self._run_sync(trip_terminals, self._db_path, sorted(trip_ids))

    async def _route_lookup(self, positions: list[VehiclePosition]) -> dict[str, str]:
        """Return (and cache) trip_id → route_id for positions lacking a feed route.

        Only trips whose feed entry carries no ``route_id`` need the schedule
        lookup; the cache is bounded (oldest entries evicted first).
        """
        missing = [
            p.trip_id
            for p in positions
            if p.trip_id and not p.route_id and p.trip_id not in self._route_cache
        ]
        if missing:
            found = await self._run_sync(route_ids_for_trips, self._db_path, missing)
            self._route_cache.update(found)
            overflow = len(self._route_cache) - _ROUTE_CACHE_MAX
            if overflow > 0:
                for key in list(self._route_cache)[:overflow]:
                    self._route_cache.pop(key, None)
        return self._route_cache


class PTVFleetVehicleTracker(
    CoordinatorEntity[FleetTrackerCoordinator],  # type: ignore[misc]
    TrackerEntity,  # type: ignore[misc]
):
    """A device_tracker entity for one active vehicle on a watched route.

    Keyed by the feed's ``vehicle.id`` (stable within a feed).  The entity is
    added when the vehicle first appears on a watched route and removed by the
    platform once it has been absent for the grace window.  ``has_entity_name``
    is False so the name is the full entity name (no device prefix); the name is
    ``"Vehicle {id suffix}"`` plus the live destination ("to {terminal}") when
    the trip's terminal is known (updates live when the trip changes).
    """

    _attr_has_entity_name = False
    _attr_source_type = SourceType.GPS

    def __init__(
        self,
        coordinator: FleetTrackerCoordinator,
        entry: ConfigEntry,
        vehicle_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self.vehicle_id = vehicle_id
        self._attr_unique_id = f"{entry.entry_id}:vehiclefleet:{vehicle_id}"
        self._attr_icon = _fleet_icon(entry)

    @property
    def name(self) -> str:
        """Vehicle id suffix, plus the destination when known (updates live).

        The terminal is read from the current coordinator data each poll, so a
        vehicle that changes trips mid-day gets its name updated to the new
        destination ("Vehicle {suffix} → {terminal}").  Falls back to just the
        suffix when the terminal is unknown (trip not in the local schedule).
        """
        suffix = self.vehicle_id[-4:] if len(self.vehicle_id) >= 4 else self.vehicle_id
        vehicle = self._vehicle()
        if vehicle is not None and vehicle.terminal_stop_name:
            return f"Vehicle {suffix} → {vehicle.terminal_stop_name}"
        return f"Vehicle {suffix}"

    def _vehicle(self) -> FleetVehicle | None:
        data = self.coordinator.data
        return data.get(self.vehicle_id) if data else None

    @property
    def latitude(self) -> float | None:
        vehicle = self._vehicle()
        return vehicle.latitude if vehicle else None

    @property
    def longitude(self) -> float | None:
        vehicle = self._vehicle()
        return vehicle.longitude if vehicle else None

    @property
    def state(self) -> str:
        """Moving/stationary (from feed status); fall back to ``not_home`` (GPS)."""
        vehicle = self._vehicle()
        if vehicle is not None:
            motion = vehicle_state(vehicle.current_status)
            if motion is not None:
                return motion
        return "not_home"

    @property
    def available(self) -> bool:
        """Available once we have a successful fetch (a stale feed still counts)."""
        return bool(self.coordinator.last_update_success)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        vehicle = self._vehicle()
        if vehicle is None:
            return {}
        # GTFS-RT VehiclePosition has no headsign field, so the human-readable
        # "which direction" signal is the trip's terminal stop ("destination").
        return {
            "route": vehicle.route_id,
            "trip_id": vehicle.trip_id,
            "direction_id": vehicle.direction_id,
            "destination": vehicle.terminal_stop_name,
            "terminal_stop_id": vehicle.terminal_stop_id,
            "trip_terminal_direction_id": vehicle.trip_terminal_direction_id,
            "bearing": vehicle.bearing,
            "speed_kmh": vehicle.speed_kmh,
            "vehicle_id": vehicle.vehicle_id,
            "vehicle_label": vehicle.vehicle_label,
            "current_status": vehicle.current_status,
            "motion": vehicle_state(vehicle.current_status),
            "occupancy": vehicle.occupancy_status,
            "last_seen": vehicle.last_seen.isoformat() if vehicle.last_seen else None,
        }


def _fleet_scope(
    entry: ConfigEntry,
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Derive ``(explicit_routes, modes, stop_ids)`` for fleet tracking.

    * A board watches the union of its enabled pins' ``route_id`` and fetches the
      union of their realtime modes (tram, which has no vehicle-positions feed,
      is skipped silently).  ``stop_ids`` is the union of the pins' boarding
      stops, used only as a fallback when no pin carries a ``route_id``.
    * A watch watches its ``route_id_filter``; without one, it derives routes
      from its stop (``stop_ids``), resolved by ``routes_for_stop`` at reconcile
      time.  ``modes`` is the watch's single mode (empty for tram).
    """
    entry_type = entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_WATCH)

    if entry_type == ENTRY_TYPE_BOARD:
        pins = [PinData.from_dict(sub.data) for sub in entry.subentries.values()]
        routes = {pin.route_id for pin in pins if pin.include and pin.route_id}
        stops = {
            pin.boarding_stop_id
            for pin in pins
            if pin.include and pin.boarding_stop_id
        }
        modes: set[str] = set()
        for pin in pins:
            if not pin.include:
                continue
            mode = route_type_to_mode(pin.route_type)
            if mode and mode != MODE_TRAM:
                modes.add(mode)
        return frozenset(routes), frozenset(modes), frozenset(stops)

    mode = entry.data.get(CONF_MODE, "")
    modes = frozenset() if mode == MODE_TRAM else frozenset({mode})
    route_filter = entry.data.get(CONF_ROUTE_ID_FILTER)
    if route_filter:
        return frozenset({route_filter}), modes, frozenset()

    stop_id = entry.data.get(CONF_STOP_ID, "")
    stop_ids = frozenset({stop_id}) if stop_id else frozenset()
    return frozenset(), modes, stop_ids


def _fleet_icon(entry: ConfigEntry) -> str:
    """Pick an MDI icon: the watch's mode, else a generic bus icon."""
    mode = entry.data.get(CONF_MODE, "")
    return MODE_MDI_ICON.get(mode, "mdi:bus")


# ---------------------------------------------------------------------------
# Route-based tracker (retained)
# ---------------------------------------------------------------------------


def _setup_route_tracker(entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Wire the route tracker's dynamic trip_id slots (unchanged behaviour)."""
    coordinator: RouteVehicleCoordinator = entry.runtime_data
    known_trip_ids: set[str] = set()

    @callback  # type: ignore[untyped-decorator]
    def _async_add_new_vehicles() -> None:
        vehicles: list[VehiclePosition] = coordinator.data or []
        new_entities: list[PTVRouteVehicleTracker] = []
        for pos in vehicles:
            if pos.trip_id and pos.trip_id not in known_trip_ids:
                known_trip_ids.add(pos.trip_id)
                new_entities.append(
                    PTVRouteVehicleTracker(coordinator, entry, pos.trip_id)
                )
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_vehicles))
    _async_add_new_vehicles()


class RouteVehicleCoordinator(DataUpdateCoordinator[list[VehiclePosition]]):  # type: ignore[misc]
    """Coordinator tracking all active vehicles on a route.

    Fetches the vehicle-positions feed for the mode, filters by route_id,
    applies optional proximity sorting/filtering, and returns up to
    max_vehicles positions ordered by distance (if proximity filter active)
    or vehicle_label.  Rate limiting is the shared client's per-feed limiter.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        entry_index: int,
    ) -> None:
        self._entry = entry
        self._client: PTVRealtimeClient | None = None
        # Log-once guard for the "feed not published for this mode" degradation.
        self._missing_logged = False

        stagger = entry_index * COORDINATOR_STAGGER_SECONDS
        poll_interval = timedelta(seconds=DEFAULT_POLL_INTERVAL_SECONDS + stagger)

        route_short_name = entry.data.get(CONF_ROUTE_SHORT_NAME, entry.entry_id)
        super().__init__(
            hass,
            _LOGGER,
            name=f"PTV Route Tracker {route_short_name}",
            update_interval=poll_interval,
        )

    async def _async_setup(self) -> None:
        """Initialise the realtime client (no separate rate limiter — see T13)."""
        api_key: str = self._entry.data.get(CONF_API_KEY, "")
        session = async_get_clientsession(self.hass)
        self._client = PTVRealtimeClient(api_key, session)

    async def _async_update_data(self) -> list[VehiclePosition]:
        """Fetch and filter vehicle positions for the configured route."""
        client = self._client
        if client is None:
            raise UpdateFailed("Route tracker client not initialised")

        mode: str = self._entry.data.get(CONF_MODE, "metro")
        route_id: str = self._entry.data.get(CONF_ROUTE_ID, "")
        max_vehicles: int = int(
            self._entry.options.get(
                CONF_MAX_VEHICLES,
                self._entry.data.get(CONF_MAX_VEHICLES, DEFAULT_MAX_VEHICLES),
            )
        )

        ref_stop_id: str | None = self._entry.data.get(CONF_REFERENCE_STOP_ID)
        ref_lat: float | None = self._entry.data.get(CONF_REFERENCE_STOP_LAT)
        ref_lon: float | None = self._entry.data.get(CONF_REFERENCE_STOP_LON)
        radius_m: float = float(self._entry.data.get(CONF_PROXIMITY_RADIUS_M, 2000.0))

        try:
            feed = await client.fetch_feed(mode, FEED_VEHICLE_POSITIONS)
        except FeedError as exc:
            if exc.unavailable:
                # The portal does not publish this feed (e.g. tram vehicle
                # positions, T13) — degrade to "no vehicles" rather than failing
                # setup and retrying forever.  Log once per entry.
                if not self._missing_logged:
                    self._missing_logged = True
                    _LOGGER.warning(
                        "Vehicle positions are not published for %s — vehicle "
                        "tracking disabled for route tracker %s",
                        mode, route_id,
                    )
                return []
            if exc.status == 401:
                raise ConfigEntryAuthFailed("PTV API key rejected (401)") from exc
            raise UpdateFailed(f"Vehicle positions fetch failed: {exc.status}") from exc
        except aiohttp.ClientResponseError as exc:
            if exc.status == 401:
                raise ConfigEntryAuthFailed("PTV API key rejected (401)") from exc
            raise UpdateFailed(f"Vehicle positions fetch failed: {exc.status}") from exc
        except aiohttp.ClientError as exc:
            raise UpdateFailed(f"Vehicle positions network error: {exc}") from exc

        all_positions = parse_vehicle_positions(feed)

        # Filter to vehicles on this route (primary: route_id from feed)
        on_route = [
            pos for pos in all_positions.values()
            if pos.route_id == route_id
        ]

        # Fallback: if no vehicles matched by route_id (feed may not populate it),
        # we can't distinguish routes without a DB query — log and return empty.
        if not on_route and route_id:
            _LOGGER.debug(
                "No vehicles found with route_id=%s in feed "
                "(total positions: %d). Feed may not include route_id.",
                route_id, len(all_positions),
            )

        # Apply proximity filter if a reference stop is configured
        if ref_stop_id and ref_lat is not None and ref_lon is not None:
            with_distance: list[tuple[float, VehiclePosition]] = []
            for pos in on_route:
                dist = _haversine_m(ref_lat, ref_lon, pos.latitude, pos.longitude)
                if dist <= radius_m:
                    with_distance.append((dist, pos))
            with_distance.sort(key=lambda t: t[0])
            result = [pos for _, pos in with_distance[:max_vehicles]]
        else:
            # Sort by vehicle_label for a stable, human-readable ordering
            on_route.sort(key=lambda p: (p.vehicle_label or "", p.trip_id))
            result = on_route[:max_vehicles]

        _LOGGER.debug(
            "Route tracker %s: %d vehicles on route, %d within filter, returning %d",
            route_id, len(on_route), len(result), len(result),
        )
        return result


class PTVRouteVehicleTracker(
    CoordinatorEntity[RouteVehicleCoordinator],  # type: ignore[misc]
    TrackerEntity,  # type: ignore[misc]
):
    """A device_tracker entity for one active vehicle on a route.

    Entities are created dynamically as new trip_ids appear in the coordinator
    data.  Each entity is permanently keyed to a single trip_id; it shows its
    GPS position while that trip is active and "Away" when it drops out of the
    feed (proximity filter, trip ended, etc.).
    """

    _attr_has_entity_name = True
    _attr_source_type = SourceType.GPS

    def __init__(
        self,
        coordinator: RouteVehicleCoordinator,
        entry: ConfigEntry,
        trip_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._trip_id = trip_id
        self._attr_unique_id = f"{entry.entry_id}_{trip_id}"

        mode = entry.data.get(CONF_MODE, "")
        picto = MODE_PICTOGRAM.get(mode)
        self._attr_entity_picture = f"{PICTOGRAM_URL_BASE}/{picto}.png" if picto else None
        self._attr_icon = MODE_MDI_ICON.get(mode, "mdi:bus")

    @property
    def name(self) -> str:
        """Use vehicle_label from current coordinator data; fall back to trip_id."""
        pos = self._get_position()
        if pos and pos.vehicle_label:
            return pos.vehicle_label
        return self._trip_id

    @property
    def device_info(self) -> DeviceInfo:
        """All entities for this route share one device."""
        route_short = self._entry.data.get(CONF_ROUTE_SHORT_NAME, "")
        route_long = self._entry.data.get(CONF_ROUTE_LONG_NAME, "")
        display = route_short or route_long or self._entry.entry_id
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=f"{display} Tracker",
            manufacturer="Transport Victoria",
        )

    def _get_position(self) -> VehiclePosition | None:
        """Return the current position for this entity's trip_id, or None."""
        vehicles: list[VehiclePosition] = self.coordinator.data or []
        for pos in vehicles:
            if pos.trip_id == self._trip_id:
                return pos
        return None

    @property
    def latitude(self) -> float | None:
        pos = self._get_position()
        return pos.latitude if pos else None

    @property
    def longitude(self) -> float | None:
        pos = self._get_position()
        return pos.longitude if pos else None

    @property
    def available(self) -> bool:
        """Available once coordinator has fetched at least once."""
        return bool(self.coordinator.last_update_success)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        route_short = self._entry.data.get(CONF_ROUTE_SHORT_NAME, "")
        route_long = self._entry.data.get(CONF_ROUTE_LONG_NAME, "")
        route_color: str | None = self._entry.data.get(CONF_ROUTE_COLOR)

        base: dict[str, Any] = {
            "route_name": f"{route_short} \u2013 {route_long}" if route_long else route_short,
            "route_color": route_color,
            "trip_id": self._trip_id,
        }

        pos = self._get_position()
        if pos is None:
            return base

        base.update({
            "vehicle_id": pos.vehicle_id,
            "vehicle_label": pos.vehicle_label,
            "bearing": pos.bearing,
            "speed_kmh": pos.speed_kmh,
            "current_stop_id": pos.current_stop_id,
            "current_status": pos.current_status,
            "last_updated": pos.timestamp.isoformat() if pos.timestamp else None,
        })

        # Include distance if proximity filter is active
        ref_lat: float | None = self._entry.data.get(CONF_REFERENCE_STOP_LAT)
        ref_lon: float | None = self._entry.data.get(CONF_REFERENCE_STOP_LON)
        if ref_lat is not None and ref_lon is not None:
            base["distance_m"] = round(
                _haversine_m(ref_lat, ref_lon, pos.latitude, pos.longitude)
            )

        return base


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in metres between two WGS84 points."""
    import math

    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
