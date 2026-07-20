# Forcing engine

MRMS precipitation + AORC meteorology onto NextGen HydroFabric catchments,
per flash flood event, merged into one forcing NetCDF.

## Timesteps

All three stages take `--timestep-min {10,15,30,60}` (default 15, the original
pipeline). The three stages must use the SAME value:

```bash
python engine/forcing/mrms/extract.py  --events-csv events.csv --timestep-min 10
python engine/forcing/aorc/extract.py  --events-csv events.csv --timestep-min 10
python engine/forcing/merge_15min.py   --timestep-min 10
```

Sub-hourly steps (10/15/30) are accumulated from the 2-minute PrecipRate
scans; AORC is always hourly at source and is disaggregated to the chosen
step (uniform split for accumulated variables, linear interpolation for
instantaneous ones) before the catchment aggregation.

## Hourly products (no 2-minute download needed)

For 1-hour forcing use the hourly accumulation grids directly:

```bash
# radar-only 1-h / gauge-corrected Pass 1 / Pass 2
python engine/forcing/mrms/extract_hourly.py --events-csv events.csv --product radar_1h
python engine/forcing/mrms/extract_hourly.py --events-csv events.csv --product pass1
python engine/forcing/mrms/extract_hourly.py --events-csv events.csv --product pass2

python engine/forcing/aorc/extract.py --events-csv events.csv --timestep-min 60
python engine/forcing/merge_15min.py  --timestep-min 60
```

Products come from noaa-mrms-pds on AWS (Iowa State mtarchive as fallback):
`CONUS/RadarOnly_QPE_01H_00.00`, `CONUS/MultiSensor_QPE_01H_Pass1_00.00`,
`CONUS/MultiSensor_QPE_01H_Pass2_00.00`.

## Bring your own QPE/QPF

Convert any user product (zarr, netcdf, hdf5, grib2, geotiff, csv) to the
AORC convention, declaring its temporal frequency and units:

```bash
python engine/forcing/byo/convert.py \
    --in-path /path/to/my_qpe --format zarr \
    --timestep-min 15 --units mm/h
```

Output is AORC-format NetCDF (APCP_surface, kg m-2 per step, EPSG:4326, UTC,
end-of-interval stamps), ready for the same aggregation + merge chain.
