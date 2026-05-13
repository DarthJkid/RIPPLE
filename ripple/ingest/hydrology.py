import os
import io
import time
import logging
import requests
import pandas as pd
from datetime import date
from dateutil.relativedelta import relativedelta
from tenacity import retry, wait_fixed, stop_after_attempt, retry_if_exception
 
# ==========================================
# 1. SETUP & CONFIG
# ==========================================
 
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)
 
os.makedirs("data/raw/readings", exist_ok=True)
 
# Three case-study catchments. lat/lon centre + radius in km. Picked for
# contrasting hydrology, contrasting media profiles, and (Tees) local
# relevance for the "why this project" interview answer.
CATCHMENTS = {
    "tees":   {"lat": 54.575, "lon": -1.235, "dist": 25},
    "wharfe": {"lat": 53.925, "lon": -1.823, "dist": 15},
    "wye":    {"lat": 51.913, "lon": -2.583, "dist": 30},
}
 
# Parameters to pull. These kebab-case slugs are the contract the API uses
# in BOTH the `observedProperty` query parameter AND the trailing segment
# of each measure's `observedProperty.@id` URL. Match measures to wanted
# parameters on THIS slug -- not on `parameterName`, which is human-readable
# ("Dissolved Oxygen") and inconsistent with the kebab-case query value.
PARAMETERS = [
    "dissolved-oxygen",
    "ammonium",
    "temperature",
    "conductivity",
    "ph",
    "water-flow",
    "turbidity",
]
 
# When a station publishes a parameter in multiple units, prefer this one.
# DO is the main case (often both '%' saturation and 'mg/L'). mg/L is more
# directly interpretable for pollution-signature work.
PREFERRED_UNITS = {"dissolved-oxygen": "mg/L"}
 
# 6-month rolling window anchored to today. Long enough to fit a seasonal-ish
# baseline in Week 2 without overwhelming a laptop.
TODAY = date.today()
END_DATE = TODAY.isoformat()
START_DATE = (TODAY - relativedelta(months=6)).isoformat()
 
# Polite request spacing. EA's limit is undocumented but ~1/s is reliable.
REQUEST_DELAY_SECONDS = 1.0
 
# Sparse-station heuristic from the Week 1 plan: under this many readings
# in the window, the station confuses seasonal baselines. Logged but the
# Parquet file is still written -- you may want sparse stations later for
# spatial coverage of the catchment.
MIN_USEFUL_ROWS = 1_000
 
 
# ==========================================
# 2. HTTP RETRY POLICY
# ==========================================
 
def is_retryable_error(exception):
    """
    Retry on 5xx server errors and transient network failures only.
    4xx is our fault (bad URL, bad params) -- never retry those.
    """
    if isinstance(exception, requests.exceptions.HTTPError):
        return 500 <= exception.response.status_code < 600
    return isinstance(exception, (requests.Timeout, requests.ConnectionError))
 
 
# ==========================================
# 3. CORE INGEST FUNCTIONS
# ==========================================
 
@retry(wait=wait_fixed(5), stop=stop_after_attempt(3), retry=retry_if_exception(is_retryable_error))
def discover_stations(lat, lon, dist, observed_property):
    """
    Find ACTIVE stations within `dist` km of (lat, lon) that publish
    `observed_property` (e.g. 'dissolved-oxygen').
 
    Returns a list of dicts:
        station_guid, name, river, lat, lon, status, measures
 
    Closed and suspended stations are filtered out -- they often still
    appear in lat/lon results with notional measures listed, but have
    no recent data.
    """
    url = "https://environment.data.gov.uk/hydrology/id/stations.json"
    params = {
        "lat": lat,
        "long": lon,  # API uses 'long', not 'lon'
        "dist": dist,
        "observedProperty": observed_property,
        "_limit": 500,
    }
 
    response = requests.get(url, params=params, timeout=30)
    if response.status_code == 404:
        return []
    response.raise_for_status()
 
    items = response.json().get("items", [])
    results = []
    for item in items:
        # `status` is usually a list of dicts but occasionally a single dict.
        status_field = item.get("status") or []
        if isinstance(status_field, dict):
            status_field = [status_field]
        status_label = status_field[0].get("label", "Unknown") if status_field else "Unknown"
 
        if status_label.lower() != "active":
            continue  # skip Closed / Suspended -- see gotcha #1
 
        results.append({
            "station_guid": item.get("stationGuid") or item.get("notation"),
            "name": item.get("label"),
            "river": item.get("riverName"),
            "lat": item.get("lat"),
            "lon": item.get("long"),
            "status": status_label,
            "measures": item.get("measures", []),
        })
 
    time.sleep(REQUEST_DELAY_SECONDS)
    return results
 
 
def pick_measure_id(measures, parameter):
    """
    Return the `notation` of the measure on this station that matches
    `parameter` (a kebab-case slug). Returns None if none match.
 
    Why this isn't a substring match on parameterName: parameter is
    'dissolved-oxygen' (hyphen), parameterName is 'Dissolved Oxygen'
    (space). `"dissolved-oxygen" in "dissolved oxygen"` is False, so
    naive substring matching silently fails on every station and you
    write zero Parquet files. See Day 1 gotcha #2.
 
    The reliable contract is observedProperty.@id, whose trailing URL
    segment is the exact kebab-case slug we use in queries.
 
    When multiple measures match (DO often has both '%' and 'mg/L'),
    PREFERRED_UNITS decides; otherwise the first match wins.
    """
    matches = []
    for m in measures:
        op = m.get("observedProperty") or {}
        if not isinstance(op, dict):
            continue
        slug = op.get("@id", "").rsplit("/", 1)[-1]
        if slug == parameter:
            matches.append(m)
 
    if not matches:
        return None
 
    preferred = PREFERRED_UNITS.get(parameter)
    if preferred:
        matches.sort(key=lambda m: m.get("unitName") != preferred)
 
    return matches[0].get("notation")
 
 
