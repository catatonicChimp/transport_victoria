# Offline fixture catalogue (T6 contract for T7–T13)

Everything here is constructed **at test time** from
[`tests/gtfs_builder.py`](../gtfs_builder.py) and
[`tests/gtfs_fixtures.py`](../gtfs_fixtures.py). No SQLite binary, no GTFS
ZIP and no network response is committed. The only committed fixture binaries
are the small realtime protobuf feeds in [`realtime/`](realtime/).

## Pinned clock

| Constant | Value | Notes |
| --- | --- | --- |
| `REFERENCE_DATE` | `2026-06-01` (Monday) | Passed as `today` to the importer and as `today`/`now` to horizon/repository queries. |
| `REFERENCE_NOW` | `2026-06-01T08:00:00` | Default query clock used by the tests. |

The importer validates horizons against its `today` argument, so a fixture can
be imported at `REFERENCE_DATE` and later evaluated at any date (this is how
the expiry scenarios work without wall-clock drift). All GTFS times are
strings — post-midnight `25:10:00` values are preserved verbatim.

## Builder API

```python
from tests.gtfs_builder import build_bundle, build_generation, import_bundle
from tests import gtfs_fixtures as fx

# 1. flat CSV set into a directory or zip (no import)
build_bundle(tmp_path / "flat", **fx.metro_spec())

# 2. one bundle → one real generation DB + pointer (returns the DB anchor)
db = import_bundle(tmp_path, **fx.metro_spec())

# 3. several bundles → one generation (per-bundle provenance recorded)
db = build_generation(tmp_path, fx.two_bundle_specs())
```

`build_generation(tmp_path, {"2": spec_a, "5": spec_b})` writes the PTV nested
layout `<folder>/google_transit.zip` and calls the production
`gtfs.importer.import_bundles_to_generation`. The session-scoped pytest
fixtures in [`tests/conftest.py`](../conftest.py) build each catalogue entry
once per run (each bundle import takes single-digit milliseconds).

## Schedule fixtures

| pytest fixture | Spec (`tests/gtfs_fixtures.py`) | Scenario | Key expected facts |
| --- | --- | --- | --- |
| `metro_db` | `metro_spec()` | 2a. Two metro routes (Frankston, Werribee) **sharing one stop** (`RICHMOND`), both directions, several hours, shapes per pattern. | Both route ids interleave at `RICHMOND` ordered by `departure_seconds`; route/direction filters work; `25:10:00` trip `T_FRK_LATE` is returned at 08:00 as the next service and at 00:20 next day as a Monday-service departure (`scheduled_departure = Tue 01:10`). |
| `tram_db` | `tram_spec()` | 2b. Route 96 with parent station (`ST_KILDA`, `location_type=1`), child platforms `T96_P1`/`T96_P2` (`location_type=0`, `platform_code` 1/2), an unserved entrance (`location_type=2`), plus a clean pattern and a **first-stop == last-stop loop** (`T_TRAM_LOOP`). | Querying a child platform expands to sibling platforms; `platform_filter` keeps one platform; the loop yields two future rows for the same trip at `T96_TERM` (sequences 1 and 4). |
| `bus_db` | `bus_spec()` | 2c. Route 220 with **short-working** trips (`T_BUS_SHORT` → Footscray, `T_BUS_SHORT2` → Braybrook), duplicate `"City"` headsigns via Footscray vs Kensington (`T_BUS_VIA_A`/`T_BUS_VIA_B`), and a daily **night service** with `00:30`, `03:45`, `04:00` times. | All five outbound trips returned at Sunshine; stop counts 4/3/2; duplicate headsigns differ only by intermediate stop; at 03:30 the night rows `03:45`/`04:00` are upcoming and `00:30` is past. |
| `vline_db` | `vline_spec()` | 2d. One long V/Line route (Geelong) with two directions. | At `V_WERRIBEE` the 07:10 inbound (`direction_id=1`) precedes the 07:25 outbound (`direction_id=0`); destination filter selects one direction. |
| `duplicate_labels_db` | `duplicate_labels_spec()` | 2d-ii. Upfield line where **three patterns reach one terminus with the same penultimate stop** (`U_GOWRIE`), plus two inbound patterns with distinct penultimate stops. | `get_terminals_for_route` collapses the three identical labels ("Upfield Station via Gowrie Station") to one candidate (the 3-trip pattern) while keeping "Flinders Street via North Melbourne" and "... via Southern Cross" distinct. |
| `expired_db` | `expired_calendar_spec()` | 2e-i. Calendar valid at import (`REF`) but expired afterwards. | Horizon `REF+9`; `schedule_state(["2"], today=REF)` = `ready`, at `REF+10` = `expired`; `is_expired` true. |
| `extension_db` | `dates_only_extension_spec()` | 2e-ii. `calendar.txt` ends tomorrow (`REF+1`); `calendar_dates` additions extend to `REF+20`/`REF+44` (past the 7-day margin). | Horizon `REF+44` (max addition); `schedule_state(["2"], today=REF+10)` = `ready`. |
| `removed_today_db` | `removed_today_spec()` | 2e-iii. Removal (`exception_type=2`) creates a **gap today**. | `has_service_on_date(REF)` false, `REF+1` true; `refresh_due(today=REF)` true while `schedule_state` is `ready`; departure query returns `[]` on `REF`, resumes on `REF+1`. |
| — (inline, `tmp_path`) | `no_service_spec()` | 2e-iv. Removals only: no calendar rows and no additions. | `build_generation` raises `ValueError` ("no calendar service dates"); no pointer is published; `schedule_state` = `missing`. |
| `two_bundle_db` | `two_bundle_specs()` | 2f. Bundles `"2"` (train) and `"5"` (tram) in **one generation**, overlapping calendar ranges, distinct horizons. | `read_bundle_horizons` = `{"2": REF+30, "5": REF+5}`; pointer names the generation; per-bundle `schedule_state` scopes health (bundle 5 expired at `REF+10` while bundle 2 is ready); both route types queryable. |

