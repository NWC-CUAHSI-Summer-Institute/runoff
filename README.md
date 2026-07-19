> **⚠️ Active Development**: This package is currently under active development.

<h1 align="center">RUNOFF: Flash Flood Event Dataset</h1>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.9--3.13-blue?labelColor=333333" alt="Python"></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json&labelColor=333333" alt="Ruff"></a>
  <!-- <a href="https://github.com/mhpi/dhbv2/actions/workflows/lint.yaml"><img src="https://img.shields.io/github/actions/workflow/status/mhpi/dhbv2/lint.yaml?branch=master&logo=github&label=lint&labelColor=333333" alt="Build"></a> -->
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-yellow?labelColor=333333" alt="License"></a>
</p>

Data aggregation and preprocessing scripts for the RUNOFF flash flood benchmarking dataset:

1. NOAA-recognized **flash flood events** from USGS streamflow gauges, geolocated to NextGen HydroFabric (Community; v2.2) catchments;
2. **MRMS** precipitation at sub-hourly (2, 15 min) resolution;
3. **AORC** forcing (precip, temp, radiation, humidity, pressure, wind u/v) at hourly resolution;
4. **USGS streamflow** gauge observations.

</br>

## Installation

```bash
uv pip install -e .
```

Then update `config.yaml` at the repo root (gitignored) with your local paths: `hydrofabric_gpkg`, `events_csv`, `cache_dir`, `study_start`/`study_end`, `huc8_shp`, `gages_csv`, `event_output_dir`. See `src/runoff/paths.py` for how these load.

Every `engine/` script has a **CONFIG block** at the top (editable directly) plus matching CLI flags -- run any script with `--help`. `None` in a CONFIG block generally means "use the `config.yaml` default."

</br>

## Pipeline

Run in order (see `engine/README.md`, `engine/geo/README.md`, `engine/events/README.md` for details):

```bash
# 1. USGS hydrographs -> flash flood events
python engine/events/extract.py --huc8 03020201 --wy-start 2021 --wy-end 2025

# 2. Subset hydrofabric to event catchments + upstream network
python engine/geo/extract_hf.py --csv events.csv --gpkg conus_nextgen.gpkg --output-dir data/upper_neuse/

# 3. Snap gages to catchments (adds gage_cat-id, needed by steps 4-5)
python engine/geo/_gage_to_cat.py --csv events.csv --gpkg conus_nextgen.gpkg

# 4. MRMS precip (optional sharding for large events CSVs, then merge parts)
python engine/forcing/mrms/sharding.py --events-csv events.csv --n-shards 8
python engine/forcing/mrms/extract.py --events-csv events.csv --window-days 6 --centroid peak
python engine/forcing/mrms/merge.py

# 5. AORC forcing (--window-days/--centroid must match step 4)
python engine/forcing/aorc/extract.py --events-csv events.csv --window-days 6 --centroid peak --antecedent-days 30

# 6. Merge AORC + MRMS into one 15-min forcing NetCDF
python engine/forcing/merge_15min.py --aorc data/aorc_15min.nc --aorc-hr data/aorc_hr.nc --mrms data/mrms_15min.nc --output data/forcing_15min.nc

# 7. USGS streamflow (parallel to 4-6), aligned to the merged forcing's event windows
python engine/streamflow/usgs/extract.py --events events.csv --start 2020-01-01 --end 2025-12-31
python engine/streamflow/usgs/to_events.py --forcing data/forcing_15min.nc --csv data/usgs_discharge.csv --output data/streamflow.nc
```

</br>

## Architecture

```text
src/runoff/     # Installable library (paths.py, aorc.py, mrms.py, pet.py, utils.py)
engine/
├── events/      # USGS hydrograph -> flash flood event detection
├── geo/         # Hydrofabric subsetting + gage-to-catchment mapping
├── forcing/
│   ├── aorc/    # AORC extraction
│   ├── mrms/    # MRMS extraction + sharding/merge
│   └── merge_15min.py
└── streamflow/usgs/  # USGS discharge download + event-NetCDF conversion
```

</br>

<!-- ## Contributing

We welcome contributions! See [CONTRIBUTING.md](https://github.com/mhpi/generic_deltamodel/blob/master/docs/CONTRIBUTING.md) for details. -->

---

*Please submit an [issue](https://github.com/mhpi/generic_deltamodel/issues) to report any questions, concerns, bugs, etc.*
