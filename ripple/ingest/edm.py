"""
ripple/ingest/edm.py
 
Parse the Environment Agency's annual EDM (Event Duration Monitoring) storm
overflow returns into the DuckDB `edm_returns` table.
 
One workbook per year, one sheet per Water and Sewerage Company (WaSC), plus
(in 2025+) a summary 'All WaSC' sheet that must be filtered out to avoid
double-counting. The source schema varies:
 
  - 2024: 2 sheets pre-normalised to snake_case (23 cols),
          8 sheets in the original EA template (28 cols, long titles).
  - 2025: 10 per-WaSC sheets, all in normalised snake_case (21 cols),
          plus 1 'All WaSC' summary sheet.
 
This module handles every variant by:
  1. Cleaning header strings (collapse embedded newlines).
  2. Mapping every known source column to a single canonical snake_case name.
  3. Reindexing to a fixed TARGET schema (drops extras, adds NaN for missing).
  4. Deriving easting/northing/lat/lon from the OS NGR field.
  5. Converting total_spill_time (timedelta) -> total_spill_hours (float).
  6. Tagging each row with the year extracted from the filename.
 
Requires pyproj for BNG -> WGS84 conversion:
    pip install pyproj
 
Run directly:
    python ripple/ingest/edm.py
 
Or import:
    from ripple.ingest.edm import load_edm_workbooks
"""
 
import logging
import re
from pathlib import Path
import datetime
 
import duckdb
import pandas as pd
import pyproj
from pyproj import Transformer
 
logger = logging.getLogger(__name__)
 
# ==========================================
# 1. SCHEMA AND RENAME MAP
# ==========================================
 
# Every known source column name -> canonical target snake_case name.
# Includes BOTH the original EA template headers (long, with embedded \n
# that the header cleaner collapses to single spaces) AND the user's
# pre-normalised headers (which mostly map to themselves, with two small
# tidy-ups for downstream usability).
COLUMN_RENAME = {
    # --- Original EA template (2024 sheets that weren't pre-normalised) ---
    "Unique ID": "site_id",
    "Water Company Name": "wasc",
    "Site Name (EA Consents Database)": "site_name",
    "EA Permit Reference (EA Consents Database)": "permit_ref",
    "Activity Reference on Permit": "activity_ref_on_permit",
    "Storm Discharge Asset Type": "asset_type",
    "Outlet Discharge NGR (EA Consents Database)": "outlet_discharge",
    "WFD Waterbody ID (Cycle 3) (discharge outlet)": "waterbody_id",
    "WFD Waterbody Catchment Name (Cycle 3) (discharge outlet)": "wfd_catchment",
    "Receiving Water / Environment (common name) (EA Consents Database)": "receiving_water",
    "Total Duration (hh:mm:ss) all spills prior to processing through 12-24h count method": "total_spill_time",
    "Counted spills using 12-24h count method": "spill_count",
    "Long-term average spill count": "long_term_avg_spill_count",
    "Data start - calendar year": "data_start_year",
    "EDM Operation - % of reporting period EDM operational": "edm_reporting_pct",
    "EDM Operation - Reporting % - Primary Reason <90%": "reporting_low_reason",
    "EDM Operation - Action taken / planned - Status & timeframe": "action_taken",
    "High Spill Frequency - Operational Review - Single year reason": "high_freq_single_year_reason",
    "High Spill Frequency - Operational Review - Long-term average reason": "high_freq_long_term_reason",
    "Investigation activity for the reporting period": "investigation_activity",
    "Improvement activity for the reporting period": "improvement_activity",
 
    # --- User's pre-normalised names: rename the awkward ones, others map to selves ---
    "edm_reporting_period_pct_share": "edm_reporting_pct",
    "reporting_<_90_reason_primary": "reporting_low_reason",
    "action_taken_increase_reporting": "action_taken",
    "spill_frequency_review_single_year_reason": "high_freq_single_year_reason",
    "spill_frequency_review_long_term_avg_reason": "high_freq_long_term_reason",
    "period_investigation_activity": "investigation_activity",
    "period_improvement_activity": "improvement_activity",
    # Self-mapping for clarity; rename() is a no-op for these:
    "site_id": "site_id",
    "wasc": "wasc",
    "site_name": "site_name",
    "permit_ref": "permit_ref",
    "activity_ref_on_permit": "activity_ref_on_permit",
    "asset_type": "asset_type",
    "outlet_discharge": "outlet_discharge",
    "waterbody_id": "waterbody_id",
    "wfd_catchment": "wfd_catchment",
    "receiving_water": "receiving_water",
    "total_spill_time": "total_spill_time",
    "spill_count": "spill_count",
    "long_term_avg_spill_count": "long_term_avg_spill_count",
    "data_start_year": "data_start_year",
}
 
