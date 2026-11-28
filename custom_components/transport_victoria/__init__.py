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
    CONF_API_KEY,
    CONF_DEPARTURE_COUNT,
    CONF_DISPLAY_NAME,
    CONF_ENTRY_TYPE,
    CONF_MODE,
    CONF_SOURCE_ENTRY_ID,
    DEFAULT_DEPARTURE_COUNT,
    DEFAULT_SOURCE_NAME,
    DOMAIN,
    ENTRY_TYPE_BOARD,
    ENTRY_TYPE_WATCH,
    ENTRY_TYPE_ROUTE_TRACKER,
    ENTRY_TYPE_SOURCE,
    ENTRY_TYPE_VEHICLE_TRACKER,
    GTFS_REFRESH_CHECK_INTERVAL,
    GTFS_REFRESH_MARGIN_DAYS,
    GTFS_REFRESH_RETRY_SECONDS,
    GTFS_SCHEDULE_URL,
    MODE_BUNDLE_MAP,
    PICTOGRAM_URL_BASE,
    READINESS_CONFIGURED,
    READINESS_DOWNLOADING,
    READINESS_IMPORTING,
    READINESS_READY,
    READINESS_SCHEDULE_FAILED,
    SOURCE_UNIQUE_ID,
)
from .board import PinData, board_bundles, validate_pin
from .board_coordinator import BoardCoordinator
from .coordinator import PTVDataUpdateCoordinator
from .device_tracker import RouteVehicleCoordinator
from .source import (
    all_mode_bundles,
    async_reload_dependents,
    async_remove_dependents,
    find_dependents,
    get_source_api_key,
    get_source_entry,
    is_source_entry,
    source_lock,
)
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

# Re-entrancy guard for the ownership migration: the migration creates the
# source entry via a config flow, which (in some environments) triggers a nested
# ``async_setup`` that re-runs the migration.  A non-reentrant asyncio.Lock would
# deadlock there; this flag lets the nested call skip while the outer owns it.
_MIGRATION_RUNNING = False


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
    on_phase: Callable[[str], None] | None = None,
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
    return await _refresh_or_join(
        hass, db_path, zip_path, guard, bundle_folders, skip_download_if_exists, on_phase
    )


async def _refresh_or_join(
    hass: HomeAssistant,
    db_path: Path,
    zip_path: Path,
    guard: _RefreshGuard,
    bundle_folders: list[str],
    skip_download_if_exists: bool,
    on_phase: Callable[[str], None] | None = None,
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
            _refresh_critical(hass, db_path, zip_path, guard, list(requested), skip_download_if_exists, on_phase)
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
    on_phase: Callable[[str], None] | None = None,
) -> bool:
    """Hold the per-DB lock across the whole download→import→publish sequence."""
    async with guard.lock:
        return await _refresh_impl(
            hass, db_path, zip_path, bundle_folders, skip_download_if_exists, on_phase
        )


async def _refresh_impl(
    hass: HomeAssistant,
    db_path: Path,
    zip_path: Path,
    bundle_folders: list[str],
    skip_download_if_exists: bool,
    on_phase: Callable[[str], None] | None = None,
) -> bool:
    """Download (if needed) and import a fresh generation. Returns success."""
    def _phase(state: str) -> None:
        if on_phase is not None:
            on_phase(state)

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
    _phase(READINESS_DOWNLOADING)
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
    _phase(READINESS_IMPORTING)

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
        if entry_type == ENTRY_TYPE_SOURCE:
            # The hub imports the union of every mode (import-all policy v1).
            folders = all_mode_bundles()
        elif entry_type == ENTRY_TYPE_BOARD:
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


