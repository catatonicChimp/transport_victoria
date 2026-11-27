# Migration guide

## Coming from the PTV "Public Transport Victoria" integration

The old integration used the legacy PTV developer API (dev ID + signed API key). New API
keys for that platform have not been issued for some time, and it is no longer the
recommended way to access transport data in Home Assistant.

1. Install **Transport Victoria** at your current release (HACS custom repository or manual copy).
2. Open Settings → Devices & Services → **Add Integration → Transport Victoria**:
   - enter your free **subscription key** from [opendata.transport.vic.gov.au](https://opendata.transport.vic.gov.au)
     (My Account → Subscription Keys — self-service, no approval queue),
   - pick your transport mode, line, destination and stop.
3. You can keep the old integration installed side by side while you move dashboards and
   automations over. Entity names differ, so update any templates to the new sensors.
4. Once everything works, delete the old integration. Rollback is as simple as reinstalling it.

An optional **Import from existing watches** flow exists when adding a board, which copies
compatible existing stops into a trip-group board automatically. Sources are never deleted
or modified — rollback is deleting the board.

## Coming from Transport Victoria v0.1.0

Everything carries over automatically when you upgrade and restart — your existing
watches, journeys and trackers are kept. The v0.2.0 changes (schedule auto-refresh,
realtime hardening, boards) are additive.

If an existing entry acts up after the upgrade, remove and re-add it in
Settings → Devices & Services — that is the extent of any required reconfiguration.

## Compatibility

- Home Assistant **2026.1** or newer.
- Transport **GTFS Schedule** downloads need no credentials. **Realtime** (delays,
  cancellations, alerts, vehicle positions) needs the free portal subscription key.
- The classic PTV Timetable API is **not** used anywhere in this integration.
