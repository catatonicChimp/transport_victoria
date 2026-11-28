# Config Flow Design v2 — ownership first, then screens

**Status:** replaces the v1 design (9601ab4) after an independent review. This document is the map;
every config-flow PR updates §3 (current state), §5 (screens) and §7 (packages) in the same PR.
**The maintainer's directive:** Add Integration → **API key (optional)** → **schedule download** —
and that is the whole of the main flow. Everything else (watches, boards, journeys, route trackers,
imports) is sub-setup inside the integration afterwards, added via its Configure panel.

---

## 1. The whack-a-mole ledger (observed defects → root-cause classes)

| Observed (live) | Root-cause class | ID |
|---|---|---|
| Radio wall of ~20 "X via Y" candidates | List rendering without searchable selection | C1 |
| "Search routes" box that does nothing | Dead companion field from a pre-selector era | C2 |
| Infinite "importing" spinner | Background-task failure not surfaced to the flow | C3 |
| "Vehicle positions fetch failed: 404" retried forever | Documented missing feed raised as a hard error (capability matrix not enforced) | C4 |
| Board options = menu → submenu crawl | Navigation sprawl | C5 |
| Board name field on a separate screen; key re-typed per feature | State reuse absent | C6 |
| Duplicate destination labels | Candidate model collapse by display text | C7 |
| "vehicle_vehicle_vehicle" one-trip tracker | Legacy surface without capability gating | C8 |
| **Each feature wizard re-asks for credentials; key failure kills feature setup** | **No ownership model: credential and schedule lifecycle live in feature entries** | **C9** |
| **A watch created during an import shows entities that later silently vanish** | **No readiness contract: "configured" and "usable" are conflated** | **C10** |
| **Deleting/disabling one feature's relationship to the data source is undefined** | **No deletion/reload cascade rules** | **C11** |

C9–C11 were added after the independent review — they are the classes the whack-a-mole pattern comes
from, and fixing individual screens does not close them.

## 2. Constraints & corrections ledger (verified on HA 2026.2.3 or by code inspection)

1. Selector policy: lists >6 → searchable dropdown; ≤6 → list. Dropdown search is client-side only —
   it cannot recover candidates the server discarded (a server-side cap like "top 100" must not be
   combined with "the dropdown is searchable, so fine").
2. Real-HA flow tests in the `ptv-p12-harness` venv are mandatory for config-flow changes; the light
   env stubs the flow manager and has masked the `async_step_<step_id>` routing twice already.
3. **Subentries**: one subentry per flow run is a limitation of the *terminal result only*. A pending
   pin can be committed with `async_add_subentry` and the flow may continue (this is the adopted
   "Add another line" mechanism) — multiple explicit subentry writes in one run are possible. Do
   not describe multi-add as impossible; describe which pattern is used. Nested subentries
   (board → pin) do not exist in HA — boards keep their own pin subentries.
4. **Options updates do not inherently reload** — integration listeners decide. Watch options currently
   lack a listener; board options reload on count changes. Runtime direction toggling remains
   separate by *semantics* (it must not rewrite config), not by HA limitation.
5. **Cameras of capability**: separate four states per mode/feed — *supported by this integration*,
   *credential required*, *portal product entitlement*, *current feed health*. "Tram vehicle
   tracking" is **unsupported by this integration** (an undocumented tram vehicle-positions endpoint
   exists in production but is deliberately unconsumed). "Tram has no vehicle feed" was imprecise.
6. **Feasibility checks required before claiming UI interactions**: options-flow sections group
   fields but do not provide inline ordered-list editors, clickable pin previews, dependent dropdowns
   or transactional multi-object edits. Any of those needs a real-HA prototype first.
7. Label-text-only collapse of destinations is **rejected** (identical labels can encode distinct
   patterns). Dedupe by semantic identity; improve labels to distinguish what remains.

## 3. Current state (corrected map — accurate to `config_flow.py`)