def _covers_source_requirement(bundle_folders: list[str]) -> bool:
    """Return True when ``bundle_folders`` covers the hub's import-all union."""
    required = frozenset(all_mode_bundles())
    return required <= frozenset(bundle_folders)


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
        # Source (hub) entry id — the owner of the schedule lifecycle.  When set,
        # the hub's import-all refresh + readiness + retry are driven by this
        # manager (not an untracked driver).
        self._source_entry_id: str | None = None
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
    # Source (hub) ownership
    # ------------------------------------------------------------------

    def register_source(self, entry_id: str) -> None:
        """Register the source hub as the owner of the schedule lifecycle."""
        self._source_entry_id = entry_id

    def unregister_source(self) -> None:
        """Detach the source hub (no further readiness writes or retries)."""
        self._source_entry_id = None

    async def refresh_source(self) -> bool:
        """Drive the hub's import-all refresh, emitting readiness phases + retry.

        Delegates the guarded refresh to :meth:`refresh` (which normalizes
        exceptions into failure outcomes and centralizes readiness/retry) with a
        phase callback so DOWNLOADING/IMPORTING are emitted at the real work
        boundaries.
        """
        if self._source_entry_id is None:
            return True
        bundles = all_mode_bundles()
        return await self.refresh(bundles, on_phase=self._on_source_phase)

    def _on_source_phase(self, phase: str) -> None:
        """Record a refresh phase (DOWNLOADING/IMPORTING) against the hub."""
        if self._source_entry_id is not None:
            self._write_readiness(self._source_entry_id, phase)

    def _write_readiness(self, entry_id: str, state: str) -> None:
        """Write the hub's readiness (guarded against an absent/unloaded hub)."""
        _set_readiness(self._hass, entry_id, state)

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
        self._source_entry_id = None
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

    async def refresh(
        self, bundle_folders: list[str], *, on_phase: Callable[[str], None] | None = None
    ) -> bool:
        """Refresh the schedule; record the readiness outcome (F2).

        Concurrency is handled by the module-scope guard inside
        async_trigger_gtfs_refresh, so concurrent callers share one import.
        A refresh that covers the hub's all-mode requirement updates readiness:
        success → READY, failure → SCHEDULE_FAILED.  (The general retry is
        scheduled on any failure, source or not.)  An existing usable incumbent
        generation keeps serving departures regardless (readiness is lifecycle,
        not current-data).

        Exception-safe: an unexpected exception from the guarded refresh body is
        normalized into a failure outcome (SCHEDULE_FAILED + retry) so the retry
        chain survives a repeated crash.  ``asyncio.CancelledError`` is re-raised
        (never converted) to preserve cancellation propagation.
        """
        if self._stopping:
            return False
        try:
            ok = await async_trigger_gtfs_refresh(
                self._hass, bundle_folders, on_phase=on_phase
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize to a failure outcome
            _LOGGER.error(
                "GTFS refresh raised unexpectedly for bundles %s: %s",
                bundle_folders,
                exc,
            )
            ok = False
        if not ok:
            self._schedule_retry(bundle_folders)
        self._record_refresh_outcome(bundle_folders, ok)
        return ok

    def _record_refresh_outcome(self, bundle_folders: list[str], ok: bool) -> None:
        """Update source readiness for a source-covering refresh outcome."""
        if self._source_entry_id is None:
            return
        if not _covers_source_requirement(bundle_folders):
            return
        self._write_readiness(
            self._source_entry_id, READINESS_READY if ok else READINESS_SCHEDULE_FAILED
        )

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
    # Ownership reconciliation (T18.1) runs as a fire-and-forget task — NOT
    # awaited here.  Awaiting it would make the hub-creation config flow call
    # ``async_setup_component`` re-entrantly, which awaits this very setup's
    # future and deadlocks until HA's bootstrap interrupt cancels it (the 10s+
    # "Setup is taking over 10 seconds" stall).  Deferred, the migration runs
    # after the component is marked loaded and the flow no longer re-enters.
    hass.async_create_task(_migration_task(hass))
    return True


async def _migration_task(hass: HomeAssistant) -> None:
    """Run the ownership reconciliation in the background (exception-safe)."""
    try:
        await async_migrate_entries_to_source(hass)
    except Exception as exc:  # noqa: BLE001 - never kill the background task
        _LOGGER.warning("Ownership migration failed: %s", exc)


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


def _set_readiness(hass: HomeAssistant, hub_id: str, state: str) -> None:
    """Write the hub's readiness state into ``hass.data[DOMAIN][hub_id]``.

    Guarded against a deleted/absent hub: a late phase callback or retry that
    fires after the hub was unloaded/removed must not recreate its runtime
    state.  If the entry no longer exists or its runtime dict was dropped, the
    write is a no-op.
    """
    config_entries = getattr(hass, "config_entries", None)
    if config_entries is not None:
        get_entry = getattr(config_entries, "async_get_entry", None)
        if get_entry is not None and get_entry(hub_id) is None:
            return
    stored = hass.data.get(DOMAIN, {}).get(hub_id)
    if stored is None:
        return
    stored["readiness"] = state


async def _async_setup_source(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the source (hub) config entry.

    The hub owns the credential (already in ``entry.data``) and the schedule
    lifecycle: it registers with the ScheduleManager as the SOLE schedule
    consumer and source owner, and the manager drives the import-all refresh
    (with readiness phases + retry).  Its readiness state is exposed at
    ``hass.data[DOMAIN][entry.entry_id]["readiness"]``.
    """
    await hass.async_add_executor_job(
        lambda: _data_dir(hass).mkdir(parents=True, exist_ok=True)
    )

    manager = _get_or_create_schedule_manager(hass)
    manager.register_consumer(entry.entry_id)
    manager.register_source(entry.entry_id)

    # Runtime state dict must exist BEFORE the first readiness write (the write
    # is guarded against an absent dict).
    hass.data[DOMAIN][entry.entry_id] = {
        "hub": entry,
        "readiness": READINESS_CONFIGURED,
    }

    # Import-all refresh driven by the manager (tracked so unload can cancel it).
    task = hass.async_create_task(manager.refresh_source())
    hass.data[DOMAIN][entry.entry_id]["refresh_task"] = task

    # Re-enable cascade (C11): dependents that were left loaded while the hub
    # was disabled are reloaded so their coordinators rebuild against the
    # re-enabled hub (no-op on first setup, when no dependents exist yet).
    await async_reload_dependents(hass, entry.entry_id)
    return True


async def async_migrate_entries_to_source(hass: HomeAssistant) -> ConfigEntry | None:
    """Reconcile legacy feature entries into the ownership contract (T18.1).

    Idempotent — safe to run on EVERY ``async_setup`` (and after association
    changes), not just a one-shot migration:

      1. Ensure the source hub exists (create it from any feature's carried-over
         credential when none exists; singleton race-safe via
         ``async_set_unique_id``, NOT under the source lock).
      2. For EVERY feature entry whose ``source_entry_id`` is missing or dangling
         (points at a removed hub), set it to the live hub id AND strip the
         feature's local ``api_key`` copy (the hub alone owns credentials), then
         reload that feature once — under ``source_lock``.

    Returns the source entry, or None when there are no feature entries to
    reconcile.
    """
    global _MIGRATION_RUNNING
    if _MIGRATION_RUNNING:
        # Re-entrant call — e.g. our own entry creation triggered a nested
        # ``async_setup``.  The outer call owns the reconcile; skip (and avoid a
        # non-reentrant lock deadlock).
        return get_source_entry(hass)
    _MIGRATION_RUNNING = True
    try:
        # 1. Ensure the hub exists — NOT under ``source_lock``.  ``flow.async_init``
        #    (hub creation) re-enters HA's component setup, which synchronously
        #    sets up every feature entry; those setups call
        #    ``_ensure_feature_associated`` which takes ``source_lock`` — so
        #    holding the lock across creation would deadlock.  The singleton is
        #    already race-safe via ``async_set_unique_id`` in the flow.
        hub = get_source_entry(hass)
        if hub is None:
            features = [
                e for e in hass.config_entries.async_entries(DOMAIN)
                if not is_source_entry(e)
            ]
            if not features:
                return None
            carried_key = next(
                (str(e.data.get(CONF_API_KEY)) for e in features if e.data.get(CONF_API_KEY)),
                "",
            ) or None
            hub = await _create_source_entry(hass, carried_key or "")
            if hub is None:
                return None

        # 2. Reconcile every feature under the lock (stamp + conditional strip).
        async with source_lock():
            hub = get_source_entry(hass) or hub
            hub_id = hub.entry_id
            for entry in hass.config_entries.async_entries(DOMAIN):
                if is_source_entry(entry):
                    continue
                new_data = _reconcile_feature_data(hass, entry, hub_id)
                if new_data is None:
                    continue
                hass.config_entries.async_update_entry(entry, data=new_data)
                _LOGGER.info(
                    "Reconciled feature entry '%s' with source hub %s", entry.title, hub_id
                )
                if getattr(entry, "runtime_data", None) is not None:
                    await hass.config_entries.async_reload(entry.entry_id)

        return hub
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - background task must not die
        _LOGGER.warning("Ownership reconciliation failed: %s", exc)
        return None
    finally:
        _MIGRATION_RUNNING = False


async def _create_source_entry(hass: HomeAssistant, api_key: str) -> ConfigEntry | None:
    """Create the source hub entry programmatically (migration path).

    Uses a SOURCE_USER flow with an ``entry_type: "source"`` data marker so the
    config flow's ``async_step_user`` creates the hub directly.  The stable
    ``unique_id`` (SOURCE_UNIQUE_ID) makes a concurrent duplicate creation fail
    the flow instead of producing a second hub.
    """
    from homeassistant.config_entries import SOURCE_USER

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_SOURCE,
            CONF_DISPLAY_NAME: DEFAULT_SOURCE_NAME,
            CONF_API_KEY: api_key,
        },
    )
    hub = result.get("result")
    if hub is None:
        _LOGGER.error("Source creation failed: flow did not create an entry")
    return hub


def _reconcile_feature_data(
    hass: HomeAssistant, entry: ConfigEntry, hub_id: str
) -> dict | None:
    """Return the reconciled data dict for a feature entry, or None if unchanged.

    Shared by BOTH association paths (the background-migration reconcile loop and
    the defensive setup stamp) so they cannot diverge:

      * ``source_entry_id`` is always stamped (association).
      * the feature's local ``api_key`` copy is stripped ONLY when the hub
        carries a credential — a scheduled-only hub must never destroy a
        feature's only credential (that would degrade a realtime install to
        scheduled-only with no recovery source).
    """
    data = dict(entry.data)
    sid = data.get(CONF_SOURCE_ENTRY_ID)
    dangling = sid is not None and hass.config_entries.async_get_entry(sid) is None
    needs_association = sid != hub_id or dangling
    has_local_key = bool(data.get(CONF_API_KEY))
    hub_has_credential = get_source_api_key(hass) is not None

    if needs_association:
        data[CONF_SOURCE_ENTRY_ID] = hub_id
    if has_local_key and hub_has_credential:
        data.pop(CONF_API_KEY, None)

    return None if data == dict(entry.data) else data


async def _ensure_feature_associated(
    hass: HomeAssistant, entry: ConfigEntry
) -> ConfigEntry | None:
    """Associate a feature entry with the singleton hub (defensive setup stamp).

    The background migration normally reconciles associations, but stamping here
    too makes association *guaranteed* regardless of load state, restart count,
    or whether the migration has run yet.  A single combined data update stamps
    ``source_entry_id`` and (only when the hub carries a credential) strips the
    feature's local ``api_key``.  Returns the hub (or None when no hub exists
    yet — nothing is stripped then, so a feature never loses its key before a
    hub carries a credential).  Serialized with the migration via ``source_lock``.
    """
    if get_source_entry(hass) is None:
        return None
    async with source_lock():
        hub = get_source_entry(hass)
        if hub is None:
            return None
        new_data = _reconcile_feature_data(hass, entry, hub.entry_id)
        if new_data is not None:
            hass.config_entries.async_update_entry(entry, data=new_data)
            _LOGGER.info(
                "Associated feature entry '%s' with source hub %s (setup stamp)",
                entry.title,
                hub.entry_id,
            )
    return hub


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PTV from a config entry."""
    _LOGGER.debug("Setting up PTV entry: %s", entry.entry_id)

    hass.data.setdefault(DOMAIN, {})

    # Expose the shared GTFS DB path so the device_tracker fleet platform can
    # reach the trips table without a circular import.
    hass.data[DOMAIN].setdefault("db_path", _db_path(hass))

    entry_type = entry.data.get(CONF_ENTRY_TYPE, "watch")

    if entry_type == ENTRY_TYPE_SOURCE:
        return await _async_setup_source(hass, entry)

    # Defensive association: guarantee the feature is linked to the singleton
    # hub (and its copied credential stripped) before setup, regardless of
    # whether the background migration has run yet or was interrupted.
    await _ensure_feature_associated(hass, entry)

    if entry_type == ENTRY_TYPE_VEHICLE_TRACKER:
        return await _async_setup_legacy_vehicle_tracker(hass, entry)

    if entry_type == ENTRY_TYPE_ROUTE_TRACKER:
        return await _async_setup_route_tracker(hass, entry)

    if entry_type == ENTRY_TYPE_BOARD:
        return await _async_setup_board(hass, entry)

    return await _async_setup_watch(hass, entry)


