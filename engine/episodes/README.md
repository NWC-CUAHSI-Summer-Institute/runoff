# engine/episodes - episode catalog and precomputed MRMS statistics

This is the backend of the "General flash flood modeling" access path
(episodes.html): flash flood EPISODES from the NOAA Storm Events Database,
their events, their Local Storm Reports, the HUC8 watersheds each episode
touched, and MRMS rainfall and FLASH return period statistics over that
footprint and over every watershed. No stream gage is required anywhere in
this path, and nothing runs online: every number on the website is computed
once, here, and published as static files.

## Workflow

    # 0. inputs: StormEvents details files for the calendar years you need
    #    https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/
    #    put StormEvents_details-ftp_v1.0_dYYYY_*.csv.gz under data/stormevents/

    # 1. Local Storm Reports archive (resumable, ~10-20 min for CONUS x 5 WYs)
    python engine/lsr/fetch_lsrs.py --wy 2021 2025

    # 2. the catalog + site payloads (~2-5 min)
    python engine/episodes/build_catalog.py --stormevents data/stormevents

    # 3. HUC8 watersheds (USGS WBD, once, ~60 MB, gitignored) and the footprint of
    #    every episode (~1 min)
    python engine/episodes/fetch_huc8_wbd.py
    python engine/episodes/build_huc8_footprints.py

    # 4. MRMS statistics per episode, resumable; ~2.5 h for the 5,418 episodes of
    #    WY 2021-2025 on 2 cores with 5 download threads (about 22,000 hourly files,
    #    17 GB transferred, nothing kept on disk)
    python engine/episodes/precompute_mrms.py
    #    or in pieces, any order, the JSON of finished episodes is never recomputed:
    python engine/episodes/precompute_mrms.py --states TX --min-events 5
    python engine/episodes/precompute_mrms.py --start 2024-01-01 --end 2025-01-01
    python engine/episodes/precompute_mrms.py --episode 204934 --force

    # 5. merge the statistics into the site payload (seconds; build_catalog.py also
    #    calls it at the end)
    python engine/episodes/build_mrms_payload.py

Step 1 is optional: without it the catalog is built with LSRs marked "not
fetched" and the LSR filter is hidden. Without steps 3 to 5 the rainfall
filters stay hidden and the detail card says the statistics are not built.
Nothing on the site pretends to have data it does not have.

Dependencies: pandas, numpy, requests (all steps); geopandas, shapely,
rasterio (steps 3 and 4); opencv-python-headless (step 4, PNG decoding of
the GRIB2 messages). `pip install pandas numpy requests geopandas shapely
rasterio opencv-python-headless`.

## What gets computed

Per episode (one NOAA EPISODE_ID, weather-caused flash floods only, county
reports, lower 48 + DC, dam and levee failures removed):

- UTC window from the events' local times and CZ_TIMEZONE offsets; t0 and
  t1 are truncated to the hour
- county footprint (FIPS list) and bounding box from the report coordinates
- events, fatalities, injuries, damage (property + crops, K/M/B parsed)
- LSRs matched by type (default FLASH FLOOD), time window [start - 3 h,
  end + 6 h], and county footprint, with a padded-bbox fallback when a
  report has no county UGC

### Watershed footprint (build_huc8_footprints.py)

Any HUC8 touched by the episode is part of the episode. A HUC8 is kept when
its intersection with the union of the reporting counties is at least 1
percent of the HUC8 area or at least 10 km2, or when an event or an LSR of
the episode falls inside it; the small threshold only removes boundary
slivers of the simplified county polygons. Events and LSRs are counted per
HUC8 by point in polygon. `data/episodes/episode_huc8.csv` keeps every
intersection with the kept flag so the rule can be revisited.

### MRMS statistics (precompute_mrms.py)

