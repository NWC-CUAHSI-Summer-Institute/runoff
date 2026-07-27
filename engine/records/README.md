# Data preparation

Everything on the site is rebuilt from two inputs:

1. NOAA NCEI Storm Events Database annual `StormEvents_details-ftp_v1.0_dYYYY_*.csv.gz`
   files for 2015 through 2025, from
   https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/
2. Boundary files: the NWS AWIPS county shapefile (`c_16ap26`) and the USGS Watershed
   Boundary Dataset HUC8 layer for CONUS.

Steps (Python 3.10+, pandas + geopandas + pyogrio + shapely):

    python prep_events.py   # filter + aggregate -> county_stats.js, huc8_stats.js
    python prep_geo.py      # simplify boundaries -> counties_geo.js, huc8_geo.js, states_geo.js

Filtering applied, in order: EVENT_TYPE equals Flash Flood; county-type reports
(CZ_TYPE C); lower 48 states and DC; flood causes containing "dam" or "levee" removed
(94 reports). Damage strings such as 10.00K and 2.5M are parsed to dollars. Deaths and
injuries are direct plus indirect. Episode counts are distinct EPISODE_ID values per
county or watershed per year; each event is assigned to the HUC8 containing its
reported begin coordinates (one event lacked coordinates and is absent from the
watershed view).

Edit the paths at the top of each script to point at your local copies of the inputs.
The scripts write the `assets/data/*.js` payloads consumed by `records.html`. The
HUC8 boundary payload is not duplicated here: the records page reuses the site's
existing `assets/data/huc8.js` layer.
