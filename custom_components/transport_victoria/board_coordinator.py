"""Board coordinator adapter (T10).

Wraps the T9 aggregation (``board_aggregation``) behind a Home Assistant
``DataUpdateCoordinator`` so the merged board can be exposed as real entities
without changing the aggregation semantics.  Nothing here imports the
aggregation's internals — it only calls the two public ports:

* :func:`~.board_aggregation.fetch_board_realtime` (async I/O + offloaded parse)
* :func:`~.board_aggregation.aggregate_board` (pure sync, run in the executor)

Contract notes for T13/T14 (what to soak):

* **Offload.**  Every blocking call — the protobuf parse inside
  ``fetch_board_realtime`` and the whole ``aggregate_board`` schedule join —
  runs through ``run_sync`` (``hass.async_add_executor_job`` by default), never
  on the event loop.
* **Clocks.**  ``now_utc`` is aware UTC (``dt_util.utcnow``); ``local_tz`` is a
  ``zoneinfo.ZoneInfo`` derived from ``hass.config.time_zone`` (NOT hardcoded);
  the local ``now`` is naive local.
* **Generation swap.**  ``aggregate_board`` already restarts once internally on
  ``_GenerationSwapped``.  This adapter surfaces its errors honestly — if the
  internal restart still fails, the exception propagates and the coordinator
  marks entities unavailable.
* **Direction toggle.**  A runtime (in-memory) ``direction_mode`` flag flips
  each specific-destination pin to its opposite direction before aggregation
  (see ``board.opposite_direction_pins``).  It is never persisted, so it resets
  on restart.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .board import PinData, normalize_order, opposite_direction_pins
from .board_aggregation import BoardResult, aggregate_board, fetch_board_realtime
from .const import (
    BOARD_DEFAULT_DEPARTURE_COUNT,
    BOARD_DIRECTION_CONFIGURED,
    BOARD_DIRECTION_OPPOSITE,
    CONF_DEPARTURE_COUNT,
    CONF_REALTIME_ENABLED,
    CONF_SUBENTRY_ORDER,
    DEFAULT_BOARD_REALTIME_ENABLED,
)
from .realtime.client import PTVRealtimeClient
from .source import effective_realtime_enabled, get_source_api_key, source_disabled

_LOGGER = logging.getLogger(__name__)


class BoardCoordinator(DataUpdateCoordinator[BoardResult]):  # type: ignore[misc]
    """Coordinator for one trip-group board (parent entry).

    Polls the shared realtime feeds and the local GTFS schedule, then merges
    every pin into one time-sorted :class:`BoardResult` (rows / alerts /
    feed-health / sync-index) exposed to the board sensor/binary-sensor/select
    platforms.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        db_path: Path,
        *,
        invalid_pins: set[str] | frozenset[str] = frozenset(),
        update_interval=None,
    ) -> None:
        self.hass = hass  # DataUpdateCoordinator sets this; explicit for test stubs
        self._entry = entry
        self._db_path = db_path
        self._invalid_pins = frozenset(invalid_pins)
        # Runtime direction toggle (in-memory; resets on restart).
        self._direction_mode = BOARD_DIRECTION_CONFIGURED
        # Realtime client, built lazily in _async_setup (needs the HA session).
        self._client: PTVRealtimeClient | None = None
        # ``run_sync``: runs a sync callable off the event loop.  Defaults to
        # HA's executor (injectable for tests).
        self._run_sync = hass.async_add_executor_job
        # Last applied pin set (pin_key → PinData) so entities can surface
        # per-pin labels (label_from/label_to) not carried on BoardDeparture.
        self._pin_by_key: dict[str, PinData] = {}

        super().__init__(
            hass,
            _LOGGER,
            name=f"PTV board {entry.title}",
            update_interval=update_interval,
        )

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    async def _async_setup(self) -> None:
        """Initialise the realtime client (once HA's session is available).

        The client is built only when effective realtime is enabled (a credential
        exists on the source hub) — scheduled-only boards never issue an empty
        ``KeyID`` request.
        """
        self._sync_realtime_client()

    def _sync_realtime_client(self) -> None:
        """Rebuild/dispose the realtime client when effective realtime changes.

        Recomputed each poll so a ``realtime_enabled`` toggle (or a hub recovery)
        takes effect post-setup without a full entry reload.
        """
        enabled = effective_realtime_enabled(self.hass, self._entry)
        if enabled == (self._client is not None):
            return
        if enabled:
            api_key = get_source_api_key(self.hass) or ""
            session = async_get_clientsession(self.hass)
            self._client = PTVRealtimeClient(api_key, session)
        else:
            self._client = None

    # ------------------------------------------------------------------
    # Runtime direction toggle (select entity)
    # ------------------------------------------------------------------

    @property
    def direction_mode(self) -> str:
        """Current direction mode: BOARD_DIRECTION_CONFIGURED or _OPPOSITE."""
        return self._direction_mode

    def set_direction_mode(self, mode: str) -> None:
        """Set the runtime direction mode (does not persist to options)."""
        if mode not in (BOARD_DIRECTION_CONFIGURED, BOARD_DIRECTION_OPPOSITE):
            raise ValueError(f"Unknown board direction mode: {mode!r}")
        self._direction_mode = mode

    # ------------------------------------------------------------------
    # Options
    # ------------------------------------------------------------------

    @property
    def departure_count(self) -> int:
        """Number of ranked departure slots (drives sensor.N count)."""
        return int(
            self._entry.options.get(CONF_DEPARTURE_COUNT, BOARD_DEFAULT_DEPARTURE_COUNT)
        )

    @property
    def realtime_enabled(self) -> bool:
        """Whether realtime feeds are fetched (False → scheduled-only rows)."""
        return bool(
            self._entry.options.get(
                CONF_REALTIME_ENABLED, DEFAULT_BOARD_REALTIME_ENABLED
            )
        )

    @property
    def local_tz(self):
        """The board's zoneinfo (from ``hass.config.time_zone``, not hardcoded)."""
        return dt_util.get_time_zone(self.hass.config.time_zone)

    @property
    def pin_by_key(self) -> dict[str, PinData]:
        """The pin set from the last successful poll (pin_key → PinData)."""
        return self._pin_by_key

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def _ordered_pins(self) -> list[PinData]:
        """Return pins in canonical display order (never trust frontend order)."""
        ids = list(self._entry.subentries.keys())
        order = normalize_order(
            self._entry.options.get(CONF_SUBENTRY_ORDER, []), ids
        )
        return [PinData.from_dict(self._entry.subentries[sid].data) for sid in order]

    async def _async_update_data(self) -> BoardResult:
        """Poll + aggregate: fetch realtime, then merge pins into a board."""
        if source_disabled(self.hass):
            raise RuntimeError("Source hub is disabled")

        # Recompute effective realtime each poll (option toggle / recovery).
        self._sync_realtime_client()
        client = self._client
        # Effective realtime: the board's option AND a credential (client built).
        # Scheduled-only boards never issue an empty-KeyID request.
        realtime_enabled = self.realtime_enabled and client is not None

        pins = self._ordered_pins()
        if self._direction_mode == BOARD_DIRECTION_OPPOSITE:
            pins = await self._run_sync(opposite_direction_pins, self._db_path, pins)

        self._pin_by_key = {pin.pin_key: pin for pin in pins}

        now_utc = dt_util.utcnow()
        local_tz = self.local_tz
        local_now = dt_util.now().replace(tzinfo=None)

        # Phase 1: fetch + parse realtime feeds (I/O on the loop, parse off).
        realtime = await fetch_board_realtime(
            client,
            pins,
            realtime_enabled,
            now=now_utc,
            run_sync=self._run_sync,
        )

        # Phase 2: aggregate (pure sync) — offloaded, never on the event loop.
        def _aggregate() -> BoardResult:
            return aggregate_board(
                self._db_path,
                pins,
                local_now,
                local_tz=local_tz,
                now_utc=now_utc,
                realtime_enabled=realtime_enabled,
                trip_updates=realtime.trip_updates,
                stale_modes=realtime.stale_modes,
                alerts=realtime.alerts,
                feed_health=realtime.feed_health,
                invalid_pins=self._invalid_pins,
            )

        return await self._run_sync(_aggregate)
