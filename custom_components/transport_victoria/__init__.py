"""Transport Victoria Home Assistant integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import date
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    BOARD_DEFAULT_DEPARTURE_COUNT,
    CONF_DEPARTURE_COUNT,
    CONF_ENTRY_TYPE,
    CONF_MODE,
    DEFAULT_DEPARTURE_COUNT,
    DOMAIN,
    ENTRY_TYPE_BOARD,
    ENTRY_TYPE_ROUTE_TRACKER,
    ENTRY_TYPE_VEHICLE_TRACKER,
    GTFS_REFRESH_CHECK_INTERVAL,
    GTFS_REFRESH_MARGIN_DAYS,
    GTFS_REFRESH_RETRY_SECONDS,
    GTFS_SCHEDULE_URL,
    MODE_BUNDLE_MAP,
    PICTOGRAM_URL_BASE,
)
from .board import PinData, board_bundles, validate_pin
from .board_coordinator import BoardCoordinator
from .coordinator import PTVDataUpdateCoordinator
from .device_tracker import RouteVehicleCoordinator
from .gtfs.downloader import (
    check_for_remote_changes,
    download_zip,
    resolve_gtfs_schedule_url,
)
from .gtfs.horizon import refresh_due
from .gtfs.importer import (
    get_imported_bundles,
    get_stored_zip_meta,
    import_bundles_to_generation,
)
from .gtfs.repository import schedule_state
from .gtfs.time_utils import service_day_for_time

_LOGGER = logging.getLogger(__name__)

# Config-flow-only integration (YAML has no keys); required because we implement async_setup.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Platforms registered for departure watch entries.  ``device_tracker`` is
# included for the opt-in T16 fleet tracking (the platform no-ops unless
# ``CONF_TRACK_VEHICLES`` is set).
_WATCH_PLATFORMS: list[str] = ["sensor", "binary_sensor", "device_tracker"]

# Platforms registered for trip-group board entries (T10 adds the direction
# select; sensor + binary_sensor carry the ranked departures + disruption;
# device_tracker carries the opt-in T16 fleet).
_BOARD_PLATFORMS: list[str] = ["sensor", "binary_sensor", "select", "device_tracker"]

# Platforms registered for vehicle tracker entries
_TRACKER_PLATFORMS: list[str] = ["device_tracker"]

# Legacy alias (used by async_unload_platforms when entry_type not available)
PLATFORMS: list[str] = ["sensor", "binary_sensor"]

# Subdirectory under hass.config.path() for all PTV data files
_DATA_DIR_NAME = "transport_victoria_data"

# Key under hass.data[DOMAIN] for the shared schedule manager
_SCHEDULE_MANAGER_KEY = "schedule_manager"


# ---------------------------------------------------------------------------
# Process-global refresh guard (keyed by canonical DB path)
#
# The lock and in-flight task live at module scope (NOT per ScheduleManager
# instance) so that a config-flow refresh, a manager refresh, and a manager
# recreated after an unload all coordinate against the SAME guard. A cancelled
# waiter is prevented from dropping the lock mid-import by running the critical
# section in a shielded task (see async_trigger_gtfs_refresh).
# ---------------------------------------------------------------------------

class _RefreshGuard:
    """One per DB path: a serialization lock + the current in-flight task.

    ``bundles`` records the bundle set the in-flight task is (or was) building,
    so a joiner can detect when the in-flight work does not cover its own
    requirements and trigger a follow-up refresh instead of a false success.
    """

    __slots__ = ("lock", "task", "bundles")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.task: asyncio.Task[bool] | None = None
        self.bundles: frozenset[str] | None = None


_GUARDS: dict[str, _RefreshGuard] = {}


def _guard_for_db(db_path: Path) -> _RefreshGuard:
    """Return (and create if needed) the process-global guard for a DB path."""
    key = str(db_path)
    guard = _GUARDS.get(key)
    if guard is None:
        guard = _GUARDS[key] = _RefreshGuard()
    return guard


def _data_dir(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(_DATA_DIR_NAME))


def _zip_path(hass: HomeAssistant) -> Path:
    return _data_dir(hass) / "gtfs.zip"


def _db_path(hass: HomeAssistant) -> Path:
    return _data_dir(hass) / "gtfs.db"


def _service_today(hass: HomeAssistant) -> date:
    """Return the GTFS service-local date (HA timezone), accounting for the
    00:00–03:00 service-day cutover so horizon/expiry checks match the
    coordinator's departure-query service day."""
    return service_day_for_time(dt_util.now())[0]


