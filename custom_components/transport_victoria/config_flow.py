"""Config flow for the PTV integration."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    OptionsFlow,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector

from .const import (
    ALL_ROUTE_TYPES,
    BOARD_DEFAULT_DEPARTURE_COUNT,
    BOARD_MAX_DEPARTURE_COUNT,
    CITY_LOOP_STOP_IDS,
    CONF_ALL_DESTINATIONS,
    CONF_API_KEY,
    CONF_BOARD_NAME,
    CONF_COLOUR_OVERRIDE,
    CONF_DESTINATION_STOP_ID,
    CONF_DESTINATION_STOP_NAME,
    CONF_DIRECTION,
    CONF_ENTRY_TYPE,
    CONF_INCLUDE,
    CONF_JOURNEY_DESTINATION_STOP_ID,
    CONF_JOURNEY_DESTINATION_STOP_NAME,
    CONF_JOURNEY_MODE,
    CONF_LABEL_FROM,
    CONF_LABEL_TO,
    CONF_MAX_VEHICLES,
    CONF_MIN_TRANSFER_MINUTES,
    CONF_MODE,
    CONF_PARENT_ENTRY_ID,
    CONF_PLATFORM_FILTER,
    CONF_DEPARTURE_COUNT,
    CONF_POLL_INTERVAL,
    CONF_PROXIMITY_RADIUS_M,
    CONF_REALTIME_ENABLED,
    CONF_REFERENCE_STOP_ID,
    CONF_REFERENCE_STOP_LAT,
    CONF_REFERENCE_STOP_LON,
    CONF_REFERENCE_STOP_NAME,
    CONF_ROUTE_COLOR,
    CONF_ROUTE_ID,
    CONF_ROUTE_ID_FILTER,
    CONF_ROUTE_LONG_NAME,
    CONF_ROUTE_SHORT_NAME,
    CONF_SHOW_RUN_NUMBERS,
    CONF_SHOW_VEHICLE_TYPE,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    CONF_SUBENTRY_ORDER,
    CONF_TRANSFER_STOP_ID,
    CONF_TRANSFER_STOP_NAME,
    CONF_WATCH_NAME,
    DEFAULT_DEPARTURE_COUNT,
    DEFAULT_MAX_VEHICLES,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_SHOW_RUN_NUMBERS,
    DEFAULT_SHOW_VEHICLE_TYPE,
    DOMAIN,
    ENTRY_TYPE_BOARD,
    ENTRY_TYPE_ROUTE_TRACKER,
    ENTRY_TYPE_VEHICLE_TRACKER,
    ENTRY_TYPE_WATCH,
    MODE_BUNDLE_MAP,
    MODE_LABELS,
    MODES,
    ROUTE_TYPE_MAP,
    SUBENTRY_TYPE_PIN,
)
from .board import (
    PinData,
    line_value,
    normalize_order,
    pin_from_line,
    pin_from_route_all,
)
from .gtfs.repository import (
    Destination,
    LineCandidate,
    RouteResult,
    StopResult,
    get_all_routes,
    get_lines_between,
    get_platforms_for_stop,
    get_routes_for_stop,
    get_stop_lat_lon,
    get_terminals_for_route,
    is_mode_ready,
    search_routes_by_name,
    search_stops_by_distance,
    search_stops_by_name,
)
from .realtime.client import PTVRealtimeClient
from .routing.destination import (
    destination_label,
    destination_value,
    line_label,
    resolve_destination,
)
from .routing.path_finder import validate_route
from .watch_import import (
    REASON_LABELS,
    STALE_LABELS,
    WatchMapping,
    map_watches,
)
from . import async_trigger_gtfs_refresh

_LOGGER = logging.getLogger(__name__)

# Sentinel value used in the destination selector to mean "all destinations"
_ALL_DESTINATIONS_KEY = "__all__"

# Entry-type menu value that branches into the T11 "import watches as a board"
# flow (not a persisted entry type — the created entry is always a board).
_IMPORT_OPTION = "import"

# Upper bound on the post-download import wait (5 s per poll → 15 minutes).
# Without a bound a failed/cancelled refresh leaves the spinner up forever.
_MAX_IMPORT_POLLS = 180

# How many select options before a flat radio list renders as a searchable
# dropdown instead of a radio wall.  Lists at or below this stay radio buttons.
_DROPDOWN_OPTION_THRESHOLD = 6

# Upper bound on the number of destination candidates rendered in the
# destination dropdown (bounds render size; the free-text search fallback
# remains for the no-candidate case).
_DEST_CANDIDATE_CAP = 100


def _select_selector_mode(option_count: int) -> str:
    """Return the select mode name for a list of ``option_count`` options.

    Kept as a plain-string decision (mapped to the HA enum at render time) so
    the light-env tests can assert the dropdown/list choice without importing
    the real ``homeassistant.helpers.selector`` enums.
    """
    return "dropdown" if option_count > _DROPDOWN_OPTION_THRESHOLD else "list"


def _select_selector(
    options: list[selector.SelectOptionDict],
    *,
    multiple: bool = False,
) -> selector.SelectSelector:
    """Build a single/multi select selector; dropdown when the list is long."""
    if _select_selector_mode(len(options)) == "dropdown":
        mode = selector.SelectSelectorMode.DROPDOWN
    else:
        mode = selector.SelectSelectorMode.LIST
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            multiple=multiple,
            mode=mode,
        )
    )


def _reorder_subentry(
    ordered: list[str], subentry_id: str, action: str
) -> list[str]:
    """Return a copy of ``ordered`` with ``subentry_id`` moved per ``action``.

    ``action`` is ``"up"``, ``"down"``, ``"first"`` or ``"last"``; a no-op at
    the boundaries.  Kept local to the config flow so the reorder UX (up/down/
    first/last) does not touch ``board.move_subentry`` (which only moves ±1).
    """
    result = list(ordered)
    if subentry_id not in result or len(result) < 2:
        return result
    idx = result.index(subentry_id)
    if action == "first":
        result.insert(0, result.pop(idx))
        return result
    if action == "last":
        result.append(result.pop(idx))
        return result
    direction = -1 if action == "up" else 1
    new_idx = idx + direction
    if new_idx < 0 or new_idx >= len(result):
        return result
    result[idx], result[new_idx] = result[new_idx], result[idx]
    return result


def _task_succeeded(task: asyncio.Task[Any]) -> bool:
    """Return True when a finished refresh/poll task reported success.

    Callers must only invoke this once ``task.done()`` is true.
    """
    if task.cancelled():
        return False
    if task.exception() is not None:
        return False
    return bool(task.result())


def _data_dir(hass_config_path: str) -> Path:
    return Path(hass_config_path) / "transport_victoria_data"


def _zip_path(hass_config_path: str) -> Path:
    return _data_dir(hass_config_path) / "gtfs.zip"


def _db_path(hass_config_path: str) -> Path:
    return _data_dir(hass_config_path) / "gtfs.db"


def _stops_to_selector_options(
    stops: list[StopResult],
    show_distance: bool = False,
) -> list[selector.SelectOptionDict]:
    options = []
    for s in stops:
        if show_distance and s.distance_m is not None:
            label = f"{s.stop_name}  ({s.distance_m:.0f} m)"
        else:
            label = s.stop_name
        options.append(selector.SelectOptionDict(value=s.stop_id, label=label))
    return options


def _route_display_label(r: RouteResult) -> str:
    """Build a clean display label for a route, avoiding duplication.

    Rules:
    - If route_short_name is blank: show route_long_name only
    - If route_long_name starts with route_short_name (trains): show route_short_name only
    - Otherwise: show "{route_short_name} – {route_long_name}"
    """
    short = (r.route_short_name or "").strip()
    long = (r.route_long_name or "").strip()
    if not short:
        return long or r.route_id
    if long and not long.startswith(short):
        return f"{short} \u2013 {long}"
    return short


class PTVConfigFlow(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg, misc]
    """Handle the PTV config flow.

    Steps:
      1. user    — API key (validated against the realtime API)
      2. mode    — transport mode (metro train, tram, bus, regional bus, v/line)
      3. stop    — origin stop (by name search against local GTFS DB)
      4. dest    — destination: all or specific stop
      5. confirm — name the watch, preview active disruptions
    """

    VERSION = 2

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._stop_search_results: list[StopResult] = []
        self._dest_search_results: list[StopResult] = []
        self._transfer_search_results: list[StopResult] = []
        self._journey_dest_search_results: list[StopResult] = []
        self._zip_wait_task: asyncio.Task[None] | None = None
        self._db_wait_task: asyncio.Task[None] | None = None
        self._stop_method: str = "name"   # "name" | "distance" | "map"
        self._available_routes: list[RouteResult] = []
        self._available_platforms: list[str] = []
        self._route_tracker_routes: list[RouteResult] = []
        self._dest_candidates: list[Destination] = []
        self._dest_route_id: str | None = None
        # Step to redirect to after GTFS download/import completes.
        # "stop" for departure watches; "route_tracker_route" for route trackers.
        self._post_import_step: str = "stop"
        # T11 watch-import scratch state (populated between the select and
        # summary steps; never persisted to the created board entry).
        self._import_watch_ids: list[str] = []
        self._import_board_name: str = ""
        self._import_skipped: list[tuple[str, str]] = []
        self._import_stale: list[tuple[str, str]] = []

    # ------------------------------------------------------------------
    # Step 1 — API key (skipped if a key is already stored)
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect and validate the PTV Open Data API key.

        If an existing PTV config entry already has a valid key, this step is
        skipped automatically — the stored key is re-used so the user doesn't
        have to paste it in every time they add a new watch.
        """
        # Reuse key from an existing entry (first-time setup still asks)
        if user_input is None:
            existing_key = self._get_existing_api_key()
            if existing_key:
                self._config[CONF_API_KEY] = existing_key
                self.hass.async_create_task(async_trigger_gtfs_refresh(self.hass))
                return await self.async_step_entry_type()

        errors: dict[str, str] = {}

        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()

            if not api_key:
                errors[CONF_API_KEY] = "api_key_required"
            else:
                errors = await self._validate_api_key(api_key)
                if not errors:
                    self._config[CONF_API_KEY] = api_key
                    # Kick off GTFS download immediately so it runs while the
                    # user completes the remaining config flow steps.
                    self.hass.async_create_task(
                        async_trigger_gtfs_refresh(self.hass)
                    )
                    return await self.async_step_entry_type()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_API_KEY, default=self._get_existing_api_key() or ""
                ): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }),
            errors=errors,
            description_placeholders={
                "portal_url": "https://opendata.transport.vic.gov.au",
            },
        )

    def _get_existing_api_key(self) -> str | None:
        """Return the API key from an existing PTV config entry, if any."""
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            key = entry.data.get(CONF_API_KEY)
            if key:
                return str(key)
        return None

    async def _validate_api_key(self, api_key: str) -> dict[str, str]:
        """Return an errors dict if the key is invalid, empty dict if OK."""
        session = async_get_clientsession(self.hass)
        client = PTVRealtimeClient(api_key, session)
        try:
            await client.test_connection()
        except aiohttp.ClientResponseError as exc:
            if exc.status == 401:
                return {"base": "invalid_auth"}
            _LOGGER.warning("PTV API returned HTTP %s during validation", exc.status)
            return {"base": "cannot_connect"}
        except aiohttp.ClientError:
            return {"base": "cannot_connect"}
        return {}

    # ------------------------------------------------------------------
    # Step 2 — Mode
    # ------------------------------------------------------------------

    async def async_step_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the transport mode."""
        if user_input is not None:
            self._config[CONF_MODE] = user_input[CONF_MODE]
            return await self.async_step_downloading()

        return self.async_show_form(
            step_id="mode",
            data_schema=vol.Schema({
                vol.Required(CONF_MODE): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=m, label=MODE_LABELS[m])
                            for m in MODES
                        ],
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }),
        )

    # ------------------------------------------------------------------
    # Step 3a — ZIP download progress screen
    # ------------------------------------------------------------------

    async def async_step_downloading(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show a spinner while the GTFS ZIP is being downloaded.

        The refresh runs as a task off the flow and the frontend polls this
        step; when the task finishes we either advance to the import step or —
        if the download/import failed — offer a retry instead of pretending
        work is still in progress.
        """
        zip_path = _zip_path(self.hass.config.config_dir)
        db_path = _db_path(self.hass.config.config_dir)
        mode = self._config.get(CONF_MODE, "")
        bundles = MODE_BUNDLE_MAP.get(mode, [])

        # On the very first call (before the wait task exists), check whether
        # we can skip ahead entirely.  We must NOT do this on subsequent calls
        # while the progress spinner is active — HA requires that a progress
        # step only returns show_progress or show_progress_done.
        if self._zip_wait_task is None:
            # Already imported → skip both progress steps
            if await self.hass.async_add_executor_job(is_mode_ready, db_path, bundles):
                return await getattr(self, f"async_step_{self._post_import_step}")()

            # Kick off the work now.  If the ZIP is already on disk (e.g. a second
            # mode being added after metro was set up) pass skip_download_if_exists=True
            # so we jump straight to the import without re-downloading 230 MB.
            def _zip_exists() -> bool:
                return zip_path.exists() and zip_path.stat().st_size > 0

            zip_on_disk = await self.hass.async_add_executor_job(_zip_exists)
            self._zip_wait_task = self.hass.async_create_task(
                async_trigger_gtfs_refresh(
                    self.hass,
                    bundles,
                    skip_download_if_exists=zip_on_disk,
                )
            )

        if not self._zip_wait_task.done():
            return self.async_show_progress(
                step_id="downloading",
                progress_action="downloading_gtfs",
                progress_task=self._zip_wait_task,
            )

        if not _task_succeeded(self._zip_wait_task):
            _LOGGER.error(
                "Config flow: GTFS refresh failed for bundles %s — offering retry",
                bundles,
            )
            return self.async_show_progress_done(next_step_id="schedule_failed")

        return self.async_show_progress_done(next_step_id="importing")

    # ------------------------------------------------------------------
    # Step 3a-failure — schedule download/import failed
    # ------------------------------------------------------------------

    async def async_step_schedule_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explain a failed timetable download and offer retry or cancel."""
        return self.async_show_menu(
            step_id="schedule_failed",
            menu_options=["retry_download", "abort_setup"],
        )

    async def async_step_retry_download(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Restart the download/import after a failure."""
        self._zip_wait_task = None
        self._db_wait_task = None
        return await self.async_step_downloading()

    async def async_step_abort_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Give up on setup when the schedule cannot be fetched."""
        return self.async_abort(reason="schedule_unavailable")

    # ------------------------------------------------------------------
    # Step 3b — SQLite import progress screen
    # ------------------------------------------------------------------

    async def async_step_importing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show a spinner while the selected mode's bundle is being imported."""
        db_path = _db_path(self.hass.config.config_dir)
        mode = self._config.get(CONF_MODE, "")
        bundles = MODE_BUNDLE_MAP.get(mode, [])

        # On the very first call only: check if already done or start polling.
        # Must NOT re-check on subsequent calls while the spinner is active.
        # Note: the actual download+import was already kicked off as _zip_wait_task
        # in async_step_downloading, so by the time we reach this step the DB is
        # typically already ready — is_mode_ready will return True and we skip through.
        if self._db_wait_task is None:
            if await self.hass.async_add_executor_job(is_mode_ready, db_path, bundles):
                return await getattr(self, f"async_step_{self._post_import_step}")()

            # Import not done yet (e.g. ZIP was huge and import is still running).
            # Poll until ready without firing a second async_trigger_gtfs_refresh,
            # and stop as soon as the refresh task reports failure (or the poll
            # budget runs out) so the UI never spins forever.
            self._db_wait_task = self.hass.async_create_task(
                self._poll_until_mode_ready(db_path, bundles, self._zip_wait_task)
            )

        if not self._db_wait_task.done():
            return self.async_show_progress(
                step_id="importing",
                progress_action="importing_gtfs",
                progress_task=self._db_wait_task,
            )

        if not _task_succeeded(self._db_wait_task):
            _LOGGER.error(
                "Config flow: timetable import did not complete for bundles %s",
                bundles,
            )
            return self.async_show_progress_done(next_step_id="schedule_failed")

        return self.async_show_progress_done(next_step_id=self._post_import_step)

    async def _poll_until_mode_ready(
        self,
        db_path: Path,
        bundles: list[str],
        refresh_task: asyncio.Task[Any] | None = None,
    ) -> bool:
        """Poll every 5 seconds until the selected mode's bundles are imported.

        Returns False when the refresh task has finished without a usable
        schedule, or after ``_MAX_IMPORT_POLLS`` polls (15 minutes).
        """
        poll = 0
        while not self.hass.is_stopping:
            ready = await self.hass.async_add_executor_job(
                is_mode_ready, db_path, bundles
            )
            if ready:
                _LOGGER.debug("Config flow: mode bundles %s ready", bundles)
                return True
            if refresh_task is not None and refresh_task.done():
                if not _task_succeeded(refresh_task):
                    _LOGGER.debug(
                        "Config flow: refresh finished without usable schedule; "
                        "stopping import poll"
                    )
                    return False
            poll += 1
            if poll >= _MAX_IMPORT_POLLS:
                _LOGGER.warning(
                    "Config flow: timetable import not ready after %ds — giving up",
                    poll * 5,
                )
                return False
            if poll % 6 == 0:
                _LOGGER.debug(
                    "Config flow: waiting for bundles %s (%ds elapsed)", bundles, poll * 5
                )
            await asyncio.sleep(5)
        return False

    # ------------------------------------------------------------------
    # Step 3 — Stop search (name / distance / map)
    # ------------------------------------------------------------------

    async def async_step_stop(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Search for and select the origin stop.

        Supports three sub-modes selectable via a radio at the top of the form:
          name     — type-ahead text search (default)
          distance — auto-loads stops sorted by distance from HA home zone
          map      — LocationSelector → resolves to nearest stop within 500 m
        """
        errors: dict[str, str] = {}
        db_path = _db_path(self.hass.config.config_dir)
        mode = self._config.get(CONF_MODE, "")
        route_types = ROUTE_TYPE_MAP.get(mode, [])

        if user_input is not None:
            new_method = user_input.get("stop_method", self._stop_method)

            # Method change → clear stale results and re-render without processing input
            if new_method != self._stop_method:
                self._stop_search_results = []
                self._stop_method = new_method
                return await self.async_step_stop(None)

            # Stop selected from results list → advance to route filter
            if CONF_STOP_ID in user_input and user_input[CONF_STOP_ID]:
                stop_id = user_input[CONF_STOP_ID]
                matching = [s for s in self._stop_search_results if s.stop_id == stop_id]
                stop_name = matching[0].stop_name if matching else stop_id
                self._config[CONF_STOP_ID] = stop_id
                self._config[CONF_STOP_NAME] = stop_name
                return await self.async_step_route()

            if self._stop_method == "name":
                query = user_input.get("query", "").strip()
                if len(query) < 2:
                    if query:
                        errors["query"] = "search_too_short"
                else:
                    self._stop_search_results = await self.hass.async_add_executor_job(
                        search_stops_by_name, db_path, query, route_types, 20
                    )
                    if not self._stop_search_results:
                        errors["query"] = "no_stops_found"

            elif self._stop_method == "distance":
                home = self.hass.states.get("zone.home")
                if not home:
                    errors["stop_method"] = "no_home_zone"
                else:
                    lat = home.attributes.get("latitude", 0.0)
                    lon = home.attributes.get("longitude", 0.0)
                    self._stop_search_results = await self.hass.async_add_executor_job(
                        search_stops_by_distance, db_path, lat, lon, route_types, 2000.0, 20
                    )
                    if not self._stop_search_results:
                        errors["stop_method"] = "no_stops_found"


        # Build schema for current method
        # Reset any stale "map" method left from a prior code version
        if self._stop_method == "map":
            self._stop_method = "name"

        schema_fields: dict[Any, Any] = {
            vol.Required("stop_method", default=self._stop_method): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value="name", label="Search by name"),
                        selector.SelectOptionDict(value="distance", label="Nearest to home"),
                    ],
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
        }

        if self._stop_method == "name":
            schema_fields[vol.Optional("query")] = selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.SEARCH)
            )

        if self._stop_search_results:
            schema_fields[vol.Optional(CONF_STOP_ID)] = _select_selector(
                _stops_to_selector_options(
                    self._stop_search_results,
                    show_distance=self._stop_method in ("distance", "map"),
                )
            )

        mode_label = MODE_LABELS.get(mode, "transport")
        return self.async_show_form(
            step_id="stop",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
            description_placeholders={"mode": mode_label},
        )

    # ------------------------------------------------------------------
    # Step 3.5 — Route/Line filter (optional)
    # ------------------------------------------------------------------

    async def async_step_route(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Optionally restrict departures to a specific route/line.

        Shows the routes served by the selected stop. If only one route is
        available the step is skipped automatically. Selecting "All lines"
        leaves route_id_filter as None.
        """
        db_path = _db_path(self.hass.config.config_dir)
        stop_id = self._config[CONF_STOP_ID]
        mode = self._config.get(CONF_MODE, "")
        route_types = ROUTE_TYPE_MAP.get(mode, [])

        if not self._available_routes:
            self._available_routes = await self.hass.async_add_executor_job(
                get_routes_for_stop, db_path, stop_id, route_types
            )

        # Auto-advance if zero or one route (no useful choice to present)
        if len(self._available_routes) <= 1:
            self._config[CONF_ROUTE_ID_FILTER] = None
            return await self.async_step_platform()

        if user_input is not None:
            chosen = user_input.get("route_choice", "__all__")
            self._config[CONF_ROUTE_ID_FILTER] = None if chosen == "__all__" else chosen
            return await self.async_step_platform()

        options = [selector.SelectOptionDict(value="__all__", label="All lines")]
        for r in self._available_routes:
            options.append(selector.SelectOptionDict(value=r.route_id, label=_route_display_label(r)))

        return self.async_show_form(
            step_id="route",
            data_schema=vol.Schema({
                vol.Required("route_choice", default="__all__"): _select_selector(options),
            }),
            description_placeholders={"stop_name": self._config.get(CONF_STOP_NAME, "")},
        )

    # ------------------------------------------------------------------
    # Step 3.6 — Platform filter (optional, shown when >1 platform)
    # ------------------------------------------------------------------

    async def async_step_platform(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Optionally restrict departures to a specific platform.

        Only shown when the selected stop has more than one platform code.
        """
        db_path = _db_path(self.hass.config.config_dir)
        stop_id = self._config[CONF_STOP_ID]

        if not self._available_platforms:
            self._available_platforms = await self.hass.async_add_executor_job(
                get_platforms_for_stop, db_path, stop_id
            )

        # Auto-advance if zero or one platform
        if len(self._available_platforms) <= 1:
            self._config[CONF_PLATFORM_FILTER] = None
            return await self.async_step_dest()

        if user_input is not None:
            chosen = user_input.get("platform_choice", "__all__")
            self._config[CONF_PLATFORM_FILTER] = None if chosen == "__all__" else chosen
            return await self.async_step_dest()

        options = [selector.SelectOptionDict(value="__all__", label="All platforms")]
        for p in self._available_platforms:
            options.append(selector.SelectOptionDict(value=p, label=f"Platform {p}"))

        return self.async_show_form(
            step_id="platform",
            data_schema=vol.Schema({
                vol.Required("platform_choice", default="__all__"): _select_selector(options),
            }),
            description_placeholders={"stop_name": self._config.get(CONF_STOP_NAME, "")},
        )

    # ------------------------------------------------------------------
    # Step 4 — Destination
    # ------------------------------------------------------------------

    async def async_step_dest(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between all destinations or a specific terminal.

        Terminal candidates are derived from the route's trip patterns (T7), so
        every mode — including buses with per-variant short workings — can pick a
        concrete destination.  Direction is derived and stored, not asked for.
        """
        errors: dict[str, str] = {}
        db_path = _db_path(self.hass.config.config_dir)
        mode = self._config.get(CONF_MODE, "")
        route_types = ROUTE_TYPE_MAP.get(mode, [])
        origin_stop_id = self._config[CONF_STOP_ID]

        route_id = await self._resolve_destination_route_id(db_path, route_types)
        if route_id is not None and route_id != self._dest_route_id:
            self._dest_candidates = []
            self._dest_route_id = route_id
        if self._dest_route_id is not None and not self._dest_candidates:
            self._dest_candidates = await self.hass.async_add_executor_job(
                get_terminals_for_route, db_path, self._dest_route_id
            )

        if user_input is not None:
            choice = user_input.get("destination_choice", _ALL_DESTINATIONS_KEY)

            if choice == _ALL_DESTINATIONS_KEY:
                self._config[CONF_ALL_DESTINATIONS] = True
                self._config[CONF_DESTINATION_STOP_ID] = None
                self._config[CONF_DESTINATION_STOP_NAME] = None
                self._config[CONF_DIRECTION] = None
                self._config[CONF_JOURNEY_MODE] = False
                return await self.async_step_confirm()

            if choice == "journey":
                # Branch into the journey sub-flow
                return await self.async_step_journey()

            # A terminal candidate selected from the derived list
            candidate = next(
                (c for c in self._dest_candidates if destination_value(c) == choice),
                None,
            )
            if candidate is not None:
                reachable = await self.hass.async_add_executor_job(
                    validate_route,
                    db_path,
                    origin_stop_id,
                    candidate.terminal_stop_id,
                    route_types,
                )
                if not reachable:
                    errors["destination_choice"] = "destination_unreachable"
                else:
                    self._config[CONF_ALL_DESTINATIONS] = False
                    self._config[CONF_DESTINATION_STOP_ID] = candidate.terminal_stop_id
                    self._config[CONF_DESTINATION_STOP_NAME] = candidate.terminal_stop_name
                    # Direction derived from trip evidence (via the candidate,
                    # which get_terminals_for_route built from stop_times).
                    self._config[CONF_DIRECTION] = candidate.direction_id
                    self._config[CONF_JOURNEY_MODE] = False
                    return await self.async_step_confirm()

            if choice == "specific":
                # Fallback: free-text search for an arbitrary destination.
                if (
                    CONF_DESTINATION_STOP_ID in user_input
                    and user_input[CONF_DESTINATION_STOP_ID]
                ):
                    stop_id = user_input[CONF_DESTINATION_STOP_ID]
                    matching = [
                        s for s in self._dest_search_results if s.stop_id == stop_id
                    ]
                    stop_name = matching[0].stop_name if matching else stop_id

                    route_ok = await self.hass.async_add_executor_job(
                        validate_route, db_path, origin_stop_id, stop_id, route_types
                    )
                    if not route_ok:
                        errors[CONF_DESTINATION_STOP_ID] = "no_direct_service"
                    else:
                        self._config[CONF_ALL_DESTINATIONS] = False
                        self._config[CONF_DESTINATION_STOP_ID] = stop_id
                        self._config[CONF_DESTINATION_STOP_NAME] = stop_name
                        self._config[CONF_JOURNEY_MODE] = False
                        self._config[CONF_DIRECTION] = None
                        if self._dest_route_id is not None:
                            resolution = await self.hass.async_add_executor_job(
                                resolve_destination,
                                db_path,
                                self._dest_route_id,
                                origin_stop_id,
                                stop_id,
                            )
                            if resolution.candidates:
                                self._config[CONF_DIRECTION] = (
                                    resolution.candidates[0].direction_id
                                )
                        return await self.async_step_confirm()

                query = user_input.get("dest_query", "").strip()
                if len(query) < 2:
                    if query:
                        errors["dest_query"] = "search_too_short"
                else:
                    self._dest_search_results = await self.hass.async_add_executor_job(
                        search_stops_by_name, db_path, query, route_types, 20
                    )
                    if not self._dest_search_results:
                        errors["dest_query"] = "no_stops_found"

        # Build schema: "All destinations" + derived terminal candidates +
        # journey; fall back to a free-text search only when no terminals are
        # derivable (e.g. multiple lines and none chosen).
        options = [
            selector.SelectOptionDict(
                value=_ALL_DESTINATIONS_KEY, label="All destinations"
            )
        ]
        for c in self._dest_candidates[:_DEST_CANDIDATE_CAP]:
            options.append(
                selector.SelectOptionDict(
                    value=destination_value(c), label=destination_label(c)
                )
            )
        options.append(
            selector.SelectOptionDict(value="journey", label="Journey (change at stop)")
        )
        if not self._dest_candidates:
            options.append(
                selector.SelectOptionDict(
                    value="specific", label="Specific destination"
                )
            )

        schema_fields: dict[Any, Any] = {
            vol.Required(
                "destination_choice", default=_ALL_DESTINATIONS_KEY
            ): _select_selector(options),
        }

        if user_input and user_input.get("destination_choice") == "specific":
            schema_fields[vol.Optional("dest_query")] = selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.SEARCH)
            )
            if self._dest_search_results:
                schema_fields[
                    vol.Optional(CONF_DESTINATION_STOP_ID)
                ] = _select_selector(
                    _stops_to_selector_options(self._dest_search_results)
                )

        return self.async_show_form(
            step_id="dest",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
            description_placeholders={
                "stop_name": self._config.get(CONF_STOP_NAME, ""),
            },
        )

    async def _resolve_destination_route_id(
        self, db_path: Path, route_types: list[int]
    ) -> str | None:
        """Return the route_id used to derive destination candidates.

        Uses the chosen line filter when set; otherwise, when the origin stop is
        served by exactly one route, that route is used implicitly (so bus and
        V/Line modes still get per-terminal candidates without an explicit line
        step).
        """
        route_filter = self._config.get(CONF_ROUTE_ID_FILTER)
        if route_filter:
            return str(route_filter)
        routes = await self.hass.async_add_executor_job(
            get_routes_for_stop, db_path, self._config[CONF_STOP_ID], route_types
        )
        if len(routes) == 1:
            return routes[0].route_id
        return None

    # ------------------------------------------------------------------
    # Step 4b — Journey sub-flow
    # ------------------------------------------------------------------

    async def async_step_journey(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure a multi-leg journey: origin → transfer → destination."""
        errors: dict[str, str] = {}
        db_path = _db_path(self.hass.config.config_dir)
        route_types = ROUTE_TYPE_MAP.get(self._config.get(CONF_MODE, ""), [])

        if user_input is not None:
            # Phase 1: transfer stop selected?
            if CONF_TRANSFER_STOP_ID in user_input and user_input[CONF_TRANSFER_STOP_ID]:
                transfer_id = user_input[CONF_TRANSFER_STOP_ID]
                matching = [s for s in self._transfer_search_results if s.stop_id == transfer_id]
                transfer_name = matching[0].stop_name if matching else transfer_id

                origin_stop_id = self._config[CONF_STOP_ID]
                route_ok = await self.hass.async_add_executor_job(
                    validate_route, db_path, origin_stop_id, transfer_id, route_types
                )
                if not route_ok:
                    errors[CONF_TRANSFER_STOP_ID] = "no_direct_service"
                else:
                    self._config[CONF_TRANSFER_STOP_ID] = transfer_id
                    self._config[CONF_TRANSFER_STOP_NAME] = transfer_name
                    return await self.async_step_journey_dest()

            # Search for transfer stop
            query = user_input.get("transfer_query", "").strip()
            if len(query) < 2:
                if query:
                    errors["transfer_query"] = "search_too_short"
            else:
                self._transfer_search_results = await self.hass.async_add_executor_job(
                    search_stops_by_name, db_path, query, route_types, 20
                )
                if not self._transfer_search_results:
                    errors["transfer_query"] = "no_stops_found"

        schema_fields: dict[Any, Any] = {
            vol.Optional("transfer_query"): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.SEARCH)
            ),
        }
        if self._transfer_search_results:
            schema_fields[vol.Optional(CONF_TRANSFER_STOP_ID)] = _select_selector(
                _stops_to_selector_options(self._transfer_search_results)
            )

        return self.async_show_form(
            step_id="journey",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
            description_placeholders={
                "stop_name": self._config.get(CONF_STOP_NAME, ""),
            },
        )

    async def async_step_journey_dest(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the final destination for a journey watch."""
        errors: dict[str, str] = {}
        db_path = _db_path(self.hass.config.config_dir)
        route_types = ROUTE_TYPE_MAP.get(self._config.get(CONF_MODE, ""), [])

        if user_input is not None:
            # Final destination selected?
            if CONF_JOURNEY_DESTINATION_STOP_ID in user_input and user_input[CONF_JOURNEY_DESTINATION_STOP_ID]:
                dest_id = user_input[CONF_JOURNEY_DESTINATION_STOP_ID]
                matching = [s for s in self._journey_dest_search_results if s.stop_id == dest_id]
                dest_name = matching[0].stop_name if matching else dest_id

                transfer_id = self._config[CONF_TRANSFER_STOP_ID]
                route_ok = await self.hass.async_add_executor_job(
                    validate_route, db_path, transfer_id, dest_id, route_types
                )
                if not route_ok:
                    errors[CONF_JOURNEY_DESTINATION_STOP_ID] = "no_direct_service"
                else:
                    self._config[CONF_JOURNEY_DESTINATION_STOP_ID] = dest_id
                    self._config[CONF_JOURNEY_DESTINATION_STOP_NAME] = dest_name
                    self._config[CONF_JOURNEY_MODE] = True
                    self._config[CONF_ALL_DESTINATIONS] = False
                    # Store min transfer minutes
                    raw_min = user_input.get(CONF_MIN_TRANSFER_MINUTES, "5")
                    self._config[CONF_MIN_TRANSFER_MINUTES] = int(raw_min)
                    return await self.async_step_confirm()

            # Search
            query = user_input.get("journey_dest_query", "").strip()
            if len(query) < 2:
                if query:
                    errors["journey_dest_query"] = "search_too_short"
            else:
                self._journey_dest_search_results = await self.hass.async_add_executor_job(
                    search_stops_by_name, db_path, query, route_types, 20
                )
                if not self._journey_dest_search_results:
                    errors["journey_dest_query"] = "no_stops_found"

        schema_fields: dict[Any, Any] = {
            vol.Required(CONF_MIN_TRANSFER_MINUTES, default="5"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value="2", label="2 minutes"),
                        selector.SelectOptionDict(value="5", label="5 minutes"),
                        selector.SelectOptionDict(value="10", label="10 minutes"),
                        selector.SelectOptionDict(value="15", label="15 minutes"),
                    ],
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Optional("journey_dest_query"): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.SEARCH)
            ),
        }
        if self._journey_dest_search_results:
            schema_fields[vol.Optional(CONF_JOURNEY_DESTINATION_STOP_ID)] = _select_selector(
                _stops_to_selector_options(self._journey_dest_search_results)
            )

        return self.async_show_form(
            step_id="journey_dest",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
            description_placeholders={
                "transfer_stop": self._config.get(CONF_TRANSFER_STOP_NAME, ""),
            },
        )

    # ------------------------------------------------------------------
    # Step 5 — Confirm
    # ------------------------------------------------------------------

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Name the watch and create the config entry."""
        if user_input is not None:
            watch_name = user_input[CONF_WATCH_NAME].strip()
            if watch_name:
                self._config[CONF_WATCH_NAME] = watch_name
                self._config[CONF_ENTRY_TYPE] = ENTRY_TYPE_WATCH
                return self.async_create_entry(
                    title=watch_name,
                    data=self._config,
                )

        # Auto-suggest a name
        stop_name = self._config.get(CONF_STOP_NAME, "")
        mode_label = MODE_LABELS.get(self._config.get(CONF_MODE, ""), "")
        if self._config.get(CONF_JOURNEY_MODE):
            transfer = self._config.get(CONF_TRANSFER_STOP_NAME, "")
            final = self._config.get(CONF_JOURNEY_DESTINATION_STOP_NAME, "")
            suggested = f"{stop_name} → {transfer} → {final}"
        elif self._config.get(CONF_DESTINATION_STOP_NAME):
            dest_name = self._config[CONF_DESTINATION_STOP_NAME]
            suggested = f"{stop_name} → {dest_name}"
        else:
            suggested = f"{stop_name} {mode_label}".strip()

        # Fetch any active alerts for this stop to show in confirm step
        disruption_summary = await self._get_disruption_summary()

        # City Loop note — warn when both stops are loop platforms
        city_loop_note = ""
        stop_id = self._config.get(CONF_STOP_ID, "")
        dest_stop_id = self._config.get(CONF_DESTINATION_STOP_ID)
        if stop_id in CITY_LOOP_STOP_IDS:
            if dest_stop_id and dest_stop_id in CITY_LOOP_STOP_IDS:
                city_loop_note = (
                    "\n\n⚠️ Both stops are City Loop stations. "
                    "**All destinations** is recommended — it shows services in both "
                    "loop directions so you never miss a train."
                )
            else:
                city_loop_note = (
                    "\n\n💡 This is a City Loop station. Trains run in two loop "
                    "directions — **All destinations** covers both automatically."
                )

        if self._config.get(CONF_JOURNEY_MODE):
            dest_display = (
                f"{self._config.get(CONF_TRANSFER_STOP_NAME, '')} → "
                f"{self._config.get(CONF_JOURNEY_DESTINATION_STOP_NAME, '')}"
            )
        else:
            dest_display = self._config.get(CONF_DESTINATION_STOP_NAME) or "All destinations"

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({
                vol.Required(CONF_WATCH_NAME, default=suggested): selector.TextSelector(),
            }),
            description_placeholders={
                "stop_name": stop_name,
                "mode": mode_label,
                "destination": dest_display,
                "disruption": disruption_summary,
                "city_loop_note": city_loop_note,
            },
        )

    async def _get_disruption_summary(self) -> str:
        """Fetch current service alerts for the configured mode, return summary string."""
        from .realtime.client import PTVRealtimeClient
        from .realtime.service_alerts import parse_alerts

        mode = self._config.get(CONF_MODE, "")
        stop_id = self._config.get(CONF_STOP_ID, "")
        api_key = self._config.get(CONF_API_KEY, "")

        try:
            session = async_get_clientsession(self.hass)
            client = PTVRealtimeClient(api_key, session)
            feed = await client.fetch_feed(mode, "service-alerts")
            alerts = parse_alerts(feed, stop_id=stop_id)
            if not alerts:
                return "No current disruptions."
            return f"{len(alerts)} active disruption(s): {alerts[0].header}"
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Vehicle tracker sub-flow (branched from Step 1 via entry type choice)
    # ------------------------------------------------------------------

    async def async_step_entry_type(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between a Departure Watch, Vehicle Tracker, Route Tracker, or Board.

        The board name is collected on this same screen (no separate naming
        step): picking "Trip-Group Board" and submitting creates the board with
        the name entered here.
        """
        if user_input is not None:
            entry_type = user_input.get("entry_type", ENTRY_TYPE_WATCH)
            if entry_type == ENTRY_TYPE_VEHICLE_TRACKER:
                return await self.async_step_vehicle_tracker()
            if entry_type == ENTRY_TYPE_ROUTE_TRACKER:
                return await self.async_step_route_tracker_mode()
            if entry_type == ENTRY_TYPE_BOARD:
                return await self._create_board_entry(
                    user_input.get(CONF_BOARD_NAME, "")
                )
            if entry_type == _IMPORT_OPTION:
                return await self.async_step_import()
            # Default: departure watch — continue with mode selection
            return await self.async_step_mode()

        return self.async_show_form(
            step_id="entry_type",
            data_schema=self._entry_type_schema(),
        )

    def _entry_type_schema(self) -> vol.Schema:
        """Build the "what to add" form (type selector + board name)."""
        return vol.Schema({
            vol.Required("entry_type", default=ENTRY_TYPE_WATCH): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(
                            value=ENTRY_TYPE_WATCH,
                            label="Station Departures",
                        ),
                        selector.SelectOptionDict(
                            value=ENTRY_TYPE_VEHICLE_TRACKER,
                            label="Vehicle Tracker (linked to Station)",
                        ),
                        selector.SelectOptionDict(
                            value=ENTRY_TYPE_ROUTE_TRACKER,
                            label="Route Tracker (all vehicles on a line)",
                        ),
                        selector.SelectOptionDict(
                            value=ENTRY_TYPE_BOARD,
                            label="Trip-Group Board (multiple lines at a stop)",
                        ),
                        selector.SelectOptionDict(
                            value=_IMPORT_OPTION,
                            label="Trip-Group Board — import from existing watches",
                        ),
                    ],
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Optional(CONF_BOARD_NAME, default="Departure Board"): selector.TextSelector(),
        })

    async def _create_board_entry(self, board_name: str) -> ConfigFlowResult:
        """Create the board parent config entry from the merged first screen.

        Pins are added afterwards as subentries (see PTVBoardSubentryFlow).  A
        fresh board triggers a full GTFS refresh (all bundles) in the background
        so the subsequent pin flow has schedule data to resolve against.
        """
        board_name = board_name.strip()
        if not board_name:
            return self.async_show_form(
                step_id="entry_type",
                errors={CONF_BOARD_NAME: "required"},
                data_schema=self._entry_type_schema(),
            )
        self.hass.async_create_task(async_trigger_gtfs_refresh(self.hass))
        return self.async_create_entry(
            title=board_name,
            data={
                CONF_ENTRY_TYPE: ENTRY_TYPE_BOARD,
                CONF_BOARD_NAME: board_name,
                CONF_API_KEY: self._config.get(CONF_API_KEY, ""),
            },
            options={
                CONF_DEPARTURE_COUNT: BOARD_DEFAULT_DEPARTURE_COUNT,
                CONF_REALTIME_ENABLED: True,
                CONF_SUBENTRY_ORDER: [],
                CONF_SHOW_RUN_NUMBERS: DEFAULT_SHOW_RUN_NUMBERS,
                CONF_SHOW_VEHICLE_TYPE: DEFAULT_SHOW_VEHICLE_TYPE,
            },
        )

    # ------------------------------------------------------------------
    # T11 — Import existing watches into a new trip-group board
    # ------------------------------------------------------------------

    async def async_step_import(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select one or more compatible watches to import into a new board.

        Lists the CURRENT compatible watches (non-journey, non-tracker, with a
        resolvable single line) as checkboxes plus a board name.  On submit the
        selection is classified (imported / skipped / stale-flagged) and the
        flow advances to the summary step.  Source entries are never touched.
        """
        db_path = _db_path(self.hass.config.config_dir)
        entries = self.hass.config_entries.async_entries(DOMAIN)

        watch_entries = [
            e for e in entries
            if e.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_WATCH) == ENTRY_TYPE_WATCH
        ]
        tracker_entries = [
            e for e in entries
            if e.data.get(CONF_ENTRY_TYPE) in (ENTRY_TYPE_VEHICLE_TRACKER, ENTRY_TYPE_ROUTE_TRACKER)
        ]

        # Map all watches off the event loop (the mapper runs sqlite queries).
        mappings: dict[str, WatchMapping] = await self.hass.async_add_executor_job(
            map_watches, db_path, [(e.entry_id, e.data) for e in watch_entries]
        )
        compatible = [
            (e, mappings[e.entry_id]) for e in watch_entries
            if mappings[e.entry_id].compatible
        ]

        if not compatible:
            return self.async_abort(reason="no_compatible_watches")

        if user_input is not None:
            selected = list(user_input.get("watch_ids") or [])
            board_name = user_input.get(CONF_BOARD_NAME, "").strip()
            errors: dict[str, str] = {}
            if not selected:
                errors["watch_ids"] = "select_at_least_one"
            elif not board_name:
                errors[CONF_BOARD_NAME] = "required"
            if not errors:
                self._import_watch_ids = selected
                self._import_board_name = board_name
                self._import_skipped = self._classify_skipped(
                    watch_entries, tracker_entries, mappings
                )
                self._import_stale = [
                    (
                        self._entry_label(entry),
                        "; ".join(
                            STALE_LABELS[f] for f in mappings[entry.entry_id].stale_fields
                        ),
                    )
                    for entry in watch_entries
                    if entry.entry_id in selected
                    and mappings[entry.entry_id].stale
                ]
                return await self.async_step_import_summary()
            return self.async_show_form(
                step_id="import",
                data_schema=self._import_schema(compatible, board_name),
                errors=errors,
            )

        return self.async_show_form(
            step_id="import",
            data_schema=self._import_schema(compatible, "Departure Board"),
        )

    @staticmethod
    def _entry_label(entry: ConfigEntry) -> str:
        """Return a stable display label for an entry (watch name → title)."""
        return entry.data.get(CONF_WATCH_NAME) or entry.title or entry.entry_id

    @staticmethod
    def _classify_skipped(
        watch_entries: list[ConfigEntry],
        tracker_entries: list[ConfigEntry],
        mappings: dict[str, WatchMapping],
    ) -> list[tuple[str, str]]:
        """Return ``(label, reason_label)`` for every entry that stays as-is.

        Covers transfer journeys + incompatible all-lines watches (from the
        watch mappings) and vehicle/route trackers (separate entry types).
        """
        skipped: list[tuple[str, str]] = []
        for entry in watch_entries:
            mapping = mappings[entry.entry_id]
            if mapping.compatible:
                continue
            reason = REASON_LABELS.get(mapping.reason, mapping.reason or "incompatible")
            skipped.append((PTVConfigFlow._entry_label(entry), reason))
        for entry in tracker_entries:
            skipped.append((PTVConfigFlow._entry_label(entry), REASON_LABELS["tracker"]))
        return skipped

    def _import_schema(
        self, compatible: list[tuple[ConfigEntry, WatchMapping]], default_name: str
    ) -> vol.Schema:
        """Build the checkbox + board-name schema for the import select step."""
        options = [
            selector.SelectOptionDict(
                value=e.entry_id, label=self._entry_label(e)
            )
            for e, _m in compatible
        ]
        return vol.Schema({
            vol.Required("watch_ids"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Required(CONF_BOARD_NAME, default=default_name): selector.TextSelector(),
        })

    async def async_step_import_summary(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the import summary, then create the board with one pin per watch.

        The summary lists imported ✓ entries, skipped entries (with reason), and
        stale-flagged entries.  Submitting creates the board parent entry with
        one pin subentry per selected compatible watch — via the same pin
        derivation path T8 uses.  Source entries are never modified.
        """
        if user_input is not None:
            return await self._create_imported_board()

        imported = self._import_watch_ids
        imported_labels = [
            self._entry_label(
                self.hass.config_entries.async_get_entry(entry_id)
            )
            for entry_id in imported
            if self.hass.config_entries.async_get_entry(entry_id) is not None
        ]
        skipped_text = (
            "\n".join(f"- {name}: {reason}" for name, reason in self._import_skipped)
            or "- none"
        )
        stale_text = (
            "\n".join(f"- {name}: {fields}" for name, fields in self._import_stale)
            or "- none"
        )

        return self.async_show_form(
            step_id="import_summary",
            data_schema=vol.Schema({
                vol.Required("confirm", default=True): selector.BooleanSelector(),
            }),
            description_placeholders={
                "board_name": self._import_board_name,
                "imported": "\n".join(f"- {n}" for n in imported_labels) or "- none",
                "skipped": skipped_text,
                "stale": stale_text,
            },
        )

    async def _create_imported_board(self) -> ConfigFlowResult:
        """Create the board parent entry with one pin subentry per watch."""
        db_path = _db_path(self.hass.config.config_dir)
        entries = self.hass.config_entries.async_entries(DOMAIN)
        selected = [
            e for e in entries
            if e.entry_id in self._import_watch_ids
            and e.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_WATCH) == ENTRY_TYPE_WATCH
        ]

        mappings: dict[str, WatchMapping] = await self.hass.async_add_executor_job(
            map_watches, db_path, [(e.entry_id, e.data) for e in selected]
        )

        subentries: list[dict[str, Any]] = []
        for entry in selected:
            pin = mappings[entry.entry_id].pin
            if pin is None:
                continue
            subentries.append({
                "data": pin.to_dict(),
                "subentry_type": SUBENTRY_TYPE_PIN,
                "title": pin.title,
                "unique_id": pin.pin_key,
            })

        self.hass.async_create_task(async_trigger_gtfs_refresh(self.hass))
        return self.async_create_entry(
            title=self._import_board_name,
            data={
                CONF_ENTRY_TYPE: ENTRY_TYPE_BOARD,
                CONF_BOARD_NAME: self._import_board_name,
                CONF_API_KEY: self._get_existing_api_key() or "",
            },
            options={
                CONF_DEPARTURE_COUNT: BOARD_DEFAULT_DEPARTURE_COUNT,
                CONF_REALTIME_ENABLED: True,
                CONF_SUBENTRY_ORDER: [],
                CONF_SHOW_RUN_NUMBERS: DEFAULT_SHOW_RUN_NUMBERS,
                CONF_SHOW_VEHICLE_TYPE: DEFAULT_SHOW_VEHICLE_TYPE,
            },
            subentries=subentries,
        )

    @classmethod
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the subentry types this handler supports (pins for boards)."""
        if config_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_BOARD:
            return {SUBENTRY_TYPE_PIN: PTVBoardSubentryFlow}
        return {}

    async def async_step_vehicle_tracker(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure a vehicle tracker linked to an existing departure watch."""
        errors: dict[str, str] = {}

        # Get existing departure watch entries
        existing_watches = [
            e for e in self.hass.config_entries.async_entries(DOMAIN)
            if e.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_WATCH) == ENTRY_TYPE_WATCH
        ]

        if not existing_watches:
            return self.async_abort(reason="no_watches")

        if user_input is not None:
            parent_id = user_input.get("parent_entry_id", "")
            if not parent_id:
                errors["parent_entry_id"] = "required"
            else:
                # Store selected parent and move to name step
                self._config[CONF_PARENT_ENTRY_ID] = parent_id
                return await self.async_step_vehicle_tracker_name()

        watch_options = [
            selector.SelectOptionDict(
                value=e.entry_id,
                label=e.data.get(CONF_WATCH_NAME, e.entry_id),
            )
            for e in existing_watches
        ]

        return self.async_show_form(
            step_id="vehicle_tracker",
            data_schema=vol.Schema({
                vol.Required("parent_entry_id", default=existing_watches[0].entry_id): _select_selector(watch_options),
            }),
            errors=errors,
        )

    async def async_step_vehicle_tracker_name(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Name the vehicle tracker (auto-populated from the selected station)."""
        parent_id: str = self._config.get(CONF_PARENT_ENTRY_ID, "")
        parent_entry = self.hass.config_entries.async_get_entry(parent_id)
        parent_name = parent_entry.data.get(CONF_WATCH_NAME, parent_id) if parent_entry else parent_id
        parent_mode = parent_entry.data.get(CONF_MODE, "") if parent_entry else ""
        suggested_name = f"{parent_name} Vehicle" if parent_name else "PTV Vehicle"

        if user_input is not None:
            watch_name = user_input.get(CONF_WATCH_NAME, "").strip() or suggested_name
            return self.async_create_entry(
                title=watch_name,
                data={
                    CONF_ENTRY_TYPE: ENTRY_TYPE_VEHICLE_TRACKER,
                    CONF_PARENT_ENTRY_ID: parent_id,
                    CONF_WATCH_NAME: watch_name,
                    CONF_MODE: parent_mode,
                },
            )

        return self.async_show_form(
            step_id="vehicle_tracker_name",
            data_schema=vol.Schema({
                vol.Required(CONF_WATCH_NAME, default=suggested_name): selector.TextSelector(),
            }),
            description_placeholders={"station_name": parent_name},
        )

    # ------------------------------------------------------------------
    # Route tracker sub-flow
    # ------------------------------------------------------------------

    async def async_step_route_tracker_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the transport mode for the route tracker."""
        if user_input is not None:
            self._config[CONF_MODE] = user_input[CONF_MODE]
            bundles = MODE_BUNDLE_MAP.get(user_input[CONF_MODE], [])
            db = _db_path(self.hass.config.config_dir)
            if bundles and not is_mode_ready(db, bundles):
                self._post_import_step = "route_tracker_route"
                return await self.async_step_downloading()
            return await self.async_step_route_tracker_route()

        return self.async_show_form(
            step_id="route_tracker_mode",
            data_schema=vol.Schema({
                vol.Required(CONF_MODE, default=MODES[0]): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=m, label=MODE_LABELS[m])
                            for m in MODES
                        ],
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }),
        )

    async def async_step_route_tracker_route(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a route/line to track."""
        mode: str = self._config.get(CONF_MODE, MODES[0])
        route_types = ROUTE_TYPE_MAP.get(mode, [])
        db = _db_path(self.hass.config.config_dir)

        if user_input is not None:
            route_id = user_input.get("route_id_select", "")
            if not route_id:
                return self.async_show_form(
                    step_id="route_tracker_route",
                    errors={"route_id_select": "required"},
                    data_schema=self._route_tracker_route_schema(self._route_tracker_routes, ""),
                )
            # Find the route in the cached list, or search all routes
            matched = next((r for r in self._route_tracker_routes if r.route_id == route_id), None)
            if matched is None:
                all_routes = await self.hass.async_add_executor_job(get_all_routes, db, route_types)
                matched = next((r for r in all_routes if r.route_id == route_id), None)

            if matched is None:
                return self.async_show_form(
                    step_id="route_tracker_route",
                    errors={"route_id_select": "invalid_route"},
                    data_schema=self._route_tracker_route_schema(self._route_tracker_routes, ""),
                )

            self._config[CONF_ROUTE_ID] = matched.route_id
            self._config[CONF_ROUTE_SHORT_NAME] = matched.route_short_name
            self._config[CONF_ROUTE_LONG_NAME] = matched.route_long_name
            self._config[CONF_ROUTE_COLOR] = matched.route_color
            return await self.async_step_route_tracker_filter()

        # Auto-load routes for the mode and cache them for re-display on error
        self._route_tracker_routes = await self.hass.async_add_executor_job(
            get_all_routes, db, route_types
        )
        if not self._route_tracker_routes:
            return self.async_abort(reason="no_routes_found")

        return self.async_show_form(
            step_id="route_tracker_route",
            data_schema=self._route_tracker_route_schema(self._route_tracker_routes, ""),
        )

    def _route_tracker_route_schema(
        self,
        routes: list[RouteResult],
        default_route_id: str,
    ) -> vol.Schema:
        options = [
            selector.SelectOptionDict(value=r.route_id, label=_route_display_label(r))
            for r in routes
        ]
        default = default_route_id or (routes[0].route_id if routes else "")
        return vol.Schema({
            vol.Optional("route_search", default=""): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.SEARCH)
            ),
            vol.Required("route_id_select", default=default): _select_selector(options),
        })

    async def async_step_route_tracker_filter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose whether to filter vehicles by proximity to a stop."""
        _PROXIMITY_MODE = "proximity"
        _ALL_MODE = "all"

        if user_input is not None:
            filter_mode = user_input.get("filter_mode", _ALL_MODE)
            if filter_mode == _PROXIMITY_MODE:
                return await self.async_step_route_tracker_stop()
            return await self.async_step_route_tracker_confirm()

        return self.async_show_form(
            step_id="route_tracker_filter",
            data_schema=vol.Schema({
                vol.Required("filter_mode", default=_ALL_MODE): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=_ALL_MODE, label="Track all vehicles on route"),
                            selector.SelectOptionDict(value=_PROXIMITY_MODE, label="Filter by proximity to a stop"),
                        ],
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }),
        )

    async def async_step_route_tracker_stop(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a reference stop for proximity filtering."""
        mode: str = self._config.get(CONF_MODE, MODES[0])
        route_types = ROUTE_TYPE_MAP.get(mode, [])
        db = _db_path(self.hass.config.config_dir)

        if user_input is not None:
            stop_id = user_input.get("ref_stop_id", "")
            stop_name = user_input.get("ref_stop_name", "")
            if not stop_id:
                # Re-show with error — load stops for the form
                error_stops: list[StopResult] = await self.hass.async_add_executor_job(
                    search_stops_by_name, db, "", route_types
                )
                return self.async_show_form(
                    step_id="route_tracker_stop",
                    errors={"ref_stop_id": "required"},
                    data_schema=self._route_tracker_stop_schema(error_stops),
                )
            # Resolve lat/lon from DB
            coords = await self.hass.async_add_executor_job(get_stop_lat_lon, db, stop_id)
            if coords:
                self._config[CONF_REFERENCE_STOP_LAT] = coords[0]
                self._config[CONF_REFERENCE_STOP_LON] = coords[1]
            self._config[CONF_REFERENCE_STOP_ID] = stop_id
            self._config[CONF_REFERENCE_STOP_NAME] = stop_name or stop_id
            return await self.async_step_route_tracker_proximity()

        # Pre-load stops: try distance first (HA home zone), fallback to empty
        stops: list[StopResult] = []
        try:
            home_zone = self.hass.states.get("zone.home")
            if home_zone and home_zone.attributes.get("latitude"):
                lat = float(home_zone.attributes["latitude"])
                lon = float(home_zone.attributes["longitude"])
                stops = await self.hass.async_add_executor_job(
                    search_stops_by_distance, db, lat, lon, route_types, 2000.0
                )
        except Exception:  # noqa: BLE001
            pass

        return self.async_show_form(
            step_id="route_tracker_stop",
            data_schema=self._route_tracker_stop_schema(stops),
        )

    def _route_tracker_stop_schema(self, stops: list[StopResult]) -> vol.Schema:
        options = _stops_to_selector_options(stops, show_distance=True)
        default = stops[0].stop_id if stops else ""
        return vol.Schema({
            vol.Optional("ref_stop_search", default=""): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.SEARCH)
            ),
            vol.Required("ref_stop_id", default=default): _select_selector(options),
            vol.Optional("ref_stop_name", default=stops[0].stop_name if stops else ""): selector.TextSelector(),
        })

    async def async_step_route_tracker_proximity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set proximity radius and max vehicle count."""
        if user_input is not None:
            radius_str = user_input.get("proximity_radius", "2000")
            try:
                self._config[CONF_PROXIMITY_RADIUS_M] = float(radius_str)
            except (ValueError, TypeError):
                self._config[CONF_PROXIMITY_RADIUS_M] = 2000.0
            self._config[CONF_MAX_VEHICLES] = int(user_input.get(CONF_MAX_VEHICLES, DEFAULT_MAX_VEHICLES))
            return await self.async_step_route_tracker_confirm()

        radius_options = [
            selector.SelectOptionDict(value="500", label="500 m"),
            selector.SelectOptionDict(value="1000", label="1 km"),
            selector.SelectOptionDict(value="2000", label="2 km"),
            selector.SelectOptionDict(value="5000", label="5 km"),
        ]

        return self.async_show_form(
            step_id="route_tracker_proximity",
            data_schema=vol.Schema({
                vol.Required("proximity_radius", default="2000"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=radius_options,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(CONF_MAX_VEHICLES, default=DEFAULT_MAX_VEHICLES): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=10, step=1, mode=selector.NumberSelectorMode.BOX)
                ),
            }),
        )

    async def async_step_route_tracker_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Name the route tracker and create the config entry."""
        route_short = self._config.get(CONF_ROUTE_SHORT_NAME, "")
        route_long = self._config.get(CONF_ROUTE_LONG_NAME, "")
        suggested_name = f"{route_short} Tracker" if route_short else f"{route_long} Tracker"

        if user_input is not None:
            tracker_name = user_input.get(CONF_WATCH_NAME, "").strip() or suggested_name
            max_vehicles = int(user_input.get(CONF_MAX_VEHICLES, self._config.get(CONF_MAX_VEHICLES, DEFAULT_MAX_VEHICLES)))

            entry_data: dict[str, Any] = {
                CONF_ENTRY_TYPE: ENTRY_TYPE_ROUTE_TRACKER,
                CONF_API_KEY: self._config.get(CONF_API_KEY, ""),
                CONF_MODE: self._config.get(CONF_MODE, ""),
                CONF_ROUTE_ID: self._config.get(CONF_ROUTE_ID, ""),
                CONF_ROUTE_SHORT_NAME: self._config.get(CONF_ROUTE_SHORT_NAME, ""),
                CONF_ROUTE_LONG_NAME: self._config.get(CONF_ROUTE_LONG_NAME, ""),
                CONF_ROUTE_COLOR: self._config.get(CONF_ROUTE_COLOR),
                CONF_WATCH_NAME: tracker_name,
                CONF_MAX_VEHICLES: max_vehicles,
            }

            # Proximity filter fields (only if reference stop was chosen)
            if self._config.get(CONF_REFERENCE_STOP_ID):
                entry_data[CONF_REFERENCE_STOP_ID] = self._config[CONF_REFERENCE_STOP_ID]
                entry_data[CONF_REFERENCE_STOP_NAME] = self._config.get(CONF_REFERENCE_STOP_NAME, "")
                entry_data[CONF_REFERENCE_STOP_LAT] = self._config.get(CONF_REFERENCE_STOP_LAT)
                entry_data[CONF_REFERENCE_STOP_LON] = self._config.get(CONF_REFERENCE_STOP_LON)
                entry_data[CONF_PROXIMITY_RADIUS_M] = float(self._config.get(CONF_PROXIMITY_RADIUS_M, 2000.0))

            return self.async_create_entry(title=tracker_name, data=entry_data)

        # If no proximity filter, ask for max_vehicles here
        has_proximity = bool(self._config.get(CONF_REFERENCE_STOP_ID))
        ref_stop_name = self._config.get(CONF_REFERENCE_STOP_NAME, "")

        description_placeholders: dict[str, str] = {
            "route_name": f"{route_short} \u2013 {route_long}" if route_long else route_short,
        }
        if has_proximity:
            description_placeholders["filter_note"] = (
                f"Filtering to vehicles within {float(self._config.get(CONF_PROXIMITY_RADIUS_M, 2000)):.0f} m "
                f"of {ref_stop_name}."
            )

        schema_fields: dict[Any, Any] = {
            vol.Required(CONF_WATCH_NAME, default=suggested_name): selector.TextSelector(),
        }
        if not has_proximity:
            schema_fields[vol.Required(CONF_MAX_VEHICLES, default=DEFAULT_MAX_VEHICLES)] = (
                selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=10, step=1, mode=selector.NumberSelectorMode.BOX)
                )
            )

        return self.async_show_form(
            step_id="route_tracker_confirm",
            data_schema=vol.Schema(schema_fields),
            description_placeholders=description_placeholders,
        )

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow for this entry."""
        return PTVOptionsFlow()


