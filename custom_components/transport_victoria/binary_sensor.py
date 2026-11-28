"""Transport Victoria disruption binary sensor.

One binary sensor per watch — on when there is at least one active service
alert affecting the configured stop or route. Attributes carry the first
alert's details for use in HA notifications.

Board entries (T10) instead get a single ``disruptions`` binary sensor over the
merged board's matched alerts, with a trustworthy OFF (unavailable when the
service-alert feed failed, so OFF never means "confidently no disruption").
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .board_aggregation import FEED_FAILED, BoardResult
from .board_coordinator import BoardCoordinator
from .const import (
    ATTRIBUTION,
    BOARD_ROLE_DISRUPTION,
    CONF_BOARD_NAME,
    CONF_ENTRY_TYPE,
    CONF_MODE,
    CONF_STOP_NAME,
    CONF_WATCH_NAME,
    DOMAIN,
    ENTRY_TYPE_BOARD,
    FEED_SERVICE_ALERTS,
    MODE_PICTOGRAM,
    PICTOGRAM_URL_BASE,
    board_entity_unique_id,
)
from .coordinator import PTVDataUpdateCoordinator
from .models import Alert, CoordinatorData
from .source import source_is_scheduled_only

_LOGGER = logging.getLogger(__name__)


def _scheduled_only(entity: object) -> bool:
    """Return True when the entity's source hub is in scheduled-only mode.

    Defensive: the light-env entity tests construct entities directly without a
    real ``hass`` handle; a missing/stub hass yields "no source hub", which is
    not scheduled-only (so those tests keep their existing availability).
    """
    hass = getattr(entity, "hass", None)
    if hass is None:
        return False
    return source_is_scheduled_only(hass)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the disruption binary sensor for a config entry."""
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_BOARD:
        coordinator: BoardCoordinator = entry.runtime_data
        async_add_entities([BoardDisruptionBinarySensor(coordinator, entry)])
        return

    coordinator: PTVDataUpdateCoordinator = entry.runtime_data
    async_add_entities([PTVDisruptionBinarySensor(coordinator, entry)])


class BoardDisruptionBinarySensor(
    CoordinatorEntity[BoardCoordinator], BinarySensorEntity  # type: ignore[misc]
):
    """Binary sensor: on when an active alert matches any enabled board pin.

    OFF is trustworthy: when the service-alert feed for any mode failed, the
    sensor is marked unavailable (not confidently-off), because a missing alert
    feed cannot certify "no disruption".
    """

    _attr_has_entity_name = True
    _attr_name = "Disruptions"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: BoardCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = board_entity_unique_id(
            entry.entry_id, BOARD_ROLE_DISRUPTION, 0
        )

    @property
    def device_info(self) -> DeviceInfo:
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

    def _alert_feed_failed(self, data: BoardResult) -> bool:
        """True when any service-alert feed failed (OFF would be dishonest)."""
        return any(
            key[1] == FEED_SERVICE_ALERTS and health.state == FEED_FAILED
            for key, health in data.feed_health.items()
        )

    @property
    def available(self) -> bool:
        """Unavailable when the coordinator failed OR an alert feed failed.

        Also unavailable (never a false OFF) in scheduled-only mode: without a
        credential there is no service-alert feed, so "no disruption" cannot be
        certified.
        """
        if _scheduled_only(self):
            return False
        if not self.coordinator.last_update_success:
            return False
        data = self._data
        if data is None:
            return False
        return not self._alert_feed_failed(data)

    @property
    def is_on(self) -> bool:
        data = self._data
        return bool(data and data.alerts)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._data
        if not data or not data.alerts:
            return {}
        # Compact: alert ids + first 120 chars of header + matched pin keys.
        alerts: list[dict[str, Any]] = []
        for alert in data.alerts:
            alerts.append(
                {
                    "id": alert.alert_id,
                    "header": (alert.header or "")[:120],
                    "pins": [m.pin_key for m in alert.matches],
                }
            )
        return {"alerts": alerts, "alert_count": len(alerts)}


class PTVDisruptionBinarySensor(
    CoordinatorEntity[PTVDataUpdateCoordinator], BinarySensorEntity  # type: ignore[misc]
):
    """Binary sensor: on when service alerts are active for this watch."""

    _attr_has_entity_name = True
    _attr_name = "Disruption"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_attribution = ATTRIBUTION
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: PTVDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_disruption"
        mode = entry.data.get(CONF_MODE, "")
        picto = MODE_PICTOGRAM.get(mode)
        self._attr_entity_picture = f"{PICTOGRAM_URL_BASE}/{picto}.png" if picto else None

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
    def is_on(self) -> bool:
        data: CoordinatorData | None = self.coordinator.data
        return bool(data and data.alerts)

    @property
    def available(self) -> bool:
        """Unavailable (never a false OFF) in scheduled-only mode.

        Without a credential there is no service-alert feed, so "no disruption"
        cannot be certified — the sensor reports unavailable rather than OFF.
        """
        if _scheduled_only(self):
            return False
        return super().available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data: CoordinatorData | None = self.coordinator.data
        if not data or not data.alerts:
            return {}

        alert: Alert = data.alerts[0]
        attrs: dict[str, Any] = {
            "header": alert.header,
            "description": alert.description,
            "cause": alert.cause,
            "effect": alert.effect,
            "alert_count": len(data.alerts),
        }
        if alert.active_from:
            attrs["active_from"] = alert.active_from.isoformat()
        if alert.active_until:
            attrs["active_until"] = alert.active_until.isoformat()
        return attrs
