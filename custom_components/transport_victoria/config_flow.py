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
    CONF_ALL_DESTINATIONS,
    CONF_API_KEY,
    CONF_BOARD_NAME,
    CONF_COLOUR_OVERRIDE,
    CONF_DESTINATION_STOP_ID,
    CONF_DESTINATION_STOP_NAME,
    CONF_DIRECTION,
    CONF_DISPLAY_NAME,
    CONF_ENABLE_REALTIME,
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
    CONF_SOURCE_ENTRY_ID,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    CONF_SUBENTRY_ORDER,
    CONF_TRACK_VEHICLES,
    CONF_TRANSFER_STOP_ID,
    CONF_TRANSFER_STOP_NAME,
    CONF_WATCH_NAME,
    DEFAULT_DEPARTURE_COUNT,
    DEFAULT_MAX_VEHICLES,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_SHOW_RUN_NUMBERS,
    DEFAULT_SHOW_VEHICLE_TYPE,
    DEFAULT_SOURCE_NAME,
    DOMAIN,
    ENTRY_TYPE_BOARD,
    ENTRY_TYPE_ROUTE_TRACKER,
    ENTRY_TYPE_SOURCE,
    ENTRY_TYPE_VEHICLE_TRACKER,
    ENTRY_TYPE_WATCH,
    MODE_BUNDLE_MAP,
    MODE_LABELS,
    MODE_METRO,
    MODES,
    READINESS_READY,
    ROUTE_TYPE_LABELS,
    ROUTE_TYPE_MAP,
    SOURCE_CHOICE_REALTIME,
    SOURCE_CHOICE_SCHEDULED,
    SOURCE_UNIQUE_ID,
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
    get_routes_for_stop,
    get_stop_lat_lon,
    is_mode_ready,
    schedule_state,
    search_stops_by_distance,
    search_stops_by_name,
)
from .realtime.client import PTVRealtimeClient
from .routing.destination import (
    line_label,
)
from . import _get_or_create_schedule_manager, async_trigger_gtfs_refresh
from .source import (
    all_mode_bundles,
    async_reload_dependents,
    async_remove_dependents,
    find_dependents,
    get_source_api_key,
    get_source_entry,
    get_source_entry_id,
    source_lock,
    source_readiness,
)

_LOGGER = logging.getLogger(__name__)

# Sentinel value used in the destination selector to mean "all destinations"
_ALL_DESTINATIONS_KEY = "__all__"

# Entry-type menu value that branches into the T11 "import watches as a board"
# flow (not a persisted entry type — the created entry is always a board).

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
            label = _stop_search_label(s)
        options.append(selector.SelectOptionDict(value=s.stop_id, label=label))
    return options


def _stop_search_label(s: StopResult) -> str:
    """Build a stop-search result label: name + route type + stop ID.

    Physical signs at tram/bus stops carry the stop ID, and a name alone is
    ambiguous (a "station", a tram stop and a bus stop may share a name), so
    the label surfaces both the mode (train/tram/bus/…) and the ID, e.g.
    ``"Coburg Station (Metro Train, 14308)"``.
    """
    type_label = ROUTE_TYPE_LABELS.get(s.route_type) if s.route_type is not None else None
    if type_label:
        return f"{s.stop_name} ({type_label}, {s.stop_id})"
    return f"{s.stop_name} ({s.stop_id})"


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


def _mode_for_route_type(route_type: int | None) -> str:
    """Return the config mode for a GTFS ``route_type``.

    The TripView-style O/D flow is cross-mode, so the mode is derived from the
    selected line's ``route_type`` rather than asked for (T18.4).  Ambiguous
    route types (e.g. 701 spanning bus + regional-bus) resolve to the first
    matching mode in ``MODES`` order; unknown types fall back to metro.
    """
    if route_type is None:
        return MODE_METRO
    for mode in MODES:
        if route_type in ROUTE_TYPE_MAP.get(mode, []):
            return mode
    return MODE_METRO


async def _validate_api_key(hass, api_key: str) -> dict[str, str]:
    """Return an errors dict if the key is invalid, empty dict if OK.

    Module-level (shared by the config flow and the source options flow) so a
    realtime credential change re-validates against the live API the same way a
    first-time key does.
    """
    session = async_get_clientsession(hass)
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