# Canonical column order for the parsed DataFrame, before adding derived cols.
# Reindexing to this list silently drops any columns we don't want (the five
# out-of-scope optional ones plus UWWTR status and the old-format ID).
TARGET_COLUMNS = [
    "site_id", "wasc", "site_name", "permit_ref", "activity_ref_on_permit",
    "asset_type", "outlet_discharge", "waterbody_id", "wfd_catchment",
    "receiving_water", "total_spill_time", "spill_count",
    "long_term_avg_spill_count", "data_start_year", "edm_reporting_pct",
    "reporting_low_reason", "action_taken", "high_freq_single_year_reason",
    "high_freq_long_term_reason", "investigation_activity", "improvement_activity",
]
 
# Sheets to skip during workbook iteration.
SUMMARY_SHEETS = {"All WaSC"}
 
 
# ==========================================
# 2. NGR PARSING
# ==========================================
 
# OSGB grid: 500km major-square origins (easting, northing in metres).
GRID_500KM = {
    "S": (0, 0),         "T": (500_000, 0),
    "N": (0, 500_000),   "O": (500_000, 500_000),
    "H": (0, 1_000_000), "J": (500_000, 1_000_000),
}
 
 
def _second_letter_offset(letter):
    """100km sub-square offset within a 500km major square. None for 'I' or invalid."""
    if letter == "I" or not letter.isalpha():
        return None
    idx = ord(letter) - ord("A")
    if idx > ord("I") - ord("A"):
        idx -= 1            # OSGB skips the letter I
    if not 0 <= idx <= 24:
        return None
    row, col = divmod(idx, 5)
    return (col * 100_000, (4 - row) * 100_000)
 
 
def parse_ngr(ngr):
    """
    Parse an OSGB National Grid Reference string into (easting, northing) in metres.
 
    Handles every format observed in the EDM workbooks:
      - 12-figure:  'SJ5049211756'                   (1m precision)
      - 10-figure:  'SX50155478'                     (10m precision, mostly South West Water)
      -  8-figure:  'SJ501175'                       (100m precision)
      - whitespace: 'SJ 50492 11756'                 (DCWW style)
      - multi-NGR:  'NZ4071712229 & NZ4071412228'    (sites with two outlets — takes first)
      - 'and':      'SU7900043670 and SU7931043500'
 
    Returns (None, None) on unparseable input.
    """
    if pd.isna(ngr):
        return (None, None)
    s = str(ngr).strip().upper()
    for sep in (" & ", " AND "):
        if sep in s:
            s = s.split(sep, 1)[0].strip()
    s = re.sub(r"\s+", "", s)
    m = re.match(r"^([A-HJ-Z])([A-HJ-Z])(\d+)$", s)
    if not m:
        return (None, None)
    l1, l2, digits = m.group(1), m.group(2), m.group(3)
    if len(digits) % 2 != 0 or len(digits) < 2:
        return (None, None)
    if l1 not in GRID_500KM:
        return (None, None)
    sub = _second_letter_offset(l2)
    if sub is None:
        return (None, None)
    half = len(digits) // 2
    # Pad on the right to 5 digits so the integer value is always metres.
    east_str = digits[:half].ljust(5, "0")
    north_str = digits[half:].ljust(5, "0")
    east = GRID_500KM[l1][0] + sub[0] + int(east_str)
    north = GRID_500KM[l1][1] + sub[1] + int(north_str)
    return (east, north)
 
 
