> **⚠️ Active Development**: This package is currently under active development.

<h1 align="center">Flash Preprocessor: Data Constructor for ML</h1>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.9--3.13-blue?labelColor=333333" alt="Python"></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json&labelColor=333333" alt="Ruff"></a>
  <!-- <a href="https://github.com/mhpi/dhbv2/actions/workflows/lint.yaml"><img src="https://img.shields.io/github/actions/workflow/status/mhpi/dhbv2/lint.yaml?branch=master&logo=github&label=lint&labelColor=333333" alt="Build"></a> -->
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-yellow?labelColor=333333" alt="License"></a>
</p>

This repository serves as a library of data aggregation and preprocessing scripts for

1. Defining and aggregating NOAA-recognized **flash flood events** across the Contiguous United States (CONUS), and geolocating these to catchments defined by the NextGen HydroFabric (Community; v2.2);

2. Downloading and aggregating Multi-Radar Multi-Sensor (MRMS) **precipitation measurements** at sub-hourly resolution (2, 15 min);

3. Downloading and aggregation Analysis of Record for Calibration (AORC) **forcing measurements** (precipitation, temperature, solar radiation lw/sw, humidity, pressure, and wind velocity u/v) at hourly resolution;

4. **USGS streamflow gauge observations**.

</br>

## dMG Preprocess Steps:

1. Hydrofabric extraction:

    ```bash
    # Download Community HydroFabric v2.2
    aws s3 cp s3://communityhydrofabric/hydrofabrics/community/conus_nextgen.gpkg . --no-sign-request

    # Or
    aws s3 cp s3://communityhydrofabric/hydrofabrics/community/conus_nextgen.tar.gz . --no-sign-request

    python ./engine/geo/extract_hf.py --csv  --gpkg ~/.ngiab/hydrofabric/v2.2/conus_nextgen.gpkg --output-dir data/upper_neuse/
    ```

2. AORC extraction:

    ```bash
    # Get index
    python engine/forcing/aorc/index_hf_weighted.py --csv /Users/leoglonz/Desktop/noaa/data/upper_neuse/events.csv --upstream --output data/upper_neuse/weighted_index_dict.pkl

    # Get AORC
    python engine/forcing/aorc/extract.py --start 2021-01-01 --end 2025-12-31 --index data/upper_neuse/weighted_index_dict.pkl --output-dir data/upper_neuse

    # Extract to hourly and 15min event datasets
    python engine/forcing/aorc/to_events.py --events /Users/leoglonz/Desktop/noaa/data/upper_neuse/events.csv --forcing /Users/leoglonz/Desktop/noaa/data/upper_neuse/aorc_extracted.nc --output-dir /Users/leoglonz/Desktop/noaa/data/upper_neuse
    ```

3. Combine AORC + MRMS

    ```bash
    python engine/forcing/merge_15min.py --aorc /gpfs/leoglonz/suijin/flash-preprocess/data/aorc_15min.nc --mrms /gpfs/leoglonz/suijin/flash-preprocess/data/mrms_15min.nc  --output /gpfs/leoglonz/suijin/flash-preprocess/data/forcing_15min.nc
    ```
