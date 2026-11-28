"""Transport Victoria departure sensors — next, next_2, next_3.

Each watch (config entry) produces three departure sensors, one per slot.
The sensor state is the actual departure datetime (timezone-aware), displayed
by HA as a local time. The `minutes_until` attribute carries the integer minutes
value for automations.

Board entries (T10) instead produce ``departure_1..N`` ranked sensors over the
merged board's rows, plus one diagnostics sensor per watch.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .board_aggregation import BoardDeparture, BoardResult
from .board_coordinator import BoardCoordinator
from .const import (
    ATTRIBUTION,
    BOARD_ROLE_DEPARTURE,
    CONF_BOARD_NAME,
    CONF_DEPARTURE_COUNT,
    CONF_ENTRY_TYPE,
    CONF_MODE,
    CONF_STOP_NAME,
    CONF_WATCH_NAME,
    DEFAULT_DEPARTURE_COUNT,
    DOMAIN,
    ENTRY_TYPE_BOARD,
    MODE_MDI_ICON,
    MODE_PICTOGRAM,
    PICTOGRAM_URL_BASE,
    board_entity_unique_id,
)
from .coordinator import PTVDataUpdateCoordinator
from .models import CoordinatorData, Departure
from .source import source_is_scheduled_only

_LOGGER = logging.getLogger(__name__)


def _scheduled_only(entity: object) -> bool:
    """Return True when the entity's source hub is in scheduled-only mode.

    Defensive (mirrors ``binary_sensor._scheduled_only``): the light-env entity
    tests construct entities directly without a real ``hass`` handle; a
    missing/stub hass yields "no source hub", which is not scheduled-only.
    """
    hass = getattr(entity, "hass", None)
    if hass is None:
        return False
    return source_is_scheduled_only(hass)


def _pictogram_url(mode: str) -> str | None:
    """Return the entity_picture URL for the given transport mode, or None."""
    name = MODE_PICTOGRAM.get(mode)
    if name is None:
        return None
    return f"{PICTOGRAM_URL_BASE}/{name}.png"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up departure sensor entities for a config entry."""
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_BOARD:
        await _async_setup_board_entries(hass, entry, async_add_entities)
        return

    coordinator: PTVDataUpdateCoordinator = entry.runtime_data
    count = int(entry.options.get(CONF_DEPARTURE_COUNT, DEFAULT_DEPARTURE_COUNT))
    entities: list[SensorEntity] = [
        PTVDepartureSensor(coordinator, entry, slot) for slot in range(1, count + 1)
    ]
    entities.append(PTVDiagnosticsSensor(coordinator, entry))
    async_add_entities(entities)