class PTVConfigFlow(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg, misc]
    """Handle the PTV config flow.

    Steps (entry-point routes on whether a source hub exists):
      1. user        — source hub creation (name + realtime/scheduled-only choice)
                      when no hub exists; otherwise "Add something to monitor".
      2. source_key  — API key (validated against the realtime API) when realtime.
      3. downloading/importing — GTFS schedule progress (import-all for the hub).
      4. create_source — create the source entry.
      5. feature flows (entry_type → mode → stop → dest → confirm / board /
         route-tracker / import) run AFTER the hub exists, each carrying
         ``source_entry_id``.
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
        # TripView-style O/D journey state: every (route, direction, terminal)
        # candidate serving the origin→destination pair, and the user's pick.
        self._line_choices: list[LineCandidate] = []
        self._selected_line: LineCandidate | None = None
        # Step to redirect to after GTFS download/import completes.  Only the
        # source hub ("create_source") and route tracker ("route_tracker_route")
        # flows trigger a download; the T18.4 watch/board flow is cross-mode and
        # runs against the already-imported hub.  Default kept valid ("origin")
        # so an accidental download redirect never resolves to a removed step.
        self._post_import_step: str = "origin"
        # T18.1 source-hub creation: True when this flow run is creating the
        # source entry (import-all bundles + create a ``source`` entry instead of
        # a feature entry).
        self._source_mode: bool = False

    # ------------------------------------------------------------------
    # Step 1 — source hub creation (name + realtime choice → optional key)
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Entry point: create the source hub, or add a feature to an existing hub.

        Single-hub rule: when a source entry already exists, this flow routes to
        the "Add something to monitor" feature menu (a second source entry is
        never created here).  With no source entry, the user is creating the hub:
        a display name + a realtime/scheduled-only choice (the API key is only
        collected when realtime is chosen).
        """
        # Existing hub → add a feature (never a second source).
        if user_input is None and get_source_entry(self.hass) is not None:
            return await self.async_step_entry_type()

        # Single-hub rule: a hub form submission while a hub already exists
        # (e.g. a race) aborts rather than creating a second source entry.
        if user_input is not None and get_source_entry(self.hass) is not None:
            return self.async_abort(reason="already_configured")

        # Programmatic source creation (migration): a SOURCE_USER flow with
        # ``entry_type: "source"`` in its data creates the hub directly.  The
        # caller (migration) already holds the source-creation lock, so we only
        # re-check the single-hub rule here (no re-acquisition — asyncio.Lock is
        # not reentrant).  The stable unique id makes a concurrent duplicate
        # creation fail the flow.
        if user_input is not None and user_input.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SOURCE:
            # Singleton via HA's unique-id machinery: setting the stable unique id
            # returns an existing entry (abort) or raises already_in_progress if a
            # concurrent flow is creating it.
            existing = await self.async_set_unique_id(SOURCE_UNIQUE_ID)
            if existing is not None:
                return self.async_abort(reason="already_configured")
            name = (user_input.get(CONF_DISPLAY_NAME) or "").strip() or DEFAULT_SOURCE_NAME
            api_key = user_input.get(CONF_API_KEY, "")
            return self.async_create_entry(
                title=name,
                data={
                    CONF_ENTRY_TYPE: ENTRY_TYPE_SOURCE,
                    CONF_DISPLAY_NAME: name,
                    CONF_API_KEY: api_key,
                },
            )

        errors: dict[str, str] = {}

        if user_input is not None:
            name = (user_input.get(CONF_DISPLAY_NAME) or "").strip() or DEFAULT_SOURCE_NAME
            self._config[CONF_DISPLAY_NAME] = name
            self._source_mode = True
            self._post_import_step = "create_source"
            choice = user_input.get(CONF_ENABLE_REALTIME, SOURCE_CHOICE_REALTIME)
            if choice == SOURCE_CHOICE_REALTIME:
                return await self.async_step_source_key()
            # Scheduled-only: no credential.
            self._config[CONF_API_KEY] = ""
            return await self.async_step_downloading()

        return self.async_show_form(
            description_placeholders={
                "portal_url": "https://opendata.transport.vic.gov.au",
            },
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_DISPLAY_NAME, default=DEFAULT_SOURCE_NAME
                ): selector.TextSelector(),
                vol.Required(
                    CONF_ENABLE_REALTIME, default=SOURCE_CHOICE_REALTIME
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value=SOURCE_CHOICE_REALTIME,
                                label="Enable realtime (recommended)",
                            ),
                            selector.SelectOptionDict(
                                value=SOURCE_CHOICE_SCHEDULED,
                                label="Scheduled times only (no key needed)",
                            ),
                        ],
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }),
            errors=errors,
        )

    async def async_step_source_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect and validate the API key for a realtime source hub.

        Validation failure is a retryable error (never silent): the form re-shows
        with the error until a live test succeeds.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            api_key = user_input.get(CONF_API_KEY, "").strip()
            if not api_key:
                errors[CONF_API_KEY] = "api_key_required"
            else:
                errors = await _validate_api_key(self.hass, api_key)
                if not errors:
                    self._config[CONF_API_KEY] = api_key
                    return await self.async_step_downloading()

        return self.async_show_form(
            step_id="source_key",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_API_KEY, default=""
                ): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }),
            errors=errors,
            description_placeholders={
                "portal_url": "https://opendata.transport.vic.gov.au",
            },
        )

    def _get_source_entry_id(self) -> str | None:
        """Return the single live source entry id, or None when no hub exists."""
        return get_source_entry_id(self.hass)

    def _get_existing_api_key(self) -> str | None:
        """Return the API key from the source entry (or any entry), if present."""
        key = get_source_api_key(self.hass)
        if key:
            return key
        config_entries = getattr(self.hass, "config_entries", None)
        if config_entries is None:
            return None
        for entry in config_entries.async_entries(DOMAIN):
            key = entry.data.get(CONF_API_KEY)
            if key:
                return str(key)
        return None

    async def _validate_api_key(self, api_key: str) -> dict[str, str]:
        """Return an errors dict if the key is invalid, empty dict if OK."""
        return await _validate_api_key(self.hass, api_key)

    # ------------------------------------------------------------------
    # Step 2 — Origin stop search (TripView-style O/D journey, T18.4)
    # ------------------------------------------------------------------

    async def async_step_origin(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Search for and select the origin (boarding) stop.

        Cross-mode ``search_stops_by_name`` round-trip: the user types a
        partial name, HA searches, and a dropdown of matches appears.  Selecting
        one stores ``origin_stop_id`` and advances to the destination search.
        """
        errors: dict[str, str] = {}
        db_path = _db_path(self.hass.config.config_dir)

        if user_input is not None:
            if CONF_STOP_ID in user_input and user_input[CONF_STOP_ID]:
                stop_id = user_input[CONF_STOP_ID]
                matching = [s for s in self._stop_search_results if s.stop_id == stop_id]
                stop_name = matching[0].stop_name if matching else stop_id
                self._config[CONF_STOP_ID] = stop_id
                self._config[CONF_STOP_NAME] = stop_name
                return await self.async_step_dest(None)

            query = user_input.get("query", "").strip()
            if len(query) < 2:
                if query:
                    errors["query"] = "search_too_short"
            else:
                self._stop_search_results = await self.hass.async_add_executor_job(
                    search_stops_by_name, db_path, query, ALL_ROUTE_TYPES, 20
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

    # ------------------------------------------------------------------
    # Step 3a — ZIP download progress screen
    # ------------------------------------------------------------------

    def _flow_bundles(self) -> list[str]:
        """Return the bundle folders this flow run must import.

        Source-hub creation imports the union of EVERY mode's bundles (import-all
        policy v1); feature flows import only their mode's bundles.
        """
        if self._source_mode:
            return all_mode_bundles()
        mode = self._config.get(CONF_MODE, "")
        return MODE_BUNDLE_MAP.get(mode, [])

    async def async_step_create_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the source (hub) config entry after the import-all refresh.

        Re-checks the single-hub rule under the source-creation lock and uses the
        stable unique id so a concurrent duplicate creation fails the flow (F2).
        """
        async with source_lock():
            existing = await self.async_set_unique_id(SOURCE_UNIQUE_ID)
            if existing is not None:
                return self.async_abort(reason="already_configured")
            name = self._config.get(CONF_DISPLAY_NAME, DEFAULT_SOURCE_NAME)
            api_key = self._config.get(CONF_API_KEY, "")
            return self.async_create_entry(
                title=name,
                data={
                    CONF_ENTRY_TYPE: ENTRY_TYPE_SOURCE,
                    CONF_DISPLAY_NAME: name,
                    CONF_API_KEY: api_key,
                },
            )

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
        bundles = self._flow_bundles()

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
        bundles = self._flow_bundles()

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
    # Step 3 — Destination search (TripView-style, T18.4)
    # ------------------------------------------------------------------

    async def async_step_dest(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose a destination: All destinations, a specific stop, or a journey.

        A departure watch defaults to "All destinations" (no line step); a board's
        first pin needs a specific line, so boards only offer a specific
        destination or a journey.  A specific destination that no direct service
        reaches falls back to All destinations (watch) rather than aborting.
        "Journey" is always an explicit choice, never an automatic fallback.
        """
        errors: dict[str, str] = {}
        db_path = _db_path(self.hass.config.config_dir)
        is_board = self._config.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_BOARD

        if user_input is not None:
            choice = user_input.get(
                "destination_choice", "specific" if is_board else "__all__"
            )

            if choice == "__all__" and not is_board:
                self._config[CONF_ALL_DESTINATIONS] = True
                self._config[CONF_DESTINATION_STOP_ID] = None
                self._config[CONF_DESTINATION_STOP_NAME] = None
                return await self.async_step_confirm(None)

            if choice == "journey" and not is_board:
                return await self.async_step_journey(None)

            if (
                CONF_DESTINATION_STOP_ID in user_input
                and user_input[CONF_DESTINATION_STOP_ID]
            ):
                dest_id = user_input[CONF_DESTINATION_STOP_ID]
                matching = [
                    s for s in self._dest_search_results if s.stop_id == dest_id
                ]
                dest_name = matching[0].stop_name if matching else dest_id
                self._config[CONF_ALL_DESTINATIONS] = False
                self._config[CONF_DESTINATION_STOP_ID] = dest_id
                self._config[CONF_DESTINATION_STOP_NAME] = dest_name
                return await self.async_step_lines(None)

            query = user_input.get("dest_query", "").strip()
            if len(query) < 2:
                if query:
                    errors["dest_query"] = "search_too_short"
            else:
                self._dest_search_results = await self.hass.async_add_executor_job(
                    search_stops_by_name, db_path, query, ALL_ROUTE_TYPES, 20
                )
                if not self._dest_search_results:
                    errors["dest_query"] = "no_stops_found"

        options: list[Any] = []
        if not is_board:
            options.append(
                selector.SelectOptionDict(value="__all__", label="All destinations")
            )
            options.append(
                selector.SelectOptionDict(value="journey", label="Journey (change at stop)")
            )
        options.append(
            selector.SelectOptionDict(value="specific", label="Specific destination")
        )
        schema_fields: dict[Any, Any] = {
            vol.Required(
                "destination_choice", default="specific" if is_board else "__all__"
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Optional("dest_query"): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.SEARCH)
            ),
        }
        if self._dest_search_results:
            schema_fields[vol.Optional(CONF_DESTINATION_STOP_ID)] = _select_selector(
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

    # ------------------------------------------------------------------
    # Step 3.5 — Lines found (TripView "Select Lines", T18.4)
    # ------------------------------------------------------------------

    async def async_step_lines(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show every route+direction serving the O/D pair; pick one.

        ``get_lines_between`` returns the full set of (route_id, direction_id,
        destination_label) candidates across both directions.  A single
        candidate auto-advances; otherwise one dropdown lists them all.  When NO
        direct service reaches the chosen destination (e.g. a City Loop stop
        that only some patterns serve), the flow falls back to All destinations
        rather than aborting.
        """
        db_path = _db_path(self.hass.config.config_dir)
        origin_stop_id = self._config[CONF_STOP_ID]
        destination_stop_id = self._config[CONF_DESTINATION_STOP_ID]

        if not self._line_choices:
            self._line_choices = await self.hass.async_add_executor_job(
                get_lines_between, db_path, origin_stop_id, destination_stop_id, None
            )
        if not self._line_choices:
            if self._config.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_BOARD:
                # A board pin is route-scoped; without a direct service there is
                # no fallback line, so abort (translated via config.abort).
                return self.async_abort(reason="destination_unreachable")
            # No direct service to this destination → fall back to All
            # destinations (a watch at the origin stop, unfiltered).
            self._config[CONF_ALL_DESTINATIONS] = True
            self._config[CONF_DESTINATION_STOP_ID] = None
            self._config[CONF_DESTINATION_STOP_NAME] = None
            return await self.async_step_confirm(None)

        if user_input is not None:
            chosen = user_input.get("line_select")
            matched = next(
                (l for l in self._line_choices if line_value(l) == chosen), None
            )
            if matched is None:
                matched = self._line_choices[0]
            self._selected_line = matched
            return await self.async_step_confirm(None)

        if len(self._line_choices) == 1:
            self._selected_line = self._line_choices[0]
            return await self.async_step_confirm(None)

        options = [
            selector.SelectOptionDict(value=line_value(l), label=line_label(l))
            for l in self._line_choices
        ]
        return self.async_show_form(
            step_id="lines",
            data_schema=vol.Schema({
                vol.Required(
                    "line_select", default=line_value(self._line_choices[0])
                ): _select_selector(options),
            }),
            description_placeholders={
                "stop_name": self._config.get(CONF_STOP_NAME, ""),
                "destination": self._config.get(CONF_DESTINATION_STOP_NAME, ""),
            },
        )

    # ------------------------------------------------------------------
    # Step 4b — Journey sub-flow
    # ------------------------------------------------------------------

    async def async_step_journey(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure a multi-leg journey: origin → transfer → destination."""
        errors: dict[str, str] = {}
        db_path = _db_path(self.hass.config.config_dir)
        route_types = ROUTE_TYPE_MAP.get(self._config.get(CONF_MODE, "")) or ALL_ROUTE_TYPES

        if user_input is not None:
            # Phase 1: transfer stop selected → advance to the final-destination
            # step.  No direct-service validation here: a journey's whole point
            # is to change services, so origin→transfer need NOT be direct.
            if CONF_TRANSFER_STOP_ID in user_input and user_input[CONF_TRANSFER_STOP_ID]:
                transfer_id = user_input[CONF_TRANSFER_STOP_ID]
                matching = [s for s in self._transfer_search_results if s.stop_id == transfer_id]
                transfer_name = matching[0].stop_name if matching else transfer_id
                self._config[CONF_TRANSFER_STOP_ID] = transfer_id
                self._config[CONF_TRANSFER_STOP_NAME] = transfer_name
                return await self.async_step_journey_dest()

            # Search for transfer stop
            # TODO(polish): auto-suggest interchange stops — prefer stops whose
            # routes also serve the origin (``get_routes_for_stop(origin)``
            # intersected with the search results), e.g. City Loop stations for
            # an Upfield-line origin.  Manual search works today; this is a hint.
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
        route_types = ROUTE_TYPE_MAP.get(self._config.get(CONF_MODE, "")) or ALL_ROUTE_TYPES

        if user_input is not None:
            # Final destination selected → store it and confirm.  No direct
            # service validation: a journey's legs need not be direct (and the
            # final leg is cross-mode in general).
            if CONF_JOURNEY_DESTINATION_STOP_ID in user_input and user_input[CONF_JOURNEY_DESTINATION_STOP_ID]:
                dest_id = user_input[CONF_JOURNEY_DESTINATION_STOP_ID]
                matching = [s for s in self._journey_dest_search_results if s.stop_id == dest_id]
                dest_name = matching[0].stop_name if matching else dest_id

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
    # Step 5 — Confirm (review screen, T18.4)
    # ------------------------------------------------------------------

    def _suggested_name(self) -> str:
        """Return the prefilled watch name for the review screen."""
        line = self._selected_line
        if line is not None:
            route_title = line.route_short_name or line.route_long_name or line.route_id
            origin = self._config.get(CONF_STOP_NAME, "")
            dest = self._config.get(CONF_DESTINATION_STOP_NAME, "")
            return f"Route {route_title} from {origin} to {dest}"
        stop_name = self._config.get(CONF_STOP_NAME, "")
        if self._config.get(CONF_JOURNEY_MODE):
            transfer = self._config.get(CONF_TRANSFER_STOP_NAME, "")
            final = self._config.get(CONF_JOURNEY_DESTINATION_STOP_NAME, "")
            return f"{stop_name} → {transfer} → {final}"
        dest = self._config.get(CONF_DESTINATION_STOP_NAME, "")
        if not dest:
            # "All destinations" — no specific destination to name.
            return stop_name
        return f"{stop_name} → {dest}"

    def _direction_summary(self) -> str:
        """Human direction summary derived from the selected line."""
        line = self._selected_line
        if line is None:
            return ""
        if line.direction_id in (0, 1):
            direction = "Outbound" if line.direction_id == 1 else "Inbound"
        else:
            direction = ""
        label = line.terminal_stop_name or line.route_short_name or line.route_id
        return f"{direction} → {label}".strip() if direction else label

    async def _derive_mode_for_stop(self, db_path: Path, stop_id: str) -> str:
        """Derive a config mode for a journey watch from the origin's routes."""
        routes = await self.hass.async_add_executor_job(
            get_routes_for_stop, db_path, stop_id, ALL_ROUTE_TYPES
        )
        if routes:
            return _mode_for_route_type(routes[0].route_type)
        return MODE_METRO

    async def _create_watch_entry(
        self, name: str, journey_mode: bool
    ) -> ConfigFlowResult:
        """Build and create a watch entry from the selected line / journey.

        The stored schema stays identical to the previous watch flow (no key
        renames): ``stop_id``/``destination_stop_id``/``direction``/``mode`` etc.
        """
        db_path = _db_path(self.hass.config.config_dir)
        if journey_mode:
            data: dict[str, Any] = {
                CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH,
                CONF_MODE: await self._derive_mode_for_stop(
                    db_path, self._config[CONF_STOP_ID]
                ),
                CONF_STOP_ID: self._config[CONF_STOP_ID],
                CONF_STOP_NAME: self._config.get(CONF_STOP_NAME, ""),
                CONF_WATCH_NAME: name,
                CONF_API_KEY: self._config.get(CONF_API_KEY, ""),
                CONF_SOURCE_ENTRY_ID: self._get_source_entry_id(),
                CONF_ALL_DESTINATIONS: False,
                CONF_JOURNEY_MODE: True,
                CONF_TRANSFER_STOP_ID: self._config.get(CONF_TRANSFER_STOP_ID),
                CONF_TRANSFER_STOP_NAME: self._config.get(CONF_TRANSFER_STOP_NAME, ""),
                CONF_JOURNEY_DESTINATION_STOP_ID: self._config.get(
                    CONF_JOURNEY_DESTINATION_STOP_ID
                ),
                CONF_JOURNEY_DESTINATION_STOP_NAME: self._config.get(
                    CONF_JOURNEY_DESTINATION_STOP_NAME, ""
                ),
                CONF_MIN_TRANSFER_MINUTES: self._config.get(
                    CONF_MIN_TRANSFER_MINUTES, 5
                ),
            }
            return self.async_create_entry(
                title=name, data=data, options={CONF_TRACK_VEHICLES: False}
            )

        line = self._selected_line
        if line is None:
            # "All destinations": no specific line/destination selected — derive
            # the mode from the origin stop's routes.
            data: dict[str, Any] = {
                CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH,
                CONF_MODE: await self._derive_mode_for_stop(
                    db_path, self._config[CONF_STOP_ID]
                ),
                CONF_STOP_ID: self._config[CONF_STOP_ID],
                CONF_STOP_NAME: self._config.get(CONF_STOP_NAME, ""),
                CONF_WATCH_NAME: name,
                CONF_API_KEY: self._config.get(CONF_API_KEY, ""),
                CONF_SOURCE_ENTRY_ID: self._get_source_entry_id(),
                CONF_ALL_DESTINATIONS: True,
                CONF_DESTINATION_STOP_ID: None,
                CONF_DESTINATION_STOP_NAME: None,
                CONF_DIRECTION: None,
                CONF_ROUTE_ID_FILTER: None,
                CONF_PLATFORM_FILTER: None,
            }
            return self.async_create_entry(
                title=name, data=data, options={CONF_TRACK_VEHICLES: False}
            )

        data: dict[str, Any] = {
            CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH,
            CONF_MODE: _mode_for_route_type(line.route_type),
            CONF_STOP_ID: self._config[CONF_STOP_ID],
            CONF_STOP_NAME: self._config.get(CONF_STOP_NAME, ""),
            CONF_WATCH_NAME: name,
            CONF_API_KEY: self._config.get(CONF_API_KEY, ""),
            CONF_SOURCE_ENTRY_ID: self._get_source_entry_id(),
            CONF_ALL_DESTINATIONS: False,
            CONF_DESTINATION_STOP_ID: self._config.get(CONF_DESTINATION_STOP_ID),
            CONF_DESTINATION_STOP_NAME: self._config.get(
                CONF_DESTINATION_STOP_NAME, ""
            ),
            CONF_DIRECTION: line.direction_id,
            CONF_ROUTE_ID_FILTER: line.route_id,
            CONF_PLATFORM_FILTER: None,
        }
        return self.async_create_entry(
            title=name, data=data, options={CONF_TRACK_VEHICLES: False}
        )

    async def _create_board_and_pin(self, board_name: str) -> ConfigFlowResult:
        """Create the board parent entry and its first pin in ONE round-trip.

        ``subentries=`` on ``async_create_entry`` attaches the pin subentry
        atomically, avoiding the flaky transient ``async_add_subentry`` bug.
        """
        line = self._selected_line
        pin = pin_from_line(
            line, self._config[CONF_STOP_ID], self._config.get(CONF_STOP_NAME, "")
        )
        return self.async_create_entry(
            title=board_name,
            data={
                CONF_ENTRY_TYPE: ENTRY_TYPE_BOARD,
                CONF_BOARD_NAME: board_name,
                CONF_API_KEY: self._config.get(CONF_API_KEY, ""),
                CONF_SOURCE_ENTRY_ID: self._get_source_entry_id(),
            },
            options={
                CONF_DEPARTURE_COUNT: BOARD_DEFAULT_DEPARTURE_COUNT,
                CONF_REALTIME_ENABLED: True,
                CONF_SUBENTRY_ORDER: [],
                CONF_SHOW_RUN_NUMBERS: DEFAULT_SHOW_RUN_NUMBERS,
                CONF_SHOW_VEHICLE_TYPE: DEFAULT_SHOW_VEHICLE_TYPE,
            },
            subentries=[
                {
                    "data": pin.to_dict(),
                    "subentry_type": SUBENTRY_TYPE_PIN,
                    "title": pin.title,
                    "unique_id": pin.pin_key,
                }
            ],
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Review the selection and create the watch / board entry.

        One screen: a prefilled name (watch) or ``board_name`` (board), plus the
        derived direction shown in the description.
        """
        entry_type = self._config.get(CONF_ENTRY_TYPE, ENTRY_TYPE_WATCH)
        is_board = entry_type == ENTRY_TYPE_BOARD
        journey_mode = bool(self._config.get(CONF_JOURNEY_MODE))
        suggested = self._suggested_name()

        if user_input is not None:
            if is_board:
                board_name = (
                    (user_input.get(CONF_BOARD_NAME, "") or "").strip()
                    or "Departure Board"
                )
                return await self._create_board_and_pin(board_name)
            name = (user_input.get(CONF_WATCH_NAME, "") or "").strip() or suggested
            return await self._create_watch_entry(name, journey_mode)

        schema_fields: dict[Any, Any] = {}
        if is_board:
            schema_fields[vol.Required(CONF_BOARD_NAME, default="Departure Board")] = (
                selector.TextSelector()
            )
        else:
            schema_fields[vol.Required(CONF_WATCH_NAME, default=suggested)] = (
                selector.TextSelector()
            )

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(schema_fields),
            description_placeholders={
                "stop_name": self._config.get(CONF_STOP_NAME, ""),
                "destination": self._config.get(CONF_DESTINATION_STOP_NAME, ""),
                "direction": self._direction_summary(),
            },
        )

    # ------------------------------------------------------------------
    # Entry type choice (departure watch / route tracker / board)
    # ------------------------------------------------------------------

    async def async_step_entry_type(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between a Departure Watch, Route Tracker, or Board.

        The board name is collected on this same screen (no separate naming
        step): picking "Trip-Group Board" and submitting creates the board with
        the name entered here.

        (The legacy linked-vehicle-tracker entry type is no longer offered — it
        was deprecated as a no-op by T16; existing entries still load.)
        """
        if user_input is not None:
            entry_type = user_input.get("entry_type", ENTRY_TYPE_WATCH)
            self._config[CONF_ENTRY_TYPE] = entry_type
            if entry_type == ENTRY_TYPE_ROUTE_TRACKER:
                return await self.async_step_route_tracker_mode()
            # Watch and board share the same TripView-style O/D journey,
            # starting at the origin stop search.
            return await self.async_step_origin()

        return self.async_show_form(
            step_id="entry_type",
            data_schema=self._entry_type_schema(),
        )

    def _entry_type_schema(self) -> vol.Schema:
        """Build the "what do you want to monitor?" form (type only)."""
        return vol.Schema({
            vol.Required("entry_type", default=ENTRY_TYPE_WATCH): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(
                            value=ENTRY_TYPE_WATCH,
                            label="Departure Watch (single stop)",
                        ),
                        selector.SelectOptionDict(
                            value=ENTRY_TYPE_BOARD,
                            label="Departure Board (multiple stops)",
                        ),
                        selector.SelectOptionDict(
                            value=ENTRY_TYPE_ROUTE_TRACKER,
                            label="Route Tracker (all vehicles on a line)",
                        ),
                    ],
                    mode=selector.SelectSelectorMode.LIST,
                )
            )
        })

    async def async_step_monitor_name(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Board name step — only shown for Departure Board / import types.

        TripView-style flows do not need this screen.
        """
        if user_input is not None:
            board_name = user_input.get(CONF_BOARD_NAME, "").strip()
            if not board_name:
                return self.async_show_form(
                    step_id="monitor_name",
                    errors={CONF_BOARD_NAME: "required"},
                    data_schema=self._monitor_name_schema(),
                )
            return await self._create_board_entry(board_name)

        return self.async_show_form(
            step_id="monitor_name",
            data_schema=self._monitor_name_schema(),
            description_placeholders={"monitor_type": "Departure Board"},
        )

    def _monitor_name_schema(self) -> vol.Schema:
        return vol.Schema({
            vol.Required(CONF_BOARD_NAME, default="Departure Board"): selector.TextSelector(),
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
                CONF_SOURCE_ENTRY_ID: self._get_source_entry_id(),
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
                CONF_SOURCE_ENTRY_ID: self._get_source_entry_id(),
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
        if config_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SOURCE:
            return PTVSourceOptionsFlow()
        return PTVOptionsFlow()
    @classmethod
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the subentry types this handler supports (pins for boards)."""
        if config_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_BOARD:
            return {SUBENTRY_TYPE_PIN: PTVBoardSubentryFlow}
        return {}



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
            options = dict(self.config_entry.options)
            options[CONF_DEPARTURE_COUNT] = int(
                user_input.get(CONF_DEPARTURE_COUNT, options.get(CONF_DEPARTURE_COUNT, BOARD_DEFAULT_DEPARTURE_COUNT))
            )
            options[CONF_REALTIME_ENABLED] = bool(
                user_input.get(CONF_REALTIME_ENABLED, options.get(CONF_REALTIME_ENABLED, True))
            )
            options[CONF_SHOW_RUN_NUMBERS] = bool(user_input.get(CONF_SHOW_RUN_NUMBERS, options.get(CONF_SHOW_RUN_NUMBERS, DEFAULT_SHOW_RUN_NUMBERS)))
            options[CONF_SHOW_VEHICLE_TYPE] = bool(user_input.get(CONF_SHOW_VEHICLE_TYPE, options.get(CONF_SHOW_VEHICLE_TYPE, DEFAULT_SHOW_VEHICLE_TYPE)))
            options[CONF_TRACK_VEHICLES] = bool(user_input.get(CONF_TRACK_VEHICLES, options.get(CONF_TRACK_VEHICLES, False)))
            # Persist the name change (data) + options.
            if board_name:
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={**self.config_entry.data, CONF_BOARD_NAME: board_name},
                )
            return self.async_create_entry(data=options)

        options = self.config_entry.options
        data = self.config_entry.data
        current_name = data.get(CONF_BOARD_NAME, self.config_entry.title)
        current_count = int(options.get(CONF_DEPARTURE_COUNT, BOARD_DEFAULT_DEPARTURE_COUNT))
        current_realtime = bool(options.get(CONF_REALTIME_ENABLED, True))
        current_runs = bool(options.get(CONF_SHOW_RUN_NUMBERS, DEFAULT_SHOW_RUN_NUMBERS))
        current_vehicle = bool(options.get(CONF_SHOW_VEHICLE_TYPE, DEFAULT_SHOW_VEHICLE_TYPE))
        current_track = bool(options.get(CONF_TRACK_VEHICLES, False))

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
                vol.Required(CONF_TRACK_VEHICLES, default=current_track): selector.BooleanSelector(),
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
        current_track = bool(current.get(CONF_TRACK_VEHICLES, False))

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
                vol.Required(CONF_TRACK_VEHICLES, default=current_track): selector.BooleanSelector(),
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



