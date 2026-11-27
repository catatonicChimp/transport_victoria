"""Constants for the Transport Victoria integration."""

from datetime import timedelta

DOMAIN = "transport_victoria"

# -------------------------------------------------------------------
# PTV Open Data API
# -------------------------------------------------------------------

# Fallback direct-download URL for the GTFS Schedule ZIP.
#
# The portal re-publishes this dataset periodically and each publication gets a
# NEW resource UUID — the old URL then 404s. Never treat this constant as
# permanent: ``gtfs.downloader.resolve_gtfs_schedule_url`` asks the CKAN API for
# the current ZIP resource at refresh time and only falls back to this value if
# that lookup fails. Keep it pointed at the newest known-good resource so a
# resolution failure still has a chance of working.
GTFS_SCHEDULE_URL = (
    "https://opendata.transport.vic.gov.au/dataset/"
    "3f4e292e-7f8a-4ffe-831f-1953be0fe448/resource/"
    "8af7737c-5c9e-4022-aa98-911aa04370a7/download/gtfs.zip"
)

# CKAN action API used to discover the current resource URL dynamically.
# ``package_show`` accepts either the dataset slug or its UUID; the slug is
# stable across re-publications.
GTFS_SCHEDULE_CKAN_URL = (
    "https://opendata.transport.vic.gov.au/api/3/action/package_show"
)
GTFS_SCHEDULE_DATASET_ID = "gtfs-schedule"

REALTIME_BASE_URL = (
    "https://api.opendata.transport.vic.gov.au"
    "/opendata/public-transport/gtfs/realtime/v1"
)

# Auth header name for the PTV Open Data API key
REALTIME_AUTH_HEADER = "KeyID"

# Rate limits.
#
# The portal publishes "20–27 calls per minute (depending on data size)" PER
# realtime endpoint — not one global budget across all endpoints.  We enforce
# two layers (see realtime/client.py + rate_limiter.py):
#
#   * Per-(mode, feed_type): 20 calls / 60 s — the conservative floor of the
#     published 20–27 range.
#   * Global safety ceiling: 40 calls / 60 s across ALL endpoints.  The published
#     limit is per endpoint, but bursts across the 10 endpoints must not hammer
#     the API, so total traffic is kept well inside the per-endpoint aggregate.
RATE_LIMIT_PER_FEED_CALLS = 20
RATE_LIMIT_PER_FEED_PERIOD = 60  # seconds

RATE_LIMIT_GLOBAL_CALLS = 40
RATE_LIMIT_GLOBAL_PERIOD = 60  # seconds

# A feed honours a 429 ``Retry-After`` as a per-feed cooldown, clamped to this
# many seconds (a server that asks for minutes still only pauses us ≤ 60 s).
RATE_LIMIT_RETRY_AFTER_MAX_SECONDS = 60

# -------------------------------------------------------------------
# Realtime feed modes
# -------------------------------------------------------------------

MODE_METRO = "metro"
MODE_TRAM = "tram"
MODE_BUS = "bus"
MODE_REGIONAL_BUS = "regional-bus"
MODE_VLINE = "vline"

MODES = [MODE_METRO, MODE_TRAM, MODE_BUS, MODE_REGIONAL_BUS, MODE_VLINE]

# Human-readable labels for each mode (used in config flow)
MODE_LABELS: dict[str, str] = {
    MODE_METRO: "Metro Trains",
    MODE_TRAM: "Trams",
    MODE_BUS: "Buses",
    MODE_REGIONAL_BUS: "Regional Buses",
    MODE_VLINE: "V/Line",
}

# -------------------------------------------------------------------
# Realtime URL-mode resolution (regional buses ride the combined "bus" feed)
# -------------------------------------------------------------------
#
# The GTFS *schedule* bundle splits regional buses (folders 4/11) from metro
# buses (folder 6), but the *realtime* feeds do NOT: the portal serves a single
# combined "bus" RT feed, and ``regional-bus`` / ``rural-bus`` / ``coach`` URL
# segments return 404.  A pin configured as ``MODE_REGIONAL_BUS`` therefore
# fetches the "bus" RT feed.  This maps a config mode to the RT URL segment ONLY
# — ``MODE_REGIONAL_BUS`` itself is NOT renamed (config entries, labels, bundle
# mapping, and schedule semantics are unchanged).  The realtime client resolves
# the URL mode, and its cache / rate buckets / known-missing / scope-unavailable
# keys all use the RESOLVED mode so regional-bus and bus SHARE one cached fetch.
RT_URL_FEED_MODE: dict[str, str] = {
    MODE_REGIONAL_BUS: MODE_BUS,
}