- `user` collects **only the API key** (vol.Required today) and routes to `entry_type`.
- `entry_type` (dropdown) routes to: watch branch (`downloading` → `stop` → `route` → `platform` →
  `dest` → `confirm`), journey branch (`journey` → `journey_dest` → `confirm`), route-tracker branch
  (`route_tracker_mode` → `_route` → `_filter` → `_stop` → `_proximity` → `_confirm`), import branch
  (`import` → `import_summary`), and **board parent creation (immediately), with pins added
  afterwards as subentries** (`origin` → `to` — wait, the board branch creates the parent entry then
  drives pin subentry flows; `abort_setup` exists for unready schedules; the pin flow is
  `user` → `origin` → `to` → `select_lines` → `options` → `add_another`/`finish`; `reconfigure` for
  pin edit) — and options flows: `init` → `board_menu` (single form) → `board_settings` /
  `board_reorder` / `board_remove`; watch options = `watch_step`.
- Failure/retry branches (`downloading` → `schedule_failed` → `retry_download`) are their own
  reachable steps, not sequential forms.
- Credentials are duplicated into every feature entry; an absent/failed key kills feature setup
  (C9); a watch can be created mid-import and shows entities that never become trustworthy (C10).

(The v1 §3 map generated from step-grep was wrong on these exact points — method-name greps cannot
produce a truthful transition map. Keep maps hand-maintained.)

## 4. Capability matrix (verified live; wording per §2.5)

| Capability | Metro train | Tram | Bus (incl. regional) | V/Line regional |
|---|---|---|---|---|
| Schedule departures | ✅ | ✅ | ✅ (short workings → via-hints) | ✅ |
| Destination candidates from patterns | ✅ | ✅ (loops → via hints) | ✅ (headsign identity dedupe) | ✅ |
| Realtime delays | ✅ | ✅ | ✅ | ✅ |
| Cancellations | ✅ | ✅ | ✅ | ✅ |
| Service alerts (consumed) | ✅ | ✅ | ⚠️ portal product scope — 403 until enabled | ❌ not consumed (feed exists? verified 403) |
| Vehicle positions (consumed) | ✅ | ❌ unsupported by this integration | ✅ (combined incl. regional) | ✅ |
| Fleet device trackers | ✅ | ❌ | ✅ (route-scoped, cap 30) | ✅ |
| Journey (multi-leg) | ✅ | ✅ | ✅ | ✅ |

Realtime **consumption** is a per-feature integration decision layered on `credential configured` ×
`realtime requested` × `credential usable` × `feed health` (T13 taxonomy).

## 5. Ownership & readiness contract (NEW — first-class)

### 5.1 The source entry (hub)

One **source config entry** (new type, e.g. `entry_type: "source"`) owns:
- the **credential** (`api_key`, optional — see 5.2),
- the **schedule lifecycle** (download → validate → import → generations),
- the **bundle policy** (v1: import **all** network bundles at first setup — matches demonstrated
  capacity; per-mode on-demand imports are a documented future refinement),
- the **readiness state machine** for the whole integration.

State machine (exposed as the source's diagnostic state):

```
CONFIGURED → DOWNLOADING (bytes %) → IMPORTING (stages) → READY
     ↺ retry / cancelled → CONFIGURED
DOWNLOADING/IMPORTING failure → SCHEDULE_FAILED (retry menu) → CONFIGURED
READY + calendar expired → STALE → refresh → READY|FAILED
READY + generation unusable/corrupt → force re-import → READY
```

"Configured" = settings saved. "READY" = a usable generation exists. Features require READY (not
CONFIGURED) before creating entities; features created while !READY are allowed and simply wait
(entities unavailable, a repair visible), so the C10 class is closed by contract.

### 5.2 Feature entries

Watches, boards, journeys and route trackers remain **separate config entries** (native patterns
preserved) but:
- are explicitly **associated with the source entry** (`source_entry_id` in their data,
  auto-set at creation from the single live hub; migration sets it retroactively),
- no longer store credentials themselves (C9 closed: one credential, one lifecycle),
- have their realtime usage gated on **effective capability** = `realtime requested for THIS
  feature` AND `credential configured` AND `credential usable` (T13 feed health still applies per
  feed) — "requested but no credential" is a normal **scheduled-only** state, not an error,
- when the source is deleted → a confirmed cascade removes dependent features (deletion lists them
  first); when the source is disabled → features go **unavailable** (not deleted), and reload on
  re-enable.

### 5.3 Credential states (keyless is a supported runtime mode)

| State | Meaning | Runtime |
|---|---|---|
| Credential absent | Intentional scheduled-only | Departures work (scheduled); disruption sensors **unavailable/not-created** (never a false OFF); vehicle/fleet trackers **not created**; no 401 retry logs; scheduled-only banner on entities |
| Credential present + usable | Normal realtime | Full features |
| Credential present + invalid/revoked | Repair due | Departures stay available; repair invites fixing the key; scheduled fallback continues without a reauth storm |

Rules: an absent credential must never issue a request with an empty `KeyID`; adding/rotating/
removing the key via **source reconfigure** rebuilds the client + resets feed-cache/health state;
intentional keyless operation must not log as an error (T13 taxonomy handles real 401/403).

### 5.4 Migration into the contract

On integration start, if feature entries exist but no source entry → auto-create the source from
the first feature's credentials (the existing carry-over logic, institutionalised), set
`source_entry_id` on all features, preserve every unique id, and import missing bundles before any
feature requires READY (features created while !READY wait per 5.1).

## 6. Target screens (v2)

### 6.1 Setup = two surfaces, nothing chained into the dialog

```mermaid
flowchart TD
    A[Add Integration → Transport Victoria] --> B[Screen 1: name<br/>+ API key: ENABLE REALTIME (recommended) / SCHEDULE ONLY]
    B -->|either| C[Screen 2: schedule download<br/>percentage + MiB + failure menu]
    C --> D[Source entry: CONFIGURED → READY]
    D --> E[Configure panel:<br/>Add something to monitor]
    style B fill:#2b3b4e
```

- "Add something to monitor" opens the menu: Watch / Board / Journey / Route tracker / Import.
- Feature wizards are unchanged in shape from the T17 UX (search dropdowns, one coherent task per
  screen, review at the end, Save pin as the normal endpoint with optional "Add another line").
- The hub's Configure also hosts **Source reconfigure** (add/replace/remove credential + force
  schedule refresh + bundle status).