class PTVBoardSubentryFlow(ConfigSubentryFlow):  # type: ignore[misc]
    """Create or edit one trip-group pin (a HA subentry of a board entry).

    Add flow (source ``user``) mirrors the TripView order, adapted to the T7
    resolver:

        1. ``origin``       — pick the boarding stop (cross-mode name search)
        2. ``to``           — "All destinations" (default) or a specific stop
        3. ``select_lines`` — one line/direction candidate from the resolver
                              (``get_lines_between`` for a specific destination,
                              ``get_routes_for_stop`` for "all destinations");
                              auto-advances when a single candidate exists
        4. ``options``      — per-pin labels, include toggle, platform filter,
                              colour override
        5. create the subentry (``pin_key`` as the unique_id)

    Reconfigure flow (source ``reconfigure``) edits the labels / include /
    platform / colour of an existing pin in place.
    """

    def __init__(self) -> None:
        self._stop_search_results: list[StopResult] = []
        self._dest_search_results: list[StopResult] = []
        self._all_destinations = True
        self._destination_stop_id: str | None = None
        self._destination_stop_name: str | None = None
        self._boarding_stop_id = ""
        self._boarding_stop_name = ""
        self._route_choices: list[RouteResult] = []
        self._line_choices: list[LineCandidate] = []
        self._selected_route: RouteResult | None = None
        self._selected_line: LineCandidate | None = None
        # Pin drafted by ``options``, committed by the "add another?" menu.
        self._pending_pin: PinData | None = None

    def _db(self) -> Path:
        return _db_path(self.hass.config.config_dir)

    def _reset_pin_add_state(self) -> None:
        """Clear add-flow scratch state so the next pin starts fresh."""
        self._stop_search_results = []
        self._dest_search_results = []
        self._all_destinations = True
        self._destination_stop_id = None
        self._destination_stop_name = None
        self._boarding_stop_id = ""
        self._boarding_stop_name = ""
        self._route_choices = []
        self._line_choices = []
        self._selected_route = None
        self._selected_line = None
        self._pending_pin = None

    def _commit_pending_pin(self) -> None:
        """Add the pending pin directly to the board's subentries.

        Used by the "Add another line" branch so the flow can continue for a
        second pin without ending (``async_create_entry`` finalises the flow,
        and a subentry flow creates exactly one subentry per run).
        """
        assert self._pending_pin is not None
        pin = self._pending_pin
        subentry = ConfigSubentry(
            data=pin.to_dict(),
            subentry_type=SUBENTRY_TYPE_PIN,
            title=pin.title,
            unique_id=pin.pin_key,
        )
        self.hass.config_entries.async_add_subentry(self._get_entry(), subentry)

    # -- add flow -------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start the add-pin flow at the origin stop step."""
        return await self.async_step_origin(user_input)

    async def async_step_origin(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Search for and select the boarding stop (cross-mode)."""
        errors: dict[str, str] = {}
        db = self._db()

        if user_input is not None:
            if CONF_STOP_ID in user_input and user_input[CONF_STOP_ID]:
                stop_id = user_input[CONF_STOP_ID]
                matching = [s for s in self._stop_search_results if s.stop_id == stop_id]
                stop_name = matching[0].stop_name if matching else stop_id
                self._boarding_stop_id = stop_id
                self._boarding_stop_name = stop_name
                return await self.async_step_to(None)

            query = user_input.get("query", "").strip()
            if len(query) < 2:
                if query:
                    errors["query"] = "search_too_short"
            else:
                self._stop_search_results = await self.hass.async_add_executor_job(
                    search_stops_by_name, db, query, ALL_ROUTE_TYPES, 20
                )
                if not self._stop_search_results:
                    errors["query"] = "no_stops_found"

        schema_fields: dict[Any, Any] = {
            vol.Optional("query"): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.SEARCH)
            ),
        }
        if self._stop_search_results:
            schema_fields[vol.Optional(CONF_STOP_ID)] = _select_selector(
                _stops_to_selector_options(self._stop_search_results)
            )

        return self.async_show_form(
            step_id="origin",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
        )

    async def async_step_to(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose "All destinations" or a specific destination stop."""
        errors: dict[str, str] = {}
        db = self._db()

        if user_input is not None:
            choice = user_input.get("destination_choice", "__all__")

            if choice == "__all__":
                self._all_destinations = True
                self._destination_stop_id = None
                self._destination_stop_name = None
                return await self.async_step_select_lines(None)

            # "specific" — a destination stop was selected from the search list
            if CONF_DESTINATION_STOP_ID in user_input and user_input[CONF_DESTINATION_STOP_ID]:
                stop_id = user_input[CONF_DESTINATION_STOP_ID]
                matching = [s for s in self._dest_search_results if s.stop_id == stop_id]
                stop_name = matching[0].stop_name if matching else stop_id
                self._all_destinations = False
                self._destination_stop_id = stop_id
                self._destination_stop_name = stop_name
                return await self.async_step_select_lines(None)

            query = user_input.get("dest_query", "").strip()
            if len(query) < 2:
                if query:
                    errors["dest_query"] = "search_too_short"
            else:
                self._dest_search_results = await self.hass.async_add_executor_job(
                    search_stops_by_name, db, query, ALL_ROUTE_TYPES, 20
                )
                if not self._dest_search_results:
                    errors["dest_query"] = "no_stops_found"

        options = [
            selector.SelectOptionDict(value="__all__", label="All destinations"),
            selector.SelectOptionDict(value="specific", label="Specific destination"),
        ]
        schema_fields: dict[Any, Any] = {
            vol.Required(
                "destination_choice", default="__all__"
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
        }
        if user_input and user_input.get("destination_choice") == "specific":
            schema_fields[vol.Optional("dest_query")] = selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.SEARCH)
            )
            if self._dest_search_results:
                schema_fields[
                    vol.Optional(CONF_DESTINATION_STOP_ID)
                ] = _select_selector(
                    _stops_to_selector_options(self._dest_search_results)
                )

        return self.async_show_form(
            step_id="to",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
            description_placeholders={
                "stop_name": self._boarding_stop_name,
            },
        )

    async def async_step_select_lines(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick one line/direction combination (single-select; auto-advance)."""
        db = self._db()

        if self._all_destinations:
            if not self._route_choices:
                self._route_choices = await self.hass.async_add_executor_job(
                    get_routes_for_stop, db, self._boarding_stop_id, ALL_ROUTE_TYPES
                )
            if not self._route_choices:
                return self.async_abort(reason="no_routes_found")
            if user_input is not None:
                route_id = user_input.get("route_id_select")
                matched = next(
                    (r for r in self._route_choices if r.route_id == route_id), None
                )
                if matched is None:
                    matched = self._route_choices[0]
                self._selected_route = matched
                return await self.async_step_options(None)
            if len(self._route_choices) == 1:
                self._selected_route = self._route_choices[0]
                return await self.async_step_options(None)
            options = [
                selector.SelectOptionDict(
                    value=r.route_id, label=_route_display_label(r)
                )
                for r in self._route_choices
            ]
            return self.async_show_form(
                step_id="select_lines",
                data_schema=vol.Schema({
                    vol.Required(
                        "route_id_select", default=self._route_choices[0].route_id
                    ): _select_selector(options),
                }),
                description_placeholders={"stop_name": self._boarding_stop_name},
            )

        # Specific destination → resolver candidates via get_lines_between
        if not self._line_choices:
            self._line_choices = await self.hass.async_add_executor_job(
                get_lines_between,
                db,
                self._boarding_stop_id,
                self._destination_stop_id,
                None,  # cross-mode
            )
        if not self._line_choices:
            return self.async_abort(reason="destination_unreachable")
        if user_input is not None:
            line_value_str = user_input.get("line_select")
            matched = next(
                (l for l in self._line_choices if line_value_str == line_value(l)),
                None,
            )
            if matched is None:
                matched = self._line_choices[0]
            self._selected_line = matched
            return await self.async_step_options(None)
        if len(self._line_choices) == 1:
            self._selected_line = self._line_choices[0]
            return await self.async_step_options(None)
        options = [
            selector.SelectOptionDict(value=line_value(l), label=line_label(l))
            for l in self._line_choices
        ]
        return self.async_show_form(
            step_id="select_lines",
            data_schema=vol.Schema({
                vol.Required(
                    "line_select", default=line_value(self._line_choices[0])
                ): _select_selector(options),
            }),
            description_placeholders={
                "stop_name": self._boarding_stop_name,
                "destination": self._destination_stop_name or "",
            },
        )

    def _draft_pin(self) -> PinData:
        """Return the draft pin for the currently selected route/line."""
        if self._selected_line is not None:
            return pin_from_line(
                self._selected_line, self._boarding_stop_id, self._boarding_stop_name
            )
        assert self._selected_route is not None
        return pin_from_route_all(
            self._selected_route, self._boarding_stop_id, self._boarding_stop_name
        )

    async def async_step_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Per-pin labels, include toggle, platform filter, colour override."""
        draft = self._draft_pin()

        if user_input is not None:
            pin = PinData(
                pin_key=draft.pin_key,
                route_type=draft.route_type,
                route_id=draft.route_id,
                boarding_stop_id=draft.boarding_stop_id,
                boarding_stop_name=draft.boarding_stop_name,
                route_short_name=draft.route_short_name,
                route_long_name=draft.route_long_name,
                all_destinations=draft.all_destinations,
                destination_stop_id=draft.destination_stop_id,
                destination_stop_name=draft.destination_stop_name,
                direction_id=draft.direction_id,
                via_hint=draft.via_hint,
                label_from=user_input.get(CONF_LABEL_FROM, draft.label_from).strip()
                or draft.label_from,
                label_to=user_input.get(CONF_LABEL_TO, draft.label_to).strip()
                or draft.label_to,
                platform_filter=user_input.get(CONF_PLATFORM_FILTER) or None,
                colour_override=user_input.get(CONF_COLOUR_OVERRIDE) or None,
                include=bool(user_input.get(CONF_INCLUDE, True)),
            )
            self._pending_pin = pin
            return await self.async_step_add_another()

        return self.async_show_form(
            step_id="options",
            data_schema=vol.Schema({
                vol.Required(CONF_LABEL_FROM, default=draft.label_from): selector.TextSelector(),
                vol.Required(CONF_LABEL_TO, default=draft.label_to): selector.TextSelector(),
                vol.Required(CONF_INCLUDE, default=True): selector.BooleanSelector(),
                vol.Optional(CONF_PLATFORM_FILTER, default=""): selector.TextSelector(),
                vol.Optional(CONF_COLOUR_OVERRIDE, default=""): selector.TextSelector(),
            }),
        )

    # -- "Add another line?" menu ---------------------------------------

    async def async_step_add_another(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer to add another line or finish after drafting a pin."""
        return self.async_show_menu(
            step_id="add_another",
            menu_options=["add_another_line", "finish"],
        )

    async def async_step_add_another_line(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Commit the pending pin and restart the add flow for the same board."""
        self._commit_pending_pin()
        self._reset_pin_add_state()
        return await self.async_step_origin(None)

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the pending pin and end the add flow."""
        assert self._pending_pin is not None
        pin = self._pending_pin
        return self.async_create_entry(
            title=pin.title,
            data=pin.to_dict(),
            unique_id=pin.pin_key,
        )

    # -- reconfigure (edit) flow ----------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit a pin's labels, include flag, platform filter, and colour."""
        subentry = self._get_reconfigure_subentry()
        pin = PinData.from_dict(subentry.data)

        if user_input is not None:
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                data_updates={
                    CONF_LABEL_FROM: user_input.get(CONF_LABEL_FROM, pin.label_from),
                    CONF_LABEL_TO: user_input.get(CONF_LABEL_TO, pin.label_to),
                    CONF_INCLUDE: bool(user_input.get(CONF_INCLUDE, pin.include)),
                    CONF_PLATFORM_FILTER: user_input.get(CONF_PLATFORM_FILTER) or None,
                    CONF_COLOUR_OVERRIDE: user_input.get(CONF_COLOUR_OVERRIDE) or None,
                },
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({
                vol.Required(CONF_LABEL_FROM, default=pin.label_from): selector.TextSelector(),
                vol.Required(CONF_LABEL_TO, default=pin.label_to): selector.TextSelector(),
                vol.Required(CONF_INCLUDE, default=pin.include): selector.BooleanSelector(),
                vol.Optional(CONF_PLATFORM_FILTER, default=pin.platform_filter or ""): selector.TextSelector(),
                vol.Optional(CONF_COLOUR_OVERRIDE, default=pin.colour_override or ""): selector.TextSelector(),
            }),
        )


class PTVOptionsFlow(OptionsFlow):  # type: ignore[misc]
    """Options flow for an existing PTV entry.

    Shows different fields depending on the entry type:
    - Departure watch: name, poll interval, realtime toggle, departure count
    - Route tracker: name, max vehicles
    - Linked vehicle tracker: name only
    - Board: settings + pin reorder/remove (see ``_board_*`` helpers)
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options form, branching on entry type."""
        entry_type = self.config_entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_WATCH)

        if entry_type == ENTRY_TYPE_ROUTE_TRACKER:
            return await self._step_route_tracker(user_input)
        if entry_type == ENTRY_TYPE_VEHICLE_TRACKER:
            return await self._step_vehicle_tracker(user_input)
        if entry_type == ENTRY_TYPE_BOARD:
            return await self.async_step_board_menu(user_input)
        return await self._step_watch(user_input)

    # ------------------------------------------------------------------
    # Board options (settings + pin reorder/remove)
    # ------------------------------------------------------------------

    def _board_subentries(
        self,
    ) -> list[tuple[str, object]]:
        """Return ``(subentry_id, subentry)`` in canonical display order."""
        ids = list(self.config_entry.subentries.keys())
        order = normalize_order(
            self.config_entry.options.get(CONF_SUBENTRY_ORDER, []), ids
        )
        return [(sid, self.config_entry.subentries[sid]) for sid in order]

    async def async_step_board_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Single-screen board options: one dropdown routes to the sub-step.

        Replaces the old two-hop menu (menu → substep) with a single selection
        that routes directly to settings / reorder / remove.
        """
        if user_input is not None:
            action = user_input.get("board_action", "settings")
            if action == "reorder":
                return await self.async_step_board_reorder()
            if action == "remove":
                return await self.async_step_board_remove()
            return await self.async_step_board_settings()

        return self.async_show_form(
            step_id="board_menu",
            data_schema=vol.Schema({
                vol.Required("board_action", default="settings"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value="settings", label="Board settings"),
                            selector.SelectOptionDict(value="reorder", label="Reorder pins"),
                            selector.SelectOptionDict(value="remove", label="Remove a pin"),
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }),
        )

    async def async_step_board_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Board settings: name, departure count, realtime, and display prefs."""
        if user_input is not None:
            board_name = user_input.get(CONF_BOARD_NAME, "").strip()
            data_updates = {}
            if board_name:
                data_updates[CONF_BOARD_NAME] = board_name
            options = dict(self.config_entry.options)
            options[CONF_DEPARTURE_COUNT] = int(
                user_input.get(CONF_DEPARTURE_COUNT, options.get(CONF_DEPARTURE_COUNT, BOARD_DEFAULT_DEPARTURE_COUNT))
            )
            options[CONF_REALTIME_ENABLED] = bool(
                user_input.get(CONF_REALTIME_ENABLED, options.get(CONF_REALTIME_ENABLED, True))
            )
            options[CONF_SHOW_RUN_NUMBERS] = bool(user_input.get(CONF_SHOW_RUN_NUMBERS, options.get(CONF_SHOW_RUN_NUMBERS, DEFAULT_SHOW_RUN_NUMBERS)))
            options[CONF_SHOW_VEHICLE_TYPE] = bool(user_input.get(CONF_SHOW_VEHICLE_TYPE, options.get(CONF_SHOW_VEHICLE_TYPE, DEFAULT_SHOW_VEHICLE_TYPE)))
            # Persist the name change (data) + options.
            self.hass.config_entries.async_update_entry(
                self.config_entry, **data_updates
            )
            return self.async_create_entry(data=options)

        options = self.config_entry.options
        data = self.config_entry.data
        current_name = data.get(CONF_BOARD_NAME, self.config_entry.title)
        current_count = int(options.get(CONF_DEPARTURE_COUNT, BOARD_DEFAULT_DEPARTURE_COUNT))
        current_realtime = bool(options.get(CONF_REALTIME_ENABLED, True))
        current_runs = bool(options.get(CONF_SHOW_RUN_NUMBERS, DEFAULT_SHOW_RUN_NUMBERS))
        current_vehicle = bool(options.get(CONF_SHOW_VEHICLE_TYPE, DEFAULT_SHOW_VEHICLE_TYPE))

        return self.async_show_form(
            step_id="board_settings",
            data_schema=vol.Schema({
                vol.Required(CONF_BOARD_NAME, default=current_name): selector.TextSelector(),
                vol.Required(CONF_DEPARTURE_COUNT, default=current_count): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=BOARD_MAX_DEPARTURE_COUNT,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(CONF_REALTIME_ENABLED, default=current_realtime): selector.BooleanSelector(),
                vol.Required(CONF_SHOW_RUN_NUMBERS, default=current_runs): selector.BooleanSelector(),
                vol.Required(CONF_SHOW_VEHICLE_TYPE, default=current_vehicle): selector.BooleanSelector(),
            }),
        )

    async def async_step_board_reorder(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Move a pin (up/down/first/last); re-renders for rapid adjustments.

        Applying a move persists the new order immediately and re-renders this
        step so the user can make repeated adjustments without re-navigating.
        Choosing "Done" finalises the options flow.
        """
        subentries = self._board_subentries()
        if not subentries:
            return self.async_abort(reason="no_pins")

        if user_input is not None:
            sid = user_input.get("pin_select")
            action = user_input.get("reorder_action", "done")

            if action == "done":
                options = dict(self.config_entry.options)
                return self.async_create_entry(data=options)

            order = normalize_order(
                self.config_entry.options.get(CONF_SUBENTRY_ORDER, []),
                list(self.config_entry.subentries.keys()),
            )
            new_order = _reorder_subentry(order, sid, action)
            options = {**self.config_entry.options, CONF_SUBENTRY_ORDER: new_order}
            # Persist immediately, then re-render for the next adjustment.
            self.hass.config_entries.async_update_entry(
                self.config_entry, options=options
            )
            return await self.async_step_board_reorder(None)

        pin_options = [
            selector.SelectOptionDict(
                value=sid, label=PinData.from_dict(sub.data).title
            )
            for sid, sub in subentries
        ]
        return self.async_show_form(
            step_id="board_reorder",
            data_schema=vol.Schema({
                vol.Required(
                    "pin_select", default=subentries[0][0]
                ): _select_selector(pin_options),
                vol.Required("reorder_action", default="down"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value="up", label="Move up"),
                            selector.SelectOptionDict(value="down", label="Move down"),
                            selector.SelectOptionDict(value="first", label="Move to first"),
                            selector.SelectOptionDict(value="last", label="Move to last"),
                            selector.SelectOptionDict(value="done", label="Done"),
                        ],
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }),
        )

    async def async_step_board_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove a pin (subentry) from the board."""
        subentries = self._board_subentries()
        if not subentries:
            return self.async_abort(reason="no_pins")

        if user_input is not None:
            sid = user_input.get("pin_select")
            self.hass.config_entries.async_remove_subentry(self.config_entry, sid)
            order = [
                x for x in self.config_entry.options.get(CONF_SUBENTRY_ORDER, [])
                if x != sid
            ]
            options = {**self.config_entry.options, CONF_SUBENTRY_ORDER: order}
            return self.async_create_entry(data=options)

        pin_options = [
            selector.SelectOptionDict(
                value=sid, label=PinData.from_dict(sub.data).title
            )
            for sid, sub in subentries
        ]
        return self.async_show_form(
            step_id="board_remove",
            data_schema=vol.Schema({
                vol.Required(
                    "pin_select", default=subentries[0][0]
                ): _select_selector(pin_options),
            }),
        )

    async def _step_watch(
        self, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Options for a departure watch."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options
        data = self.config_entry.data

        current_name = current.get(CONF_WATCH_NAME, data.get(CONF_WATCH_NAME, ""))
        current_interval = str(current.get(
            CONF_POLL_INTERVAL,
            data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_SECONDS),
        ))
        current_realtime = current.get(
            CONF_REALTIME_ENABLED,
            data.get(CONF_REALTIME_ENABLED, True),
        )
        current_count = int(current.get(
            CONF_DEPARTURE_COUNT,
            data.get(CONF_DEPARTURE_COUNT, DEFAULT_DEPARTURE_COUNT),
        ))

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_WATCH_NAME, default=current_name): selector.TextSelector(),
                vol.Required(CONF_POLL_INTERVAL, default=current_interval): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value="15", label="15 seconds"),
                            selector.SelectOptionDict(value="30", label="30 seconds"),
                            selector.SelectOptionDict(value="60", label="60 seconds"),
                        ],
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(CONF_REALTIME_ENABLED, default=current_realtime): selector.BooleanSelector(),
                vol.Required(CONF_DEPARTURE_COUNT, default=current_count): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=10,
                        step=1,
                        mode=selector.NumberSelectorMode.SLIDER,
                    )
                ),
            }),
        )

    async def _step_route_tracker(
        self, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Options for a route tracker: name and max vehicle count."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options
        data = self.config_entry.data

        current_name = current.get(CONF_WATCH_NAME, data.get(CONF_WATCH_NAME, ""))
        current_max = int(current.get(
            CONF_MAX_VEHICLES,
            data.get(CONF_MAX_VEHICLES, DEFAULT_MAX_VEHICLES),
        ))

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_WATCH_NAME, default=current_name): selector.TextSelector(),
                vol.Required(CONF_MAX_VEHICLES, default=current_max): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=20,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }),
        )

    async def _step_vehicle_tracker(
        self, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Options for a linked vehicle tracker: name only."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options
        data = self.config_entry.data
        current_name = current.get(CONF_WATCH_NAME, data.get(CONF_WATCH_NAME, ""))

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_WATCH_NAME, default=current_name): selector.TextSelector(),
            }),
        )