def _union_bundles(hass: HomeAssistant, requested: list[str] | None) -> list[str]:
    """Return the union of the requested bundles and every live entry's bundles.

    A rebuild must include every bundle required by live entries — never just
    the triggering entry's subset — so refreshing one mode cannot discard
    another mode's schedule.
    """
    seen: set[str] = set(requested or [])
    result: list[str] = list(requested or [])
    for folder in _bundles_for_all_entries(hass):
        if folder not in seen:
            seen.add(folder)
            result.append(folder)
    return result


def _guard_task_done(guard: _RefreshGuard, task: asyncio.Task[bool]) -> None:
    if guard.task is task:
        guard.task = None
        guard.bundles = None


async def async_trigger_gtfs_refresh(
    hass: HomeAssistant,
    bundle_folders: list[str] | None = None,
    *,
    skip_download_if_exists: bool = False,
) -> bool:
    """Refresh the GTFS schedule: download (if needed) + import a new generation.

    Public so config_flow can trigger a refresh during first-time setup, before
    any config entry exists.

    Rebuild necessity is derived from DB state (schema version, per-bundle
    horizon, coverage, import age) INDEPENDENT of the download-unchanged check:
    a schema-stale DB, an aged 45-day window, or an expired/missing bundle is
    rebuilt even when the upstream ZIP is byte-for-byte unchanged.  A refresh
    that cannot make the schedule usable reports False (a failure state), so an
    expired-but-unchanged upstream feed stays unhealthy rather than pretending
    success.

    The whole download→import→publish sequence runs under a process-global,
    per-DB guard and is shielded from waiter cancellation, so concurrent
    callers (config-flow + manager) share one import and exactly one wins
    publication. A caller whose bundle requirements are NOT covered by the
    in-flight refresh waits for it to finish, then triggers a follow-up refresh
    of the larger union.

    Args:
        bundle_folders: Folder numbers to import (e.g. ["2"] for metro trains).
                        If None, imports the union of bundles for existing entries.
        skip_download_if_exists: Pass True from config-flow steps that know the
                        ZIP was already downloaded; avoids a redundant download.

    Returns:
        True if the schedule is now usable (or was already up-to-date);
        False if the download/import failed or the schedule is still stale.
    """
    db_path = _db_path(hass)
    zip_path = _zip_path(hass)

    bundle_folders = _union_bundles(hass, bundle_folders)
    if not bundle_folders:
        _LOGGER.debug("No bundles to import")
        return True

    guard = _guard_for_db(db_path)
    return await _refresh_or_join(hass, db_path, zip_path, guard, bundle_folders, skip_download_if_exists)


async def _refresh_or_join(
    hass: HomeAssistant,
    db_path: Path,
    zip_path: Path,
    guard: _RefreshGuard,
    bundle_folders: list[str],
    skip_download_if_exists: bool,
) -> bool:
    """Start a refresh, or join an in-flight one if it covers our requirements.

    If an in-flight refresh does NOT cover this caller's bundle set, await it to
    completion, then loop to start a follow-up refresh of the enlarged union.
    """
    requested = frozenset(bundle_folders)
    while True:
        if guard.task is not None and not guard.task.done():
            in_flight = guard.bundles or frozenset()
            if requested <= in_flight:
                _LOGGER.debug(
                    "GTFS refresh already in flight covering %s — awaiting",
                    sorted(in_flight),
                )
                return await asyncio.shield(guard.task)
            # In-flight work doesn't cover our needs: wait for it, then re-loop
            # with the enlarged union (which now includes the newcomer's bundles).
            _LOGGER.debug(
                "GTFS refresh in flight for %s, needs %s — awaiting then re-refreshing",
                sorted(in_flight),
                sorted(requested),
            )
            await asyncio.shield(guard.task)
            requested = requested | in_flight
            bundle_folders = list(requested)
            continue

        # No in-flight task: start one for the current (possibly enlarged) set.
        guard.bundles = requested
        task = asyncio.create_task(
            _refresh_critical(hass, db_path, zip_path, guard, list(requested), skip_download_if_exists)
        )
        guard.task = task
        task.add_done_callback(lambda t: _guard_task_done(guard, t))
        # Shield so a cancelled waiter doesn't abort the import or drop the lock.
        return await asyncio.shield(task)