# -------------------------------------------------------------------
# Realtime feed types
# -------------------------------------------------------------------

FEED_TRIP_UPDATES = "trip-updates"
FEED_SERVICE_ALERTS = "service-alerts"
FEED_VEHICLE_POSITIONS = "vehicle-positions"

# (mode, feed_type) pairs the portal does NOT publish (verified via CKAN, which
# lists each mode's GTFS-RT resources): trams have no Vehicle Positions, and
# V/Line has no Service Alerts.  The realtime client treats a 404 on one of these
# as "unavailable" (suppressed retry) instead of a transient failure, so it never
# hammers endpoints that will never exist.
REALTIME_KNOWN_MISSING_FEEDS: frozenset[tuple[str, str]] = frozenset({
    (MODE_TRAM, FEED_VEHICLE_POSITIONS),
    (MODE_VLINE, FEED_SERVICE_ALERTS),
})

# (mode, feed_type) pairs the portal serves but which a subscription key may not
# be *scoped* to — these return 403 "Forbidden" (not 404).  ``bus/service-alerts``
# returns 403 with the maintainer's key: the "Public Transport GTFS Realtime (Bus
# Service Alerts)" portal product is not enabled on the key (enable it under My
# Account → Products on opendata.transport.vic.gov.au).  The realtime client
# treats a 403 on one of these as "unavailable" (suppressed retry + a one-time
# diagnosis hint) and SELF-HEALS if the product is later enabled — it still makes
# the request each poll and a 200 simply overrides the classification.
REALTIME_SCOPE_UNAVAILABLE_FEEDS: frozenset[tuple[str, str]] = frozenset({
    (MODE_BUS, FEED_SERVICE_ALERTS),
})

# -------------------------------------------------------------------
# GTFS route_type codes used by PTV
#
# PTV uses a mix of standard and extended (HVT) route_type values.
# Discovered by inspecting each mode bundle inside the GTFS ZIP:
#   folder 1 → V/Line Rail          route_type 2
#   folder 2 → Metro Trains         route_type 400  (HVT: metro/underground)
#   folder 3 → Trams                route_type 0
#   folder 4 → Metro Buses          route_type 3, 701
#   folder 5 → V/Line Coaches       route_type 204  (HVT: international coach)
#   folder 6 → Regional Buses       route_type 701  (HVT: rural bus)
#   folder 10 → Ferry               route_type 102
#   folder 11 → Airport/Special     route_type 3
# -------------------------------------------------------------------

ROUTE_TYPE_TRAM = 0
ROUTE_TYPE_VLINE_RAIL = 2
ROUTE_TYPE_BUS = 3
ROUTE_TYPE_FERRY = 102
ROUTE_TYPE_VLINE_COACH = 204
ROUTE_TYPE_METRO_TRAIN = 400   # PTV HVT code — NOT the standard GTFS value of 1
ROUTE_TYPE_REGIONAL_BUS = 701

# Maps each mode to the folder number(s) inside the PTV outer ZIP.
# Used by the importer to only extract the bundles needed for a given mode.
#   folder 1  → V/Line Rail       (route_type 2)
#   folder 2  → Metro Trains      (route_type 400)
#   folder 3  → Trams             (route_type 0)
#   folder 4  → Metro Buses       (route_type 3, 701)
#   folder 5  → V/Line Coaches    (route_type 204)
#   folder 6  → Regional Buses    (route_type 701)
#   folder 10 → Ferry             (route_type 102)
#   folder 11 → Airport/Special   (route_type 3)
MODE_BUNDLE_MAP: dict[str, list[str]] = {
    MODE_METRO: ["2"],
    MODE_TRAM: ["3"],
    MODE_BUS: ["4", "11"],
    MODE_REGIONAL_BUS: ["6"],
    MODE_VLINE: ["1", "5"],
}

ROUTE_TYPE_MAP: dict[str, list[int]] = {
    MODE_METRO: [ROUTE_TYPE_METRO_TRAIN],                        # 400
    MODE_TRAM: [ROUTE_TYPE_TRAM],                                # 0
    MODE_BUS: [ROUTE_TYPE_BUS, ROUTE_TYPE_REGIONAL_BUS],         # 3, 701
    MODE_REGIONAL_BUS: [ROUTE_TYPE_REGIONAL_BUS, ROUTE_TYPE_VLINE_COACH],  # 701, 204
    MODE_VLINE: [ROUTE_TYPE_VLINE_RAIL, ROUTE_TYPE_VLINE_COACH], # 2, 204
}

