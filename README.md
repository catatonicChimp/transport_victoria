# Transport Victoria for Home Assistant

[![Tests](https://img.shields.io/github/actions/workflow/status/catatonicChimp/transport_victoria/tests.yaml?branch=main&label=tests&logo=github)](https://github.com/catatonicChimp/transport_victoria/actions/workflows/tests.yaml)
[![HACS](https://img.shields.io/github/actions/workflow/status/catatonicChimp/transport_victoria/hacs.yaml?branch=main&label=HACS&logo=github)](https://github.com/catatonicChimp/transport_victoria/actions/workflows/hacs.yaml)
[![Hassfest](https://img.shields.io/github/actions/workflow/status/catatonicChimp/transport_victoria/hassfest.yaml?branch=main&label=hassfest&logo=github)](https://github.com/catatonicChimp/transport_victoria/actions/workflows/hassfest.yaml)

A [Home Assistant](https://www.home-assistant.io/) custom integration for **[Transport Victoria](https://www.transport.vic.gov.au/)** data published on the **[Transport Victoria Open Data Portal](https://opendata.transport.vic.gov.au/)** (GTFS schedule and GTFS Realtime). It shows upcoming departures at a chosen stop — metro trains, trams, buses, regional buses and V/Line — and merges **GTFS schedule** data with **GTFS Realtime** delays and service alerts.

Schedule data is downloaded without a key and stored locally in SQLite. Realtime feeds require a free subscription key from the [open data portal](https://opendata.transport.vic.gov.au/).

---

## Features

- **Departure watches** — one stop, all modes. Each watch exposes the next departures (default 3) as timestamp sensors with scheduled/actual time, delay, route, headsign, platform, direction, cancellation state and trip id in the attributes. Optional specific-destination filtering, platform filtering, and multi-leg journeys with a transfer.
- **Trip-group boards** — group several line/destination/stop combinations ("pins") into one merged, time-sorted departure board. Ranked departure sensors (default 5, maximum 10) carry compact per-pin attributes, including delay, cancellation, skipped stops, realtime state and a stable `sync_index`.
- **Board disruption sensor** — on when an active service alert matches any enabled pin. It reports `unavailable` rather than a confident "off" when an alert feed has failed.
- **Direction select** — flip specific-destination pins between their configured direction and the opposite direction without editing the pins. All-destinations pins are unaffected.
- **Import existing watches** — copy compatible departure watches into a new board, one pin per watch. Existing entries are never modified or deleted; rollback is deleting the board.
- **Vehicle and route trackers** — follow the vehicle serving the next departure from a station, or all vehicles on a line (optionally filtered by proximity). Available for modes with a published Vehicle Positions feed: Metro Trains, the combined Metro & Regional Bus feed, and V/Line. Tram vehicle tracking is not supported.
- **Self-healing schedule** — the GTFS timetable is refreshed before its service calendar expires, with change detection, last-known-good retention, hourly retry after failure, and atomic generation swaps. Imports window `stop_times` to the next 45 days.
- **Realtime hardening** — one shared fetch per feed across entries, per-feed budgets (20 requests / 60 s), a global safety ceiling (40 requests / 60 s), a 30 s cache, `Retry-After` handling for HTTP 429, and one-time hints for HTTP 403 product-scope restrictions.
- **Local search** — stop, route and destination search runs against the local SQLite database, so configuration does not depend on a remote search API.

---

## Requirements

- Home Assistant **2026.1** or newer. The minimum is set in **`hacs.json`** (`homeassistant`) for HACS; custom-integration `manifest.json` does not use that field. This integration targets current APIs (`runtime_data`, config flow, subentries, etc.).
- A **subscription key** from the [Transport Victoria Open Data Portal](https://opendata.transport.vic.gov.au/). The setup flow validates it with a realtime request, and it is sent as the HTTP header `KeyID` for realtime feeds. Get one under **My Account → Subscription Keys** (self-service, no approval queue).

GTFS schedule downloads themselves do not require a key. The key is required by the setup flow and for all realtime data.

---

## Installation

### HACS (recommended)

1. Open HACS → **Integrations** → **⋮** → **Custom repositories**.
2. Add this repository URL, category **Integration**.
3. Install **Transport Victoria** and restart Home Assistant.
4. **Settings → Devices & services → Add integration** → search **Transport Victoria**.

### Manual

Copy the `custom_components/transport_victoria` folder into your Home Assistant `config/custom_components/` directory, then restart Home Assistant and add the integration from the UI.

```text
config/
└── custom_components/
    └── transport_victoria/
        ├── manifest.json
        └── ...
```

---

## Configuration

Configuration is done entirely in the UI.

1. **Subscription key** — validated with a test request to the realtime API. Once a key is stored, later entries reuse it automatically.
2. **Entry type**:
   - **Station Departures** — a departure watch for one stop.
   - **Vehicle Tracker (linked to Station)** — follows the vehicle serving a station watch's next departure.
   - **Route Tracker (all vehicles on a line)** — tracks every vehicle on a route, optionally within a radius of a reference stop.
   - **Trip-Group Board (multiple lines at a stop)** — a merged board of pins.
   - **Trip-Group Board — import from existing watches** — copies compatible watches into a new board.

### Departure watch

1. **Mode** — Metro Trains, Trams, Buses, Regional Buses or V/Line.
2. **Stop** — search by name, list by distance from the [Home Assistant zone](https://www.home-assistant.io/integrations/zone/), or drop a pin on the map.
3. **Optional route/platform filter** and, where relevant, City Loop direction handling.
4. **Destination** — all destinations (recommended) or a specific destination with route validation. Transfer journeys are available as a separate path.
5. **Name** — used for the device and entity ids.

### Trip-group board

1. **Name** the board. Pins are added afterwards as config subentries.
2. **Add a pin** — choose the boarding stop, then **All destinations** or a specific destination, then the line/direction from the candidates the local timetable resolves. Finally set the pin's origin/destination labels, include toggle, optional platform filter and optional colour override.
3. Manage pins from the board's **Configure** menu: **Board settings** (name, number of departures, realtime toggle), **Reorder pins**, **Remove a pin**. Edit a pin's labels/options in place through its reconfigure action.

### First import

On first use the integration downloads and imports the relevant GTFS bundle(s); this can take a minute. Data is refreshed before the service calendar expires and when you add modes that need new bundles.

**Options** (after setup): rename the entry, adjust the polling interval (watches), choose the number of departures, and toggle realtime updates. Route trackers expose a vehicle-count option; vehicle trackers can be renamed.

---

## Entities

Per **departure watch** (each config entry):

| Entity | Description |
|--------|-------------|
| Departure sensors | One per slot (1…N, default 3). State is the departure timestamp; attributes include `minutes_until`, scheduled/actual time, delay, route, headsign, platform, direction, cancellation and trip id. |
| Disruption binary sensor | On when an active service alert matches the watch. Attributes carry the first alert's header, description, cause, effect and active period. |
| Realtime Status | Diagnostic sensor reporting `ok`, `degraded` or `offline`, with last-fetch and error details. |

Per **trip-group board**:

| Entity | Description |
|--------|-------------|
| Departure 1 … N | Ranked merged departures (default 5, maximum 10). Attributes include route, destination/headsign, boarding stop, platform, scheduled/expected time, delay, cancelled/skipped state, realtime state, pin order and labels; `departure_1` also carries the board's `sync_index`. |
| Disruptions | On when any active alert matches any enabled pin. `unavailable` when an alert feed failed, so "off" is trustworthy. |
| Direction | `configured` or `opposite`; runtime-only, resets to configured on restart. |

Per **vehicle/route tracker**: one `device_tracker` per tracked vehicle with latitude/longitude, bearing, speed, vehicle id/label, current stop and status.

Exact entity ids follow Home Assistant's naming; see **Developer tools → States** after setup.

---

## Importing existing watches as a board

You can copy several existing watches into a single **trip-group board** without touching the originals. From **Settings → Devices & services → Add integration → Transport Victoria**, pick **"Trip-Group Board — import from existing watches"**.

The import flow lists your current compatible watches as checkboxes plus a new board name. On submit it creates the board with **one line (pin) per watch** — using the same pin-derivation path as manual board creation, so labels and identity match — then shows a summary of what was imported, what was skipped, and any stale references.

### Non-destructive guarantee

The import is **opt-in and read-only with respect to your existing entries**:

- Source watches are **never deleted, disabled, or modified** — their config data is untouched.
- The only thing created is the new board (and its pin subentries).
- **Rollback = delete the new board.** Removing the board removes its pins and entities; your original watches remain exactly as they were.

### Compatibility matrix

| Watch type | Imports? | Notes |
|------------|----------|-------|
| Departure watch with a specific line | ✅ | One pin per watch |
| Departure watch, all-destinations, single line at the stop | ✅ | Line resolved automatically |
| Departure watch, all lines across several routes | ❌ | No single line — stays as-is (create a board manually) |
| Transfer **journey** (change at stop) | ❌ | Transfer journeys stay as-is |
| Vehicle / route **tracker** | ❌ | Trackers stay as-is |
| Watch whose route/stop/destination no longer resolve | ⚠️ | Imported with a *needs attention* flag — never silently dropped |

Missing/optional fields (platform, colour override, direction) map to explicit empty values rather than being guessed.

---

## Data sources and realtime feeds

All datasets are published by Transport Victoria on the **[open data portal](https://opendata.transport.vic.gov.au/)**.

| Data | Source |
|------|--------|
| Static timetables | [GTFS Schedule](https://opendata.transport.vic.gov.au/dataset/gtfs-schedule) (ZIP, updated regularly) |
| Delays, cancellations, skipped stops | GTFS Realtime Trip Updates |
| Disruptions | GTFS Realtime Service Alerts |
| Vehicle positions | GTFS Realtime Vehicle Positions |

Published realtime feed coverage:

| Mode | Trip Updates | Service Alerts | Vehicle Positions |
|------|:---:|:---:|:---:|
| Metro Trains | ✅ | ✅ | ✅ |
| Trams | ✅ | ✅ | — † |
| Buses (combined Metro & Regional) | ✅ | product-dependent * | ✅ |
| V/Line | ✅ | — | ✅ |

\* The bus Service Alerts product must be enabled for your key under **My Account → Products**; otherwise the endpoint returns HTTP 403 and the integration logs a one-time hint and treats it as unavailable. Enabling the product is picked up on the next successful poll. Regional-bus watches use the combined bus realtime feed (there is no separate regional-bus realtime endpoint).

† The portal publishes no documented tram Vehicle Positions product. The integration therefore does not support tram vehicle tracking; see [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md).

Realtime requests are budgeted per feed (20 requests / 60 seconds — the conservative floor of the portal's published 20–27 range) with a global safety ceiling of 40 requests / 60 seconds across all feeds. Responses are cached for 30 seconds and shared by all entries, so adding boards does not multiply fetches. HTTP 429 honours `Retry-After` (clamped to 60 seconds).

Dataset terms: **CC BY 4.0** where applicable — see the portal's [Public Transport organisation](https://opendata.transport.vic.gov.au/organization/public-transport) for current licence text.

This integration is **not** affiliated with Transport Victoria or the Victorian Government.

---

## Known limitations

The integration is honest about what open data does not contain: no stop facilities beyond wheelchair/platform/parent-station fields, no fare estimates, no myki outlets, no PTV disruption themes, no tram vehicle tracking, and a 45-day `stop_times` window with weekly self-refresh. See **[docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md)** for the user-facing list and **[docs/PARITY.md](docs/PARITY.md)** for the full PTV API parity record and live endpoint evidence.

---

## Development

Clone the repo and use the included Compose stack to run Home Assistant with this component mounted read-only.

**Prerequisites:** [Podman](https://podman.io/) with Compose (or Docker Compose with minor script tweaks). The helper scripts assume `podman compose`.

```bash
./scripts/dev.sh up      # start HA → http://localhost:8123
./scripts/dev.sh logs    # follow logs
./scripts/dev.sh restart # reload after code changes
./scripts/dev.sh reset   # wipe local dev config under dev/ (ignored by git)
./scripts/dev.sh down
```

The `dev/` directory is gitignored and holds your local `ha-config` and downloaded GTFS data.

**Tests** (Python 3.13 in CI):

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements_test.txt
pytest tests/ -q            # run from repository root so `custom_components` imports resolve
```

Strict typing: `mypy` is configured via `mypy.ini` for maintainers who run it locally.

---

## Project layout

```text
custom_components/transport_victoria/   # integration package
tests/                                  # pytest suite
docs/                                   # parity and known-limitation records
.github/workflows/                      # CI: tests, HACS, hassfest
compose.yaml                            # local HA dev service
scripts/dev.sh                          # Podman-focused dev helpers
```

---

## Issues & contributions

Bug reports and pull requests are welcome: [Issues](https://github.com/catatonicChimp/transport_victoria/issues).

---

## Acknowledgements

GTFS and GTFS Realtime are industry-standard formats; this integration uses the **gtfs-realtime-bindings** Python package to decode protobuf feeds.

Thanks to **[bremor/public_transport_victoria](https://github.com/bremor/public_transport_victoria)** for the earlier Home Assistant custom component that brought PTV departures into HA. That project uses the **PTV Timetable API** (developer id + API key). This integration is a separate design: it uses **Transport Victoria open data** (GTFS schedule + GTFS Realtime) instead, but the idea of monitoring Victorian public transport in Home Assistant owes a debt to that work.