async def _refresh_critical(
    hass: HomeAssistant,
    db_path: Path,
    zip_path: Path,
    guard: _RefreshGuard,
    bundle_folders: list[str],
    skip_download_if_exists: bool,
) -> bool:
    """Hold the per-DB lock across the whole download→import→publish sequence."""
    async with guard.lock:
        return await _refresh_impl(
            hass, db_path, zip_path, bundle_folders, skip_download_if_exists
        )


async def _refresh_impl(
    hass: HomeAssistant,
    db_path: Path,
    zip_path: Path,
    bundle_folders: list[str],
    skip_download_if_exists: bool,
) -> bool:
    """Download (if needed) and import a fresh generation. Returns success."""
    today = _service_today(hass)

    # Rebuild necessity from DB state alone (independent of download checks).
    must_rebuild = await hass.async_add_executor_job(
        refresh_due, db_path, bundle_folders, today, GTFS_REFRESH_MARGIN_DAYS
    )

    stored_meta = await hass.async_add_executor_job(get_stored_zip_meta, db_path)
    stored_sha = stored_meta.get("zip_sha256")
    stored_etag = stored_meta.get("zip_etag")
    stored_size = stored_meta.get("zip_size")

    # Default to the last known-good URL; refresh it from CKAN below unless the
    # caller already has the ZIP and no HEAD check is needed.
    schedule_url = GTFS_SCHEDULE_URL
    remote_changed = True
    if not skip_download_if_exists:
        # The portal re-publishes the dataset with a new resource UUID, so a
        # hardcoded URL eventually 404s. Resolution never raises — it falls
        # back to GTFS_SCHEDULE_URL.
        schedule_url = await resolve_gtfs_schedule_url(hass)
        remote_changed = await check_for_remote_changes(
            hass, schedule_url, stored_etag, stored_size
        )

    # Healthy DB + unchanged upstream → genuinely up to date.
    if not must_rebuild and not remote_changed:
        _LOGGER.debug("GTFS schedule healthy and unchanged — no refresh needed")
        return True

    # Reuse the cached ZIP when we only need a local rebuild of unchanged data
    # (e.g. an aged 45-day window); otherwise fetch fresh bytes.
    reuse_cached = skip_download_if_exists or (must_rebuild and not remote_changed)
    # Try the CKAN-resolved URL first; if that fetch fails (stale metadata can
    # point at a deleted resource), retry once against the pinned fallback.
    download_urls = [schedule_url]
    if schedule_url != GTFS_SCHEDULE_URL:
        download_urls.append(GTFS_SCHEDULE_URL)
    for attempt, download_url in enumerate(download_urls, start=1):
        try:
            _, sha256, etag, content_length = await download_zip(
                hass,
                download_url,
                zip_path,
                skip_if_exists=reuse_cached,
            )
            break
        except Exception as exc:
            if attempt < len(download_urls):
                _LOGGER.warning(
                    "Download from the resolved GTFS URL failed (%s) — retrying "
                    "the pinned fallback URL",
                    exc,
                )
                continue
            _LOGGER.error("Failed to download GTFS schedule: %s", exc)
            return False

    # Healthy DB + identical content → up to date (HEAD may have been
    # inconclusive, e.g. missing ETag).
    if not must_rebuild and sha256 == stored_sha:
        _LOGGER.debug("GTFS schedule healthy and SHA-256 unchanged — no import needed")
        return True

    _LOGGER.info("Importing GTFS bundles %s (SHA-256: %s)", bundle_folders, sha256)

    def _run_import() -> None:
        import_bundles_to_generation(
            db_path,
            zip_path,
            sha256,
            bundle_folders,
            zip_etag=etag or stored_etag,
            zip_size=content_length or stored_size,
            today=today,
        )

    try:
        await hass.async_add_executor_job(_run_import)
    except Exception as exc:
        _LOGGER.error("Failed to import GTFS bundles %s: %s", bundle_folders, exc)
        return False

    # Honest result: a successful import must leave the schedule usable.
    state = await hass.async_add_executor_job(
        schedule_state, db_path, bundle_folders, today
    )
    if state != "ready":
        _LOGGER.warning(
            "GTFS refresh completed but schedule is still %s — upstream feed may be stale",
            state,
        )
        return False

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """React to options changes.

    Departure count changes require a full reload to add/remove sensor entities.
    All other changes (poll interval, realtime toggle, name) are applied live.
    """
    entry_type = entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_WATCH)
    stored = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})

    if entry_type == ENTRY_TYPE_BOARD:
        prev_count = stored.get("departure_count", BOARD_DEFAULT_DEPARTURE_COUNT)
        new_count = int(
            entry.options.get(CONF_DEPARTURE_COUNT, BOARD_DEFAULT_DEPARTURE_COUNT)
        )
        if new_count != prev_count:
            await hass.config_entries.async_reload(entry.entry_id)
            return
        # realtime toggle is read live from entry.options on each board poll.
        return

    prev_count = stored.get("departure_count", DEFAULT_DEPARTURE_COUNT)
    new_count = int(entry.options.get(CONF_DEPARTURE_COUNT, DEFAULT_DEPARTURE_COUNT))

    if new_count != prev_count:
        await hass.config_entries.async_reload(entry.entry_id)
        return

    coordinator: PTVDataUpdateCoordinator = entry.runtime_data
    coordinator.async_update_listeners_for_options()


