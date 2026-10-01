# RUNOFF episodes dataset, version 1

Flash flood storm episodes over the lower 48 states, October 2020 to September 2025, with the
NOAA Storm Events reports and NWS Local Storm Reports that make them up, the HUC8
watersheds they touched, MRMS rainfall and FLASH return periods over those watersheds,
FLASH model output and flash flood guidance ratios, and the RUNOFF USGS gages inside them.
No stream gage is required anywhere: this is the storm-first view of the dataset, built for
flash flood guidance style detection models.

Built by engine/episodes/export_dataset.py in the RUNOFF repository
(https://github.com/NWC-CUAHSI-Summer-Institute/runoff). Website: episodes.html.

## Getting the data

This folder is the dataset. Clone the repository, or download the folder from GitHub, or take
the zip attached to the latest release. Inside:

    README.md, DATA_DICTIONARY.csv    this file and every column explained
    RUNOFF_storm_episodes_overview.pdf  15 slides: what is in the dataset, how it was built,
                                      the notebook, the radar coverage layer
    data/                             the eight tables, each as .parquet and .csv
    data/radar_cov.tif                the hydrologic radar coverage layer (GeoTIFF, 0.01 degree)
    notebooks/quickstart.ipynb        a worked tour: load, filter, join, model, map

Reading a table needs only pandas (`pd.read_parquet("data/episode_huc8.parquet")`; add pyarrow
for Parquet, or read the CSV with `dtype={"huc8": str}` so the leading zeros survive).

## Tables

| table | rows | one row is |
|---|---|---|
| episodes | 5,418 | one storm episode (NOAA EPISODE_ID) |
| episode_huc8 | 35,697 | one HUC8 watershed inside one episode footprint: the modeling table |
| events | 20,882 | one Storm Events flash flood report |
| lsrs | 24,907 | one Local Storm Report matched to an episode |
| hourly | 50,390 | one hour of one episode, footprint statistics |
| huc8 | 2,193 | one HUC8 watershed, static attributes |
| gages | 5,897 | one RUNOFF USGS gage (drainage area under 1000 km2) |
| episode_gages | 119,770 | one gage inside one episode footprint |

Keys: `episode_id` (integer) joins everything to episodes; `huc8` (8 character string,
keep the leading zero) joins episode_huc8, events, lsrs, huc8 and gages; `site_no` joins
gages and episode_gages. Times are UTC strings, `YYYY-MM-DD HH` for episode hours and
`YYYY-MM-DD HH:MM` for reports. Every table exists as Parquet and as CSV; read the Parquet
when you can (types and narratives survive), the CSV when you need Excel.

## Definitions

- Episode: one NOAA Storm Events EPISODE_ID with at least one weather-caused flash flood
  report in a county of the lower 48 or DC (dam and levee failures excluded). The window is
  the earliest report start to the latest report end, in UTC, truncated to the hour.
- Footprint: every HUC8 (USGS WBD) that intersects the reporting counties by at least 1
  percent of its area or 10 km2, or that holds a report. `share_of_footprint` and
  `inside_counties_frac` tell how much of a watershed the counties cover.
- Rain: MRMS MultiSensor QPE 1 h Pass 2 (gauge corrected; Pass 1 or radar only where Pass 2
  is absent, listed in `qpe_products`). `max1h/3h/6h_peak_mm` is the highest rolling
  accumulation of that length reached by any 1 km cell in the unit, windows ending from the
  episode start hour to one hour after its end; `_mean_of_peaks_mm` is the area weighted mean
  of the cell peaks; `rain_ep_*` is the total over the same window; `rain_pre5h_*` the 5 hours
  before the start.
- Return period: FLASH QPE_ARI 1H/3H/6H, the average recurrence interval in years of the
  observed accumulation at each cell (NOAA Atlas 14 based, capped at 200), sampled at the
  top of every hour; `_peak_yr` is the cell maximum over the episode, `_cov_geTyr` the share
  of the unit area at or above T years at any hour (cos latitude weighted; cells without an
  ARI value count as not exceeding; `_valid_frac` says how much of the area had a value).
- FLASH models: `crest/sac/hp_peak_m3s_km2` is the episode peak unit streamflow of the
  CREST, SAC-SMA and hydrophobic (burn scar) models at any cell, m3/s/km2.
- Guidance: `ffg1h/3h/6h/max_peak_ratio` is the peak MRMS QPE to NWS flash flood guidance
  ratio (1.0 = rain reached the guidance); `_share_ge1` the share of the unit area whose
  peak ratio reached 1.0 during the episode. FLASH products are read at the top of each
  hour of the window only, no lookback.
- Label: `flooded` = 1 when at least one Storm Events report or Local Storm Report fell
  inside the watershed during the episode (`n_reports` = `n_events` + `n_lsr`). It is a
  report based label: 11,446 of 35,697 episode-watershed rows are
  positive, and the negatives are watersheds inside a storm footprint where nobody reported
  flooding, not watersheds with no storm. Reports happen where people and roads are.
- Coordinates in events and lsrs are NWS report locations, not storm centers. The footprint
  and the MRMS columns are what characterize the storm.

## Coverage of this build

- FLASH products present for 5,418 of 5,418 episodes.
- QPE/FFG values are ratios in every record.
- Reports assigned to watersheds: 20,880 of 20,882 events, 24,906 of 24,907 LSRs.
- Extra HUC8 attributes joined: radar_coverage_mean, radar_coverage_min, radar_coverage_max, radar_coverage_valid_frac, radar_coverage_frac_ge50, radar_coverage_frac_ge100.
- Radar coverage: `data/radar_cov.tif` is a hydrologic coverage layer on the 0.01 degree
  MRMS grid. At every pixel the value is the percent of the drainage area upstream of that
  pixel that weather radar covers (0 to 100). Read it that way: the value at a gage is the
  coverage of the basin above the gage; the value at a flash flood report is the coverage of
  the basin draining to that place. The dataset carries it three ways: sampled at each gage
  (`radar_coverage_at_gage` in gages), at each report (`radar_coverage_at_report` in events
  and lsrs, averaged per watershed as `radar_coverage_at_reports_mean` in episode_huc8), and
  summarized over every pixel of a watershed (`radar_coverage_mean/min/max/valid_frac/frac_ge*`
  in huc8 and episode_huc8, from engine/episodes/huc8_radar_coverage.py). Over the lower 48,
  56 percent of land pixels read 100, 37 percent read 0, the rest are partial.

## Sources

NOAA NCEI Storm Events Database; NWS Local Storm Reports via the Iowa Environmental
Mesonet; NOAA MRMS archive (IEM mirror, AWS noaa-mrms-pds fallback); NSSL FLASH products
(QPE ARI, CREST, SAC-SMA, hydrophobic unit streamflow, QPE/FFG); USGS Watershed Boundary
Dataset (HUC8); USGS GAGES-II, NWIS and NLDI for the gages.

See DATA_DICTIONARY.csv for every column and notebooks/quickstart.ipynb for the worked tour
(load, filter, join, summarize, map, build a training matrix, use the narratives).
