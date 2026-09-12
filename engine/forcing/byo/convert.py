"""Bring-your-own QPE/QPF: convert a user precipitation product to the AORC
convention so the RUNOFF engine can treat it exactly like MRMS precipitation.

You declare WHAT you have (path, format, temporal frequency, units) and this
script writes an AORC-format NetCDF:

    APCP_surface(time, latitude, longitude)   float32, kg m-2 per timestep
    EPSG:4326 regular lat/lon grid, UTC, time stamped at interval END.

Supported input formats: zarr, netcdf, hdf5, grib2, geotiff, csv.
Supported input units:   mm, mm/h, in, in/h, kg m-2 (per timestep or rate).

Edit the CONFIG block at the top of this file to set all options, or
override per-invocation via CLI flags (run with --help).
"""

import argparse
import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger('byo-convert')


# CONFIG -------------------------- #
# Path to your product (file, directory, or zarr store).
IN_PATH = '/path/to/my_qpe'

# One of: 'zarr', 'netcdf', 'hdf5', 'grib2', 'geotiff', 'csv'.
DATA_FORMAT = 'netcdf'

# Temporal frequency of YOUR data, in minutes (e.g. 2, 5, 10, 15, 60).
TIMESTEP_MIN = 15

# Units of YOUR data:
#   'mm'      depth per timestep          'mm/h'   rate
#   'in'      depth per timestep (inch)   'in/h'   rate (inch per hour)
#   'kg m-2'  depth per timestep (equals mm)
UNITS = 'mm'

# Name of the precipitation variable inside your file.
#   None = take the only (or first) data variable.
VAR_NAME = None

# Coordinate names in your file, if not the usual ones.
TIME_NAME = 'time'
LAT_NAME = None   # None = auto-detect among lat / latitude / y
LON_NAME = None   # None = auto-detect among lon / longitude / x

# Your time stamps mark the 'end' or the 'start' of each interval.
# AORC convention is END; 'start' stamps are shifted forward one timestep.
INPUT_STAMP = 'end'

# Output NetCDF path.
OUT_NC = 'my_qpe_aorc_format.nc'
# -------------------------- #

DEPTH_FACTORS = {
    'mm': 1.0,
    'kg m-2': 1.0,
    'in': 25.4,
}
RATE_UNITS = {'mm/h': 1.0, 'in/h': 25.4}


def parse_args():
    """Parse command-line overrides for the CONFIG block above."""
    p = argparse.ArgumentParser(
        description='Convert a user QPE/QPF product to AORC-format NetCDF',
    )
    p.add_argument('--in-path', default=IN_PATH)
    p.add_argument(
        '--format',
        dest='data_format',
        choices=['zarr', 'netcdf', 'hdf5', 'grib2', 'geotiff', 'csv'],
        default=DATA_FORMAT,
    )
    p.add_argument(
        '--timestep-min',
        type=int,
        default=TIMESTEP_MIN,
        help='temporal frequency of YOUR data in minutes (default: %(default)s)',
    )
    p.add_argument(
        '--units',
        choices=sorted(list(DEPTH_FACTORS) + list(RATE_UNITS)),
        default=UNITS,
        help='units of YOUR data (default: %(default)s)',
    )
    p.add_argument('--var-name', default=VAR_NAME)
    p.add_argument('--time-name', default=TIME_NAME)
    p.add_argument('--lat-name', default=LAT_NAME)
    p.add_argument('--lon-name', default=LON_NAME)
    p.add_argument(
        '--input-stamp',
        choices=['end', 'start'],
        default=INPUT_STAMP,
        help="whether your time stamps mark the interval end or start "
        '(default: %(default)s)',
    )
    p.add_argument('--out-nc', default=OUT_NC)
    return p.parse_args()