def _bundles_for_all_entries(hass: HomeAssistant) -> list[str]:
    """Return the union of bundle folders needed by all live config entries.

    Board entries contribute the union of their pins' route_type bundles (a
    board can span modes, so it has no single ``CONF_MODE``).
    """
    seen: set[str] = set()
    result: list[str] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        entry_type = entry.data.get(CONF_ENTRY_TYPE)
        # Vehicle/route tracker entries don't have a mode/bundle to import
        if entry_type in (ENTRY_TYPE_VEHICLE_TRACKER, ENTRY_TYPE_ROUTE_TRACKER):
            continue
        if entry_type == ENTRY_TYPE_BOARD:
            folders = _board_bundles_for_entry(entry)
        else:
            mode = entry.data.get(CONF_MODE, "")
            folders = MODE_BUNDLE_MAP.get(mode, [])
        for folder in folders:
            if folder not in seen:
                seen.add(folder)
                result.append(folder)
    return result


def _board_bundles_for_entry(entry: ConfigEntry) -> list[str]:
    """Return the union bundle folders for a board entry's pins."""
    pins = (PinData.from_dict(sub.data) for sub in entry.subentries.values())
    return sorted(board_bundles(pins))


class ScheduleManager:
    """Owns the GTFS schedule lifecycle: horizon check + self-healing refresh.

    One instance is shared across all watch config entries (created lazily on
    the first watch setup). The concurrency guard (lock + in-flight task) lives
    at module scope in :data:`_GUARDS` keyed by DB path — NOT per instance —
    so a config-flow refresh, a manager refresh, and a manager recreated after
    an unload all coordinate against the same guard.

    Responsibilities:
      * a scheduled tick (async_track_time_interval) checks the horizon and
        triggers a refresh BEFORE the calendar expires;
      * a failed refresh schedules a retry (constant backoff), which is
        cancelled on shutdown;
      * tracks LOADED consumers (watch entries that called setup) so it can
        stop rescheduling and cancel retries when the last one unloads.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        db_path: Path,
        zip_path: Path,
        *,
        retry_seconds: int = GTFS_REFRESH_RETRY_SECONDS,
    ) -> None:
        self._hass = hass
        self._db_path = db_path
        self._zip_path = zip_path
        self._retry_seconds = retry_seconds
        # {frozenset(bundle_folders): asyncio.Task} — scheduled retries
        self._retry_tasks: dict[frozenset[str], asyncio.Task[None]] = {}
        self._cancel_check: Callable[[], None] | None = None
        # Loaded consumer entry_ids (idempotent — a setup retry re-registers the
        # same entry_id without double-counting).
        self._consumers: set[str] = set()
        self._stopping = False

    @property
    def stopped(self) -> bool:
        return self._stopping

    # ------------------------------------------------------------------
    # Consumer tracking (loaded watch entries)
    # ------------------------------------------------------------------

    def register_consumer(self, entry_id: str) -> None:
        """Record a loaded watch entry; start the tick on the first one.

        Idempotent by entry_id, so a setup retry (which re-enters
        ``_async_setup_watch`` after a failed first refresh) does not inflate the
        consumer count.
        """
        if entry_id in self._consumers:
            return
        self._consumers.add(entry_id)
        if len(self._consumers) == 1:
            self.start()

    async def consumer_unloaded(self, entry_id: str) -> None:
        """Record a watch entry unload/rollback; shut down on the last one.

        Also used to roll back a registration when setup fails before the entry
        is fully established.
        """
        self._consumers.discard(entry_id)
        if not self._consumers:
            await self.shutdown()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Begin the periodic horizon check (idempotent)."""
        if self._cancel_check is not None or self._stopping:
            return
        self._cancel_check = async_track_time_interval(
            self._hass,
            lambda _now: self._hass.async_create_task(self.tick()),
            GTFS_REFRESH_CHECK_INTERVAL,
        )

    def stop_tick(self) -> None:
        if self._cancel_check is not None:
            self._cancel_check()
            self._cancel_check = None

    async def shutdown(self) -> None:
        """Stop rescheduling and cancel pending retries.

        After shutdown, no NEW refresh may start (the tick is cancelled and
        ``refresh``/``_schedule_retry`` become no-ops). An import already running
        in an executor thread is allowed to finish and publish — it is shielded
        and publishes atomically, and the module-scope guard is retained so a
        fresh manager reuses it without clashing.
        """
        if self._stopping:
            return
        self._stopping = True
        self.stop_tick()
        for task in list(self._retry_tasks.values()):
            task.cancel()
        for task in list(self._retry_tasks.values()):
            with suppress(asyncio.CancelledError):
                await task
        self._retry_tasks.clear()

    # ------------------------------------------------------------------
    # Refresh orchestration
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        """Periodic horizon check; triggers a refresh when the schedule is due."""
        if self._stopping:
            return
        bundles = _bundles_for_all_entries(self._hass)
        if not bundles:
            return
        try:
            await self.ensure_fresh(bundles)
        except Exception as exc:  # keep the loop alive regardless
            _LOGGER.warning("GTFS schedule refresh tick failed: %s", exc)

    async def ensure_fresh(
        self,
        bundle_folders: list[str],
        today: date | None = None,
    ) -> bool:
        """Check the horizon and refresh if the schedule is due.

        Returns True if a refresh was triggered (or was already in flight),
        False if the schedule was already fresh and nothing happened.
        """
        due = await self._hass.async_add_executor_job(
            refresh_due,
            self._db_path,
            bundle_folders,
            today or _service_today(self._hass),
            GTFS_REFRESH_MARGIN_DAYS,
        )
        if not due:
            _LOGGER.debug("GTFS schedule horizon is healthy — no refresh needed")
            return False
        await self.refresh(bundle_folders)
        return True

    async def refresh(self, bundle_folders: list[str]) -> bool:
        """Refresh the schedule; schedule a retry on failure.

        Concurrency is handled by the module-scope guard inside
        async_trigger_gtfs_refresh, so concurrent callers share one import.
        """
        if self._stopping:
            return False
        ok = await async_trigger_gtfs_refresh(self._hass, bundle_folders)
        if not ok:
            self._schedule_retry(bundle_folders)
        return ok

    def _schedule_retry(self, bundle_folders: list[str]) -> None:
        """Schedule a single retry after the backoff interval (deduplicated)."""
        if self._stopping:
            return
        key = frozenset(bundle_folders)
        existing = self._retry_tasks.get(key)
        if existing is not None and not existing.done():
            return
        self._retry_tasks[key] = self._hass.async_create_task(
            self._retry_after(key, bundle_folders)
        )

    async def _retry_after(
        self, key: frozenset[str], bundle_folders: list[str]
    ) -> None:
        """Wait the backoff interval, then re-attempt the refresh."""
        await asyncio.sleep(self._retry_seconds)
        self._retry_tasks.pop(key, None)
        await self.refresh(bundle_folders)