## Realtime fixtures

Serialized protobuf files live in [`realtime/`](realtime/). They are decoded
through the production `realtime.client._decode_feed` in
[`tests/test_realtime_decode.py`](../test_realtime_decode.py).

| Fixture (`tests/fixtures/realtime/*.pb`) | Builder (`tests/realtime_fixtures.py`) | Scenario | Key expected facts |
| --- | --- | --- | --- |
| `trip_update_delay.pb` | `trip_update_delay()` | Trip update with delay. | `T_FRK_OUT_2` / `R_METRO_FRANKSTON`; `RICHMOND` departure delay 90 (arrival 100); `FRANKSTON` arrival-only delay 240; parser prefers departure. |
| `trip_update_canceled.pb` | `trip_update_canceled()` | CANCELED trip. | `TripDescriptor.schedule_relationship == CANCELED`; parser `is_cancelled=True`. |
| `trip_update_skipped.pb` | `trip_update_skipped()` | SKIPPED stop_time_update. | `RICHMOND` relationship `SKIPPED`; `NEWPORT` delay 60; parser currently exposes delay only (no skip flag). |
| `trip_update_multi.pb` | `trip_update_multi()` | Multiple entities. | Three entities `tu-multi-a/b/c`; delays +30 and −45; no keys for the canceled entity (no stop updates). |
| `alert_service.pb` | `alert_service()` | Service alert. | Informed entities route `R_METRO_FRANKSTON`, stop `RICHMOND`, trip `T_FRK_OUT_2`; active period `1_600_000_000..2_000_000_000`; EN header, EN-AU description; effect `SIGNIFICANT_DELAYS`. |
| `vehicle_position.pb` | `vehicle_position()` | Vehicle position. | Trip descriptor (`T_FRK_OUT_2`, route, direction, start date), position (-37.85, 145.02), bearing 180, speed 12.5 m/s → 45.0 km/h, vehicle `V123`/`123`, stop `CAULFIELD`, `STOPPED_AT`. |
| `header_tick_0.pb`, `header_tick_1.pb` | `header_tick_0/1()` | Header timestamp increment. | Empty feeds; `header.timestamp` differs by exactly 60 s. |

Regenerate the committed bytes with:

```bash
.venv/bin/python -m tests.realtime_fixtures --write
```

The builder emits identical wire bytes on every run for the pinned inputs
(no maps, no wall clock). Library versions used for the committed files are
recorded in [`realtime/README.md`](realtime/README.md).

## Known gaps flagged for later packages

* **T7 — terminus derivation:** `repository.py` has no public
  “terminal stops per (route, direction)” query. `tests/test_gtfs_queries.py::
  TestTerminusDerivation` proves candidate termini through the existing
  `next_departures(..., direction_stop_id=...)` surface only. T7 still needs an
  explicit trip→last-stop/stop-sequence query (e.g. `get_terminals_for_route`).
* **T8 — repeated stops on one trip:** `_filter_by_destination` keys the origin
  sequence by `trip_id`, so a loop trip (`T_TRAM_LOOP`, first == last stop)
  collapses to the last sequence and the destination filter drops it. The
  fixture pins this behaviour; T8 should disambiguate by stop sequence.
* **T8/T9 — skipped stops:** `parse_trip_updates` records a delay but not the
  `SKIPPED` `schedule_relationship`, so a skipped stop looks like a 0-delay
  stop. The raw proto fact is asserted in T6; the parser needs a skip flag.
* **T10 — station → platform expansion:** departure/sibling expansion works
  from a *platform* stop to its siblings, not from a parent `location_type=1`
  station downward. The tram fixture covers both levels; config/entity code
  must select a platform (or add downward expansion).
* **T13 — cause/effect maps fixed:** `realtime/service_alerts.py` now derives
  `_CAUSE_MAP` / `_EFFECT_MAP` from the bound `gtfs_realtime_pb2` enums (the
  previous hand-maintained dicts numbered from 0 while GTFS-RT is 1-based, so
  e.g. `TECHNICAL_PROBLEM` (3) was mislabelled `"STRIKE"`). The mapping is
  asserted against the proto enum in `test_realtime_decode.py`.
