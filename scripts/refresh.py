"""
scripts/refresh.py
 
Backfill EA Hydrology readings for the three RIPPLE case-study catchments.
 
  - Discovers ACTIVE stations within each catchment circle.
  - For each (station, parameter) it publishes, fetches readings into a
    per-pair Parquet file under data/raw/readings/.
  - Writes data/raw/stations.json (full discovery output) and
    data/raw/manifest.csv (per-pull log: rows, status, errors).
 
The script is idempotent: existing Parquet files are skipped, so re-runs
pick up where a failed run stopped. Set --force to overwrite.
 
Usage:
    python scripts/refresh.py                  # 6-month backfill, all catchments
    python scripts/refresh.py --dry-run        # discovery only, no readings
    python scripts/refresh.py --months 12      # custom window
    python scripts/refresh.py --catchment tees # one catchment only
    python scripts/refresh.py --force          # overwrite cached files
 
This is the script wired up to GitHub Actions / cron in Weeks 3-4.
"""
 
from __future__ import annotations
 
import argparse
import json
import logging
import sys
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path
 
import pandas as pd
from dateutil.relativedelta import relativedelta
 
# Allow `python scripts/refresh.py` from repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
 
from ripple.config import CATCHMENTS, PARAMETERS
from ripple.ingest.hydrology import (
    discover_stations,
    fetch_readings,
    pick_measures_for_parameter,
)
 
DATA_DIR = Path("data/raw")
STATIONS_FILE = DATA_DIR / "stations.json"
READINGS_DIR = DATA_DIR / "readings"
MANIFEST_FILE = DATA_DIR / "manifest.csv"
 
REQUEST_DELAY_SECONDS = 1.0   # polite -- EA's rate limit is undocumented but ~1/s is fine
MIN_USEFUL_ROWS = 1_000       # heuristic from the Week 1 plan
 
 
# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------
 
def discover_all(catchments: dict, parameter_filter: set[str]) -> list[dict]:
    """
    Walk each catchment circle, return a flat list of station records
    tagged with their catchment name. Stations are filtered to ACTIVE
    only inside discover_stations().
    """
    records: list[dict] = []
    for name, cfg in catchments.items():
        stations = discover_stations(
            lat=cfg["lat"],
            lon=cfg["lon"],
            dist_km=cfg["dist"],
            parameter_filter=parameter_filter,
        )
        for s in stations:
            rec = asdict(s)
            rec["catchment"] = name
            records.append(rec)
    return records
 
 
def save_stations(records: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATIONS_FILE.write_text(json.dumps(records, indent=2, default=str))
    logging.info("Wrote %d stations to %s", len(records), STATIONS_FILE)
 
 
# --------------------------------------------------------------------------
# Readings backfill
# --------------------------------------------------------------------------
 
def backfill_one(
    catchment: str,
    station_guid: str,
    measures: list[dict],
    parameter: str,
    start_date: date,
    end_date: date,
    force: bool,
) -> dict:
    """Fetch one (station, parameter) pair. Returns a manifest row dict."""
    out_path = READINGS_DIR / f"{station_guid}__{parameter}.parquet"
 
    matches = pick_measures_for_parameter(measures, parameter)
    if not matches:
        return {
            "catchment": catchment, "station_guid": station_guid,
            "parameter": parameter, "measure_notation": "",
            "n_rows": 0, "status": "no_measure",
        }
 
    measure = matches[0]
    notation = measure["notation"]
 
    if out_path.exists() and not force:
        # Don't refetch; report cached row count for the manifest.
        try:
            n_rows = len(pd.read_parquet(out_path, columns=["ts"]))
        except Exception:
            n_rows = 0
        return {
            "catchment": catchment, "station_guid": station_guid,
            "parameter": parameter, "measure_notation": notation,
            "n_rows": n_rows, "status": "cached",
        }
 
    try:
        df = fetch_readings(notation, start_date, end_date)
    except ValueError as e:
        logging.warning("%s/%s: %s", station_guid, parameter, e)
        return {
            "catchment": catchment, "station_guid": station_guid,
            "parameter": parameter, "measure_notation": notation,
            "n_rows": 0, "status": "empty_body",
        }
    except Exception as e:
        logging.exception("%s/%s: %s", station_guid, parameter, e)
        return {
            "catchment": catchment, "station_guid": station_guid,
            "parameter": parameter, "measure_notation": notation,
            "n_rows": 0, "status": f"error:{type(e).__name__}",
        }
 
    if df.empty:
        return {
            "catchment": catchment, "station_guid": station_guid,
            "parameter": parameter, "measure_notation": notation,
            "n_rows": 0, "status": "empty_window",
        }
 
    # Tag rows with their context so we can read globs into one table later.
    df = df.assign(
        station_guid=station_guid,
        parameter=parameter,
        catchment=catchment,
        unit=measure.get("unitName", ""),
    )
 
    READINGS_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)
    logging.info("wrote %d rows -> %s", len(df), out_path.name)
 
    return {
        "catchment": catchment, "station_guid": station_guid,
        "parameter": parameter, "measure_notation": notation,
        "n_rows": len(df),
        "status": "ok" if len(df) >= MIN_USEFUL_ROWS else "ok_sparse",
    }
 
 