def _get_or_create_schedule_manager(hass: HomeAssistant) -> ScheduleManager:
    """Return the shared schedule manager for this hass (fresh if stopped)."""
    manager = hass.data[DOMAIN].get(_SCHEDULE_MANAGER_KEY)
    if manager is None or manager.stopped:
        manager = ScheduleManager(hass, _db_path(hass), _zip_path(hass))
        hass.data[DOMAIN][_SCHEDULE_MANAGER_KEY] = manager
    return manager


async def async_setup(hass: HomeAssistant, config: dict) -> bool:  # type: ignore[type-arg]
    """Register static file paths once at integration load time.

    Called by HA before any config entries are set up. Registers the
    transport_pictograms directory so entity_picture URLs work.
    """
    component_dir = Path(__file__).parent
    await hass.http.async_register_static_paths([
        StaticPathConfig(
            url_path=PICTOGRAM_URL_BASE,
            path=str(component_dir / "transport_pictograms"),
            cache_headers=True,
        ),
    ])
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entries to the current version.

    Version history:
      1 → 2: (reserved for future use — no data changes yet)
    """
    _LOGGER.debug(
        "Migrating PTV entry %s from version %s", entry.entry_id, entry.version
    )
    # No data migrations needed yet; bump the version and return True.
    if entry.version == 1:
        hass.config_entries.async_update_entry(entry, version=2)
        _LOGGER.info("Migrated PTV entry %s to version 2", entry.entry_id)
        return True

    _LOGGER.error(
        "Cannot migrate PTV entry %s from unsupported version %s",
        entry.entry_id,
        entry.version,
    )
    return False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PTV from a config entry."""
    _LOGGER.debug("Setting up PTV entry: %s", entry.entry_id)

    hass.data.setdefault(DOMAIN, {})

    # Expose the shared GTFS DB path so the device_tracker fleet platform can
    # reach the trips table without a circular import.
    hass.data[DOMAIN].setdefault("db_path", _db_path(hass))

    entry_type = entry.data.get(CONF_ENTRY_TYPE, "watch")

    if entry_type == ENTRY_TYPE_VEHICLE_TRACKER:
        return await _async_setup_legacy_vehicle_tracker(hass, entry)

    if entry_type == ENTRY_TYPE_ROUTE_TRACKER:
        return await _async_setup_route_tracker(hass, entry)

    if entry_type == ENTRY_TYPE_BOARD:
        return await _async_setup_board(hass, entry)

    return await _async_setup_watch(hass, entry)


