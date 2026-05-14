# Phase 1 Notes — RIPPLE Hydrology Ingest
 
*14-05-2026*
 
## What got built
 
- `ripple/ingest/hydrology.py` — two-phase EA Hydrology API ingestion. Per catchment, lists active stations within the bbox, fetches full detail per station, then for each parameter the station publishes from our wanted list, backfills 6 months of readings.
- `data/raw/readings/` — ~80 Parquet files, ~235k rows total across Tees, Wharfe, Wye.
- `data/raw/manifest.csv` — per-pull log with an explicit status taxonomy (`ok`, `ok_sparse`, `empty_body`, `empty_window`, `error`, `cached`).
## Key findings
 
**Continuous water-quality coverage is concentrated, not distributed.** .
 
| Catchment | Continuous-WQ stations | WQ params with data |
|---|---|---|
| Tees | 1 (`E06388A`) | 6/6 (DO, ammonium, temperature, conductivity, pH, turbidity), ~3,800 rows each |
| Wharfe | 0 | — |
| Wye | 1 (temperature only) | 9 rows total |
 
Everything else in the manifest is `waterFlow` from level/flow gauges. The three-equal-case-studies framing in the original proposal doesn't survive this. See ADR-004.
 
**Daily aggregate measures are everywhere and were being silently preferred over 15-min instantaneous.** Many stations publish flow as both `flow-i-900-m3s-qualified` (15-min) and `flow-{m,min,max}-86400-m3s-qualified` (daily mean / min / max). The initial `pick_measure_id` returned whichever appeared first; after fixing the sort to prefer `valueType == "instantaneous"` and smaller `period`, the `ok_sparse` count drops sharply.
 
**Empty-body responses are real and common.** 12 of 79 pulls returned 0-byte CSV bodies — the EA's silent failure mode for measure paths that station metadata lists but the readings endpoint doesn't actually resolve. Distinguishing these from `empty_window` (real measure, no rows in window) is the reason for the status taxonomy in ADR-005.
 
## API quirks worth knowing (and that cost time today)
 
- The list endpoint at `/id/stations` returns abbreviated `measures` in its default view; the single-station endpoint returns full nested data. `_view=full` is *not* valid on `/id/stations` (returns HTTP 400) — only `default` and `minimal` are accepted there.
- Parameter slugs are inconsistent. Water-quality parameters use kebab-case (`dissolved-oxygen`); hydrological parameters use camelCase (`waterFlow`, `waterLevel`).
- `parameterName` is human-readable with spaces (`"Dissolved Oxygen"`); the URL slugs use hyphens or camelCase. Substring matching on `parameterName` silently fails.
- Closed stations still appear in lat/long results with notional measures listed. Filter on `status.label == "Active"` at discovery.
- Co-located WiSki measurement points share a `stationGuid` but get disambiguated `notation`s, so the same station can appear twice in a list result. Dedupe in `list_stations`.
## Decisions recorded
 
- ADR-001: Storage format — Parquet, per-(station, parameter) file layout
- ADR-002: Two-phase station discovery
- ADR-003: Measure matching via `observedProperty.@id` URL slug
- ADR-004: Reframe case studies — Tees as primary
- ADR-005: Manifest CSV and status taxonomy
- ADR-006: Filter to active stations at discovery
## What's next (Day 2)
 
1. **Add `waterLevel` and `rainfall` to `PARAMETERS`.** Unlocks the ~140 stations per catchment that currently publish none of our wanted parameters, and adds the natural covariates for separating rainfall-driven from CSO-driven anomalies in Week 2.
2. **Decide between expanding the Wharfe and Wye radii vs leaning on WIMS lab data for those catchments.** Probably both. Check WIMS coverage first.
3. `ripple/store.py` — DuckDB schema, bulk-load the Parquet glob into the `readings` table.
4. `ripple/ingest/edm.py` — EDM annual returns. The Excel parsing is the genuinely fiddly piece of Day 2.
5. `ripple/ingest/rivers_trust.py` — ArcGIS feature layer for near-real-time CSO alerts.
## Open questions
 
- Whether to fold WIMS lab samples into the same `readings` table or keep them separate. They're laboratory-frequency, not continuous, so a unified schema risks confusing the modelling. Decide on Day 2 when EDM and WIMS get implemented together.
- Whether the Tees `E06388A` station has data quality issues (calibration drift, sensor gaps) that would weaken the Tees case study. Day 7's sanity-checks notebook is the right place to triage.
- Whether the 28-row-per-6-months stations (visible as `ok_sparse` in the current manifest) are legitimate weekly-resolution series or broken sensors. Spot-check one of them before trusting them in Week 2.
## Time spent
 
About six hours, mostly on the four silent-failure debugging cycles. The functional code at the end is ~200 lines; getting there required understanding three undocumented behaviours of the EA API. Worth keeping in mind when scoping similar ingestion work in future — the time multiplier for "well-documented public API" vs "poorly-documented public API" is at least 3×.