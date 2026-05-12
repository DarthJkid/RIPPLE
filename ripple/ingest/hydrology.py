"""
Environment Agency Hydrology API ingest.
 
Two public functions:
 
  discover_stations(lat, lon, dist_km, parameter_filter=None)
      -> list[StationRecord]    # active stations near a point
                                # publish at least one wanted parameter
 
  fetch_readings(measure_notation, start_date, end_date)
      -> pd.DataFrame           # tidy readings for one measure timeseries
 
Plus pick_measures_for_parameter(measures, parameter), which selects the
right measure dict from a station's measures list (handling DO's mg/L vs %
ambiguity by preferring mg/L).
 
Both treat the API's silent failure modes as loud warnings:
  - 0-byte response  -> ValueError (almost always a bogus measure ID;
                       a real-but-empty window returns a header row)
  - 404              -> ValueError
  - 5xx              -> retry with exponential backoff (tenacity)
  - closed stations  -> filtered out of discover_stations results

"""

from __future__ import annotations
 
import io
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable
 
import pandas as pd
import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
 
logger = logging.getLogger(__name__)

BASE = "https://environment.data.gov.uk/hydrology"
DEFAULT_TIMEOUT = 30

# When a station publishes a parameter in multiple units, prefer this one.
# DO is the main case: it can publish in both % saturation and mg/L. mg/L is
# more directly interpretable for pollution-signature work, so we take it
# when available and fall back to % otherwise.
PREFERRED_UNITS: dict[str, str] = {
    "dissolved-oxygen": "mg/L",
}

@dataclass
class StationRecord:
    station_guid: str
    label: str
    lat: float
    lon: float
    easting: int | None
    northing: int | None
    status: str
    measures: list[dict] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.status.lower() == "active"

#---------------------------------------------------------------------------------------
# HTTP helper
#---------------------------------------------------------------------------------------

@retry(
    retry=retry_if_exception_type(
        (requests.HTTPError, requests.Timeout, requests.ConnectionError)
    ),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    reraise=True,
)

def _get(url: str, params: dict | None = None) -> requests.Response:
    """GET with retry on 5xx and network errors. 4xx is returned as-is."""
    r = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT)
    if r.status_code >= 500:
        r.raise_for_status() # raises -> triggers retry
    return r

#----------------------------------------------------------------------------------------
# Station discovery
#----------------------------------------------------------------------------------------
 

def discover_stations(
        lat: float, 
        lon: float, 
        dist_km: int, 
        parameter_filter: Iterable[str] | None = None,
) -> list[StationRecord]:
    """
    Return ACTIVE stations within `dist_km` of (lat, lon).
 
    If parameter_filter is given (a set of kebab-case slugs like
    {'dissolved-oxygen', 'ammonium'}), only stations that publish at least
    one of those parameters are returned.
 
    The `measures` field of each StationRecord is the raw list from the
    API. Pass items from it through pick_measures_for_parameter() to get
    the right measure dict for a given parameter, then use measure['notation']
    when calling fetch_readings().
    """
    r = _get(
        f"{BASE}/id/stations",
        params={"lat": lat, "long": lon, "dist": dist_km, "_limit": 500},
    )
    r.raise_for_status()
    items = r.json().get("items", [])

    wanted = set(parameter_filter) if parameter_filter else None
    out: list[StationRecord] = []

    for s in items:
        measures = s.get("measures") or []
        if isinstance(measures, dict):
            measures = [measures]
        
        if wanted is not None:
            if not any(_measure_observed_property(m) in wanted for m in measures):
               continue

        out.append(
            StationRecord(
                station_guid=s.get("stationGuid") or s.get("notation"),
                label=s.get("label", ""),
                lat=s.get("lat"),
                lon=s.get("long"),
                easting=s.get("easting"),
                northing=s.get("northing"),
                status=_extract_status(s.get("status")),
                measures=measures,
            )
        )
    
    active = [s for s in out if s.is_active]
    logger.info(
        "discover_stations(lat=%s, lon=%s, dist=%s): %d total, %d active "
        "(dropped %d closed/suspended)",
        lat, lon, dist_km, len(out), len(active), len(out) - len(active),
    )
    return active

def _extract_status(status_field) -> str:
    """'status' is sometimes a list of dicts, sometimes a single dict."""
    if not status_field:
        return "Unknown"
    if isinstance(status_field, list):
        status_field = status_field[0] if status_field else {}
    return status_field.get("label", "Unknown")

def _measure_observed_property(m: dict) -> str:
    """Return the kebab-case slug of a measure's observedProperty ('' if none)."""
    op = m.get("observedProperty")
    if not op:
        return ""
    if isinstance(op, dict):
        url = op.get("@id", "")
    else:
        url = str(op)
    return url.rsplit("/", 1)[-1]

def pick_measures_for_parameter(
        measures: list[dict], parameter: str
) -> list[dict]:
    """
    Return the measure dicts matching 'parameter' (e.g 'dissolved-oxygen').

    if multiple match (typical for DO: both '%' and 'mg/L' exist), the
    preferred unit from PREFERRED_UNITS is sorted to the front. The caller can
    take matches[0] for "best available" semantics or iterate all
    matches if they want to keep every unit.
    """

    matches = [m for m in measures if _measure_observed_property(m) == parameter]
    preferred = PREFERRED_UNITS.get(parameter)
    if preferred:
        matches.sort(key=lambda m: m.get("unitName") != preferred)
    return matches

#---------------------------------------------------------------------------------------
# Readings
#---------------------------------------------------------------------------------------


def fetch_readings(
        measure_notation: str, 
        start_date: date, 
        end_date: date,
        limit: int = 1_000_000,
) -> pd.Dataframe:
    """
    Fetch readings for one measure as a tidy DataFrame.
 
    Columns returned:
        ts        (datetime, UTC-naive)
        value     (float)
        quality   (str, may be missing for unqualified data)
 
    Raises ValueError if the API returns a 0-byte body or a 404 -- almost
    always a bogus measure notation. A real measure with no readings in
    the window returns a header row, which we surface as an empty DataFrame
    (not an error).
    """
    url = f"{BASE}/id/measures/{measure_notation}/readings.csv"
    r = _get(
        url,
        params={
            "mineq-date": start_date.isoformat(),
            "maxeq-date": end_date.isoformat(),
            "_limit": limit,
        },
    )

    if r.status_code == 404:
        raise ValueError(f"404 for measure {measure_notation}")
    
    r.raise_for_status()

    if not r.content.strip():
        raise ValueError(
            f"Empty body fpr '{measure_notation}' -- measure probably doesn't exist"
        )
    
    df = pd.read_csv(io.StringIO(r.text))
    if df.empty:
        return df
    
    if "dateTime" in df.columns:
        df = df.rename(columns={"dateTime"}: "ts")
    if "ts" in df.columns:
        df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(None)
    
    return df