class PTVSourceOptionsFlow(OptionsFlow):  # type: ignore[misc]
    """Options flow for the source (hub) config entry.

    Owns the hub's credential and schedule lifecycle:
      * ``source_reconfigure`` — add/replace/remove the credential (choice
        "Scheduled-only" removes the key), rebuilding the realtime client +
        resetting feed cache and reloading dependents;
      * ``source_remove`` — deletion cascade: list dependents, confirm, remove
        them, then remove the hub;
      * ``source_refresh`` — force a schedule refresh + show bundle horizon
        summary.
    """

    def _hub_id(self) -> str:
        return self.config_entry.entry_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Hub menu: reconfigure / force refresh / remove."""
        return await self.async_step_source_menu(user_input)

    async def async_step_source_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Render the hub menu (distinct step so translations don't collide)."""
        return self.async_show_menu(
            step_id="source_menu",
            menu_options=[
                "source_reconfigure",
                "source_refresh",
                "source_remove",
            ],
        )

    # -- credential reconfigure ----------------------------------------

    async def async_step_source_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add / replace / remove the hub credential.

        "Scheduled-only" removes the key (feature entries fall back to
        scheduled-only); "realtime" validates and stores the new key.  A change
        invalidates the realtime cache (T13 state reset) and reloads dependents.
        """
        errors: dict[str, str] = {}
        current_key = self.config_entry.data.get(CONF_API_KEY, "")
        current_choice = SOURCE_CHOICE_REALTIME if current_key else SOURCE_CHOICE_SCHEDULED

        if user_input is not None:
            choice = user_input.get(CONF_ENABLE_REALTIME, current_choice)
            if choice == SOURCE_CHOICE_SCHEDULED:
                new_key = ""
            else:
                new_key = (user_input.get(CONF_API_KEY) or "").strip()
                if not new_key:
                    errors[CONF_API_KEY] = "api_key_required"
                else:
                    errors = await _validate_api_key(self.hass, new_key)

            if not errors:
                await self._apply_credential_change(new_key)
                return self.async_create_entry(data=dict(self.config_entry.options))

        schema_fields: dict[Any, Any] = {
            vol.Required(
                CONF_ENABLE_REALTIME, default=current_choice
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(
                            value=SOURCE_CHOICE_REALTIME, label="Enable realtime"
                        ),
                        selector.SelectOptionDict(
                            value=SOURCE_CHOICE_SCHEDULED,
                            label="Scheduled-only (remove key)",
                        ),
                    ],
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
        }
        if current_choice == SOURCE_CHOICE_REALTIME or (
            user_input is not None
            and user_input.get(CONF_ENABLE_REALTIME) == SOURCE_CHOICE_REALTIME
        ):
            schema_fields[vol.Optional(CONF_API_KEY, default=current_key)] = (
                selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                )
            )

        return self.async_show_form(
            step_id="source_reconfigure",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
        )

    async def _apply_credential_change(self, new_key: str) -> None:
        """Persist the new credential, reset feed state, reload dependents."""
        from .realtime.client import invalidate_cache

        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_API_KEY: new_key},
        )
        # T13 client-state reset: a credential change invalidates the module feed
        # cache + per-feed health state, so the next poll rebuilds from scratch.
        invalidate_cache()
        # Rebuild dependents' realtime clients against the new credential.
        await async_reload_dependents(self.hass, self._hub_id())

    # -- force refresh + bundle state ---------------------------------

    async def async_step_source_refresh(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Force a schedule refresh (via the manager) and surface the outcome.

        Routes through the manager's ``refresh_source`` so readiness + retry
        orchestration match automatic refreshes; the confirm checkbox gates the
        refresh, and the outcome (READY / failed) is surfaced — never a silent
        success.
        """
        errors: dict[str, str] = {}
        outcome = ""
        if user_input is not None:
            if user_input.get("confirm_refresh", False):
                ok = await self._refresh_source_via_manager()
                if ok:
                    outcome = "Schedule is ready."
                else:
                    errors["base"] = "schedule_refresh_failed"
            else:
                # Unchecked → done without refreshing.
                return self.async_create_entry(data=dict(self.config_entry.options))

        summary = await self._bundle_summary()
        # After a successful refresh the checkbox defaults OFF, so the next
        # submit finalises instead of re-running the refresh (F3).
        default_confirm = outcome == ""
        return self.async_show_form(
            step_id="source_refresh",
            data_schema=vol.Schema({
                vol.Required("confirm_refresh", default=default_confirm): selector.BooleanSelector(),
            }),
            errors=errors,
            description_placeholders={
                "bundle_state": summary,
                "refresh_outcome": outcome,
            },
        )

    async def _refresh_source_via_manager(self) -> bool:
        """Drive the hub refresh through the ScheduleManager (readiness + retry)."""
        manager = _get_or_create_schedule_manager(self.hass)
        manager.register_source(self._hub_id())
        return await manager.refresh_source()

    async def _bundle_summary(self) -> str:
        """Return a human-readable per-bundle horizon summary (best-effort)."""
        db_path = _db_path(self.hass.config.config_dir)
        try:
            bundles = await self.hass.async_add_executor_job(
                _bundles_summary, db_path, all_mode_bundles()
            )
        except Exception:  # noqa: BLE001
            return "- unavailable"
        return "\n".join(
            f"- bundle {folder}: {state}" for folder, state in bundles
        ) or "- none"

    # -- deletion cascade ----------------------------------------------

    async def async_step_source_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm removal of the hub, listing (and then removing) dependents."""
        dependents = find_dependents(self.hass, self._hub_id())

        if user_input is not None:
            if user_input.get("confirm", False) is not True:
                return self.async_create_entry(data=dict(self.config_entry.options))
            # Cascade: remove dependents first, then the hub itself.
            removed = await async_remove_dependents(self.hass, self._hub_id())
            _LOGGER.info(
                "Removing source hub '%s' — cascaded %d dependents",
                self.config_entry.title,
                len(removed),
            )
            await self.hass.config_entries.async_remove(self._hub_id())
            return self.async_abort(reason="reconfigure_successful")

        labels = "\n".join(f"- {e.title}" for e in dependents) or "- none"
        return self.async_show_form(
            step_id="source_remove",
            data_schema=vol.Schema({
                vol.Required("confirm", default=False): selector.BooleanSelector(),
            }),
            description_placeholders={"dependents": labels},
        )


def _bundles_summary(db_path: Path, bundles: list[str]) -> list[tuple[str, str]]:
    """Return ``(folder, state)`` for each bundle (ready/missing/stale/expired).

    Pure helper so the light-env tests can exercise the horizon classification
    without a real DB.
    """
    result: list[tuple[str, str]] = []
    for folder in bundles:
        try:
            state = schedule_state(db_path, [folder])
        except Exception:  # noqa: BLE001
            state = "missing"
        result.append((folder, state))
    return result