# Lazy-init the transformer once (it's expensive to construct).
_TRANSFORMER = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)
 
 
def bng_to_latlon(easting, northing):
    """Convert (easting, northing) BNG to (lat, lon) WGS84. (None, None) passes through."""
    if easting is None or northing is None:
        return (None, None)
    lon, lat = _TRANSFORMER.transform(easting, northing)
    return (lat, lon)
 
 
# ==========================================
# 3. PARSING
# ==========================================
 
def _clean_and_rename(df):
    """Collapse embedded newlines in header strings, then apply rename map."""
    df = df.copy()
    df.columns = [" ".join(str(c).split()) for c in df.columns]
    return df.rename(columns=COLUMN_RENAME)
 
 
def parse_workbook(path):
    """
    Parse one EDM annual return workbook into a tidy DataFrame.
    Year is taken from the filename: EDM_<year>_*.xlsx
    """
    path = Path(path)
    m = re.search(r"EDM_(\d{4})", path.name)
    if not m:
        raise ValueError(f"Cannot extract year from filename: {path.name}")
    year = int(m.group(1))
    logger.info(f"Parsing {path.name} (year={year})")
 
    xl = pd.ExcelFile(path)
    frames = []
    for sheet in xl.sheet_names:
        if sheet in SUMMARY_SHEETS:
            logger.info(f"  skipping summary sheet: {sheet}")
            continue
        raw = pd.read_excel(path, sheet_name=sheet)
        df = _clean_and_rename(raw)
        # Reindex to the canonical schema. Adds NaN columns for any missing
        # target columns; drops any extras (UWWTR status, old-format ID,
        # the five optional/out-of-scope columns).
        df = df.reindex(columns=TARGET_COLUMNS)
        df["year"] = year
        frames.append(df)
        logger.info(f"  {sheet}: {len(df)} sites")
 
    return pd.concat(frames, ignore_index=True)
 
 
def _spill_time_to_hours(x):
    """
    Convert one total_spill_time cell to float hours.

    Excel cells come back as different Python types depending on pandas
    version and cell formatting:
      - pandas 3.x and earlier: pd.Timedelta uniformly
      - pandas 4.x: datetime.time for sub-24h durations, Timedelta for longer
      - openpyxl edge cases: str, datetime.timedelta, NaT
    """
    if pd.isna(x):
        return None
    if isinstance(x, datetime.time):
        # time-of-day reading -- treat as duration, which is what the cell intends
        return x.hour + x.minute / 60.0 + x.second / 3600.0 + x.microsecond / 3.6e9
    if isinstance(x, (pd.Timedelta, datetime.timedelta)):
        return x.total_seconds() / 3600.0
    if isinstance(x, str):
        try:
            return pd.to_timedelta(x).total_seconds() / 3600.0
        except (ValueError, TypeError):
            return None
    return None


def add_derived_columns(df):
    """
    Add easting / northing / lat / lon (from NGR) and total_spill_hours
    (from a mixed-type column). Drops the now-redundant raw total_spill_time.
    """
    df = df.copy()

    # NGR -> easting/northing -> lat/lon
    coords = df["outlet_discharge"].apply(parse_ngr)
    df["easting"] = [c[0] for c in coords]
    df["northing"] = [c[1] for c in coords]
    latlons = [bng_to_latlon(e, n) for e, n in zip(df["easting"], df["northing"])]
    df["lat"] = [ll[0] for ll in latlons]
    df["lon"] = [ll[1] for ll in latlons]

    # Spill time -> hours. Robust to datetime.time / Timedelta / str / NaT.
    df["total_spill_hours"] = df["total_spill_time"].apply(_spill_time_to_hours)

    return df.drop(columns=["total_spill_time"])
 
 
def load_edm_workbooks(edm_dir="data/raw/edm"):
    """Find and parse every EDM workbook in `edm_dir`. Returns one DataFrame across years."""
    edm_dir = Path(edm_dir)
    files = sorted(edm_dir.glob("EDM_*.xlsx"))
    if not files:
        raise FileNotFoundError(f"No EDM_*.xlsx files in {edm_dir}")
    frames = [parse_workbook(f) for f in files]
    df = pd.concat(frames, ignore_index=True)
    df = add_derived_columns(df)
    return df
 
 
# ==========================================
# 4. DUCKDB LOAD
# ==========================================
 
