# Transport Victoria v0.2.0

**The open-data release.** `transport_victoria` now runs entirely on Transport
Victoria Open Data — GTFS Schedule plus GTFS Realtime — with no PTV Timetable
API credentials or endpoints at runtime.

## Highlights

### Trip-group departure boards

Group several line/destination/stop combinations into one merged, realtime-aware
departure board:

- **Pins** — add a boarding stop, choose all destinations or a specific one,
  pick the resolved line/direction, then set labels, include toggle, platform
  filter and colour override.
- **Ranked sensors** — `Departure 1…N` (default 5, maximum 10) sorted by
  effective departure time, with delay, cancellation, skipped stops, realtime
  state, per-pin labels and a stable `sync_index` for dashboard scroll
  stability.
- **Disruptions** — one binary sensor per board, on when any active alert
  matches any enabled pin. It goes `unavailable` when an alert feed fails, so
  "off" is never a false all-clear.
- **Direction** — a select that flips specific-destination pins to the opposite
  direction at runtime. All-destinations pins ignore it; the toggle resets on
  restart and never rewrites saved configuration.
- **Pin management** — reorder, remove and reconfigure pins from the board's
  Configure menu.

### Import existing watches as a board

An opt-in import flow copies compatible departure watches into a new board, one
pin per watch, using the same pin-derivation path as manual creation. Source
watches are never deleted, disabled or modified. Rollback is deleting the new
board. Transfer journeys, trackers and all-lines watches are reported and left
as-is; stale references are flagged, never silently dropped.

### Self-healing schedule

The timetable now refreshes itself before its service calendar expires:

- daily horizon check, refresh within 7 days of expiry, 7-day maximum age;
- change-detected downloads and 45-day `stop_times` windowing;
- immutable generations published by atomic pointer swap;
- last-known-good retained on failure, hourly retry;
- schema versioning for future migrations.

### Realtime hardening

- One shared cached fetch per feed across all entries (30 s TTL), so boards do
  not multiply requests.
- Per-feed budget of 20 requests / 60 s plus a global 40 / 60 s safety ceiling.
- HTTP 429 honours `Retry-After` (clamped to 60 s).
- HTTP 403 product-scope restrictions are classified as unavailable with a
  **one-time** hint and self-heal when the portal product is enabled.
- Per-feed freshness and honest `unavailable` states instead of false "no
  disruption" / "no departures".

### Existing capabilities retained

Departure watches for Metro Trains, Trams, Buses, Regional Buses and V/Line;
specific-destination and transfer-journey routing; platform/City Loop handling;
disruption sensors; vehicle trackers and route trackers; local SQLite search.

## Compatibility

- **Home Assistant 2026.1 or newer** (declared in `hacs.json`).
- A **subscription key** from the Transport Victoria Open Data Portal is
  required by the setup flow and for realtime feeds. It is self-service under
  *My Account → Subscription Keys* and is sent as the `KeyID` header.
- GTFS **schedule downloads are keyless**; only realtime needs the key.
- Python requirements are unchanged: `gtfs-realtime-bindings==1.0.0`.

## Known limitations

- **Stop detail is GTFS-only** — no amenities, staffing, contact or ticketing;
  wheelchair/platform/parent-station fields only, with ~1,365 blank wheelchair
  values and no `zone_id` in the current TV GTFS.
- **No fare estimates** and **no myki outlets** — the data does not support them.
- **No PTV disruption themes/categories** — alert text and GTFS `cause`/`effect`
  only; no PTV website scraping.
- **No tram vehicle tracking** — the portal publishes no documented tram
  Vehicle Positions product.
- **No V/Line service-alert feed** — V/Line coverage is trip updates and vehicle
  positions.
- **Regional-bus realtime uses the combined bus feed** — there is no separate
  regional-bus realtime endpoint.
- **Bus service alerts need the portal product** — `bus/service-alerts` returns
  403 until *Public Transport GTFS Realtime (Bus Service Alerts)* is enabled for
  the key; the integration logs one hint and self-heals.
- **Schedule window** — `stop_times` covers the next 45 days; the schedule
  refreshes weekly/in advance of expiry.

Full detail: [docs/KNOWN_LIMITATIONS.md](https://github.com/catatonicChimp/transport_victoria/blob/main/docs/KNOWN_LIMITATIONS.md)
and the parity record in
[docs/PARITY.md](https://github.com/catatonicChimp/transport_victoria/blob/main/docs/PARITY.md).

## Migration from the classic integration

There is **no automatic migration** and no silent backend swap. The classic
[`bremor/public_transport_victoria`](https://github.com/bremor/public_transport_victoria)
(PTV Timetable API) and this integration can run **side by side**:

1. Install **Transport Victoria** from HACS (custom repository) or manually;
   it uses the `transport_victoria` domain, so it does not collide with the
   classic component.
2. Add your subscription key and create the watches/boards you want. Entity ids
   and unique ids are new; update dashboards and automations deliberately.
3. To copy compatible watches instead of recreating them, use **Add integration
   → Transport Victoria → Trip-Group Board — import from existing watches**.
   The import only reads the source entries; rollback is deleting the new board.
4. Remove the classic integration only when your dashboards and automations have
   been moved.

Notes:

- Existing `transport_victoria` watches and trackers need no config migration;
  restart Home Assistant after upgrading.
- Boards are new and opt-in; there is no auto-conversion of existing watches.
- The direction select is runtime-only and resets to *Configured directions* on
  restart.

## Acknowledgements

Data provided by Public Transport Victoria (Department of Transport and
Planning, State of Victoria), licensed under CC BY 4.0. GTFS and GTFS Realtime
are decoded with **gtfs-realtime-bindings**.

Thanks to **[@bremor](https://github.com/bremor)** and the
`public_transport_victoria` project for the earlier Home Assistant PTV
integration that paved the way for this open-data successor.

This integration is not affiliated with Transport Victoria or the Victorian
Government.
