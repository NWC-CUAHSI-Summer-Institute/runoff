"""Convert a USGS discharge CSV to an event-indexed NetCDF.

@drworm
"""

import argparse
import logging
from pathlib import Path

import netCDF4
import numpy as np
import pandas as pd

from flash_preprocess.paths import EVENTS_CSV as _EVENTS_CSV

log = logging.getLogger('USGS-ToEvents')

_EPOCH = pd.Timestamp('1970-01-01', tz='UTC')
_MIN_TO_NS = 60 * 1_000_000_000  # nanoseconds per minute


# CONFIG -------------------------- #
# Merged 15-min forcing NetCDF (from merge_15min.py); provides event windows.
FORCING_NC = _EVENTS_CSV.parent / 'forcing_15min.nc'

# Long-format USGS discharge CSV (from extract.py's OUTPUT_CSV).
CSV_PATH = _EVENTS_CSV.parent / 'usgs_discharge.csv'

# Output event-indexed NetCDF path.
OUTPUT_NC = _EVENTS_CSV.parent / 'streamflow.nc'

# zlib compression level 1-9.
COMPLEVEL = 4
# -------------------------- #


def _minutes_to_timestamp(minutes: float) -> pd.Timestamp:
    """Convert minutes-since-epoch float to a tz-aware UTC Timestamp."""
    return _EPOCH + pd.Timedelta(minutes=minutes)


def _load_str_var(ds: netCDF4.Dataset, name: str) -> np.ndarray:
    """Read a netCDF4 string variable as a plain numpy object array."""
    raw = ds.variables[name][:]
    if isinstance(raw, np.ma.MaskedArray):
        raw = raw.filled()
    out = np.empty(len(raw), dtype=object)
    for i, v in enumerate(raw):
        out[i] = str(v) if not isinstance(v, str) else v
    return out


def parse_args():
    """Parse command-line overrides for the CONFIG block above."""
    parser = argparse.ArgumentParser(
        description="Convert USGS discharge CSV to event-indexed NetCDF.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        '--forcing',
        type=Path,
        default=FORCING_NC,
        help="Path to forcing_15min.nc, provides event windows (default: %(default)s)",
    )
    parser.add_argument(
        '--csv',
        type=Path,
        default=CSV_PATH,
        help="Path to long-format USGS discharge CSV (default: %(default)s)",
    )
    parser.add_argument(
        '--output',
        type=Path,
        default=OUTPUT_NC,
        help="Output NetCDF path (default: %(default)s)",
    )
    parser.add_argument(
        '--complevel',
        type=int,
        default=COMPLEVEL,
        help="zlib compression level 1-9 (default: %(default)s)",
    )
    return parser.parse_args()


def to_events() -> None:
    """Run the USGS discharge -> event-indexed NetCDF conversion."""
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    args = parse_args()

    nc_f = netCDF4.Dataset(args.forcing, 'r')

    event_ids = _load_str_var(nc_f, 'event_id')
    ts_starts = np.array(nc_f.variables['ts_start'][:], dtype=np.float64)
    ts_ends = np.array(nc_f.variables['ts_end'][:], dtype=np.float64)
    n_steps_arr = np.array(nc_f.variables['n_steps'][:], dtype=np.int32)
    max_steps = nc_f.dimensions['time_step'].size
    nc_f.close()

    n_events = len(event_ids)
    log.info('Forcing NC: %d events, max_steps=%d', n_events, max_steps)

    log.info('Reading CSV: %s', args.csv)
    df_raw = pd.read_csv(
        args.csv,
        usecols=['STAID', 'datetime', 'discharge_cfs'],
        parse_dates=['datetime'],
        date_format="%Y-%m-%d %H:%M:%S",
    )
    df_raw['STAID'] = df_raw['STAID'].astype(str).str.zfill(8)
    df_raw['datetime'] = df_raw['datetime'].dt.tz_localize('UTC')

    # pivot: rows = datetime, columns = STAID
    df_wide = df_raw.pivot_table(
        index='datetime',
        columns='STAID',
        values='discharge_cfs',
        aggfunc='mean',
    )
    df_wide.sort_index(inplace=True)

    gauge_ids: list[str] = list(df_wide.columns)
    n_gauges = len(gauge_ids)
    log.info('CSV: %d gauges, %d 15-min timesteps', n_gauges, len(df_wide))

    obs_times = df_wide.index  # DatetimeIndex, UTC-aware

    streamflow = np.full((n_events, max_steps, n_gauges), np.nan, dtype=np.float32)

    for e_idx in range(n_events):
        ns = int(n_steps_arr[e_idx])
        if ns <= 0:
            continue

        t_start = _minutes_to_timestamp(ts_starts[e_idx])
        t_end = _minutes_to_timestamp(ts_ends[e_idx])
        times_15min = pd.date_range(start=t_start, end=t_end, freq='15min')
        n_window = min(ns, len(times_15min), max_steps)

        # searchsorted for bulk alignment: O(n_window * log N) vs O(N^2)
        match_idx = obs_times.searchsorted(times_15min[:n_window])
        valid = match_idx < len(obs_times)
        exact = valid & (
            obs_times[np.where(valid, match_idx, 0)] == times_15min[:n_window]
        )

        if not exact.any():
            continue

        src_rows = match_idx[exact]
        dst_steps = np.where(exact)[0]

        chunk = df_wide.iloc[src_rows].values.astype(np.float32)
        streamflow[e_idx, dst_steps, :] = chunk

        if (e_idx + 1) % 50 == 0 or e_idx == n_events - 1:
            coverage = exact.sum()
            log.info(
                'event %d/%d: %d/%d timesteps matched',
                e_idx + 1,
                n_events,
                coverage,
                n_window,
            )

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    nc_out = netCDF4.Dataset(args.output, 'w', format='NETCDF4')
    nc_out.createDimension('event', n_events)
    nc_out.createDimension('time_step', max_steps)
    nc_out.createDimension('gauge', n_gauges)

    v = nc_out.createVariable('event_id', str, ('event',))
    v.long_name = "event ID (matches forcing_15min.nc)"
    v[:] = np.array(event_ids, dtype=object)

    v = nc_out.createVariable('ts_start', 'f8', ('event',))
    v.units = "minutes since 1970-01-01 00:00:00 UTC"
    v.long_name = "start of the 15-min event window"
    v[:] = ts_starts

    v = nc_out.createVariable('ts_end', 'f8', ('event',))
    v.units = "minutes since 1970-01-01 00:00:00 UTC"
    v.long_name = "end of the 15-min event window"
    v[:] = ts_ends

    v = nc_out.createVariable('gauge_id', str, ('gauge',))
    v.long_name = "zero-padded 8-digit USGS STAID"
    v[:] = np.array(gauge_ids, dtype=object)

    chunk_e = min(n_events, 16)
    chunk_g = min(n_gauges, 64)

    v_sf = nc_out.createVariable(
        'streamflow',
        'f4',
        ('event', 'time_step', 'gauge'),
        fill_value=np.nan,
        zlib=True,
        complevel=args.complevel,
        chunksizes=(chunk_e, max_steps, chunk_g),
    )
    v_sf.units = 'cfs'
    v_sf.long_name = "USGS discharge"
    v_sf.coordinates = "event_id ts_start ts_end gauge_id"
    v_sf[:] = streamflow

    nc_out.close()
    log.info('Done -> %s', args.output)
    log.info(
        'Shape: (%d events, %d time_steps, %d gauges)',
        n_events,
        max_steps,
        n_gauges,
    )


if __name__ == '__main__':
    to_events()
