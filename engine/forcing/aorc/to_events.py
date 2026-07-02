r"""Slice pre-extracted AORC forcing into per-event windows.

Reads a pre-extracted AORC NetCDF (output of extract.py) and, for each event
in the events CSV, writes two aggregated output files. Events whose time
window is absent from the forcing file are reported and skipped.

PET is computed via the hourly FAO-56 Penman-Monteith formula. TMP is
disaggregated to 15 min by linear interpolation; PET by uniform split
(/ 4 per step, conserving the hourly total).

Output
------
  {output_dir}/aorc_hr.nc
      dims: (event, time_step, catchment), up to 660 hourly steps
      time: [centroid - 30d, centroid - 2.5d]
      vars: APCP_surface, TMP_2maboveground, PET

  {output_dir}/aorc_15min.nc
      dims: (event, time_step, catchment), up to 480 15-min steps
      time: [centroid - 2.5d, centroid + 2.5d]
      vars: TMP_2maboveground, PET

  Both files carry per-event coordinates:
    event_id    (event,)  str
    n_steps     (event,)  int32   actual valid steps (slice [:n_steps] to unpad)
    event_start (event,)  float64 minutes since 1970-01-01 for step 0

Usage
-----
    python engine/forcing/aorc/to_events.py \\
        --events  /path/to/events.csv \\
        --forcing /path/to/aorc_extracted.nc \\
        --output-dir /path/to/output/
"""

import argparse
import time as _time
from pathlib import Path

import netCDF4
import numpy as np
import pandas as pd

from flash_preprocess.aorc import disaggregate_to_15min
from flash_preprocess.pet import penman_monteith_pet


_ANTECEDENT_DAYS = 30.0
_PRE_EVENT_DAYS = 2.5  # half of 5-day event window

_MAX_HOURLY = int((_ANTECEDENT_DAYS - _PRE_EVENT_DAYS) * 24)  # 660
_MAX_15MIN = int(2 * _PRE_EVENT_DAYS * 24 * 4)  # 480

_EVENT_ID_COL = 'event_ids'
_BEGIN_COL = 'BEGIN_DATE_TIME'
_END_COL = 'END_DATE_TIME'

_EPOCH = np.datetime64('1970-01-01T00:00', 'm')


def _load_forcing(nc_path: str) -> tuple[np.ndarray, dict, dict[str, np.ndarray]]:
    """Load a pre-extracted AORC NetCDF into memory.

    Returns
    -------
    time_dt  datetime64[m] array (n_times,)
    meta     dict with n_basins, station_ids, cat_lats, cat_lons
    data     dict var_name -> float32 array (n_catchments, n_times)
    """
    nc = netCDF4.Dataset(nc_path, 'r')
    time_dt = _EPOCH + nc.variables['time'][:].astype('timedelta64[m]')
    cat_ids = np.array(nc.variables['catchment'][:])
    meta = {
        'n_basins': len(cat_ids),
        'station_ids': cat_ids,
        'cat_lats': np.array(nc.variables['latitude'][:], dtype=np.float32),
        'cat_lons': np.array(nc.variables['longitude'][:], dtype=np.float32),
    }
    skip = {'time', 'catchment', 'latitude', 'longitude'}
    data = {v: nc.variables[v][:] for v in nc.variables if v not in skip}
    nc.close()
    return time_dt, meta, data


def _event_masks(
    centroid: np.datetime64,
    time_dt: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.datetime64, int]:
    """Return boolean masks for the antecedent and event windows."""
    ant_start = centroid - np.timedelta64(int(_ANTECEDENT_DAYS * 24 * 60), 'm')
    ant_end = (centroid - np.timedelta64(int(_PRE_EVENT_DAYS * 24 * 60), 'm')).astype(
        'datetime64[h]',
    )
    evt_end_15m = (
        centroid + np.timedelta64(int(_PRE_EVENT_DAYS * 24 * 60), 'm')
    ).astype('datetime64[m]')
    evt_end_h = evt_end_15m.astype('datetime64[h]') + np.timedelta64(1, 'h')
    n_15min = min(
        int((evt_end_15m - ant_end.astype('datetime64[m]')) / np.timedelta64(15, 'm')),
        _MAX_15MIN,
    )

    ant_mask = (time_dt >= ant_start.astype('datetime64[m]')) & (
        time_dt < ant_end.astype('datetime64[m]')
    )
    evt_mask = (time_dt >= ant_end.astype('datetime64[m]')) & (
        time_dt <= evt_end_h.astype('datetime64[m]')
    )

    return ant_mask, evt_mask, ant_end, n_15min