# Tight per-year spill-count assertions to catch a dropped WaSC sheet or
# an un-filtered summary sheet. Ranges chosen to absorb realistic
# year-over-year weather variance.
EXPECTED_SPILL_RANGES = {
    2024: (350_000, 550_000),   # observed ~450k
    2025: (230_000, 350_000),   # observed ~291k
}
 
 
EDM_RETURNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS edm_returns (
    site_id                       VARCHAR,
    year                          INTEGER,
    wasc                          VARCHAR,
    site_name                     VARCHAR,
    permit_ref                    VARCHAR,
    activity_ref_on_permit        VARCHAR,
    asset_type                    VARCHAR,
    outlet_discharge              VARCHAR,
    easting                       INTEGER,
    northing                      INTEGER,
    lat                           DOUBLE,
    lon                           DOUBLE,
    waterbody_id                  VARCHAR,
    wfd_catchment                 VARCHAR,
    receiving_water               VARCHAR,
    total_spill_hours             DOUBLE,
    spill_count                   INTEGER,
    long_term_avg_spill_count     DOUBLE,
    data_start_year               INTEGER,
    edm_reporting_pct             DOUBLE,
    reporting_low_reason          VARCHAR,
    action_taken                  VARCHAR,
    high_freq_single_year_reason  VARCHAR,
    high_freq_long_term_reason    VARCHAR,
    investigation_activity        VARCHAR,
    improvement_activity          VARCHAR,
    PRIMARY KEY (site_id, year)
);
"""


def load_to_duckdb(df, db_path="data/ripple.duckdb"):
    """Idempotent upsert into edm_returns. Creates schema if missing."""
    conn = duckdb.connect(db_path)
    conn.execute(EDM_RETURNS_SCHEMA)              # <-- add this
    conn.register("_edm_staging", df)
    conn.execute("""
        INSERT OR REPLACE INTO edm_returns
        SELECT
            site_id, year, wasc, site_name, permit_ref, activity_ref_on_permit,
            asset_type, outlet_discharge,
            CAST(easting  AS INTEGER) AS easting,
            CAST(northing AS INTEGER) AS northing,
            lat, lon,
            waterbody_id, wfd_catchment, receiving_water,
            CAST(total_spill_hours AS DOUBLE) AS total_spill_hours,
            CAST(spill_count AS INTEGER) AS spill_count,
            long_term_avg_spill_count,
            CAST(data_start_year AS INTEGER) AS data_start_year,
            edm_reporting_pct,
            reporting_low_reason, action_taken,
            high_freq_single_year_reason, high_freq_long_term_reason,
            investigation_activity, improvement_activity
        FROM _edm_staging
    """)
    n = conn.execute("SELECT COUNT(*) FROM edm_returns").fetchone()[0]
    conn.close()
    return n
 
 
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
    df = load_edm_workbooks()
    logger.info(f"Parsed {len(df):,} rows across all years")
 
    # Per-year sanity checks
    for year, year_df in df.groupby("year"):
        total = int(pd.to_numeric(year_df["spill_count"], errors="coerce").sum())
        logger.info(f"  {year}: {len(year_df):,} sites, {total:,} total spills")
        lo, hi = EXPECTED_SPILL_RANGES.get(year, (0, float("inf")))
        if not lo < total < hi:
            logger.warning(
                f"  {year}: spill total {total:,} outside expected [{lo:,}, {hi:,}] "
                f"-- probably a dropped WaSC sheet or unfiltered summary"
            )
 
    # Geocoding health
    n_geo = df["lat"].notna().sum()
    pct = 100 * n_geo / len(df) if len(df) else 0
    logger.info(f"  Geocoded: {n_geo:,} / {len(df):,} sites ({pct:.1f}%)")
    if pct < 99:
        # We saw 100% NGR coverage so anything under 99% means the parser
        # is rejecting valid NGRs. Print a few examples to debug.
        bad = df.loc[df["lat"].isna(), ["site_id", "outlet_discharge"]].head(5)
        logger.warning(f"  Unparsed NGR examples:\n{bad}")
 
    n = load_to_duckdb(df)
    logger.info(f"edm_returns now contains {n:,} rows")
 
 
if __name__ == "__main__":
    main()