# GTFS Realtime protobuf fixtures — provenance

These `.pb` files are **built programmatically**, not captured from the PTV
API. The builders live in [`tests/realtime_fixtures.py`](../../realtime_fixtures.py)
and the decode tests in
[`tests/test_realtime_decode.py`](../../test_realtime_decode.py).

| Item | Value |
| --- | --- |
| Generator | `python -m tests.realtime_fixtures --write` |
| `gtfs-realtime-bindings` | `3.0.0` |
| `protobuf` (transitive) | `7.36.2` |
| Python | `3.13.18` |
| Header timestamp | `1_750_000_000` (2025-06-15T14:26:40Z) |
| Alert active window | `1_600_000_000` .. `2_000_000_000` (2020-09-13 .. 2033-05-18) |

Determinism: all ids, dates, timestamps and positions are pinned constants;
the messages contain no maps and no wall-clock values, so re-running the
generator produces byte-identical files (verify with `git status`). The alert
active window is deliberately wide so the production `parse_alerts` real-clock
“is active now” check stays stable for years.

`requirements_test.txt` pins `gtfs-realtime-bindings==3.0.0`; `protobuf` is
resolved transitively. This is a **test-only** dependency — runtime code keeps
the lazy `google.transit` import inside `_decode_feed`.

| File | Scenario |
| --- | --- |
| `trip_update_delay.pb` | Departure delay 90 s at the shared stop; arrival-only delay 240 s at the terminus. |
| `trip_update_canceled.pb` | `TripDescriptor.schedule_relationship = CANCELED`. |
| `trip_update_skipped.pb` | `StopTimeUpdate.schedule_relationship = SKIPPED` at `RICHMOND`. |
| `trip_update_multi.pb` | Three entities, including a canceled trip. |
| `alert_service.pb` | Route + stop + trip informed entities, active period, EN/FR header, EN-AU description. |
| `vehicle_position.pb` | Full trip descriptor, GPS position, bearing/speed, vehicle id/label, `STOPPED_AT`. |
| `header_tick_0.pb` / `header_tick_1.pb` | Empty feeds one minute apart (header timestamp increment). |
