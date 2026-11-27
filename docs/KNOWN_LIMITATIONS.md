# Known limitations

This page lists what `transport_victoria` deliberately does **not** provide, why,
and what to do instead. The technical evidence behind each gap is in
[PARITY.md](PARITY.md); the stop-facilities investigation is in
[PARITY_STOP_FACILITIES.md](PARITY_STOP_FACILITIES.md).

## Stop detail is GTFS-only

**What the timetable contains:** stop name, coordinates, `location_type`,
`parent_station`, `platform_code` and `wheelchair_boarding` (0 = no
information, 1 = accessible, 2 = not accessible).

**What you do not get:** station amenities (toilets, lifts, parking, shelters),
detailed accessibility information beyond the wheelchair flag, staffing,
contact details or ticketing information. Wheelchair boarding is blank for
roughly 1,365 stops, and the current Transport Victoria GTFS feeds carry no
`zone_id`.

**Why:** the Open Data portal publishes no stop-facilities dataset. The only
stop-level export is a weekly GeoJSON re-export of GTFS `stops.txt`, which
contains fewer fields than the GTFS data already imported. The richer detail
exists only in the key-gated PTV Timetable API, which this integration does not
use.

**Workaround:** none from open data. Attributes are absent rather than guessed;
do not automate on them existing.

## No fare estimates

**What you get:** departure times, delays, routes, platforms and disruption
text.

**What you do not get:** myki fares, fare comparisons or cost estimates.

**Why:** fares need a trustworthy zone model. The TV GTFS feeds carry no
`zone_id`, and a hand-maintained fare table would drift from official fares.
This was rejected rather than shipped as an approximation.

## No myki outlets or ticketing locations

**What you get:** stops and their coordinates.

**What you do not get:** myki retail outlets, top-up locations, ticket windows
or park-and-ride facilities.

**Why:** no outlet dataset exists anywhere in Victorian open data, and there is
no GTFS equivalent. T12 confirmed this against the portal and the state CKAN
catalogue.

## No PTV disruption themes or categories

**What you get:** the alert header and description text, GTFS-RT `cause` and
`effect`, active periods, and affected routes/stops/trips. Transport Victoria
alerts carry no URL field, so no alert URL is exposed.

**What you do not get:** the PTV website's disruption themes, category badges or
"advice" pages, and no scraped PTV content.

**Why:** the taxonomy is not carried in GTFS Realtime and has no documented
open-data equivalent. Scraping the PTV website was rejected as fragile and
undocumented. Matching is by the feed's `informed_entity` selectors only;
`cause` and `effect` are display information, not filters.

## No tram vehicle tracking

**What you get:** live vehicle positions for Metro Trains, the combined Metro &
Regional Bus feed, and V/Line.

**What you do not get:** tram vehicle positions or tram route/vehicle trackers.

**Why:** the portal publishes no documented tram Vehicle Positions product. The
integration classifies that feed as unavailable. An authenticated probe in
November 2026 found a small undocumented endpoint with one or two trams, but it
has no published coverage or stability contract and is deliberately not
consumed.

## No V/Line service alerts as a supported feed

**What you get:** V/Line trip updates (delays, cancellations, skipped stops) and
vehicle positions.

**What you do not get:** V/Line-specific service-alert text from a V/Line alert
feed.

**Why:** the GTFS Realtime catalogue has no V/Line Service Alerts resource, and
the endpoint returns HTTP 403. V/Line disruption coverage is limited to what the
published trip-update and vehicle-position feeds carry.

## Regional bus realtime uses the combined bus feed

**What you get:** realtime updates for regional-bus watches.

**What to know:** the portal has no separate regional-bus realtime endpoint. A
`regional-bus` watch fetches the combined **Metro & Regional Bus** feed. This is
a URL resolution only — the configured mode, labels and schedule bundles stay
regional — and a bus entry and a regional-bus entry share one cached fetch
instead of doubling requests.

## Bus service alerts require a portal product

**What you get:** bus trip updates and vehicle positions.

**What may be missing:** bus service alerts, and therefore the bus entries in a
board's disruption sensor.

**Why:** `bus/service-alerts` returns HTTP 403 unless the **Public Transport
GTFS Realtime (Bus Service Alerts)** product is enabled for your subscription
key under *My Account → Products* on the Open Data portal.

**What happens:** the integration logs a **one-time** hint and treats the feed as
unavailable; it does not spin on failures. The normal poll re-checks it, so if
you enable the product the next successful request clears the condition
automatically — no reload needed.

## The GTFS schedule is windowed and refreshed weekly

**What you get:** a local SQLite timetable built from the current GTFS bundle.

**What to know:**

- `stop_times` is imported only for trips active in the **next 45 days**.
  `trips`, calendars and stops are imported in full.
- The schedule is checked daily and refreshed when the service horizon is
  within **7 days**, when the database is older than **7 days**, when the schema
  version is older, or when a required mode bundle is missing or expired.
- Downloads are change-detected. A failed refresh keeps the last-known-good
  timetable and retries after **1 hour**; sensors do not silently fall to
  `unknown` while a usable generation exists.
- Imports are written to a new generation and published by atomic pointer swap,
  so a refresh never overwrites the live database in place.

**What to expect:** the first setup downloads roughly 230 MB and can take
1–10 minutes. Later refreshes reuse cached data when the upstream ZIP is
unchanged.

## Board direction is a runtime toggle

The board's **Direction** select switches specific-destination pins to their
opposite direction. It is applied in memory only and resets to **Configured
directions** after a Home Assistant restart. All-destinations pins ignore the
toggle. This is intentional: the toggle must never rewrite saved pin
configuration.