def _compute_pet(vd: dict[str, np.ndarray]) -> np.ndarray:
    return penman_monteith_pet(
        temp=vd['TMP_2maboveground'] - 273.15,
        spfh=vd['SPFH_2maboveground'],
        dlwrf=vd['DLWRF_surface'],
        dswrf=vd['DSWRF_surface'],
        pres=vd['PRES_surface'],
        ugrd_10m=vd['UGRD_10maboveground'],
        vgrd_10m=vd['VGRD_10maboveground'],
    )


def _create_output_nc(
    path: Path,
    n_events: int,
    max_steps: int,
    meta: dict,
    data_vars: dict[str, dict],
) -> netCDF4.Dataset:
    """Create a skeleton (event, time_step, catchment) NetCDF for streaming writes."""
    n_basins = meta['n_basins']
    nc = netCDF4.Dataset(path, 'w', format='NETCDF4')
    nc.createDimension('event', n_events)
    nc.createDimension('time_step', max_steps)
    nc.createDimension('catchment', n_basins)

    v = nc.createVariable('event_id', str, ('event',))
    v.long_name = 'event identifier'

    v = nc.createVariable('n_steps', 'i4', ('event',))
    v.long_name = 'number of valid timesteps for this event'

    v = nc.createVariable('event_start', 'f8', ('event',))
    v.units = 'minutes since 1970-01-01 00:00:00 UTC'
    v.long_name = 'time of first valid timestep (step 0)'

    v = nc.createVariable('catchment', str, ('catchment',))
    v.long_name = 'NextGen catchment ID'
    v[:] = np.array(meta['station_ids'], dtype=object)

    v = nc.createVariable('latitude', 'f4', ('catchment',))
    v.units = 'degrees_north'
    v.standard_name = 'latitude'
    v[:] = meta['cat_lats']

    v = nc.createVariable('longitude', 'f4', ('catchment',))
    v.units = 'degrees_east'
    v.standard_name = 'longitude'
    v[:] = meta['cat_lons']

    chunk_e = min(n_events, 16)
    chunk_c = min(n_basins, 64)
    for vname, attrs in data_vars.items():
        nv = nc.createVariable(
            vname,
            'f4',
            ('event', 'time_step', 'catchment'),
            fill_value=np.nan,
            zlib=True,
            complevel=4,
            chunksizes=(chunk_e, max_steps, chunk_c),
        )
        for k, val in attrs.items():
            setattr(nv, k, val)

    return nc