async def _async_setup_watch(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a departure watch config entry.

    The source hub owns the schedule lifecycle (import + refresh), so a watch is
    a *reader* only: it does NOT register as a schedule consumer or trigger its
    own refresh.  If its mode's bundles are not yet imported, the coordinator's
    first refresh raises ``ConfigEntryNotReady`` and HA retries setup later.
    """
    # Ensure the data directory exists (blocking I/O → executor).
    await hass.async_add_executor_job(
        lambda: _data_dir(hass).mkdir(parents=True, exist_ok=True)
    )

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
    return True


async def _async_setup_board(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a trip-group board config entry.

    The source hub owns the schedule lifecycle; the board is a reader only (no
    consumer registration, no refresh trigger).  Pins are revalidated against the
    current generation (marking — not deleting — impossible combinations), and a
    :class:`BoardCoordinator` exposes the merged board as entities.
    """
    # Ensure the data directory exists (blocking I/O → executor).
    await hass.async_add_executor_job(
        lambda: _data_dir(hass).mkdir(parents=True, exist_ok=True)
    )

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
    is_source = entry_type == ENTRY_TYPE_SOURCE

    if is_source:
        # The source entry owns no platforms — cancel its tracked refresh task,
        # release its schedule consumer + source ownership, and drop its state.
        state = hass.data[DOMAIN].pop(entry.entry_id, {})
        task = state.get("refresh_task")
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        manager = hass.data[DOMAIN].get(_SCHEDULE_MANAGER_KEY)
        if manager is not None:
            manager.unregister_source()
            await manager.consumer_unloaded(entry.entry_id)
        unload_ok = True
    elif is_board:
        unload_ok = await hass.config_entries.async_unload_platforms(
            entry, _BOARD_PLATFORMS
        )
    else:
        platforms = _TRACKER_PLATFORMS if is_tracker else _WATCH_PLATFORMS
        unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms)

    if unload_ok and not is_source:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id, {})
        for key in ("cancel_refresh", "cancel_options"):
            cancel = entry_data.get(key)
            if cancel:
                cancel()

    if not hass.config_entries.async_entries(DOMAIN):
        hass.data.pop(DOMAIN, None)

    return bool(unload_ok)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of a PTV config entry (deletion cascade, C11).

    When the *source* (hub) entry is removed, any dependent feature entries that
    are still present are removed with it — a hub deletion cascades.  (The
    source options flow also lists + removes dependents before removing the hub,
    so this is a safety net for a removal that bypasses that confirm step.)
    """
    if not is_source_entry(entry):
        return
    hub_id = entry.entry_id
    dependents = find_dependents(hass, hub_id)
    if dependents:
        _LOGGER.info(
            "Source hub removed — cascading removal of %d dependent entries",
            len(dependents),
        )
        await async_remove_dependents(hass, hub_id)