# -------------------------------------------------------------------
# City Loop stop IDs
# Used to detect when origin/destination routing needs disambiguation.
# Stop IDs are strings in GTFS.
# -------------------------------------------------------------------

CITY_LOOP_STOP_IDS: frozenset[str] = frozenset({
    "19843",  # Flinders Street
    "19842",  # Southern Cross
    "19844",  # Melbourne Central
    "19845",  # Flagstaff
    "19846",  # Parliament
})

# -------------------------------------------------------------------
# Config entry data keys
# -------------------------------------------------------------------

CONF_API_KEY = "api_key"
CONF_MODE = "mode"
CONF_STOP_ID = "stop_id"
CONF_STOP_NAME = "stop_name"
CONF_DESTINATION_STOP_ID = "destination_stop_id"
CONF_DESTINATION_STOP_NAME = "destination_stop_name"
CONF_ALL_DESTINATIONS = "all_destinations"
CONF_DIRECTION = "direction"  # direction_id derived from trip evidence (T7)
CONF_WATCH_NAME = "watch_name"

# Options (user-adjustable after setup via the gear icon)
CONF_POLL_INTERVAL = "poll_interval"           # seconds: 15, 30, or 60
CONF_REALTIME_ENABLED = "realtime_enabled"     # bool: False disables RT fetches
CONF_DEPARTURE_COUNT = "departure_count"       # int: 1, 2, or 3 next services
DEFAULT_DEPARTURE_COUNT = 3

# Filtering (optional, set during config flow)
CONF_ROUTE_ID_FILTER = "route_id_filter"   # restrict to a specific route/line
CONF_PLATFORM_FILTER = "platform_filter"  # restrict to a specific platform code

# Multi-leg journey mode
CONF_JOURNEY_MODE = "journey_mode"
CONF_TRANSFER_STOP_ID = "transfer_stop_id"
CONF_TRANSFER_STOP_NAME = "transfer_stop_name"
CONF_JOURNEY_DESTINATION_STOP_ID = "journey_destination_stop_id"
CONF_JOURNEY_DESTINATION_STOP_NAME = "journey_destination_stop_name"
CONF_MIN_TRANSFER_MINUTES = "min_transfer_minutes"

# Entry type flag (stored in config data to distinguish entry types)
CONF_ENTRY_TYPE = "entry_type"
ENTRY_TYPE_WATCH = "watch"
ENTRY_TYPE_VEHICLE_TRACKER = "vehicle_tracker"
ENTRY_TYPE_ROUTE_TRACKER = "route_tracker"
ENTRY_TYPE_BOARD = "board"
CONF_PARENT_ENTRY_ID = "parent_entry_id"  # for vehicle tracker entries

# Route tracker config keys
CONF_ROUTE_ID = "route_id"                     # primary route being tracked
CONF_ROUTE_SHORT_NAME = "route_short_name"
CONF_ROUTE_LONG_NAME = "route_long_name"
CONF_ROUTE_COLOR = "route_color"               # hex without '#'
CONF_MAX_VEHICLES = "max_vehicles"             # number of device_tracker slots
CONF_REFERENCE_STOP_ID = "reference_stop_id"  # optional proximity filter
CONF_REFERENCE_STOP_NAME = "reference_stop_name"
CONF_REFERENCE_STOP_LAT = "reference_stop_lat"
CONF_REFERENCE_STOP_LON = "reference_stop_lon"
CONF_PROXIMITY_RADIUS_M = "proximity_radius_m"

DEFAULT_MAX_VEHICLES = 5

# Fleet tracking (T16): boolean option on watch/board entries that enables the
# dynamic, route-scoped vehicle-fleet device_trackers.  Default off so existing
# entries gain no surprise entities; the legacy ``ENTRY_TYPE_VEHICLE_TRACKER``
# entry type (a separate single-trip tracker) is deprecated and orphaned.
CONF_TRACK_VEHICLES = "track_vehicles"

# -------------------------------------------------------------------
# Trip-group board (T8)
#
# A "board" config entry groups several departure "pins" (one per line
# combination) into a single TripView-style departure board.  The parent entry
# is the board; each pin is a HA *subentry* (see config_flow.PTVBoardSubentryFlow).
# -------------------------------------------------------------------

# Subentry type registered via ConfigFlow.async_get_supported_subentry_types.
SUBENTRY_TYPE_PIN = "pin"