@retry(wait=wait_fixed(5), stop=stop_after_attempt(3), retry=retry_if_exception(is_retryable_error))
def fetch_readings(measure_notation, start_date, end_date):
    """
    Fetch readings for one measure timeseries as (DataFrame, status).
 
    `status` is one of:
        'ok'           -- got data, len(df) > 0
        'empty_window' -- measure exists but no rows in the window
                          (CSV came back with a header row only)
        'empty_body'   -- 0-byte response; the measure notation almost
                          certainly doesn't exist. See gotcha #3.
        'not_found'    -- 404 (rarer than empty_body)
    """
    url = f"https://environment.data.gov.uk/hydrology/id/measures/{measure_notation}/readings.csv"
    params = {
        "mineq-date": start_date,
        "maxeq-date": end_date,
        "_limit": 1_000_000,  # 6 months of 15-min data is ~17.5k rows, far under
    }
 
    response = requests.get(url, params=params, timeout=60)
    if response.status_code == 404:
        return pd.DataFrame(), "not_found"
    response.raise_for_status()
 
    if not response.content.strip():
        # Distinct from "no rows in window" (which returns a header row).
        # A 0-byte body almost always means the measure path doesn't exist.
        return pd.DataFrame(), "empty_body"
 
    df = pd.read_csv(io.StringIO(response.text))
    time.sleep(REQUEST_DELAY_SECONDS)
 
    if df.empty:
        return df, "empty_window"
 
    return df, "ok"
 
 
# ==========================================
# 4. DAY 1 EXECUTION
# ==========================================
 
def main():
    logger.info(f"Window: {START_DATE} -> {END_DATE}")
    logger.info(f"Catchments: {list(CATCHMENTS)}")
    logger.info(f"Parameters: {PARAMETERS}")
 
    manifest_rows = []
 
    for catchment_name, cfg in CATCHMENTS.items():
        logger.info(f"=== {catchment_name} ===")
 
        for parameter in PARAMETERS:
            stations = discover_stations(cfg["lat"], cfg["lon"], cfg["dist"], parameter)
            logger.info(f"{catchment_name}/{parameter}: {len(stations)} active stations")
 
            for station in stations:
                guid = station["station_guid"]
                measure_notation = pick_measure_id(station["measures"], parameter)
 
                if not measure_notation:
                    # Rare -- discovery filtered by observedProperty so most
                    # stations here publish the parameter. Belt and braces.
                    continue
 
                out_path = f"data/raw/readings/{guid}__{parameter}.parquet"
 
                # Idempotent: skip if already cached
                if os.path.exists(out_path):
                    logger.info(f"{guid}, {parameter}, cached, skip")
                    continue
 
                try:
                    df, status = fetch_readings(measure_notation, START_DATE, END_DATE)
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    logger.error(f"{guid}, {parameter}, error, {err}")
                    manifest_rows.append({
                        "catchment": catchment_name, "station_guid": guid,
                        "parameter": parameter, "measure_notation": measure_notation,
                        "n_rows": 0, "status": "error",
                    })
                    continue
 
                n_rows = len(df)
                logger.info(f"{guid}, {parameter}, {n_rows}, {status}")
 
                # Non-OK statuses get logged but not written.
                if status != "ok":
                    manifest_rows.append({
                        "catchment": catchment_name, "station_guid": guid,
                        "parameter": parameter, "measure_notation": measure_notation,
                        "n_rows": n_rows, "status": status,
                    })
                    continue
 
                # Tag rows so a `read_parquet('data/raw/readings/*.parquet')`
                # glob later gives one tidy table.
                df = df.assign(
                    station_guid=guid,
                    parameter=parameter,
                    catchment=catchment_name,
                )
                df.to_parquet(out_path, index=False)
 
                final_status = "ok" if n_rows >= MIN_USEFUL_ROWS else "ok_sparse"
                manifest_rows.append({
                    "catchment": catchment_name, "station_guid": guid,
                    "parameter": parameter, "measure_notation": measure_notation,
                    "n_rows": n_rows, "status": final_status,
                })
 
    # Write the manifest (append to history so you have a log across runs)
    manifest_df = pd.DataFrame(manifest_rows).assign(run_ts=pd.Timestamp.utcnow())
    manifest_path = "data/raw/manifest.csv"
    if os.path.exists(manifest_path):
        prior = pd.read_csv(manifest_path)
        manifest_df = pd.concat([prior, manifest_df], ignore_index=True)
    manifest_df.to_csv(manifest_path, index=False)
 
    # Summary of this run
    this_run = manifest_df[manifest_df["run_ts"] == manifest_df["run_ts"].max()]
    summary = this_run.groupby("status").size().to_dict()
    total_rows = int(this_run["n_rows"].sum())
    logger.info(f"Done. Status counts this run: {summary}")
    logger.info(f"Total rows written this run: {total_rows}")
 
 
if __name__ == "__main__":
    main()