async def _async_setup_watch(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a departure watch config entry."""
    # Ensure the data directory exists (blocking I/O → executor).
    await hass.async_add_executor_job(
        lambda: _data_dir(hass).mkdir(parents=True, exist_ok=True)
    )

    # Start the shared schedule manager and register this entry as a consumer.
    manager = _get_or_create_schedule_manager(hass)
    manager.register_consumer(entry.entry_id)

    try:
        mode = entry.data.get(CONF_MODE, "")
        bundles = MODE_BUNDLE_MAP.get(mode, [])
        if bundles:
            hass.async_create_task(manager.ensure_fresh(bundles))

        # Determine this entry's index for stagger calculation
        all_entries = hass.config_entries.async_entries(DOMAIN)
        entry_index = next(
            (i for i, e in enumerate(all_entries) if e.entry_id == entry.entry_id),
            0,
        )

        coordinator = PTVDataUpdateCoordinator(
            hass,
            entry,
            entry_index=entry_index,
            db_path=_db_path(hass),
        )

        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = coordinator

        cancel_options = entry.add_update_listener(_async_options_updated)

        hass.data[DOMAIN][entry.entry_id] = {
            "cancel_options": cancel_options,
            "departure_count": int(
                entry.options.get(CONF_DEPARTURE_COUNT, DEFAULT_DEPARTURE_COUNT)
            ),
        }

        await hass.config_entries.async_forward_entry_setups(entry, _WATCH_PLATFORMS)
    except BaseException:
        # Roll back the consumer registration on ANY failure or cancellation
        # (first refresh, options listener, platform forwarding). CancelledError
        # is a BaseException on Py3.8+, so this also covers mid-setup
        # cancellation; we roll back then re-raise. A HA setup retry re-registers
        # cleanly (the consumer set is idempotent by entry_id).
        await manager.consumer_unloaded(entry.entry_id)
        raise
    return True


async def _async_setup_board(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a trip-group board config entry.

    Registers the board as a schedule consumer with the union of its pins'
    bundle folders, triggers a refresh of that union, revalidates each pin
    against the current generation (marking — not deleting — pins whose
    combination became impossible), and creates a :class:`BoardCoordinator`
    (T10) that exposes the merged board as entities.
    """
    # Ensure the data directory exists (blocking I/O → executor).
    await hass.async_add_executor_job(
        lambda: _data_dir(hass).mkdir(parents=True, exist_ok=True)
    )

    manager = _get_or_create_schedule_manager(hass)
    manager.register_consumer(entry.entry_id)

    try:
        pins = [PinData.from_dict(sub.data) for sub in entry.subentries.values()]
        bundles = sorted(board_bundles(pins))
        if bundles:
            hass.async_create_task(manager.ensure_fresh(bundles))

        # Schedule-invalidated pins: validate against the current generation,
        # log, and record invalid subentry ids (never delete silently).
        invalid: set[str] = set()
        db_path = _db_path(hass)
        for sub in entry.subentries.values():
            pin = PinData.from_dict(sub.data)
            result = await hass.async_add_executor_job(validate_pin, db_path, pin)
            if not result.ok:
                invalid.add(pin.pin_key)
                _LOGGER.warning(
                    "Board '%s' pin '%s' no longer resolves (%s) — marked invalid",
                    entry.title,
                    pin.title,
                    result.reason,
                )

        coordinator = BoardCoordinator(
            hass, entry, db_path, invalid_pins=invalid
        )
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = coordinator

        cancel_options = entry.add_update_listener(_async_options_updated)

        hass.data[DOMAIN][entry.entry_id] = {
            "cancel_options": cancel_options,
            "invalid_pins": invalid,
            "departure_count": int(
                entry.options.get(CONF_DEPARTURE_COUNT, BOARD_DEFAULT_DEPARTURE_COUNT)
            ),
        }

        await hass.config_entries.async_forward_entry_setups(entry, _BOARD_PLATFORMS)
    except BaseException:
        await manager.consumer_unloaded(entry.entry_id)
        raise
    return True


async def _async_setup_route_tracker(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a route-based vehicle tracker config entry."""
    all_entries = hass.config_entries.async_entries(DOMAIN)
    entry_index = next(
        (i for i, e in enumerate(all_entries) if e.entry_id == entry.entry_id),
        0,
    )

    coordinator = RouteVehicleCoordinator(hass, entry, entry_index=entry_index)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    hass.data[DOMAIN][entry.entry_id] = {}

    await hass.config_entries.async_forward_entry_setups(entry, _TRACKER_PLATFORMS)
    return True


async def _async_setup_legacy_vehicle_tracker(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a deprecated legacy single-trip vehicle tracker entry.

    The v0.1.0 single-trip tracker has been replaced by dynamic fleet tracking
    (opt-in via ``CONF_TRACK_VEHICLES`` on the parent watch/board).  This entry
    type is retained only so the entry loads cleanly; its entities are orphaned
    (they vanish on reload).  The device_tracker platform logs the deprecation
    and creates no entities for this entry type.
    """
    _LOGGER.warning(
        "Legacy vehicle tracker entry '%s' is deprecated: its single-trip "
        "tracker has been replaced by dynamic fleet tracking. Remove this "
        "entry and enable 'track_vehicles' on the watch/board instead.",
        entry.title,
    )
    hass.data[DOMAIN][entry.entry_id] = {}
    await hass.config_entries.async_forward_entry_setups(entry, _TRACKER_PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a PTV config entry."""
    _LOGGER.debug("Unloading PTV entry: %s", entry.entry_id)

    entry_type = entry.data.get(CONF_ENTRY_TYPE, "watch")
    is_tracker = entry_type in (ENTRY_TYPE_VEHICLE_TRACKER, ENTRY_TYPE_ROUTE_TRACKER)
    is_board = entry_type == ENTRY_TYPE_BOARD

    if is_board:
        unload_ok = await hass.config_entries.async_unload_platforms(
            entry, _BOARD_PLATFORMS
        )
    else:
        platforms = _TRACKER_PLATFORMS if is_tracker else _WATCH_PLATFORMS
        unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms)

    if unload_ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id, {})
        for key in ("cancel_refresh", "cancel_options"):
            cancel = entry_data.get(key)
            if cancel:
                cancel()

        # Watch and board entries are loaded schedule consumers — release them
        # (and shut the manager down when the last one leaves).
        if not is_tracker:
            manager = hass.data[DOMAIN].get(_SCHEDULE_MANAGER_KEY)
            if manager is not None:
                await manager.consumer_unloaded(entry.entry_id)

    if not hass.config_entries.async_entries(DOMAIN):
        hass.data.pop(DOMAIN, None)

    return bool(unload_ok)