# Board (parent entry) data keys.
CONF_BOARD_NAME = "board_name"

# Board options keys (order lives here — never trust frontend subentry order).
CONF_SUBENTRY_ORDER = "subentry_order"      # ordered list of subentry_ids
CONF_SHOW_RUN_NUMBERS = "show_run_numbers"  # display pref; inert until T10
CONF_SHOW_VEHICLE_TYPE = "show_vehicle_type"  # display pref; inert until T10
BOARD_DEFAULT_DEPARTURE_COUNT = 5             # board default (bound 10, const)
BOARD_MAX_DEPARTURE_COUNT = 10

# Pin (subentry) data keys.
CONF_PIN_KEY = "pin_key"                     # stable per-pin identity (== unique_id)
CONF_LABEL_FROM = "label_from"               # display label for the origin
CONF_LABEL_TO = "label_to"                   # display label for the destination
CONF_ROUTE_TYPE = "route_type"               # GTFS route_type int (bundle mapping)
CONF_BOARDING_STOP_ID = "boarding_stop_id"   # pin origin stop
CONF_BOARDING_STOP_NAME = "boarding_stop_name"
CONF_VIA_HINT = "via_hint"                   # disambiguating via stop (label+identity)
CONF_COLOUR_OVERRIDE = "colour_override"     # optional per-pin colour override
CONF_INCLUDE = "include"                     # pin enabled/disabled

# Defaults for board display prefs (stored, but inert until T10 renders them).
DEFAULT_SHOW_RUN_NUMBERS = False
DEFAULT_SHOW_VEHICLE_TYPE = False

# Every route type across all modes — used by the cross-mode board flow (origin
# stop search and "all destinations" line listing span modes).
ALL_ROUTE_TYPES: list[int] = sorted(
    {rt for types in ROUTE_TYPE_MAP.values() for rt in types}
)

# Route type → bundle folders.  A pin carries a GTFS route_type; this maps it to
# the bundle folder(s) whose schedule must be imported for that pin to resolve.
# Route types are not unique to a mode (e.g. 701 spans bus + regional bus), so a
# route_type can map to several folders — the board consumes the UNION.
ROUTE_TYPE_BUNDLE_MAP: dict[int, frozenset[str]] = {
    rt: frozenset(
        folder
        for mode, folders in MODE_BUNDLE_MAP.items()
        if rt in ROUTE_TYPE_MAP.get(mode, [])
        for folder in folders
    )
    for rt in {rt for types in ROUTE_TYPE_MAP.values() for rt in types}
}

# -------------------------------------------------------------------
# Board entity unique-id contract (T10 consumes this)
#
# Entity unique ids for board entities are built as:
#
#     f"{parent_entry_id}:{role}:{rank}"
#
# where ``role`` names the entity slot (e.g. "departure", "realtime_status")
# and ``rank`` is the pin's 1-based position in the board's ordered subentry
# list (see CONF_SUBENTRY_ORDER).  The subentry_id is deliberately NOT part of
# the entity unique id — it is an implementation detail that can change across
# re-imports — so entities key off the stable rank instead.
# -------------------------------------------------------------------

BOARD_ENTITY_SEPARATOR = ":"


def board_entity_unique_id(parent_entry_id: str, role: str, rank: int) -> str:
    """Build the stable unique id for a board entity (see T10 contract)."""
    return BOARD_ENTITY_SEPARATOR.join((parent_entry_id, role, str(rank)))


# Board entity roles (the "role" slot of ``board_entity_unique_id``).  ``rank``
# is 1-based for per-slot entities (departure 1..N) and 0 for board-level
# singletons (disruption, direction), which have no pin position to anchor to.
BOARD_ROLE_DEPARTURE = "departure"
BOARD_ROLE_DISRUPTION = "disruption"
BOARD_ROLE_DIRECTION = "direction"

# Board direction-select runtime values (in-memory only — never persisted to
# options, so they reset to ``BOARD_DIRECTION_CONFIGURED`` on restart).
BOARD_DIRECTION_CONFIGURED = "configured"
BOARD_DIRECTION_OPPOSITE = "opposite"

# Default for the board's realtime toggle (added in T10; watches already use
# ``CONF_REALTIME_ENABLED`` with the same default).
DEFAULT_BOARD_REALTIME_ENABLED = True

# -------------------------------------------------------------------
# Coordinator / polling
# -------------------------------------------------------------------

# Default poll interval for realtime trip updates (seconds)
DEFAULT_POLL_INTERVAL_SECONDS = 30

