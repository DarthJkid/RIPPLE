"""
ripple/store.py — schema fix for edm_returns
 
The Week 1 plan sketched a 10-column edm_returns table with just wasc, site_id,
site_name, receiving_water, year, spill_count, spill_hours, monitor_pct, lat,
lon. The actual data is much richer: 24 useful columns including BNG eastings/
northings, WFD waterbody linkage, the reporting-% data-quality flag, and the
free-text reason/activity columns useful for case-study writeups.
 
REPLACE only the `CREATE TABLE IF NOT EXISTS edm_returns (...)` section of
your SCHEMA constant with the version below. The other tables (stations,
readings, cso_events) stay as the Week 1 plan specified.
"""
 
EDM_RETURNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS edm_returns (
    -- Identity
    site_id                       VARCHAR,
    year                          INTEGER,
    wasc                          VARCHAR,
    site_name                     VARCHAR,
    permit_ref                    VARCHAR,
    activity_ref_on_permit        VARCHAR,
    asset_type                    VARCHAR,
 
    -- Location (raw NGR string + parsed coords)
    outlet_discharge              VARCHAR,
    easting                       INTEGER,
    northing                      INTEGER,
    lat                           DOUBLE,
    lon                           DOUBLE,
 
    -- Catchment context (for spatial joins to the readings table)
    waterbody_id                  VARCHAR,
    wfd_catchment                 VARCHAR,
    receiving_water               VARCHAR,
 
    -- Spill metrics (the analytical payload)
    total_spill_hours             DOUBLE,
    spill_count                   INTEGER,
    long_term_avg_spill_count     DOUBLE,
 
    -- Reporting health (use to filter sites with < 90% EDM operational %)
    data_start_year               INTEGER,
    edm_reporting_pct             DOUBLE,
    reporting_low_reason          VARCHAR,
    action_taken                  VARCHAR,
 
    -- Narrative / context fields (free text from the EA returns)
    high_freq_single_year_reason  VARCHAR,
    high_freq_long_term_reason    VARCHAR,
    investigation_activity        VARCHAR,
    improvement_activity          VARCHAR,
 
    PRIMARY KEY (site_id, year)
);
"""
 
# ---------------------------------------------------------------------------
# If your existing store.py is structured as one SCHEMA constant with all
# four tables concatenated, splice EDM_RETURNS_SCHEMA in where the old EDM
# block lives. e.g.:
#
#   SCHEMA = STATIONS_SCHEMA + READINGS_SCHEMA + EDM_RETURNS_SCHEMA + CSO_EVENTS_SCHEMA
#
# Or, if it's one long triple-quoted string, just replace the
# `CREATE TABLE IF NOT EXISTS edm_returns (...);` block in place.
# ---------------------------------------------------------------------------
 
# Useful DuckDB queries to run against the loaded table -- worth pinning
# in a notebook or as docstrings for sanity:
 
VERIFICATION_QUERIES = """
-- 1. Row counts per year and WaSC
SELECT year, wasc, COUNT(*) AS n_sites,
       SUM(spill_count) AS total_spills,
       SUM(total_spill_hours) AS total_hours
FROM edm_returns
GROUP BY year, wasc
ORDER BY year, total_spills DESC;
 
-- 2. Sites near each RIPPLE catchment centre (within 10 km).
-- Tees: 54.575 N, -1.235 W. Wharfe: 53.925, -1.823. Wye: 51.913, -2.583.
-- Crude lat/lon distance (good enough at this scale).
SELECT site_id, wasc, site_name, receiving_water, lat, lon, spill_count
FROM edm_returns
WHERE year = 2025
  AND ABS(lat - 54.575) < 0.1
  AND ABS(lon - (-1.235)) < 0.17
ORDER BY spill_count DESC NULLS LAST;
 
-- 3. Data-quality filter: sites with < 90% EDM operational reporting
-- are excluded from headline analysis but worth a separate count.
SELECT year, COUNT(*) AS n_below_threshold
FROM edm_returns
WHERE edm_reporting_pct < 90
GROUP BY year ORDER BY year;
 
-- 4. Top-10 spillers in 2025 (for the blog post case studies)
SELECT site_name, wasc, receiving_water, spill_count, total_spill_hours
FROM edm_returns
WHERE year = 2025
ORDER BY spill_count DESC NULLS LAST
LIMIT 10;
"""