### 6.2 Candidate identity rule (correcting the v1 collapse)

Deduplication is by **semantic identity**: `(direction_id, terminal_stop_id, via_identity)` where
`via_identity` is the first divergent intermediate stop of the pattern, not a decorative hint.
Identical semantic keys collapse; distinct ones survive with an improved distinguishing label.
The label-text-only collapse introduced in 53f813a is replaced by this rule.

---

## 7. Packages (re-cut as complete journeys; dropdown polish folds into each)

| # | Package | Acceptance highlights | Effort |
|---|---|---|---|
| **T18.1** | **Ownership/readiness prototype**: source entry type + association field + single-hub rule + deletion cascade + migration from existing entries (auto-hub) + native-UI feasibility spike (hub Configure hosting feature-creation flows) | Real-HA test: create hub → add watch → hub reconfigure changes credential → watch reloads with new credential, unique ids preserved; hub delete with confirm lists dependents | L (oracle review) |
| **T18.2** | **Keyless vertical slice**: optional credential end-to-end — flow choice, coordinator guards (no empty-KeyID requests), disruption-unavailable semantics, fleet hidden, scheduled-only banner, add-later reconfigure, remove-key safety | Real-HA test: schedule-only watch works fully; add key → realtime turns on; remove key → scheduled-only, no reauth storm | M |
| **T18.3** | **Schedule onboarding/recovery under the hub**: bundle policy (import-all v1), readiness state machine + diagnostic state, progress phases (bytes → validation → import), retry/cancel/restart/concurrent-wizard tests | Real-HA test: cancel mid-wizard while shared import continues; wizard reopened shows correct readiness; restart during import recovers | M |
| **T18.4** | **Feature/pin creation journeys**: T17 UX + semantic candidate identity + label improvement + bounded search (server cap must not orphan candidates) + one-coherent-task screens | Destination screen: ≤ searchable N candidates, no label duplicates among distinct options — verified against live GTFS | M |
| **T18.5** | **Management + migration**: options sections (Settings/Pins/Remove), reorder, import flow into boards, legacy surface removal, release docs | Real-HA tests; every legacy entry type either works w/ new contract or is retired with a migration note | S/M |

**Order:** 1 → 2 → 3 → 4 → 5. No screen redesign PRs before T18.1 lands.

## 8. What "done" looks like

Pasting a key (or choosing Schedule Only) once; the timetable arriving with visible progress; every
feature added afterwards from the integration's Configure panel without re-entering credentials; a
schedule that heals itself; a mode matrix that is enforced by the flow; and zero screens that can
silently mislead. The maintainer's shape — *add integration → key → download → configure inside* —
delivered by contract, not by promise.
