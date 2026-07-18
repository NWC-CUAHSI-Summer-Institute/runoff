# RUNOFF_interface

A Python pipeline for extracting and characterizing flood events from USGS streamflow records. Given one or more HUC8 watershed IDs, it finds all gages inside those watersheds, downloads 15-minute discharge data via the USGS NWIS API, separates hydrographs into individual flood events, and writes per-site and combined CSV outputs.

---

## Package Structure

```
RUNOFF_interface/
└── event_separation/
    ├── event_extraction_pipeline.py   # Main script — configure and run this
    ├── usgs_events.py                 # Core functions (dependency, not run directly)
    └── event_extraction_inputs/
        ├── gages2_lt1000km2.csv       # USGS gage inventory (STAID, LAT_GAGE, LNG_GAGE), only flash flood scale catchments
        └── huc8_conus/                # CONUS HUC8 boundary shapefile
            └── HUC8_US.shp (+ dependency files)
```

---

## Dependencies

Install with pip:

```bash
pip install numpy pandas geopandas dataretrieval hydrotools.events
```

| Package | Purpose |
|---|---|
| `numpy`, `pandas` | Data handling |
| `geopandas` | Spatial join to find gages inside HUC8 polygons |
| `dataretrieval` | USGS NWIS API client |
| `hydrotools.events` | Hydrograph event detection (NOAA-OWP) |

---

## Quick Start

1. Open `event_extraction_pipeline.py` and edit the **USER SETTINGS** block near the top:

```python
# Water years to retrieve
# All timestamps and outputs in UTC
WY_START = 2021
WY_END   = 2025

# Output folder and combined filename
OUTDIR       = "/your/output/folder"
COMBINED_OUT = "combined_events.csv"

# One or more 8-digit HUC8 IDs
HUC8_LIST = ["03020201"]

# Paths to the bundled input files
HUC8_SHP  = ".../RUNOFF_interface/event_separation/event_extraction_inputs/huc8_conus/HUC8_US.shp"
GAGES_CSV = ".../RUNOFF_interface/event_separation/event_extraction_inputs/gages2_lt1000km2.csv"

# Flow threshold: keep only events with peak >= Q at this exceedance probability
# 50 = Q50 (median flow) — less selective, captures more events
# 25 = Q25 (exceeded only 25% of the time) — stricter, larger events only
Q_EXCEEDANCE_PCT = 25
```

2. Run the script:

```bash
python event_extraction_pipeline.py
```

3. Outputs are written to `OUTDIR`:
   - `stations_huc8_<ID>.csv` — list of gages found inside the HUC8
   - `huc8_<ID>_combined_events.csv` — all events across all sites in that HUC8

---

## What the Pipeline Does

For each HUC8 in `HUC8_LIST`, the pipeline runs these steps:

**Step 0 — Gage selection:** Performs a spatial join between the HUC8 polygon and the gage inventory to find all USGS gages inside the watershed.

**Step 1 — Data retrieval:** Downloads 15-minute instantaneous discharge (parameter `00060`, cfs) from USGS NWIS for each gage, covering the requested water years. Data is pulled one water year at a time for reliability and resampled to a uniform 15-minute UTC index.

**Step 2 — Event detection:** Applies NOAA-OWP `hydrotools` hydrograph decomposition with parameters tuned for flash floods (less smoothing, shorter minimum duration). Events are then filtered against a flow-duration curve threshold: only events with a peak at or above the flow exceeded `Q_EXCEEDANCE_PCT`% of the time are kept. At the default of 25 (Q25), only events peaking above the 25th-percentile flow are retained.

**Step 3 — Event metrics:** For each event, the pipeline computes:

| Column | Description |
|---|---|
| `STAID` | USGS site number |
| `BEGIN_DATE_TIME` / `END_DATE_TIME` | Event start and end (UTC) |
| `peak_time` | Time of peak discharge (UTC) |
| `peak_flow_cfs` | Peak discharge (cfs) |
| `volume_acreft` | Total runoff volume (acre-feet) |
| `flashiness_index` | Richards-Baker Flashiness Index |
| `duration_hours` | Event duration |
| `time_to_peak_h` | Time from event start to peak |
| `YEAR`, `month`, `day`, `water_year` | Date metadata |
| `gage_lat`, `gage_lon` | Gage coordinates |

**Step 4 — Output:** Per-site results are combined into a single combined CSV with a sequential `event_id` column.

---

## Tuning Event Detection

The detection parameters in the USER SETTINGS block control how the hydrograph is split into events. The defaults are tuned for flashy, small-basin responses:

```python
DETECT_KWARGS = dict(
    halflife="1h",              
    window="2D",                
    minimum_event_duration="1h",
    start_radius="2h",          
)
```

Source for information on these parameters: https://github.com/NWC-CUAHSI-Summer-Institute/flash_preprocess
For slower, larger watersheds you may want to increase `halflife` (e.g. `"6h"`) and `window` (e.g. `"7D"`).

---