def read_product(path: str, data_format: str, var_name, time_name):
    """Read the user product into an xarray DataArray (time, y, x)."""
    import xarray as xr

    if data_format == 'zarr':
        ds = xr.open_zarr(path)
    elif data_format == 'netcdf':
        ds = xr.open_dataset(path)
    elif data_format == 'hdf5':
        ds = xr.open_dataset(path, engine='h5netcdf')
    elif data_format == 'grib2':
        ds = xr.open_dataset(path, engine='cfgrib', backend_kwargs={'indexpath': ''})
    elif data_format == 'geotiff':
        try:
            import rioxarray  # noqa: F401
        except ImportError as e:
            raise SystemExit(
                'geotiff input needs rioxarray: pip install rioxarray. For a '
                'time series of rasters, stack them on a time dimension first.',
            ) from e
        import rioxarray as rxr

        da = rxr.open_rasterio(path).squeeze()
        return da.rename('precip')
    elif data_format == 'csv':
        df = pd.read_csv(path)
        need = {time_name, 'lat', 'lon', 'value'}
        if not need.issubset(df.columns):
            raise SystemExit(
                f'csv input needs columns {sorted(need)}; found {list(df.columns)}',
            )
        df[time_name] = pd.to_datetime(df[time_name], utc=True)
        da = (
            df.set_index([time_name, 'lat', 'lon'])['value']
            .to_xarray()
            .rename('precip')
        )
        return da
    else:
        raise SystemExit(f'unknown format {data_format}')

    if var_name is None:
        candidates = list(ds.data_vars)
        if not candidates:
            raise SystemExit('no data variables found; pass --var-name')
        var_name = candidates[0]
        log.info('using variable %r', var_name)
    return ds[var_name]


def main():
    """Convert a user supplied QPE/QPF product to the AORC-format forcing NetCDF."""
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    args = parse_args()

    da = read_product(args.in_path, args.data_format, args.var_name, args.time_name)

    # normalize coordinate names
    lat = args.lat_name or next(
        (c for c in ('latitude', 'lat', 'y') if c in da.coords),
        None,
    )
    lon = args.lon_name or next(
        (c for c in ('longitude', 'lon', 'x') if c in da.coords),
        None,
    )
    if lat is None or lon is None:
        raise SystemExit(
            f'could not find lat/lon coordinates in {list(da.coords)}; '
            'pass --lat-name/--lon-name',
        )
    ren = {}
    if args.time_name != 'time' and args.time_name in da.coords:
        ren[args.time_name] = 'time'
    ren[lat] = 'latitude'
    ren[lon] = 'longitude'
    da = da.rename(ren)

    # units -> depth (mm == kg m-2) per timestep
    if args.units in RATE_UNITS:
        factor = RATE_UNITS[args.units] * (args.timestep_min / 60.0)
        log.info(
            'rate input (%s): x %.4f -> depth per %d min',
            args.units,
            factor,
            args.timestep_min,
        )
    else:
        factor = DEPTH_FACTORS[args.units]
    da = (da.astype('float32') * factor).rename('APCP_surface')

    # time convention: UTC, stamped at interval END
    t = pd.DatetimeIndex(pd.to_datetime(da['time'].values))
    if t.tz is not None:
        t = t.tz_convert('UTC').tz_localize(None)
    if args.input_stamp == 'start':
        t = t + pd.Timedelta(minutes=args.timestep_min)
        log.info('shifted start-stamped times forward %d min', args.timestep_min)
    da = da.assign_coords(time=t)

    da.attrs = {
        'long_name': 'Total Precipitation',
        'units': 'kg/m^2',
        'crs': 'EPSG:4326',
        'cell_methods': 'time: sum',
        'accumulation_interval_minutes': int(args.timestep_min),
        'time_stamp_convention': 'valid for the interval ENDING at time (UTC)',
        'source': f'user-supplied QPE/QPF ({args.data_format}) via RUNOFF BYO converter',
    }
    ds = da.to_dataset()
    ds['latitude'].attrs = {'units': 'degrees_north', 'standard_name': 'latitude'}
    ds['longitude'].attrs = {'units': 'degrees_east', 'standard_name': 'longitude'}
    Path(args.out_nc).parent.mkdir(parents=True, exist_ok=True)
    ds.to_netcdf(
        args.out_nc,
        encoding={'APCP_surface': {'zlib': True, 'complevel': 4}},
    )
    log.info('wrote %s (%d timesteps)', args.out_nc, da.sizes.get('time', 1))
    log.info(
        'Next: aggregate onto NextGen catchments with the engine, then merge '
        'with AORC met at --timestep-min %d.',
        args.timestep_min,
    )


if __name__ == '__main__':
    main()
