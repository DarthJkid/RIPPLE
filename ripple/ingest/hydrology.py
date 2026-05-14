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
    "waterFlow",
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
def list_stations(lat, lon, dist):
    """Phase 1: list ALL stations near a point. Returns just GUIDs + status."""
    url = "https://environment.data.gov.uk/hydrology/id/stations.json"
    params = {"lat": lat, "long": lon, "dist": dist, "_limit": 500}
    r = requests.get(url, params=params, timeout=30)
    if r.status_code == 404:
        return []
    r.raise_for_status()

    items = r.json().get("items", [])
    active = []
    for item in items:
        status_field = item.get("status") or []
        if isinstance(status_field, dict):
            status_field = [status_field]
        status_label = status_field[0].get("label", "Unknown") if status_field else "Unknown"
        if status_label.lower() != "active":
            continue
        active.append(item.get("stationGuid") or item.get("notation"))
    
    active = list(dict.fromkeys(active))   # preserve order, drop dupes
    time.sleep(REQUEST_DELAY_SECONDS)
    return active


@retry(wait=wait_fixed(5), stop=stop_after_attempt(3), retry=retry_if_exception(is_retryable_error))
def fetch_station_detail(station_guid):
    """Phase 2: fetch full detail for one station. Includes nested measures."""
    url = f"https://environment.data.gov.uk/hydrology/id/stations/{station_guid}.json"
    r = requests.get(url, params={}, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()

    items = r.json().get("items", [])
    if not items:
        return None
    s = items[0]

    time.sleep(REQUEST_DELAY_SECONDS)
    return {
        "station_guid": s.get("stationGuid") or s.get("notation"),
        "name": s.get("label"),
        "river": s.get("riverName"),
        "lat": s.get("lat"),
        "lon": s.get("long"),
        "measures": s.get("measures", []),
    }


def discover_stations_in_catchment(lat, lon, dist):
    """Two-phase discovery. Returns Active stations with full measure metadata."""
    guids = list_stations(lat, lon, dist)
    logger.info(f"  list: {len(guids)} active station guids in bbox")
    stations = []
    for guid in guids:
        detail = fetch_station_detail(guid)
        if detail is not None:
            stations.append(detail)
    return stations
 
 
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
    matches = [m for m in measures
            if (m.get("observedProperty") or {}).get("@id", "").rsplit("/", 1)[-1] == parameter]
    if not matches:
        return None
    preferred = PREFERRED_UNITS.get(parameter)
    matches.sort(key=lambda m: (
        m.get("valueType") != "instantaneous",   # instantaneous first
        m.get("period") or 10**9,                 # smaller period first
        m.get("unitName") != preferred if preferred else False,
    ))
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
        stations = discover_stations_in_catchment(cfg["lat"], cfg["lon"], cfg["dist"])
        logger.info(f"{catchment_name}: {len(stations)} stations with full detail")

        for station in stations:
            guid = station["station_guid"]

            for parameter in PARAMETERS:
                measure_notation = pick_measure_id(station["measures"], parameter)
                if not measure_notation:
                    # Station doesn't publish this parameter -- normal, no log
                    continue

                out_path = f"data/raw/readings/{guid}__{parameter}.parquet"
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

                if status != "ok":
                    manifest_rows.append({
                        "catchment": catchment_name, "station_guid": guid,
                        "parameter": parameter, "measure_notation": measure_notation,
                        "n_rows": n_rows, "status": status,
                    })
                    continue

                df = df.assign(
                    station_guid=guid, parameter=parameter, catchment=catchment_name,
                )
                df.to_parquet(out_path, index=False)
                final_status = "ok" if n_rows >= MIN_USEFUL_ROWS else "ok_sparse"
                manifest_rows.append({
                    "catchment": catchment_name, "station_guid": guid,
                    "parameter": parameter, "measure_notation": measure_notation,
                    "n_rows": n_rows, "status": final_status,
                })
 
    # Write the manifest (append to history so you have a log across runs)
    run_ts = pd.Timestamp.now("UTC").isoformat()
    manifest_df = pd.DataFrame(manifest_rows).assign(run_ts=run_ts)
    manifest_path = "data/raw/manifest.csv"
    if os.path.exists(manifest_path):
        prior = pd.read_csv(manifest_path)
        manifest_df = pd.concat([prior, manifest_df], ignore_index=True)
    manifest_df.to_csv(manifest_path, index=False)

    this_run = manifest_df[manifest_df["run_ts"] == run_ts]   # exact match, no .max()
    summary = this_run.groupby("status").size().to_dict()
    total_rows = int(this_run["n_rows"].sum())

    if not manifest_rows:
        logger.warning("No (station, parameter) pairs processed -- check pick_measure_id")
        return
 
    # Summary of this run
    this_run = manifest_df[manifest_df["run_ts"] == manifest_df["run_ts"].max()]
    summary = this_run.groupby("status").size().to_dict()
    total_rows = int(this_run["n_rows"].sum())
    logger.info(f"Done. Status counts this run: {summary}")
    logger.info(f"Total rows written this run: {total_rows}")
 
 
if __name__ == "__main__":
    main()