Source: the Iowa Environmental Mesonet MRMS archive
(https://mtarchive.geol.iastate.edu/YYYY/MM/DD/mrms/ncep/...) with the NOAA
MRMS bucket on AWS (noaa-mrms-pds) as a file by file fallback.

- Rain: MultiSensor_QPE_01H_Pass2 (gauge corrected, 2 h latency), 0.01 deg,
  hourly, valid time h covering (h - 1 h, h]. Pass 2 exists from 14 October
  2020; earlier hours fall back to Pass 1 then RadarOnly_QPE_01H and the
  product used is recorded per hour (`qpe.products` in the JSON).
- Windows: rolling 1, 3 and 6 h accumulations with window END times from t0
  to t1 + 1 h; the 5 hours before t0 are read so the first 6 h window is
  complete (antecedent rain counts, the way a forecaster reads it). Per cell
  the maximum over the window ends is kept ("cell peak").
- Totals: rain_ep = sum of the hourly fields ending t0 .. t1 + 1 h;
  rain_pre = the 5 hourly fields before that.
- Return periods: FLASH QPE_ARI01H, 03H, 06H (average recurrence interval in
  years of the accumulation, NOAA Atlas 14 based, capped at 200, 0 below
  1 year) sampled at the top of every hour t0 .. t1 + 1 h; per cell the
  maximum over those hours. FLASH derives the ARI from the MRMS radar only
  QPE, so the ARI and the Pass 2 accumulations are two views of the same
  storm, not one computed from the other. Hours after the first window hour
  with no Pass 2 rain in any active footprint are not sampled (the ARI cannot
  exceed the previous hour there); their count is in
  `ari.dry_hours_not_sampled`.
- Statistics for the footprint (union of the kept HUC8), the county union
  and every HUC8, on the MRMS grid with cos latitude weights: area km2; for
  each duration the maximum and the area weighted mean of the cell peaks and
  the hour of the footprint maximum; episode totals (max and mean); for each
  ARI duration the maximum and the fraction of the area at or above 1, 2, 5,
  10, 20, 25, 50, 100 and 200 years (cells without an ARI value count as not
  exceeding; the valid fraction is reported).
- Hourly series of the footprint mean and maximum rain and the maximum 1 h
  ARI, for the sparkline and the download package.

GRIB2 decoding: MRMS messages are PNG packed (template 5.41). The script
parses the GRIB2 sections itself and decodes the PNG with OpenCV, which is
about five times faster than a generic GRIB library and gives identical
values (checked against ecCodes to 1e-14). Hours are visited once in
chronological order; every episode active at that hour receives its crop,
so a file is never downloaded twice. Nothing is stored on disk beyond the
per episode JSON.

## Outputs

    data/episodes/episodes.csv           one row per episode (the master table)
    data/episodes/episode_events.csv     Storm Events rows with episode_id
    data/episodes/episode_lsrs.csv       matched LSRs with episode_id and remarks
    data/episodes/episode_huc8.csv       (episode, HUC8) intersections with the kept flag
    data/episodes/huc8_lookup.csv        HUC8 name, states, area, bbox
    data/episodes/mrms_run.log           one line per hour read (product, source, misses)
    data/geo/huc8_wbd.geojson            USGS WBD HUC8 for local geoprocessing (gitignored)
    assets/data/ep/<id>.json             per episode statistics (published, fetched on click)
    assets/data/episodes.js              site payload, var EPCAT
    assets/data/episode_points.js        site payload, var EPPTS
    assets/data/rep/<id>.json            narratives per episode (fetched on click): the NWS
                                         episode narrative, the event narrative of every
                                         Storm Events report, the remark of every LSR;
                                         the same text is in episodes.csv (episode_narrative),
                                         episode_events.csv (event_narrative) and
                                         episode_lsrs.csv (narrative)

### Site payload format (consumed by assets/js/episodes.js)

EPCAT.fields order: id, t0, t1, states, nev, nlsr, deaths, inj, dmg, fips,
bbox, hucs, km2, m1, m3, m6, rain, a1, a3, a6. Times are UTC "YYYY-MM-DD HH";
nlsr is -1 when LSRs were not fetched; bbox is [min_lat, min_lon, max_lat,
max_lon] of the reports; hucs is the list of HUC8 codes; km2 the footprint
area; m1/m3/m6 = [max, mean] of the cell peak accumulation in mm; rain =
[ep_max, ep_mean, pre_mean] mm; a1/a3/a6 = [max ARI years, coverage in
permille per threshold of EPCAT.mrms.thresholds]. The MRMS fields are null
for an episode not computed yet. EPCAT.mrms documents the products, the
thresholds and how many episodes are done.

EPPTS maps episode id to {ev: [[lat, lon, "YYYY-MM-DD HH:MM", deaths,
damage_usd], ...], lsr: [[lat, lon, "YYYY-MM-DD HH:MM", TYPETEXT, SOURCE],
...]}.

assets/data/ep/<id>.json: {id, t0, t1, hours, qpe, ari, grid, fp, cty,
huc8: [...], series}. `fp`, `cty` and every `huc8` entry hold area_km2,
rain {ep, pre}, max1/max3/max6 {max, mean, t}, ari1/ari3/ari6 {max, cov[],
valid}; the huc8 entries add h, n, st, huc_km2, inter_km2, frac, nev, nlsr.

## Notes

- Storm Events coordinates are NWS report locations, not storm centers. The
  watershed footprint and the MRMS fields are what characterize the storm
  itself; say so when handing these tables to model developers.
- The website's HUC8 polygons (assets/data/huc8.js) are simplified for
  display; the statistics use the WBD polygons rasterized on the MRMS grid.
- mrms_stats.py is the earlier bounding box version of the statistics
  (rolling 6/12/24/72 h over the report bbox with xarray + cfgrib). It still
  works and build_catalog.py still merges its output into episodes.csv, but
  the website no longer reads it.
- Be considerate with the IEM archive: the default of 5 download threads
  moves about 1.5 MB/s. Do not raise it much.
