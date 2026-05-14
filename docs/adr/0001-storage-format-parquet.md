##**ADR-0001: Use Parquet for readings with per-(station, parameter) file layout**

**STATUS**: Accepted
**DATE**: 14-05-2026

##**CONTEXT**
The Environment Agency (EA) Hydrology API produces tabular time-series readings: 
one row (station, parameter, timestamp) tuple.
We need to persist these between ingestion runs and make them efficiently queryable from the DuckDB store and the sanity-checks notebook.

Three storage formats were on the table:
- **CSV**
- **JSON**
- **Parquet**

#**Decision**
Use Parquet, one file per (station_guid, parameter) pair, written to a gitignored data folder

*data/raw/readings/{guid}_{parameter}.parquet*

Station metadata (which is nested) lives separately in a *stations.json* file. The manifest log is a small CSV.

#**Consequences**
- Storage size is roughly 5x smaller than CSV and 8x smaller than JSON for equivalent reading data. The full backfill sits under 50MB.
- DuckDB reads Parquet natively. No schema redefinition required.
- Per-file layout makes the ingestion idempotent: a failed run leaves completed files intact; re-running skips them via os.path.exists.

- Negative: 80+ Parquet is fine for now until the (station, parameter) pair count grows

#**Alternatives considered**

- Single append-only CSV. Rejected: not safe under partial failures, expensive to dedupe on re-run, slow to read selectively.

- JSON for readings. Rejected (verbose, untyped). Kept for station metadata where the nested structure is natural.

- SQLite. Plausible but DuckDB is the planned analytical store (ADR-007, deferred to Day 2). Having raw Parquet + DuckDB is cleaner than SQLite-for-everything.