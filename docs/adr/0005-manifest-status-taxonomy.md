# **ADR-005: Manifest CSV with explicit status taxonomy**
 
**STATUS**: Accepted
**DATE**: 14-05-2026
 
## **CONTEXT**
 
The EA Hydrology API has multiple distinct failure modes that all surface as "no data" from a naive script:
 
| Cause | API response |
|---|---|
| Measure path doesn't exist | 0-byte body, HTTP 200 |
| Real measure, no rows in window | CSV header row only |
| Station closed before window opened | Same as above |
| 404 | HTTP 404 (rare in practice) |
| Transient server error | 5xx (retried via tenacity) |
 
These look identical to an unstructured ingestion: an empty DataFrame either way. During Day 1 debugging we hit "silent skip" failures twice — once from a hyphen/space mismatch in measure-name matching, once from abbreviated list-view measures — both of which produced zero log lines because the failure path didn't emit one. Each cost an hour to diagnose.
 
There's a similar need on the success side: a "successful" 28-row pull from a daily-aggregate flow measure looks the same as a "successful" 17,000-row pull from a 15-min instantaneous measure if we only record "ok / not ok". The two have very different downstream usefulness.
 
## **DECISION**
 
Every `(station, parameter)` ingestion attempt writes a row to `data/raw/manifest.csv` with an explicit `status` value drawn from a closed taxonomy:
 
| Status | Meaning |
|---|---|
| `ok` | Got useful data, row count ≥ `MIN_USEFUL_ROWS` (1,000). |
| `ok_sparse` | Got data but below the row-count heuristic. Includes daily-aggregate series (whose complete 6-month form is ~180 rows). |
| `empty_window` | Measure exists, no rows in the requested period. CSV came back with a header row only. |
| `empty_body` | 0-byte body. Measure path probably doesn't exist; the API's silent failure mode. |
| `not_found` | HTTP 404 from the readings endpoint. |
| `error` | Any other exception, with the exception type name in the value. |
| `cached` | File already existed on disk; skipped. |
 
The manifest appends across runs (with a `run_ts` column) so we have a complete log of what's happened over time, not just the most recent state.
 
## **CONSEQUENCES**
 
- Silent failures become loud. Any `(station, parameter)` pair that produces no Parquet file appears in the manifest with a status that tells you why. The empty-manifest crash of Day 1 cannot recur — `pick_measure_id` returning `None` for every station would now show as 100+ `no_measure` rows in the manifest.
- The taxonomy is a small public schema for the ingestion pipeline. Downstream code (DuckDB load, sanity-checks notebook, Week 2 modelling) can filter on it to e.g. "use only stations with `ok` status this run".
- `groupby("status").size()` produces an at-a-glance health summary at the end of each run.
- Negative: the taxonomy will need extending as new failure modes are discovered. Day 1 covered the major ones; Day 2's EDM and Rivers Trust ingest will likely add new ones.
## Alternatives considered
 
- **Log file only.** Lower-friction but harder to query. The CSV is structured enough to grep, group, and filter in five seconds; a log file is not.
- **Database table.** Premature on Day 1. Once DuckDB exists (Day 2) the manifest CSV can be loaded into it cheaply if needed.
- **Single boolean `ok` flag per row.** Loses the distinction between the different failure modes, which is the whole point — that distinction is what made the Day 1 bugs diagnosable.