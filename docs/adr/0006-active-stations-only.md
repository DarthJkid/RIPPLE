# **ADR-006: Filter to active stations at discovery time**
 
**STATUS**: Accepted
**DATE**: 14-05-2026
 
## **CONTEXT**
 
The EA's `/id/stations` endpoint, filtered by `lat`, `long`, `dist`, returns stations regardless of their operational status. Stations carry a `status.label` field with values `Active`, `Suspended`, or `Closed`.
 
Worked example from Day 1: station `E09235A` ("Brompton Beck Bridge House Farm") opened on 2019-04-08 and closed on 2019-07-09 — three months of operation, ending nearly seven years before today. It still appears in lat/long searches, its `measures` array still lists notional timeseries, and its `dateOpened` / `dateClosed` fields are visibly populated. Readings queries against those listed measures return either zero bytes (for measure paths that no longer resolve) or zero rows (for ones that do).
 
If we don't filter at discovery, the ingestion:
 
- wastes API budget on dead stations;
- pollutes the manifest with `empty_body` / `empty_window` rows that *look like* a bug in our ingestion logic;
- pollutes the readings table with data only relevant to obsolete monitoring programmes;
- forces every downstream consumer (DuckDB load, sanity-checks notebook, Week 2 modelling) to repeat the same filter.
The status field is part of the standard list response (no extra API call needed). Filtering at discovery costs nothing.
 
## **DECISION**
 
Both `list_stations` (the first phase of two-phase discovery) and the per-station detail handling in `discover_stations_in_catchment` enforce:
 
```python
status_field = item.get("status") or []
if isinstance(status_field, dict):
    status_field = [status_field]
status_label = status_field[0].get("label", "Unknown") if status_field else "Unknown"
if status_label.lower() != "active":
    continue
```
 
Closed and suspended stations are dropped during discovery and never receive a readings query. Their GUIDs are not retained.
 
The `status` field is sometimes returned as a list of dicts and sometimes as a single dict — both shapes are handled.
 
## **CONSEQUENCES**
 
- Eliminates a large class of `empty_body` and `empty_window` rows in the manifest. These can still occur for active stations with broken-but-listed measures, but at a much lower rate, so they retain diagnostic value.
- Operational status is part of the discovery contract; the data layer never has to reason about it.
- The decision is local to discovery and reversible — if a future analysis wanted historical data from closed stations (e.g. baselines from a previously well-monitored reach), it would either lift this filter for that one query or read directly from a different EA archive product (WIMS, NRFA).
- Negative: if the EA's status field is stale (a closed station still marked `Active`, or vice versa), the filter is wrong in either direction. No way to detect without spot-checks. Low risk: the status field is the EA's own internal categorisation and is updated reasonably promptly.
## Alternatives considered
 
- **Filter at the readings layer instead.** Wastes API calls; also makes the manifest noisier and harder to spot real bugs in.
- **Use `dateClosed`/`dateOpened` directly** instead of the categorical status. Equivalent in practice but more code, no benefit.
- **Apply the filter at the DuckDB load step on Day 2.** Equivalent for downstream consumers but doesn't fix the wasted-API-calls problem.