def main() -> None:
    """Parse CLI args and slice AORC forcing into per-event NetCDFs."""
    parser = argparse.ArgumentParser(
        description="Produce aggregated per-event AORC forcing NetCDFs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        '--events',
        required=True,
        help="CSV file with one row per event",
    )
    parser.add_argument(
        '--forcing',
        required=True,
        help="Pre-extracted AORC NetCDF (output of extract.py)",
    )
    parser.add_argument(
        '--output-dir',
        required=True,
        help="Directory for output NetCDF files",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(args.events, parse_dates=[_BEGIN_COL, _END_COL])
    df = df.dropna(subset=[_EVENT_ID_COL, _BEGIN_COL, _END_COL]).reset_index(drop=True)
    df['_centroid'] = df[_BEGIN_COL] + (df[_END_COL] - df[_BEGIN_COL]) / 2
    df['_event_id'] = df[_EVENT_ID_COL].astype(int).astype(str)
    print(
        f"Events: {len(df)}  ({df[_BEGIN_COL].min().date()} - {df[_END_COL].max().date()})",
    )

    print(f"Loading forcing: {args.forcing}")
    time_dt, meta, data = _load_forcing(args.forcing)
    print(
        f"  {meta['n_basins']} catchments  |  {len(time_dt)} hours  "
        f"({str(time_dt[0])[:16]} - {str(time_dt[-1])[:16]})",
    )

    # Pre-pass: filter to events with data in the forcing file
    valid_rows, skipped = [], 0
    for _, row in df.iterrows():
        centroid = np.datetime64(row['_centroid'], 'm')
        ant_mask, evt_mask, _, _ = _event_masks(centroid, time_dt)
        if not ant_mask.any() or not evt_mask.any():
            print(f"  [{row['_event_id']}] SKIP: time window not in forcing file")
            skipped += 1
        else:
            valid_rows.append(row)

    n_events = len(valid_rows)
    print(f"\n{n_events} valid events ({skipped} skipped)")
    if n_events == 0:
        print("Nothing to write.")
        return

    hourly_path = output_dir / 'aorc_hr.nc'
    min15_path = output_dir / 'aorc_15min.nc'

    nc_hourly = _create_output_nc(
        hourly_path,
        n_events,
        _MAX_HOURLY,
        meta,
        data_vars={
            'APCP_surface': {'units': 'kg m-2', 'long_name': 'Precipitation'},
            'TMP_2maboveground': {'units': 'K', 'long_name': 'Air temperature at 2 m'},
            'PET': {
                'units': 'mm h-1',
                'long_name': 'Penman-Monteith ET0 (FAO-56 hourly)',
            },
        },
    )
    nc_15min = _create_output_nc(
        min15_path,
        n_events,
        _MAX_15MIN,
        meta,
        data_vars={
            'TMP_2maboveground': {
                'units': 'K',
                'long_name': 'Air temperature at 2 m (interpolated)',
            },
            'PET': {
                'units': 'mm 15min-1',
                'long_name': 'Penman-Monteith ET0 (15-min, uniform split)',
            },
        },
    )

    print(f"Writing {hourly_path.name} and {min15_path.name} ...")
    t_total = _time.time()

    for i, row in enumerate(valid_rows):
        event_id = row['_event_id']
        centroid = np.datetime64(row['_centroid'], 'm')
        t0 = _time.time()

        ant_mask, evt_mask, _, n_15min = _event_masks(centroid, time_dt)
        time_ant = time_dt[ant_mask]
        time_evt = time_dt[evt_mask]

        vd_ant = {k: v[:, ant_mask] for k, v in data.items()}
        pet_ant = _compute_pet(vd_ant)
        n_ant = len(time_ant)

        nc_hourly.variables['event_id'][i] = event_id
        nc_hourly.variables['n_steps'][i] = n_ant
        nc_hourly.variables['event_start'][i] = float(
            (time_ant[0] - _EPOCH) / np.timedelta64(1, 'm'),
        )
        nc_hourly.variables['APCP_surface'][i, :n_ant, :] = vd_ant['APCP_surface'].T
        nc_hourly.variables['TMP_2maboveground'][i, :n_ant, :] = vd_ant[
            'TMP_2maboveground'
        ].T
        nc_hourly.variables['PET'][i, :n_ant, :] = pet_ant.T

        vd_evt = {k: v[:, evt_mask] for k, v in data.items()}
        pet_evt = _compute_pet(vd_evt)
        tmp_15min, _ = disaggregate_to_15min(
            vd_evt['TMP_2maboveground'],
            'TMP_2maboveground',
            time_evt,
        )
        pet_15min = np.repeat(pet_evt / 4.0, 4, axis=1)

        nc_15min.variables['event_id'][i] = event_id
        nc_15min.variables['n_steps'][i] = n_15min
        nc_15min.variables['event_start'][i] = float(
            (time_evt[0] - _EPOCH) / np.timedelta64(1, 'm'),
        )
        nc_15min.variables['TMP_2maboveground'][i, :n_15min, :] = tmp_15min[
            :,
            :n_15min,
        ].T
        nc_15min.variables['PET'][i, :n_15min, :] = pet_15min[:, :n_15min].T

        print(
            f"  [{event_id}]  hourly: {n_ant} steps  |  "
            f"15 min: {n_15min} steps  |  {_time.time() - t0:.1f}s",
        )

    nc_hourly.close()
    nc_15min.close()
    print(f"\nDone - {n_events} events in {_time.time() - t_total:.1f}s")
    print(f"  {hourly_path}")
    print(f"  {min15_path}")


if __name__ == '__main__':
    main()
