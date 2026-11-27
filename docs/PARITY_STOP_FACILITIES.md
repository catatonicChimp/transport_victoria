# Parity decision record: stop facilities (T12)

**Verdict: NOTHING-VIABLE.** The Transport Victoria Open Data portal publishes no stop
facilities dataset. Investigated 27 Nov 2026 against the full portal catalogue
(40 CKAN packages via `/api/3/action/package_list`).

## Evidence

- The only stop-level dataset on the portal ("Public Transport Lines and Stops",
  resource `6d7a28c4-9839-4590-baea-053b690ca167`, CC BY 4.0, updated weekly,
  last modified 27 Nov 2026) is a **weekly GeoJSON re-export of GTFS `stops.txt`**:
  32,055 features, every feature carrying only `STOP_ID`, `STOP_NAME`, `MODE`
  and a coordinate.
- **Join test (performed on downloaded data):** GeoJSON `STOP_ID` set vs all eight
  per-operator GTFS `stops.txt` files = identical on both sides
  (31,061 unique stop ids; 0 only-in-GeoJSON; 0 only-in-GTFS; 0 coordinate
  mismatches at 1e-5°). Examples: `11212 Flinders Street Station`,
  `20043 Southern Cross Station`, tram and bus stops all matched exactly.
- Field coverage of the GeoJSON is a **subset** of the GTFS data already imported:
  it drops `parent_station`, `location_type`, `wheelchair_boarding`, `platform_code`
  and `stop_url`. Enriching from it would be a no-op.
- Portal searches for facilities/amenities/shelter/park-and-ride/myki outlets all
  return 0 results; slug probes (`stop-facilities`, `station-facilities`,
  `myki-outlets`, `ticket-outlets`, `park-and-ride`) all 404. Neither the state
  CKAN portal (discover.data.vic.gov.au) nor a direct catalogue review produced any
  stop-facility dataset. The rich amenity/accessibility/staffing/contact/ticketing
  detail exists only in the key-gated PTV Timetable API `/v3/stops/{id}` — an API,
  not an open dataset, and out of scope for a no-credential enrichment.

## Consequence

Stop detail in this integration is documented as **GTFS-only**: name, coordinates,
`location_type`, `parent_station`, `platform_code`, `stop_url` and
`wheelchair_boarding` (0/1/2, with roughly 1,365 stops blank). The current TV GTFS
feeds contain **no `zone_id`**.

Amenity, accessibility-detail, staffing, contact and ticketing information is not
available from open data. This is a **known limitation**, not a hidden PTV API
dependency; the integration must return `unknown`/absent for these attributes rather
than synthesise them.
