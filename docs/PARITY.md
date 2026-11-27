# PTV Timetable API ↔ GTFS / GTFS Realtime parity record

**Status:** complete for the v0.2.0 sign-off (T14), 27 Nov 2026.
**Scope:** every capability of the PTV Timetable API v3 that the classic
`public_transport_victoria` integration used, or that was considered during the
successor design, mapped onto Transport Victoria Open Data (GTFS Schedule +
GTFS Realtime).

No runtime path in `transport_victoria` requires PTV Timetable API credentials
or endpoints. The remaining differences are either implemented, explicitly
documented as limitations (see [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md)), or
deliberately rejected here.

## Status legend

| Status | Meaning |
|---|---|
| **replicated-better** | Capability is present, and the open-data implementation is more capable or more robust than the PTV API path. |
| **replicated** | Capability is present with equivalent behaviour. |
| **partial** | Capability is present in a reduced form; the missing part is documented. |
| **gap** | No equivalent exists in open data; handled as a documented limitation or rejected. |

## Parity matrix

| # | PTV Timetable API capability | GTFS / GTFS-RT equivalent | Status | Disposition |
|---|---|---|---|---|
| 1 | Developer authentication and onboarding | Keyless GTFS Schedule downloads; self-service Open Data subscription key sent as the `KeyID` header for realtime | **replicated-better** | Removes the devID/HMAC signature and the stalled PTV key queue. Realtime quota handling remains mandatory (T13). |
| 2 | Departures for a stop, all routes | `stop_times` + `trips` + `routes` + service calendars, refreshed by Trip Updates | **replicated-better** | Local queries merge every serving route and sort across routes. Scheduled departure is the fallback when realtime is absent. |
| 3 | Departures for stop + route; platform, direction, maximum-results, date and look-back filters | Parameterised SQLite departure queries with route, `platform_code`/stop, derived direction, time-window and limit filters | **replicated** | Platform filtering and all-destinations behaviour preserved. Filters apply before selecting the next N departures. |
| 4 | Cancelled departures | Trip Updates with cancelled trips | **replicated** | Cancellation is retained as structured departure state (`cancelled`); display policy is the board's. |
| 5 | Live skipped stops / `num_skipped` | Trip Update stop-time updates with `SKIPPED` | **replicated-better** | Skipped stops are identified individually and counted from the same realtime trip. |
| 6 | Patterns: stopping order, terminus, skipped-stop display | Ordered `stop_times` per trip including the final stop; realtime `SKIPPED` state | **replicated-better** | Source for stopping order and destination-terminus choices. |
| 7 | Directions by route, direction or route type | Trip patterns grouped by route and `direction_id`, with destination stop and headsign evidence | **replicated-better** | Direction is derived from the selected destination/pattern; the design never assumes exactly two patterns. |
| 8 | Routes, partial-name route search, route types | `routes.txt`, GTFS `route_type`, SQLite/FTS search | **replicated** | Route colour from `routes.txt`; per-pin override preserved. |
| 9 | Runs by route or run reference; run details | `trips.txt` + ordered `stop_times`; realtime trip identity | **replicated** | GTFS trip/route ids are exposed in attributes. A GTFS `trip_id` is treated as one run identity; see [trip identity](#trip-identity-is-per-run) below. |
| 10 | Stops on a route and stops in stopping order | Distinct stops from route trip patterns; ordered `stop_times` per trip | **replicated-better** | Supports variants and short workings. |
| 11 | Stop location and stops by geolocation | `stops.txt` coordinates plus local distance queries | **replicated-better** | Offline; uses the proximity-radius option. |
| 12 | Basic stop accessibility | `wheelchair_boarding`, `platform_code`, `parent_station` | **partial** | No `zone_id` in the current TV feeds; no facilities detail (see [stop facilities](#stop-facilities--not-viable) and [PARITY_STOP_FACILITIES.md](PARITY_STOP_FACILITIES.md)). |
| 13 | Stop facilities (amenities, accessibility detail, staffing, contact, ticketing) | No core GTFS equivalent | **gap** | Portal investigated in T12: no facilities dataset exists. Documented limitation; never a PTV fallback. |
| 14 | Geopath for routes and departures | `shapes.txt` via a trip's `shape_id` | **gap** | Trip `shape_id` is stored, but `shapes.txt` geometry is deliberately not imported (11 M rows with no departure-query value). No geopath/route-line geometry is exposed; the classic integration did not use this capability either. |
| 15 | Disruptions (all / route / stop / route+stop / id / mode) | Service Alerts filtered locally by `informed_entity`, feed, route, stop, trip and active period | **replicated** | Alerts are combined across feeds; identity and active periods are retained. Selectors are AND-ed within one `informed_entity` and OR-ed across selectors. |
| 16 | Disruption status, cause, effect and text | Alert active periods, cause, effect, header and description | **replicated** | Header, description, cause, effect, informed entities and active periods are preserved. Transport Victoria alerts carry no URL field, and optional fields stay absent rather than being invented. |
| 17 | PTV disruption themes/categories/advice URLs | No consistent equivalent | **gap** | Cosmetic loss. Alert text plus cause/effect only; no PTV website scraping. |
| 18 | Vehicle positions/expansion | Vehicle Positions feeds joined to trips | **partial** | Published for Metro Trains, the combined Metro & Regional Bus feed, and V/Line. No documented tram Vehicle Positions product; tram vehicle tracking remains unsupported. |
| 19 | Search (text/id/geo) | Local SQLite + FTS + coordinates | **replicated-better** | No network needed for config search. |
| 20 | Night bus | Night services in the bus schedule and bus realtime | **replicated** | No separate endpoint. |
| 21 | Myki fare estimates | `zone_id` partial only | **gap** | Hand-maintained fare engine rejected; current TV GTFS contains no `zone_id`. |
| 22 | Myki outlets | No equivalent | **gap** | Dropped as irrelevant and unavailable. |

## Gap dispositions

### Stop facilities — NOT-VIABLE

The Transport Victoria Open Data portal publishes no stop-facilities dataset.
The only stop-level portal export is a weekly GeoJSON re-export of GTFS
`stops.txt`; a join test found identical stop ids and coordinates and strictly
less field coverage than the GTFS data already imported. The rich
amenity/accessibility/staffing/contact/ticketing detail exists only in the
key-gated PTV Timetable API.

Full evidence, including the 32,055-feature join test and catalogue probes, is
recorded in [PARITY_STOP_FACILITIES.md](PARITY_STOP_FACILITIES.md).

**Consequence:** stop detail is GTFS-only — name, coordinates, `location_type`,
`parent_station`, `platform_code` and `wheelchair_boarding` (0/1/2,
with roughly 1,365 stops blank). The current TV GTFS feeds contain no `zone_id`.
Amenities, accessibility detail beyond wheelchair boarding, staffing, contact
and ticketing attributes are absent, not synthesised.

### Disruption themes/categories — rejected

PTV's website taxonomy is not carried in GTFS Realtime and has no documented
open-data equivalent. The integration exposes what the feed carries: alert
header/description text, `cause`, `effect`, active periods and informed
entities. Alert URLs are not available from Transport Victoria alerts. `cause`
is informational and is never used for selector matching.
Scraping the PTV website for themes/advice URLs was rejected: it is fragile,
undocumented and outside the open-data licence surface.

### Geopaths — not built

PTV's geopath endpoint has no shipped equivalent. `trips.shape_id` is imported,
but `shapes.txt` geometry is deliberately skipped during import: it contains
around 11 million geometry rows that no departure, destination or board query
needs, and importing it would inflate the SQLite database and first-run time.
The classic integration did not expose geopaths either. This is an accepted gap,
not a hidden remote dependency.

### Myki fare estimates — rejected

Fare calculation needs a trustworthy zone model. The current TV GTFS feeds carry
no `zone_id`, and the PTV API's zone data was only ever partial. A
hand-maintained fare table would drift from official fares and imply accuracy
the data cannot support, so it was rejected rather than shipped as a guess.

### Myki outlets — rejected

T12 confirmed that no outlet dataset exists anywhere in Victorian open data
(portal, state CKAN portal and slug probes all returned nothing). Ticketing
outlets also have no GTFS equivalent. Dropped as irrelevant to a departure
integration.

### Tram vehicle tracking — not supported

The portal publishes **no documented tram Vehicle Positions product**; the GTFS
Realtime catalogue lists ten resources — three Metro Train, three Yarra Trams,
two Metro & Regional Bus and two V/Line — with no tram Vehicle Positions
resource. The integration classifies `tram/vehicle-positions` as unavailable and
never polls it.

An authenticated probe on 27 Nov 2026 did return an **undocumented**
`tram/vehicle-positions` endpoint with one to two vehicles. It is not a
catalogue product, has no published coverage or stability contract, and the
integration deliberately does not consume it. This is recorded so the
limitation is not misread as "the URL does not exist".

### V/Line service alerts — not published as a supported feed

The catalogue has no V/Line Service Alerts resource, and an authenticated probe
returned HTTP 403. The integration uses V/Line Trip Updates and Vehicle
Positions; V/Line disruption coverage is therefore limited to what those feeds
carry. The client suppresses repeated requests for this feed.

## Verified live endpoint catalogue

**Method:** authenticated `GET` with the subscription key in the `KeyID`
header against
`https://api.opendata.transport.vic.gov.au/opendata/public-transport/gtfs/realtime/v1/{mode}/{feed}`,
five mode segments × three feed types, on **27 Nov 2026 at 16:08 UTC**. The
CKAN `gtfs-realtime` package was read at the same time: it lists **10** GTFS-RT
resources (metro 3, tram 3, bus 2, V/Line 2).

| Mode segment | Feed | HTTP | Body on probe | Disposition |
|---|---|---|---|---|
| `metro` | `trip-updates` | 200 | 12,699 B, 31 entities | Supported; header age ~0.2 min |
| `metro` | `service-alerts` | 200 | 20,128 B, 20 entities | Supported; header age ~0.6 min |
| `metro` | `vehicle-positions` | 200 | 1,668 B, 12 entities | Supported; header age ~0.6 min |
| `tram` | `trip-updates` | 200 | 407 B, 2 entities | Supported; header age ~0.2 min |
| `tram` | `service-alerts` | 200 | 1,042 B, 2 entities | Supported; header age ~0.6 min |
| `tram` | `vehicle-positions` | 200 | 138–261 B, 1–2 entities | **Undocumented**; not consumed (see above) |
| `bus` | `trip-updates` | 200 | 84,513 B, 101 entities | Supported; combined Metro & Regional Bus feed |
| `bus` | `service-alerts` | **403** | 660 B access-managed fault | **Access-limited**: portal product not enabled on the key |
| `bus` | `vehicle-positions` | 200 | 7,077 B, 67 entities | Supported; combined Metro & Regional Bus feed |
| `regional-bus` | any feed | **404** | 660 B access-managed fault | No separate feed; resolves to `bus` (see below) |
| `vline` | `trip-updates` | 200 | 15 B, 0 entities | Supported; empty feed at probe |
| `vline` | `service-alerts` | **403** | 660 B access-managed fault | Not published as a supported product |
| `vline` | `vehicle-positions` | 200 | 15 B, 0 entities | Supported; **header age ~6.8 min** (see below) |

**Counts.** Thirteen distinct endpoint discoveries were made:

- **10 resolvable endpoints** served a protobuf body: metro ×3, tram ×2
  documented feeds (plus the undocumented Vehicle Positions endpoint described
  above), bus ×2 and V/Line ×2. Nine of these are documented portal products;
  excluding the undocumented tram endpoint, the supported catalogue is exactly
  metro ×3, tram ×2, bus ×2 and V/Line ×2.
- **2 access-limited endpoints** returned HTTP 403: `bus/service-alerts` and
  `vline/service-alerts`.
- **1 verified alias**: the three `regional-bus` probes all 404 individually and
  resolve to the single combined `bus` feed.

### `regional-bus` → `bus` resolution (verified live)

The schedule bundle splits regional buses from metro buses, but the realtime
platform does not. `regional-bus`, `rural-bus` and `coach` URL segments return
404; the combined `bus` feed carries both. `MODE_REGIONAL_BUS` therefore fetches
the `bus` realtime feed. This is a URL-resolution mapping only: the config mode,
labels, bundle mapping and schedule semantics remain unchanged. The realtime
client's cache, rate buckets and known-missing/scope-unavailable keys use the
**resolved** mode, so a regional-bus entry and a bus entry share one fetch.

### `bus/service-alerts` — product scope (403)

The portal serves the endpoint but the subscription key must have the
**Public Transport GTFS Realtime (Bus Service Alerts)** product enabled under
*My Account → Products*. Without it the endpoint returns HTTP 403, not 404.
The client treats this as `unavailable`, suppresses retries for that poll
cycle, and logs a **one-time** diagnosis hint. It still probes each poll and
self-heals if the product is enabled (a 200 overrides the classification).

### `vline/vehicle-positions` — header-age note

The V/Line Vehicle Positions feed served an **empty** feed at probe time, and
its `FeedHeader.timestamp` was ~6.8 minutes behind wall clock, while the metro,
tram and bus feeds were within 0.2–0.6 minutes. V/Line position updates are
therefore slower-moving than the other modes. Freshness must be judged
per feed from the header/fetch timestamps; a V/Line feed is not assumed to be
"live" just because it fetched successfully. The board aggregator's staleness
threshold is 180 seconds, so a V/Line feed with a header age beyond that is
reported as stale rather than silently treated as fresh.

## Supported-feed restrictions

### Trip identity is per run

GTFS Realtime `trip_id` identifies one scheduled run of one pattern. The
`start_date`/`start_time` fields in a Trip Descriptor are provenance, **not**
part of the join key: board aggregation joins on `(trip_id, stop_sequence)`,
with a `(trip_id, stop_id)` fallback only when a stop update carries no
sequence. The legacy watch index joins on `(trip_id, stop_id)`.

The PTV API's "run" object (a vehicle working that can span multiple trips) has
no direct GTFS equivalent; the integration deliberately does not reconstruct
one. Treat each `trip_id` as a unique per-run identity, not a stable
all-day vehicle working.

### Cause and effect enums

`cause` and `effect` are mapped from the values bound in
`gtfs_realtime_bindings` (1-based GTFS-RT enums), not from hand-numbered tables.
An unset field (protobuf value 0) or an unknown value becomes
`UNKNOWN_CAUSE`/`UNKNOWN_EFFECT`. They are display-only: matching is by
`informed_entity` selectors, never by cause.

Alert selectors follow GTFS-RT semantics: populated fields **within** one
`informed_entity` are AND-ed, while separate selectors are OR-ed. A selector
with no matchable field is treated as "match everything". `direction_id` and
`route_type` use field presence, so a value of `0` is a real scope.

### No tram vehicle tracking

See [Tram vehicle tracking — not supported](#tram-vehicle-tracking--not-supported).
Vehicle and route trackers are offered only for modes with a published
Vehicle Positions feed: Metro Trains, the combined Metro & Regional Bus feed,
and V/Line.

### Feed-content restrictions are not filled in

The portal's own OpenAPI descriptions document gaps in individual feeds (for
example, Metro Bus Trip Updates do not include platform information or
cancellations). The integration never invents the missing data: absent
prediction fields stay absent, the scheduled departure remains the fallback,
and the `realtime_state`/`cancelled` attributes report only what was received.
Optional alert fields (description, active periods) are likewise omitted, not
synthesised.

### Schedule window and refresh

The GTFS import windows `stop_times` to trips active in the next **45 days**
(`trips`, `calendar`, `calendar_dates` and `stops` are imported in full).
The schedule is checked daily and refreshed when the service horizon is within
**7 days**, when the database is older than **7 days**, when the schema version
is older, or when a required mode bundle is missing/expired. Downloads are
change-detected; a failed refresh keeps the last-known-good generation and
retries after **1 hour**. Imports are written to a new generation and published
by atomic pointer swap, so a refresh never overwrites the live database in
place.

## Verification

- Live endpoint matrix, CKAN resource counts and protobuf header/entity counts:
  probe of 27 Nov 2026 (this document).
- Facilities join test and portal catalogue evidence:
  [PARITY_STOP_FACILITIES.md](PARITY_STOP_FACILITIES.md).
- Offline regression suite: `pytest tests -q` — 398 passed, 2 skipped at the
  v0.2.0 sign-off commit. CI uses fixtures only and never calls live endpoints.

## Reopen criteria

Revisit a gap only if the underlying open data changes:

1. A real stop-facilities dataset (with a stable stop-id join and a publication
   cadence) appears on the portal.
2. A **documented** tram Vehicle Positions product with stable coverage is
   published.
3. PTV disruption themes/categories, or `zone_id`, are added to the published
   GTFS data.
4. The bus Service Alerts product becomes available on the standard
   subscription (the code already self-heals when it does).
