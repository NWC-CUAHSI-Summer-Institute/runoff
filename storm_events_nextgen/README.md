# RUNOFF event generation for NextGen

One notebook that turns NOAA Storm Events flash flood reports into events placed on the
NextGen hydrofabric, with radar coverage at each event's catchment outlet and, optionally,
the closest downstream USGS gage.

Notebook: `RUNOFF_NextGen_Pipeline.ipynb`

## What is in this folder

    RUNOFF_NextGen_Pipeline.ipynb      the pipeline, 11 numbered steps, settings at the top
    Radar_coverage_rfc_1km.tif         radar coverage raster, 0.01 degree, EPSG:4269, percent
    gages2_lt1000km2.csv               GAGES-II gages under 1000 km2 (5,897), only used when MATCH_GAGES = True
    flashflood_final_events_2021_2025.csv   example output (13,502 events), so the result can be
                                       inspected without downloading the hydrofabric

## Inputs you need to download

The NextGen hydrofabric is not in this folder because of its size (4.6 GB). Download it once
and place it next to the notebook:

    https://lynker-spatial.s3.amazonaws.com/hydrofabric/v2.2/conus/conus_nextgen.gpkg

The notebook reads the `network`, `divides`, `flowpaths` and `nexus` layers of that file.
The Storm Events files are read directly from NCEI inside the notebook; nothing to download.

## How to run

1. Put `conus_nextgen.gpkg` in this folder.
2. Open the notebook and set `INPUT_DIR` in the Settings cell to this folder (or leave it as `Path(".")`
   and start Jupyter from this folder).
3. Run all cells. Memory: about 8 GB for the CONUS divides layer. Time: 10 to 20 minutes, most of it
   reading the hydrofabric.
4. The result is written as `flashflood_final_events_<start>_<end>.csv` in `INPUT_DIR`.

Settings worth knowing (Settings cell):

    START_YEAR, END_YEAR        calendar years of Storm Events to read
    TIME_WINDOW_HOURS = 24      reports in the same episode are joined into one event when their
                                begin times are within this window and their circles touch
    RADAR_COVERAGE_THRESHOLD    events whose outlet coverage is below this are dropped (80 by default)
    FILTER_BY_RADAR_COVERAGE    set False to keep every event and only report the coverage
    MATCH_GAGES, REQUIRE_GAGE   optional gage matching; the pipeline does not need a gage

## What the pipeline does

1. Reads the newest Storm Events details file for each year from NCEI.
2. Keeps flash flood reports, converts local times to UTC, parses damage, sums deaths and injuries.
3. Each report becomes a circle (midpoint of begin and end point, radius half the distance between them).
4. Joins reports of the same NOAA episode into events when they are close in time and space.
   Events are never joined across episodes.
5. Aggregates each event: time window, duration, centroid, footprint radius, states, WFOs, losses,
   source event ids.
6. Places the event centroid in a hydrofabric divide (`storm_cat-id`) and its flowpath (`storm_flowpath-id`).
7. Finds the catchment outlet: the nexus the flowpath drains to (or the end of the flowpath when the
   nexus layer is missing) and samples the radar coverage there.
8. Optional: snaps the gages to flowpaths and picks the closest gage downstream of the event.
9. Applies the filters in order and prints a funnel table showing where events drop out.
10. Plots one event with its catchment, flowpath, outlet and gage for a visual check.

## Radar coverage

`Radar_coverage_rfc_1km.tif` is a hydrologic coverage, not a plain radar map. At each pixel the value
is the percent of the drainage area upstream of that pixel that is covered by weather radar. Sampling
it at the catchment outlet therefore gives the coverage of the whole area draining to that outlet.
Nodata is -9999 (outside CONUS).

## Output columns

    cluster_id              event id, <episode_id>_<n>
    episode_id              NOAA EPISODE_ID; joins to the RUNOFF storm episodes dataset
    YEAR                    calendar year of the event start
    n_reports               Storm Events reports joined into this event
    BEGIN_DATE_TIME, END_DATE_TIME   UTC
    duration_hours, duration_gt_48h
    centroid_lat, centroid_lon       mean of the report centers
    footprint_radius_km     radius around the centroid that holds all reports
    n_states, states, primary_state, n_wfos, wfos
    deaths_total, injuries_total, damage_property_usd, damage_crops_usd, damage_total_usd
    flood_causes            FLOOD_CAUSE values of the reports
    event_ids               Storm Events EVENT_IDs; join to the RUNOFF events table
    source_files            NCEI file each report came from (files are updated by NCEI)
    storm_cat-id            hydrofabric divide containing the centroid
    storm_flowpath-id       its flowpath
    outlet_nexus_id, outlet_type (nexus or flowpath_end), outlet_lat, outlet_lon
    radar_coverage_outlet   percent, hydrologic coverage at the outlet
    storm_outlet_dist_m     centroid to outlet distance
    with MATCH_GAGES = True: STAID, DRAIN_SQKM, gage_lat, gage_lon, gage_cat-id, gage_flowpath-id,
                             gage_snap_dist_m, storm_gage_dist_m, radar_coverage_gage

## Scope notes

- Storm Events covers all states and territories. Reports outside CONUS drop out at step 6 because the
  hydrofabric is CONUS only.
- Years are calendar years. The RUNOFF storm episodes dataset uses water years 2021 to 2025.
- Dam and levee failures are kept; filter on `flood_causes` to remove them.
- NCEI re-issues the Storm Events files, so counts can change slightly between runs; `source_files`
  records which files were used.

## Link to the RUNOFF storm episodes dataset

Every event here carries `episode_id` and `event_ids`. The storm episodes dataset
(https://github.com/NWC-CUAHSI-Summer-Institute/runoff/tree/master/storm_episodes) uses the same ids,
so one merge on `episode_id` adds MRMS rainfall, return periods, FLASH model output, flash flood
guidance ratios, Local Storm Reports and narratives to each event.

## Requirements

    pandas numpy geopandas pyogrio shapely rasterio networkx requests tqdm matplotlib jupyter

`pip install pandas numpy geopandas pyogrio shapely rasterio networkx requests tqdm matplotlib jupyter`
