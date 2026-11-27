"""Transport Victoria board direction select (T10).

One select per board — toggles between the configured pin directions and their
opposites.  The choice is a RUNTIME (in-memory) coordinator flag, never written
to config options, so it resets to "Configured directions" on every restart
(documented).  The actual opposite-direction mapping lives in
``board.opposite_direction_pins`` and is applied by the coordinator adapter on
each poll; all-destinations pins ignore the toggle.
"""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .board_coordinator import BoardCoordinator
from .const import (
    ATTRIBUTION,
    BOARD_DIRECTION_CONFIGURED,
    BOARD_DIRECTION_OPPOSITE,
    BOARD_ROLE_DIRECTION,
    CONF_BOARD_NAME,
    DOMAIN,
    board_entity_unique_id,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the direction select for a board config entry."""
    coordinator: BoardCoordinator = entry.runtime_data
    async_add_entities([BoardDirectionSelect(coordinator, entry)])


class BoardDirectionSelect(
    CoordinatorEntity[BoardCoordinator], SelectEntity  # type: ignore[misc]
):
    """Select: which direction each pin's services run in."""

    _attr_has_entity_name = True
    _attr_name = "Direction"
    _attr_attribution = ATTRIBUTION
    _attr_translation_key = "board_direction"

    def __init__(
        self,
        coordinator: BoardCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = board_entity_unique_id(
            entry.entry_id, BOARD_ROLE_DIRECTION, 0
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
    def options(self) -> list[str]:
        return [BOARD_DIRECTION_CONFIGURED, BOARD_DIRECTION_OPPOSITE]

    @property
    def current_option(self) -> str | None:
        return self.coordinator.direction_mode

    async def async_select_option(self, option: str) -> None:
        """Apply the direction mode and refresh immediately."""
        self.coordinator.set_direction_mode(option)
        await self.coordinator.async_request_refresh()
