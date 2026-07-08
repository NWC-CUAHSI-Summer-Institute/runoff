# MRMS × HydroFabric Pipeline

Four sequential Jupyter notebooks that build a per-catchment, 15-minute
precipitation forcing dataset from MRMS radar-rainfall data and the
NOAA/NextGen HydroFabric. Each notebook loads the previous notebook's saved
outputs from disk — they are meant to be run in order, as separate sessions.

## Pipeline order

```
01_hydrofabric_setup.ipynb
        ↓
02_crosswalk_and_event_selection.ipynb
        ↓
03_event_manifest_download_crosswalk.ipynb
        ↓
04_area_weight_15min_export.ipynb
```

---

### 1. `01_hydrofabric_setup.ipynb`
**Does:** Reads the HydroFabric geopackage, inventories every layer, and
builds the master catchment table (divide_id → VPU → nexus) with terminal
outlet nexus.

**Externally supplied file needed:** `conus_nextgen.gpkg`

**Parquet/CSV created:** `hydrofabric_outputs/`
- `catchments_master.parquet`
- `network.parquet`
- `flowpaths.parquet`
- `nexus.parquet`
- `vpu_summary.csv`

---

### 2. `02_crosswalk_and_event_selection.ipynb`
**Does:** Builds the full-CONUS MRMS↔HydroFabric crosswalk (center-based),
runs sanity checks/correctness visuals, provides a HUC8 selector widget,
filters flood events, and traces the storm→gage network.

**Externally supplied file needed:**
- `final_events_upper_neuse.csv` — single merged event + gage table
  (replaces the old two-file `final_events (6).csv` +
  `gages2_lt1000km2.csv` inputs; already has event and USGS gage columns
  combined per row)
- HUC8 boundary shapefile (WBD HUC8 layer)

**Reads (from Notebook 1):** `catchments_master.parquet`, `network.parquet`,
`flowpaths.parquet`, `nexus.parquet`

**Note:** Section 3.5 (a shim cell) reads `final_events_upper_neuse.csv`,
renames its columns to match the schema the rest of the notebook expects,
and re-writes `final_events (6).csv` + `gages2_lt1000km2.csv` on disk so
every downstream cell in the notebook runs unmodified.

**Parquet/CSV created:**
- `huc8_selection_outputs/selected_huc8.parquet`
- `huc8_selection_outputs/huc8_catchments.parquet`
- `huc8_selection_outputs/events_flagged.csv`
- `huc8_selection_outputs/valid_storms.csv`
- `huc8_selection_outputs/gages_indexed.csv`
- `huc8_selection_outputs/storm_gage_results.csv`
- `huc8_selection_outputs/huc8_code.txt`
- `mrms_crosswalk_cache/mrms_hf_crosswalk_conus.parquet` (CONUS crosswalk)

---

### 3. `03_event_manifest_download_crosswalk.ipynb`
**Does:** Builds a per-event manifest using the gage-basin method (all
catchments upstream of the recording gage), computes the unique MRMS
timestamp union, downloads MRMS data, and builds the fractional
(area-weighted) crosswalk restricted to gage-basin catchments.

**Externally supplied file needed:** `mrms_bbox_downloader.py` (must be
in the working directory) — also requires live network access to download
MRMS data.

**Reads (from Notebooks 1 & 2):** `catchments_master.parquet`,
`network.parquet`, `flowpaths.parquet`, `selected_huc8.parquet`,
`huc8_catchments.parquet`, `events_flagged.csv`, `valid_storms.csv`,
`gages_indexed.csv`, CONUS crosswalk parquet

**Parquet/CSV created:** `event_mrms_outputs/`
- `manifest.parquet`
- `event_catchment_windows.parquet`
- `selected_fractional_crosswalk.parquet`
- `gage_event_table.csv`

**Other output:** MRMS shard store `storm_precip_MRMS/shards/pr_YYYYMMDD.nc`

---

### 4. `04_area_weight_15min_export.ipynb`
**Does:** Area-weights 2-minute MRMS rainfall rate per catchment,
resamples to a 15-minute mean rate → depth (mm/15min), and exports one
resumable NetCDF per storm. Includes an interactive storm picker with a
HUC8 context map.

**Externally supplied file needed:** none — all inputs come from
Notebooks 1–3.

**Reads (from Notebooks 1, 2 & 3):** `manifest.parquet`,
`event_catchment_windows.parquet`, `selected_fractional_crosswalk.parquet`,
`catchments_master.parquet`, `network.parquet`, `flowpaths.parquet`,
`selected_huc8.parquet`, `huc8_catchments.parquet`, `gages_indexed.csv`,
`events_flagged.csv`, and the MRMS shard store

**Output created:**
- `storm_precip_MRMS/forcing_15min_nc/storm_{storm_index}_15min.nc` (one
  per storm)
- `forcing_15min_manifest.csv`

---

## Summary table

| Notebook | Externally supplied file(s) needed | Key parquet/CSV output |
|---|---|---|
| 1 | `conus_nextgen.gpkg` | `hydrofabric_outputs/*.parquet` |
| 2 | `final_events_upper_neuse.csv`, HUC8 shapefile | `huc8_selection_outputs/*`, CONUS crosswalk |
| 3 | `mrms_bbox_downloader.py` + network access | `event_mrms_outputs/*` |
| 4 | none (uses outputs of 1–3) | `storm_*_15min.nc`, `forcing_15min_manifest.csv` |