async def _async_setup_board_entries(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ranked board departure sensors (departure_1..N)."""
    coordinator: BoardCoordinator = entry.runtime_data
    count = coordinator.departure_count
    entities: list[SensorEntity] = [
        BoardDepartureSensor(coordinator, entry, slot) for slot in range(1, count + 1)
    ]
    async_add_entities(entities)


class BoardDepartureSensor(CoordinatorEntity[BoardCoordinator], SensorEntity):  # type: ignore[misc]
    """One ranked departure slot on a merged board (departure_1..N).

    State is the expected departure (realtime) else the scheduled departure, as
    a timezone-aware datetime (TIMESTAMP device class).  When fewer than N rows
    exist the sensor is ``unknown``; when the coordinator fails it is
    ``unavailable`` (via the coordinator's ``last_update_success``).
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: BoardCoordinator,
        entry: ConfigEntry,
        slot: int,
    ) -> None:
        super().__init__(coordinator)
        self._slot = slot
        self._entry = entry
        self._attr_unique_id = board_entity_unique_id(
            entry.entry_id, BOARD_ROLE_DEPARTURE, slot
        )
        self._attr_name = f"Departure {slot}"

    @property
    def device_info(self) -> DeviceInfo:
        """Group all board entities under one device (the board entry)."""
        board_name = self._entry.data.get(CONF_BOARD_NAME, self._entry.entry_id)
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=board_name,
            manufacturer="Transport Victoria",
            model="Departure board",
        )

    @property
    def _data(self) -> BoardResult | None:
        return self.coordinator.data

    @property
    def _row(self) -> BoardDeparture | None:
        data = self._data
        if data is None or len(data.rows) < self._slot:
            return None
        return data.rows[self._slot - 1]

    @property
    def native_value(self) -> datetime | None:
        """Expected (realtime) else scheduled departure, as an aware datetime."""
        row = self._row
        if row is None:
            return None
        departure = row.expected_departure or row.scheduled_departure
        return departure.replace(tzinfo=self.coordinator.local_tz)

    @property
    def icon(self) -> str:
        row = self._row
        if row is None:
            return "mdi:clock-outline"
        if row.is_cancelled:
            return "mdi:cancel"
        if row.delay_seconds and row.delay_seconds > 60:
            return "mdi:clock-alert-outline"
        return "mdi:clock-outline"

    @property
    def available(self) -> bool:
        """Unavailable only when the coordinator has not successfully updated."""
        return bool(self.coordinator.last_update_success)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # ``scheduled_only`` is the persistent per-entry banner (§5.3): present
        # (True) whenever the board is in scheduled-only mode, even with no rows.
        data = self._data
        row = self._row
        attrs: dict[str, Any] = {}
        if _scheduled_only(self):
            attrs["scheduled_only"] = True
        if data is None or row is None:
            return attrs

        pin = self.coordinator.pin_by_key.get(row.pin_key)
        attrs.update({
            "route": row.route_short_name or row.route_long_name,
            "route_number": row.route_short_name,
            "destination": row.destination_label,
            "headsign": row.headsign,
            "boarding_stop": row.boarding_stop_name,
            "platform": row.platform,
            "trip_id": row.trip_id,
            "scheduled": row.scheduled_departure.isoformat(),
            "expected": (
                row.expected_departure.isoformat()
                if row.expected_departure is not None
                else None
            ),
            "delay_seconds": row.delay_seconds,
            "cancelled": row.is_cancelled,
            "skipped": row.is_skipped,
            "skipped_stops_count": row.skipped_stops_count,
            "realtime_state": row.realtime_state,
            "direction_id": row.direction_id,
            "pin_order": row.pin_order_index,
            "pin_label_from": pin.label_from if pin is not None else None,
            "pin_label_to": (
                pin.label_to if pin is not None else row.destination_label
            ),
        })
        # sync_index surfaces once, on departure_1 only (compact, documented).
        if self._slot == 1:
            attrs["sync_index"] = data.sync_index
        return attrs


