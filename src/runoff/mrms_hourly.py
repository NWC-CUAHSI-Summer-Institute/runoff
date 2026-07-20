"""Hourly MRMS QPE products -> per-event catchment precipitation.

Companion to runoff.mrms for the HOURLY accumulation products on
noaa-mrms-pds (AWS) and the Iowa State mtarchive mirror:

    radar_1h  RadarOnly_QPE_01H_00.00            radar-only 1-h accumulation
    pass1     MultiSensor_QPE_01H_Pass1_00.00    gauge-corrected, ~1 h latency
    pass2     MultiSensor_QPE_01H_Pass2_00.00    gauge-corrected, ~12 h latency

Unlike PrecipRate (a 2-min rate that runoff.mrms accumulates), these grids
are already depth in mm per hour, so extraction is: download the hourly
grids in each event window, sample the crosswalk cells, and take the
area-weighted catchment mean. No rate-to-depth conversion, no sub-hourly
resampling, no day-shard store.

Output NetCDF matches runoff.mrms.extract_all exactly (ragged CSR layout,
P in 'mm [60 min]-1'), so engine/forcing/merge_15min.py consumes it with
--timestep-min 60.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import netCDF4
from tqdm.auto import tqdm

from .mrms import (
    MRMS_LAT_MAX_EDGE,
    MRMS_LON_MIN_EDGE,
    MRMS_NLON,
    MRMS_RES,
    _decode_precip,
    _fetch_bytes as _fetch_bytes_preciprate,  # noqa: F401 (kept for parity)
)

log = logging.getLogger('mrms-hourly')

HALF = MRMS_RES / 2.0

HOURLY_PRODUCTS = {
    'radar_1h': 'RadarOnly_QPE_01H_00.00',
    'pass1': 'MultiSensor_QPE_01H_Pass1_00.00',
    'pass2': 'MultiSensor_QPE_01H_Pass2_00.00',
}


def _paths_hourly(ts: pd.Timestamp, product: str) -> tuple[str, str]:
    """(AWS S3 key, Iowa State URL) for one hourly MRMS product timestamp."""
    ts = pd.Timestamp(ts)
    d = ts.strftime('%Y%m%d')
    hms = ts.strftime('%H%M%S')
    aws = f'noaa-mrms-pds/CONUS/{product}/{d}/MRMS_{product}_{d}-{hms}.grib2.gz'
    isu_dir = product[:-6] if product.endswith('_00.00') else product
    isu = (
        f'https://mtarchive.geol.iastate.edu/{ts:%Y/%m/%d}/mrms/ncep/'
        f'{isu_dir}/{product}_{d}-{hms}.grib2.gz'
    )
    return aws, isu


def _fetch_hour(ts: pd.Timestamp, product: str, fs=None, session=None):
    """Fetch one hourly grid's gzipped GRIB2 bytes: AWS first, Iowa State next.

    Returns bytes, or None when neither source has the file.
    """
    import urllib.request

    aws_key, isu_url = _paths_hourly(ts, product)
    if fs is not None:
        try:
            return fs.cat_file(aws_key)
        except Exception:  # noqa: BLE001 -- fall through to the mirror
            pass
    try:
        if session is not None:
            r = session.get(
                isu_url,
                timeout=60,
                headers={'User-Agent': 'mrms-hourly-dl'},
            )
            if r.ok:
                return r.content
            return None
        req = urllib.request.Request(
            isu_url,
            headers={'User-Agent': 'mrms-hourly-dl'},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except Exception:  # noqa: BLE001
        return None


def _cell_latlon(cell_ids: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """MRMS cell_id -> (cell-center latitude, cell-center longitude)."""
    rows = cell_ids // MRMS_NLON
    cols = cell_ids % MRMS_NLON
    lat = MRMS_LAT_MAX_EDGE - HALF - rows * MRMS_RES
    lon = MRMS_LON_MIN_EDGE + HALF + cols * MRMS_RES
    return lat, lon


def _grid_indices(da, lat: np.ndarray, lon: np.ndarray):
    """Row/col indices of (lat, lon) cell centers in a decoded MRMS grid."""
    glat = np.asarray(da['latitude'].values, dtype='float64')
    glon = np.asarray(da['longitude'].values, dtype='float64')
    lon = lon.copy()
    if float(glon.max()) > 180.0:
        lon = lon % 360.0
    dlat = glat[1] - glat[0]
    dlon = glon[1] - glon[0]
    jj = np.rint((lat - glat[0]) / dlat).astype(np.int64)
    ii = np.rint((lon - glon[0]) / dlon).astype(np.int64)
    ok = (jj >= 0) & (jj < glat.size) & (ii >= 0) & (ii < glon.size)
    return jj, ii, ok


def extract_all_hourly(
    manifest: pd.DataFrame,
    event_catchment_windows: pd.DataFrame,
    frac_cw: pd.DataFrame,
    out_nc: Path,
    product_key: str = 'radar_1h',
    max_steps: int = 145,
    max_workers: int = 8,
    min_coverage: float = 0.9,
    use_aws: bool = True,
) -> None:
    """Extract per-event hourly MRMS QPE catchment precipitation to out_nc."""
    product = HOURLY_PRODUCTS[product_key]

    fs = None
    if use_aws:
        try:
            import s3fs

            fs = s3fs.S3FileSystem(anon=True)
        except Exception:  # noqa: BLE001
            log.warning('s3fs unavailable; falling back to the Iowa State mirror')

    cats_by_event = event_catchment_windows.groupby('storm_index')['divide_id'].apply(
        list,
    )

    # hourly QPE files are stamped on the hour and valid for the PRECEDING hour
    manifest = manifest.assign(
        grid_start=manifest['win_start'].dt.ceil('h'),
        grid_end=manifest['win_end'].dt.ceil('h'),
    )
    hours = pd.DatetimeIndex(
        sorted(
            set().union(
                *[
                    set(pd.date_range(r.grid_start, r.grid_end, freq='h'))
                    for r in manifest.itertuples()
                ],
            ),
        ),
    )
    log.info('%s: %d hourly grids to fetch', product, len(hours))

    uniq = frac_cw.drop_duplicates('cell_id')
    cell_ids = uniq['cell_id'].to_numpy(dtype=np.int64)
    cell_col = {cid: i for i, cid in enumerate(cell_ids)}
    lat, lon = _cell_latlon(cell_ids)

    vals_by_hour: dict[pd.Timestamp, np.ndarray] = {}
    idx_cache: dict[str, np.ndarray] = {}

    def _one(ts):
        raw = _fetch_hour(ts, product, fs=fs)
        if raw is None:
            return ts, None
        da = _decode_precip(raw)
        if 'jj' not in idx_cache:
            jj, ii, ok = _grid_indices(da, lat, lon)
            idx_cache['jj'], idx_cache['ii'], idx_cache['ok'] = jj, ii, ok
        jj, ii, ok = idx_cache['jj'], idx_cache['ii'], idx_cache['ok']
        grid = np.asarray(da.values, dtype='float32')
        v = np.full(cell_ids.size, np.nan, dtype='float32')
        v[ok] = grid[jj[ok], ii[ok]]
        v[v < 0] = np.nan  # MRMS negatives = missing / no coverage
        return ts, v

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_one, ts): ts for ts in hours}
        for fut in tqdm(as_completed(futures), total=len(futures), desc='download'):
            ts, v = fut.result()
            if v is not None:
                vals_by_hour[ts] = v

    log.info('fetched %d/%d hourly grids', len(vals_by_hour), len(hours))

    from scipy.sparse import csr_matrix

    records, failed = [], []
    for r in tqdm(manifest.itertuples(), total=len(manifest), desc='events'):
        sid = r.storm_index
        divide_ids = cats_by_event.get(sid)
        if divide_ids is None:
            failed.append({'storm_id': sid, 'reason': 'no catchments'})
            continue
        stamps = pd.date_range(r.grid_start, r.grid_end, freq='h')
        have = [ts for ts in stamps if ts in vals_by_hour]
        if len(have) < min_coverage * len(stamps):
            failed.append(
                {
                    'storm_id': sid,
                    'reason': f'coverage {len(have)}/{len(stamps)} hours',
                },
            )
            continue

        cw = frac_cw[frac_cw['divide_id'].isin(divide_ids)]
        if len(cw) == 0:
            failed.append({'storm_id': sid, 'reason': 'no crosswalk match'})
            continue
        col_idx = np.array([cell_col[cid] for cid in cw['cell_id']], dtype=np.int64)
        values = np.stack(
            [
                vals_by_hour.get(ts, np.full(cell_ids.size, np.nan, 'float32'))[
                    col_idx
                ]
                for ts in stamps
            ],
        )  # (time, cell)

        cats = sorted(set(cw['divide_id']))
        cat_row = {c: i for i, c in enumerate(cats)}
        W = csr_matrix(
            (
                cw['fraction_inside'].values,
                (cw['divide_id'].map(cat_row).values, np.arange(len(cw))),
            ),
            shape=(len(cats), len(cw)),
        )
        valid = ~np.isnan(values)
        num = W @ np.nan_to_num(values, nan=0.0).T
        den = W @ valid.astype(np.float32).T
        with np.errstate(invalid='ignore', divide='ignore'):
            depth = (num / den).T  # (time, cat), mm per hour
        depth[den.T == 0] = np.nan
        depth = pd.DataFrame(depth, index=stamps, columns=cats).iloc[:max_steps]

        records.append(
            {
                'storm_id': int(sid),
                'divide_ids': list(depth.columns),
                'n_steps': len(depth),
                'ts_start': depth.index[0],
                'ts_end': depth.index[-1],
                'depth': depth.values.astype('float32'),
            },
        )

    if failed:
        failed_csv = Path(out_nc).with_name('extraction_failed_events_hourly.csv')
        pd.DataFrame(failed).to_csv(failed_csv, index=False)
        log.warning('%d events failed -- logged to %s', len(failed), failed_csv)

    # ragged CSR writer (mirrors runoff.mrms.extract_all)
    n_ev = len(records)
    n_entries = [len(r['divide_ids']) for r in records]
    total_entries = sum(n_entries)

    Path(out_nc).parent.mkdir(parents=True, exist_ok=True)
    nc = netCDF4.Dataset(out_nc, 'w', format='NETCDF4')
    nc.createDimension('event', n_ev)
    nc.createDimension('ptr', n_ev + 1)
    nc.createDimension('entry', total_entries)
    nc.createDimension('time_step', max_steps)

    nc.createVariable('storm_id', 'i4', ('event',))[:] = np.array(
        [r['storm_id'] for r in records],
        dtype=np.int32,
    )
    nc.createVariable('n_steps', 'i4', ('event',))[:] = np.array(
        [r['n_steps'] for r in records],
        dtype=np.int32,
    )
    epoch = np.datetime64('1970-01-01T00:00', 'm')
    v_ts = nc.createVariable('ts_start', 'f8', ('event',))
    v_ts.units = 'minutes since 1970-01-01 00:00:00 UTC'
    v_ts[:] = [
        (np.datetime64(r['ts_start']) - epoch) / np.timedelta64(1, 'm')
        for r in records
    ]
    v_te = nc.createVariable('ts_end', 'f8', ('event',))
    v_te.units = 'minutes since 1970-01-01 00:00:00 UTC'
    v_te[:] = [
        (np.datetime64(r['ts_end']) - epoch) / np.timedelta64(1, 'm')
        for r in records
    ]
    cat_ptr = np.zeros(n_ev + 1, dtype=np.int64)
    cat_ptr[1:] = np.cumsum(n_entries)
    nc.createVariable('cat_ptr', 'i8', ('ptr',))[:] = cat_ptr
    v_cat = nc.createVariable('divide_id', str, ('entry',))
    v_p = nc.createVariable(
        'P',
        'f4',
        ('entry', 'time_step'),
        fill_value=np.nan,
        zlib=True,
        complevel=4,
        chunksizes=(min(max(total_entries, 1), 4096), max_steps),
    )
    v_p.units = 'mm [60 min]-1'
    v_p.long_name = f'MRMS {product} precipitation depth'

    for i, r in enumerate(tqdm(records, desc='write')):
        lo, hi = int(cat_ptr[i]), int(cat_ptr[i + 1])
        v_cat[lo:hi] = np.array(r['divide_ids'], dtype=object)
        n = r['n_steps']
        block = np.full((hi - lo, max_steps), np.nan, dtype=np.float32)
        block[:, :n] = r['depth'].T
        v_p[lo:hi, :] = block
    nc.close()
    log.info(
        'wrote %s: %d events x %d steps (%s)',
        out_nc,
        n_ev,
        max_steps,
        product,
    )