# Stagger between coordinator startup times to avoid burst (seconds per entry)
COORDINATOR_STAGGER_SECONDS = 3

# Service alerts are slow-moving — poll less frequently
ALERTS_POLL_INTERVAL_SECONDS = 300  # 5 minutes

# TTL for the module-level realtime feed cache
REALTIME_CACHE_TTL_SECONDS = 30

# -------------------------------------------------------------------
# GTFS schedule refresh (self-healing lifecycle)
#
# The schedule is re-downloaded and re-imported *before* the local
# calendar expires, so departure sensors never silently fall to unknown.
# -------------------------------------------------------------------

# Refresh when the service horizon is within this many days of today.
# 7 days gives the weekly refresh a full cycle of slack to recover from
# a failed download/import before the calendar actually expires.
GTFS_REFRESH_MARGIN_DAYS = 7

# Minimum cadence: force a refresh attempt (change-detection gated) if the
# schedule on disk is older than this many days, even when the horizon is
# still comfortably in the future.
GTFS_REFRESH_MAX_AGE_DAYS = 7

# Retry interval after a failed refresh (live DB is retained in the meantime).
GTFS_REFRESH_RETRY_SECONDS = 3600  # 1 hour

# How often to check whether the schedule needs refreshing.
GTFS_REFRESH_CHECK_INTERVAL = timedelta(days=1)

# PRAGMA user_version written into freshly imported databases. At open time a
# live DB with a lower version is treated as stale and force-refreshed.
# Forward-compat contract: a DB whose user_version is *higher* than this is
# accepted as-is (we assume a newer schema is a superset; see
# gtfs/horizon.refresh_due and gtfs/repository.schedule_state).
GTFS_SCHEMA_VERSION = 1

# Immutable generation store (see gtfs/store.py). Each successful import
# writes a NEW generation file into a subdirectory and publishes an atomic
# pointer naming the active generation, so the live DB is never overwritten
# in place (which would let stale WAL sidecars corrupt it).
GTFS_GENERATIONS_DIR_NAME = "generations"
GTFS_POINTER_NAME = "current.json"

# GC: keep the most recent N generations regardless of age; delete older ones
# only after they exceed this age (approximates "no reader still has it open").
GTFS_GENERATION_KEEP_COUNT = 2
GTFS_GENERATION_RETENTION_SECONDS = 24 * 3600  # 24 hours

# Build-artifact GC: an in-progress build (under generations/build/) is only
# swept if it is BOTH not actively registered AND older than this age. Active
# imports are minutes-scale, so 2h protects unregistered builds from non-guarded
# callers while still reclaiming genuinely abandoned ones.
GTFS_BUILD_RETENTION_SECONDS = 2 * 3600  # 2 hours

# -------------------------------------------------------------------
# Departure sensor slots
# -------------------------------------------------------------------

DEPARTURE_SLOTS = [1, 2, 3]

# -------------------------------------------------------------------
# Transport Victoria pictograms
# Served at /transport_victoria/pictograms/<filename> via async_setup.
# Map each mode to its official pictogram PNG filename (no extension).
# -------------------------------------------------------------------

PICTOGRAM_URL_BASE = "/transport_victoria/pictograms"

MODE_PICTOGRAM: dict[str, str] = {
    MODE_METRO:       "PICTO_MODE_Train",
    MODE_TRAM:        "PICTO_MODE_Tram",
    MODE_BUS:         "PICTO_MODE_Bus",
    MODE_REGIONAL_BUS: "PICTO_MODE_Coach",
    MODE_VLINE:       "PICTO_MODE_RegionalTrain",
}

# -------------------------------------------------------------------
# Data attribution
# GTFS Schedule is published by the Department of Transport and Planning
# under Creative Commons Attribution 4.0 (CC BY 4.0).
# Realtime feeds are served by the same open-data platform.
# -------------------------------------------------------------------

ATTRIBUTION = (
    "Data provided by Public Transport Victoria "
    "(Department of Transport and Planning, State of Victoria) "
    "· Licensed under CC BY 4.0"
)

# MDI icons for each mode — used by the dynamic icon property on departure sensors
MODE_MDI_ICON: dict[str, str] = {
    MODE_METRO:        "mdi:train",
    MODE_TRAM:         "mdi:tram",
    MODE_BUS:          "mdi:bus",
    MODE_REGIONAL_BUS: "mdi:bus-articulated-front",
    MODE_VLINE:        "mdi:train-variant",
}