class PTVDepartureSensor(CoordinatorEntity[PTVDataUpdateCoordinator], SensorEntity):  # type: ignore[misc]
    """A single departure slot sensor (next, next_2, or next_3)."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: PTVDataUpdateCoordinator,
        entry: ConfigEntry,
        slot: int,
    ) -> None:
        super().__init__(coordinator)
        self._slot = slot
        self._entry = entry
        self._mode = entry.data.get(CONF_MODE, "")

        self._attr_unique_id = f"{entry.entry_id}_departure_{slot}"
        self._attr_entity_picture = _pictogram_url(self._mode)

    @property
    def name(self) -> str:
        """Dynamic name showing route and headsign of the current departure."""
        dep = self._departure
        fallback = "Next" if self._slot == 1 else f"Next {self._slot}"
        if dep is None:
            return fallback
        route = dep.route_short_name or dep.route_long_name or ""
        headsign = dep.headsign or ""
        if route and headsign:
            return f"{route} · {headsign}"
        if route or headsign:
            return route or headsign
        return fallback

    @property
    def icon(self) -> str:
        """Icon reflecting cancellation, delay, or transport mode."""
        dep = self._departure
        if dep is None:
            return MODE_MDI_ICON.get(self._mode, "mdi:clock-outline")
        if dep.is_cancelled:
            return "mdi:cancel"
        if dep.delay_seconds and dep.delay_seconds > 60:
            return "mdi:clock-alert-outline"
        return MODE_MDI_ICON.get(self._mode, "mdi:clock-outline")

    @property
    def device_info(self) -> DeviceInfo:
        """Group all sensors for this watch under one device."""
        watch_name = self._entry.data.get(CONF_WATCH_NAME, self._entry.entry_id)
        stop_name = self._entry.data.get(CONF_STOP_NAME, "")
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=watch_name,
            manufacturer="Transport Victoria",
            model=stop_name,
        )

    @property
    def _departure(self) -> Departure | None:
        data: CoordinatorData | None = self.coordinator.data
        if data is None or len(data.departures) < self._slot:
            return None
        return data.departures[self._slot - 1]

    @property
    def native_value(self) -> datetime | None:
        """Actual departure time as a timezone-aware datetime (HA shows as local time)."""
        dep = self._departure
        if dep is None:
            return None
        local_tz = dt_util.get_default_time_zone()
        return dep.actual_departure.replace(tzinfo=local_tz)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # ``scheduled_only`` is the persistent per-entry banner (§5.3): present
        # (True) whenever the watch is in scheduled-only mode, even with no rows,
        # so the frontend/automations can see the watch is timetable-only.
        attrs: dict[str, Any] = {}
        if _scheduled_only(self):
            attrs["scheduled_only"] = True
        dep = self._departure
        if dep is None:
            return attrs
        local_now = dt_util.now().replace(tzinfo=None)
        delta = dep.actual_departure - local_now
        attrs.update({
            "minutes_until": int(delta.total_seconds() // 60),
            "scheduled_time": dep.scheduled_departure.isoformat(),
            "actual_time": dep.actual_departure.isoformat(),
            "delay_seconds": dep.delay_seconds,
            "delay_minutes": (dep.delay_seconds or 0) // 60,
            "is_cancelled": dep.is_cancelled,
            "realtime_state": dep.realtime_state,
            "headsign": dep.headsign,
            "route_name": dep.route_short_name or dep.route_long_name,
            "route_short_name": dep.route_short_name,
            "route_long_name": dep.route_long_name,
            "route_id": dep.route_id,
            "route_color": dep.route_color,
            "direction_id": dep.direction_id,
            "platform": dep.platform,
            "service_type": dep.service_type,
            "trip_id": dep.trip_id,
        })
        # Journey mode extras
        if dep.total_journey_minutes is not None:
            attrs["transfer_stop"] = dep.transfer_stop_name
            attrs["leg2_route"] = dep.leg2_route_name
            attrs["leg2_departure"] = (
                dep.leg2_scheduled_departure.isoformat()
                if dep.leg2_scheduled_departure else None
            )
            attrs["leg2_actual_departure"] = (
                dep.leg2_actual_departure.isoformat()
                if dep.leg2_actual_departure else None
            )
            attrs["total_journey_minutes"] = dep.total_journey_minutes
        return attrs

    @property
    def available(self) -> bool:
        """Unavailable only when coordinator has never successfully updated."""
        return bool(self.coordinator.last_update_success)


class PTVDiagnosticsSensor(CoordinatorEntity[PTVDataUpdateCoordinator], SensorEntity):  # type: ignore[misc]
    """Reports realtime feed health for a PTV watch.

    State: "ok" | "degraded" | "offline"
    - ok: last trip-updates fetch succeeded
    - degraded: fetch succeeded but partial (e.g. alerts failed)
    - offline: no successful realtime fetch yet or backoff active
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:cloud-check"
    _attr_attribution = ATTRIBUTION
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: PTVDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_diagnostics"
        self._attr_name = "Realtime Status"

    @property
    def device_info(self) -> DeviceInfo:
        """Share the device with the departure sensors."""
        watch_name = self._entry.data.get(CONF_WATCH_NAME, self._entry.entry_id)
        stop_name = self._entry.data.get(CONF_STOP_NAME, "")
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=watch_name,
            manufacturer="Transport Victoria",
            model=stop_name,
        )

    @property
    def native_value(self) -> str:
        coord = self.coordinator
        if coord.last_trip_fetch_at is None:
            return "offline"
        if coord.last_http_error is not None:
            return "degraded"
        return "ok"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        coord = self.coordinator
        attrs: dict[str, Any] = {
            "last_trip_update_fetch": (
                coord.last_trip_fetch_at.isoformat()
                if coord.last_trip_fetch_at else None
            ),
            "last_alerts_fetch": (
                coord.last_alerts_fetch_at.isoformat()
                if coord.last_alerts_fetch_at else None
            ),
            "last_http_error": coord.last_http_error,
            "rate_limit_backoff_until": (
                coord._backoff_until.isoformat()
                if coord._backoff_until else None
            ),
            "departures_count": (
                len(coord.data.departures) if coord.data else 0
            ),
            "alerts_count": (
                len(coord.data.alerts) if coord.data else 0
            ),
        }
        return attrs