def backfill_all(
    records: list[dict],
    parameters: list[str],
    start_date: date,
    end_date: date,
    force: bool,
) -> pd.DataFrame:
    """Iterate every (station, parameter) pair, return a manifest DataFrame."""
    rows: list[dict] = []
    for rec in records:
        for parameter in parameters:
            row = backfill_one(
                catchment=rec["catchment"],
                station_guid=rec["station_guid"],
                measures=rec["measures"],
                parameter=parameter,
                start_date=start_date,
                end_date=end_date,
                force=force,
            )
            rows.append(row)
            if row["status"] not in ("cached", "no_measure"):
                time.sleep(REQUEST_DELAY_SECONDS)
 
    manifest = pd.DataFrame(rows)
 
    # Append to historical manifest rather than overwriting -- the log of
    # what's happened across runs is useful when debugging Week 2 surprises.
    if MANIFEST_FILE.exists():
        prior = pd.read_csv(MANIFEST_FILE)
        manifest_out = pd.concat([prior, manifest.assign(run_ts=pd.Timestamp.utcnow())], ignore_index=True)
    else:
        manifest_out = manifest.assign(run_ts=pd.Timestamp.utcnow())
 
    manifest_out.to_csv(MANIFEST_FILE, index=False)
    return manifest
 
 
# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
 
def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
 
 
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Discover and save stations.json; skip readings backfill.")
    parser.add_argument("--months", type=int, default=6,
                        help="History window in months (default 6).")
    parser.add_argument("--catchment", choices=list(CATCHMENTS), default=None,
                        help="Limit to one catchment. Default: all three.")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite cached Parquet files instead of skipping.")
    args = parser.parse_args()
 
    setup_logging()
 
    end_date = date.today()
    start_date = end_date - relativedelta(months=args.months)
    logging.info("Window: %s -> %s (%d months)", start_date, end_date, args.months)
 
    catchments = (
        {args.catchment: CATCHMENTS[args.catchment]}
        if args.catchment
        else CATCHMENTS
    )
 
    parameter_filter = set(PARAMETERS)
    records = discover_all(catchments, parameter_filter)
    save_stations(records)
 
    if args.dry_run:
        logging.info("dry-run: %d stations discovered, skipping readings", len(records))
        return 0
 
    if not records:
        logging.error("No active stations discovered. Check catchment bounding boxes.")
        return 1
 
    manifest = backfill_all(records, PARAMETERS, start_date, end_date, args.force)
 
    # Summary
    summary = manifest.groupby("status").size().to_dict()
    logging.info("Done. Status counts: %s", summary)
    logging.info("Total rows written: %d", int(manifest["n_rows"].sum()))
 
    return 0
 
 
if __name__ == "__main__":
    raise SystemExit(main())