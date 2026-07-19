"""MRMS + Hydrofabric pipeline.

- CONUS cell/catchment crosswalk
- per-event upstream catchments
- raw MRMS PrecipRate download
- area-weighted 2-min -> 15-min catchment precipitation extraction.

@drworm
"""

import gzip
import logging
import os
import pickle
import tempfile
import time
import urllib.error
import urllib.request
import warnings
from collections.abc import Callable, Iterable
from typing import Any
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor
from concurrent.futures import wait as _futures_wait
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

import geopandas as gpd
import netCDF4
import numpy as np
import pandas as pd
import pyproj
import xarray as xr
from scipy.sparse import csr_matrix
from shapely.geometry import box
from tqdm.auto import tqdm

try:
    import s3fs
except RuntimeError:
    s3fs = None

try:
    import eccodes

    _HAVE_ECCODES = True
except RuntimeError:
    _HAVE_ECCODES = False

from flash_preprocess.paths import HYDROFABRIC_GPKG as GPKG_PATH
from flash_preprocess.utils import build_upstream_graph, expand_upstream

pyproj.network.set_network_enabled(False)
warnings.filterwarnings('ignore')

log = logging.getLogger('MRMS')


### DEFAULTS --------------- #
# MRMS CONUS grid
MRMS_LON_MIN_EDGE = -130.0
MRMS_LAT_MAX_EDGE = 55.0
MRMS_RES = 0.01
MRMS_NLON = 7000
MRMS_NLAT = 3500
HALF = MRMS_RES / 2.0
AREA_CRS = 5070
PAD_CELLS = 2

WINDOW_DAYS = 6.0  # Event forcing window width (days)

HF_LAYERS = [
    'divide_id',
    'vpuid',
    'nexus_id',
    'nexus_type',
    'is_terminal',
    'geometry',
]

_CONFIRMED_MISSING = object()  # Precip missing in AWS + Iowa State
_AWS_RETRIES = 2  # Extra AWS attempts before fallback to Iowa State
_AWS_BACKOFF_S = 0.25  # Buffer between AWS retries (attempt * backoff)
# -------------------------- #


def _atomic_write(write_fn: Callable[[str], None], path: str | Path) -> None:
    """write_fn(tmp_path) then atomic rename -- avoids a torn file if a
    concurrent VPU run races to build the same shared cache entry.
    """
    tmp = f'{path}.tmp{os.getpid()}'
    write_fn(tmp)
    os.replace(tmp, path)


#### Hydrofabric handling


def load_hydrofabric(
    cache_dir: Path,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Load (or build and cache) the CONUS hydrofabric layers."""
    cache_dir = Path(cache_dir)
    f_cm, f_net, f_fp, f_nx = (
        cache_dir / 'catchments_master.parquet',
        cache_dir / 'network.parquet',
        cache_dir / 'flowpaths.parquet',
        cache_dir / 'nexus.parquet',
    )
    if f_cm.exists() and f_net.exists() and f_fp.exists() and f_nx.exists():
        return (
            gpd.read_parquet(f_cm),
            pd.read_parquet(f_net),
            gpd.read_parquet(f_fp),
            gpd.read_parquet(f_nx),
        )

    network = gpd.read_file(
        GPKG_PATH,
        layer='network',
        columns=['id', 'toid', 'divide_id', 'vpuid'],
        read_geometry=False,
    )
    divides = gpd.read_file(
        GPKG_PATH,
        layer='divides',
        columns=['divide_id', 'id', 'toid', 'areasqkm'],
    )
    flowpaths = gpd.read_file(GPKG_PATH, layer='flowpaths', columns=['id', 'toid'])
    nexus = gpd.read_file(GPKG_PATH, layer='nexus', columns=['id', 'toid'])

    net_lookup = network.dropna(subset=['divide_id']).drop_duplicates('divide_id')[
        ['divide_id', 'vpuid']
    ]
    catchments_master = divides.rename(
        columns={'id': 'flowpath_id', 'toid': 'nexus_id'},
    ).merge(net_lookup, on='divide_id', how='left')
    catchments_master['nexus_type'] = (
        catchments_master['nexus_id'].astype(str).str.extract(r'^([a-z]+)-')[0]
    )
    catchments_master['is_terminal'] = catchments_master['nexus_type'].isin(
        ['tnx', 'cnx', 'inx'],
    )

    nexus_next = dict(zip(nexus['id'].astype(str), nexus['toid'].astype(str)))
    net_wb = network[network['id'].astype(str).str.startswith('wb-')]
    wb_next = dict(zip(net_wb['id'].astype(str), net_wb['toid'].astype(str)))
    TERMINAL_WB = 'wb-0'
    terminal_cache = {}

    def _down_nexus(n: str) -> str | None:
        """Next nexus id downstream of n, or None at the terminal waterbody."""
        wb = nexus_next.get(n)
        if wb is None or wb == TERMINAL_WB:
            return None
        return wb_next.get(wb)

    def terminal_of(start: str) -> str:
        """Walk downstream from `start` to its terminal nexus id, with memoization."""
        path, cur, seen = [], start, set()
        while cur is not None and cur not in terminal_cache:
            if cur in seen:
                for p in path:
                    terminal_cache[p] = cur
                return cur
            seen.add(cur)
            path.append(cur)
            nxt = _down_nexus(cur)
            if nxt is None:
                terminal_cache[cur] = cur
                break
            cur = nxt
        result = terminal_cache.get(cur, cur)
        for p in path:
            terminal_cache[p] = result
        return result

    for n in set(catchments_master['nexus_id'].astype(str)):
        terminal_of(n)
    catchments_master['terminal_nexus_id'] = (
        catchments_master['nexus_id'].astype(str).map(terminal_cache)
    )
    catchments_master['terminal_is_clean'] = ~catchments_master[
        'terminal_nexus_id'
    ].astype(str).str.startswith('nex-')

    cache_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(lambda p: catchments_master.to_parquet(p), f_cm)
    _atomic_write(lambda p: network.to_parquet(p), f_net)
    _atomic_write(lambda p: flowpaths.to_parquet(p), f_fp)
    _atomic_write(lambda p: nexus.to_parquet(p), f_nx)
    return catchments_master, network, flowpaths, nexus


#### CONUS MRMS-cell <-> catchment crosswalk (center-based)


def mrms_centers_for_bbox(
    west: float,
    east: float,
    south: float,
    north: float,
    pad: int = 1,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
    """Return row/col/lon/lat arrays of MRMS cell centers covering a bbox."""
    i0 = max(0, int(np.floor((west - MRMS_LON_MIN_EDGE - HALF) / MRMS_RES)) - pad)
    i1 = min(
        MRMS_NLON - 1,
        int(np.ceil((east - MRMS_LON_MIN_EDGE - HALF) / MRMS_RES)) + pad,
    )
    j0 = max(0, int(np.floor((MRMS_LAT_MAX_EDGE - HALF - north) / MRMS_RES)) - pad)
    j1 = min(
        MRMS_NLAT - 1,
        int(np.ceil((MRMS_LAT_MAX_EDGE - HALF - south) / MRMS_RES)) + pad,
    )
    if i1 < i0 or j1 < j0:
        return None
    cols, rows = np.arange(i0, i1 + 1), np.arange(j0, j1 + 1)
    LON, LAT = np.meshgrid(
        MRMS_LON_MIN_EDGE + HALF + cols * MRMS_RES,
        MRMS_LAT_MAX_EDGE - HALF - rows * MRMS_RES,
    )
    COL, ROW = np.meshgrid(cols, rows)
    return ROW.ravel(), COL.ravel(), LON.ravel(), LAT.ravel()


def build_crosswalk(
    catchments_master: gpd.GeoDataFrame,
    cache_dir: Path,
    vpus: Iterable[str] | None = None,
) -> pd.DataFrame:
    """Build (or load cached) MRMS cell-to-catchment crosswalk."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    target_vpus = sorted(vpus) if vpus is not None else sorted(
        catchments_master['vpuid'].dropna().unique(),
    )
    parts = []
    t0 = time.time()
    for vpu in target_vpus:
        out = cache_dir / f'crosswalk_vpu_{vpu}.parquet'
        if out.exists():
            parts.append(pd.read_parquet(out))
            continue
        sub = catchments_master[catchments_master['vpuid'] == vpu]
        west, south, east, north = sub.to_crs(4326).total_bounds
        res = mrms_centers_for_bbox(west, east, south, north)
        if res is None:
            continue
        row, col, lon, lat = res
        pts = gpd.GeoDataFrame(
            {
                'cell_id': row.astype(np.int64) * MRMS_NLON + col.astype(np.int64),
                'row': row.astype(np.int32),
                'col': col.astype(np.int32),
                'lon': lon,
                'lat': lat,
            },
            geometry=gpd.points_from_xy(lon, lat),
            crs=4326,
        ).to_crs(5070)
        joined = gpd.sjoin(pts, sub[HF_LAYERS], how='inner', predicate='within')
        df = pd.DataFrame(
            joined.drop(columns=['geometry', 'index_right']),
        ).drop_duplicates('cell_id')
        _atomic_write(lambda p, df=df: df.to_parquet(p, index=False), out)
        parts.append(df)
    crosswalk = pd.concat(parts, ignore_index=True).drop_duplicates('cell_id')
    log.info(
        'crosswalk built/loaded in %.0fs: %d cells across %d VPU(s)',
        time.time() - t0,
        len(crosswalk),
        len(target_vpus),
    )
    return crosswalk


#### per-event manifest: 5-day window + true upstream catchments


def _upstream_graph(cache_dir: Path) -> dict:
    """Build (or load cached) the CONUS upstream-catchment graph."""
    f = Path(cache_dir) / 'upstream_graph.pkl'
    if f.exists():
        return pickle.loads(f.read_bytes())
    graph = build_upstream_graph(str(GPKG_PATH))
    _atomic_write(lambda p: Path(p).write_bytes(pickle.dumps(graph)), f)
    return graph


def build_manifest(
    events: pd.DataFrame,
    cache_dir: Path,
    tag: str = '',
    window_days: float = WINDOW_DAYS,
    centroid: str = 'midpoint',
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Resolve each event to its upstream catchments and a forcing window.

    Parameters
    ----------
    window_days
        Total width of each event's forcing window, in days (e.g. 5 or 6).
        The window is centered on `centroid` and extends window_days/2 on
        each side.
    centroid
        'midpoint' (default): window centered on the mean of BEGIN_DATE_TIME
        and END_DATE_TIME. 'peak': window centered on the event's peak_time
        column instead (requires a `peak_time` column in `events`).
    """
    cache_dir = Path(cache_dir)
    suffix = f'_{tag}' if tag else ''
    f_manifest = cache_dir / f'manifest_out{suffix}.parquet'
    f_windows = cache_dir / f'event_catchment_windows{suffix}.parquet'
    if f_manifest.exists() and f_windows.exists():
        return pd.read_parquet(f_manifest), pd.read_parquet(f_windows)

    if centroid not in ('midpoint', 'peak'):
        raise ValueError(f"centroid must be 'midpoint' or 'peak', got {centroid!r}")

    window_half = pd.to_timedelta(window_days / 2, unit='D')

    graph = _upstream_graph(cache_dir)
    upstream_cache: dict[str, set] = {}

    def upstream_cats_of(cat_id: str) -> set:
        """Upstream catchment ids of cat_id, cached across calls."""
        if cat_id not in upstream_cache:
            upstream_cache[cat_id] = expand_upstream({cat_id}, graph)
        return upstream_cache[cat_id]

    ev = events.copy()
    ev['begin_time'] = pd.to_datetime(
        ev['BEGIN_DATE_TIME'],
        errors='coerce',
        utc=True,
    ).dt.tz_localize(None)
    ev['end_time'] = pd.to_datetime(
        ev['END_DATE_TIME'],
        errors='coerce',
        utc=True,
    ).dt.tz_localize(None)

    if centroid == 'peak':
        if 'peak_time' not in ev.columns:
            raise ValueError("centroid='peak' requires a 'peak_time' column in events")
        ev['centroid_time'] = pd.to_datetime(
            ev['peak_time'],
            errors='coerce',
            utc=True,
        ).dt.tz_localize(None)
    else:
        ev['centroid_time'] = ev['begin_time'] + (ev['end_time'] - ev['begin_time']) / 2

    ev['win_start'] = (ev['centroid_time'] - window_half).dt.floor('h')
    ev['win_end'] = (ev['centroid_time'] + window_half).dt.ceil('h')

    rows = []
    for _, s in ev.iterrows():
        cats = upstream_cats_of(s['gage_cat-id'])
        if not cats:
            continue
        rows.append(
            {
                'storm_index': str(s['event_id']),
                'recording_gage_STAID': str(s['STAID']).zfill(8),
                'n_catchments': len(cats),
                'win_start': s['win_start'],
                'win_end': s['win_end'],
                '_divide_ids': sorted(cats),
            },
        )
    manifest = pd.DataFrame(rows)
    event_catchment_windows = (
        manifest.explode('_divide_ids')
        .rename(columns={'_divide_ids': 'divide_id'})
        .dropna(subset=['divide_id'])[
            ['storm_index', 'recording_gage_STAID', 'divide_id', 'win_start', 'win_end']
        ]
        .reset_index(drop=True)
    )
    manifest_out = manifest.drop(columns='_divide_ids')

    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_out.to_parquet(f_manifest, index=False)
    event_catchment_windows.to_parquet(f_windows, index=False)
    return manifest_out, event_catchment_windows


#### fractional (area-weighted) MRMS-cell -> catchment crosswalk


def _lon_from_col(col: np.ndarray) -> np.ndarray:
    """MRMS grid column index -> cell-center longitude."""
    return MRMS_LON_MIN_EDGE + HALF + col * MRMS_RES


def _lat_from_row(row: np.ndarray) -> np.ndarray:
    """MRMS grid row index -> cell-center latitude."""
    return MRMS_LAT_MAX_EDGE - HALF - row * MRMS_RES


def _cell_id(row: np.ndarray, col: np.ndarray) -> np.ndarray:
    """Flatten (row, col) grid indices into a single MRMS cell id."""
    return row.astype(np.int64) * MRMS_NLON + col.astype(np.int64)


def _mrms_cell_polys(rows: np.ndarray, cols: np.ndarray) -> gpd.GeoDataFrame:
    """Build square cell polygons (area CRS) for the given grid row/col indices."""
    rows, cols = np.asarray(rows, dtype=np.int32), np.asarray(cols, dtype=np.int32)
    lon, lat = _lon_from_col(cols), _lat_from_row(rows)
    gdf = gpd.GeoDataFrame(
        {
            'cell_id': _cell_id(rows, cols),
            'row': rows,
            'col': cols,
            'lon': lon,
            'lat': lat,
            'geometry': [
                box(x - HALF, y - HALF, x + HALF, y + HALF) for x, y in zip(lon, lat)
            ],
        },
        geometry='geometry',
        crs='EPSG:4326',
    ).to_crs(AREA_CRS)
    gdf['cell_area_m2'] = gdf.geometry.area
    return gdf


def _rows_cols_for_bbox(
    west: float, south: float, east: float, north: float, pad: int = 2,
) -> pd.DataFrame:
    """All MRMS grid (row, col) pairs covering a bbox, padded by `pad` cells."""
    i0 = max(0, int(np.floor((west - MRMS_LON_MIN_EDGE - HALF) / MRMS_RES)) - pad)
    i1 = min(
        MRMS_NLON - 1,
        int(np.ceil((east - MRMS_LON_MIN_EDGE - HALF) / MRMS_RES)) + pad,
    )
    j0 = max(0, int(np.floor((MRMS_LAT_MAX_EDGE - HALF - north) / MRMS_RES)) - pad)
    j1 = min(
        MRMS_NLAT - 1,
        int(np.ceil((MRMS_LAT_MAX_EDGE - HALF - south) / MRMS_RES)) + pad,
    )
    if i1 < i0 or j1 < j0:
        return pd.DataFrame(columns=['row', 'col'])
    COL, ROW = np.meshgrid(np.arange(i0, i1 + 1), np.arange(j0, j1 + 1))
    return pd.DataFrame(
        {'row': ROW.ravel().astype(np.int32), 'col': COL.ravel().astype(np.int32)},
    )


def _add_neighbors(df: pd.DataFrame, pad: int = 2) -> pd.DataFrame:
    """Expand each (row, col) pair in df to include all cells within `pad` steps."""
    base = df[['row', 'col']].drop_duplicates()
    pieces = []
    for dr in range(-pad, pad + 1):
        for dc in range(-pad, pad + 1):
            t = base.copy()
            t['row'] += dr
            t['col'] += dc
            pieces.append(t)
    out = pd.concat(pieces, ignore_index=True)
    out = out[
        out['row'].between(0, MRMS_NLAT - 1) & out['col'].between(0, MRMS_NLON - 1)
    ]
    return out.drop_duplicates().reset_index(drop=True)


def _intersection_area(candidates: gpd.GeoDataFrame, chunk: int = 150_000) -> np.ndarray:
    """Cell/catchment intersection area (m^2) per row, chunked to bound peak memory."""
    areas = []
    for s in range(0, len(candidates), chunk):
        c = candidates.iloc[s : s + chunk]
        cat_geoms = gpd.GeoSeries(c['catchment_geometry'], crs=AREA_CRS, index=c.index)
        areas.append(c.geometry.intersection(cat_geoms).area.to_numpy())
    return np.concatenate(areas)


def build_fractional_crosswalk(
    divide_ids: Iterable[str],
    catchments_master: gpd.GeoDataFrame,
    crosswalk: pd.DataFrame,
    cache_dir: Path,
) -> pd.DataFrame:
    """Build (or load cached) per-VPU area-fractional MRMS cell crosswalk."""
    cache_dir = Path(cache_dir)
    sub = (
        catchments_master[catchments_master['divide_id'].isin(divide_ids)]
        .to_crs(AREA_CRS)
        .copy()
    )
    sub['geometry'] = sub.geometry.buffer(0)
    sel_cw = crosswalk[crosswalk['divide_id'].isin(divide_ids)]

    vpu_cache = cache_dir / 'fractional_vpu_cache'
    vpu_cache.mkdir(parents=True, exist_ok=True)
    parts = []
    for vpu in sorted(sub['vpuid'].dropna().astype(str).unique()):
        vsub = sub[sub['vpuid'].astype(str) == vpu]
        divide_ids_vpu = vsub['divide_id'].dropna().unique()

        out_file = vpu_cache / f'vpu_{vpu}.parquet'
        if out_file.exists():
            cached_vpu = pd.read_parquet(out_file)
            if set(divide_ids_vpu) <= set(cached_vpu['divide_id']):
                parts.append(cached_vpu[cached_vpu['divide_id'].isin(divide_ids_vpu)])
                continue

        cw_vpu = sel_cw[sel_cw['divide_id'].isin(divide_ids_vpu)][
            ['divide_id', 'cell_id', 'row', 'col']
        ]
        candidate_cells = _add_neighbors(cw_vpu[['row', 'col']], pad=PAD_CELLS)

        has_center = set(cw_vpu['divide_id'].dropna().unique())
        missing = vsub[~vsub['divide_id'].isin(has_center)]
        if len(missing):
            extra = [
                _rows_cols_for_bbox(*geom.bounds, pad=PAD_CELLS)
                for geom in missing.to_crs(4326).geometry
            ]
            candidate_cells = pd.concat([candidate_cells] + extra, ignore_index=True)
        candidate_cells = candidate_cells.drop_duplicates(['row', 'col']).reset_index(
            drop=True,
        )
        if len(candidate_cells) == 0:
            continue

        mrms_cells = _mrms_cell_polys(
            candidate_cells['row'].values,
            candidate_cells['col'].values,
        )
        join_cols = [
            c
            for c in [
                'divide_id',
                'vpuid',
                'nexus_id',
                'nexus_type',
                'is_terminal',
                'terminal_nexus_id',
                'geometry',
            ]
            if c in vsub.columns
        ]
        candidates = gpd.sjoin(
            mrms_cells,
            vsub[join_cols],
            how='inner',
            predicate='intersects',
        )
        if len(candidates) == 0:
            continue
        candidates = candidates.join(
            vsub[join_cols].geometry.rename('catchment_geometry'),
            on='index_right',
        )
        candidates['intersection_area_m2'] = _intersection_area(candidates)
        candidates = candidates[candidates['intersection_area_m2'] > 0.01].copy()
        candidates['fraction_inside'] = (
            candidates['intersection_area_m2'] / candidates['cell_area_m2']
        ).clip(0, 1)

        keep = [
            'cell_id',
            'row',
            'col',
            'lon',
            'lat',
            'divide_id',
            'vpuid',
            'cell_area_m2',
            'intersection_area_m2',
            'fraction_inside',
        ]
        df = (
            pd.DataFrame(candidates[keep])
            .drop_duplicates(['divide_id', 'cell_id'])
            .sort_values(['divide_id', 'cell_id'])
            .reset_index(drop=True)
        )
        df['weight'] = df['fraction_inside'] / df.groupby('divide_id')[
            'fraction_inside'
        ].transform('sum')
        _atomic_write(lambda p, df=df: df.to_parquet(p, index=False), out_file)
        parts.append(df)

    result = (
        pd.concat(parts, ignore_index=True)
        .drop_duplicates(['divide_id', 'cell_id'])
        .reset_index(drop=True)
    )
    result['weight'] = result['fraction_inside'] / result.groupby('divide_id')[
        'fraction_inside'
    ].transform('sum')
    result['lon_360'] = result['lon'] % 360.0
    return result


#### raw MRMS PrecipRate download (AWS + Iowa State, bbox-subset in memory)


def _paths(ts: pd.Timestamp) -> tuple[str, str]:
    """(AWS S3 key, Iowa State URL) for one MRMS PrecipRate timestamp."""
    ts = pd.Timestamp(ts)
    d = ts.strftime('%Y%m%d')
    hms = ts.strftime('%H%M%S')
    aws = f'noaa-mrms-pds/CONUS/PrecipRate_00.00/{d}/MRMS_PrecipRate_00.00_{d}-{hms}.grib2.gz'
    isu = (
        f'https://mtarchive.geol.iastate.edu/{ts:%Y/%m/%d}/mrms/ncep/'
        f'PrecipRate/PrecipRate_00.00_{d}-{hms}.grib2.gz'
    )
    return aws, isu


def _fetch_bytes(ts: pd.Timestamp, fs: Any | None, session: requests.Session | None = None) -> Any:
    """Fetch one timestamp's raw gzipped GRIB2 bytes: AWS first, then Iowa
    State. Returns None on transient failure, or _CONFIRMED_MISSING if both
    sources report the file doesn't exist.
    """
    aws_key, isu_url = _paths(ts)
    aws_missing = isu_missing = False

    if fs is not None:
        for attempt in range(_AWS_RETRIES + 1):
            try:
                return fs.cat_file(aws_key)
            except FileNotFoundError:
                aws_missing = True
                break
            except RuntimeError:
                if attempt < _AWS_RETRIES:
                    time.sleep(_AWS_BACKOFF_S * (attempt + 1))
                # else: retries exhausted -> fall through to Iowa State

    try:
        if session is not None:
            r = session.get(isu_url, timeout=60, headers={'User-Agent': 'mrms-bbox-dl'})
            if r.status_code == 404:
                isu_missing = True
            elif r.ok:
                return r.content
        else:
            req = urllib.request.Request(
                isu_url,
                headers={'User-Agent': 'mrms-bbox-dl'},
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            isu_missing = True
    except RuntimeError:
        pass

    if aws_missing and isu_missing:
        return _CONFIRMED_MISSING
    return None


def _decode_precip_eccodes(raw_gz: bytes) -> xr.DataArray:
    """Decode a gzipped GRIB2 PrecipRate message via eccodes (fast path)."""
    grib = gzip.decompress(raw_gz)
    gid = eccodes.codes_new_from_message(grib)
    try:
        ni = eccodes.codes_get(gid, 'Ni')
        nj = eccodes.codes_get(gid, 'Nj')
        lat1 = eccodes.codes_get(gid, 'latitudeOfFirstGridPointInDegrees')
        lon1 = eccodes.codes_get(gid, 'longitudeOfFirstGridPointInDegrees')
        di = eccodes.codes_get(gid, 'iDirectionIncrementInDegrees')
        dj = eccodes.codes_get(gid, 'jDirectionIncrementInDegrees')
        i_neg = bool(eccodes.codes_get(gid, 'iScansNegatively'))
        j_pos = bool(eccodes.codes_get(gid, 'jScansPositively'))
        missing_value = eccodes.codes_get(gid, 'missingValue')
        values = eccodes.codes_get_values(gid).astype('float32')
    finally:
        eccodes.codes_release(gid)

    values = values.reshape(nj, ni)
    values[values == missing_value] = np.nan
    lon = lon1 + np.arange(ni, dtype='float64') * di * (-1.0 if i_neg else 1.0)
    lat = lat1 + np.arange(nj, dtype='float64') * dj * (1.0 if j_pos else -1.0)
    return xr.DataArray(
        values,
        dims=('latitude', 'longitude'),
        coords={'latitude': lat, 'longitude': lon},
        name='precip_rate',
    )


def _decode_precip_cfgrib(raw_gz: bytes) -> xr.DataArray:
    """Decode a gzipped GRIB2 PrecipRate message via cfgrib (fallback path)."""
    grib = gzip.decompress(raw_gz)
    fd, tmp = tempfile.mkstemp(suffix='.grib2')
    os.close(fd)
    try:
        with open(tmp, 'wb') as f:
            f.write(grib)
        with xr.open_dataset(
            tmp,
            engine='cfgrib',
            backend_kwargs={'indexpath': ''},
        ) as ds:
            ds = ds.load()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    var = list(ds.data_vars)[0]
    da = ds[var].rename('precip_rate')
    drop = [c for c in da.coords if c not in ('latitude', 'longitude')]
    return da.drop_vars(drop, errors='ignore')


def _decode_precip(raw_gz: bytes) -> xr.DataArray:
    """Decode a gzipped GRIB2 PrecipRate message, preferring eccodes."""
    if _HAVE_ECCODES:
        try:
            return _decode_precip_eccodes(raw_gz)
        except Exception:  # noqa: BLE001
            pass  # fall through to cfgrib
    return _decode_precip_cfgrib(raw_gz)


def _subset_bbox(
    da: xr.DataArray, bbox: tuple[float, float, float, float],
) -> xr.DataArray:
    """Slice a decoded grid to (lon_min, lat_min, lon_max, lat_max), handling
    both ascending/descending coordinate order and 0-360 longitude grids.
    """
    lon_min, lat_min, lon_max, lat_max = bbox
    lon, lat = da['longitude'], da['latitude']
    if float(lon.max()) > 180.0:
        lon_min, lon_max = lon_min % 360.0, lon_max % 360.0
    lat_slice = (
        slice(lat_min, lat_max)
        if float(lat[0]) < float(lat[-1])
        else slice(lat_max, lat_min)
    )
    lon_slice = (
        slice(lon_min, lon_max)
        if float(lon[0]) < float(lon[-1])
        else slice(lon_max, lon_min)
    )
    return da.sel(latitude=lat_slice, longitude=lon_slice)


def _process_one(
    ts: pd.Timestamp,
    bbox: tuple[float, float, float, float],
    fs: Any | None,
    mask_negative: bool = True,
    session: requests.Session | None = None,
) -> tuple[str, pd.Timestamp, xr.DataArray | None]:
    """Fetch, decode, and bbox-subset one timestamp.

    Returns (status, ts, data) where status is 'ok', 'missing', or 'error'.
    Never raises -- a malformed/failed timestamp is reported, not fatal.
    """
    raw = _fetch_bytes(ts, fs, session)
    if raw is _CONFIRMED_MISSING:
        return ('missing', ts, None)
    if not isinstance(raw, (bytes, bytearray)):
        return ('error', ts, None)
    try:
        da = _decode_precip(raw)
        da = _subset_bbox(da, bbox)
        if mask_negative:
            da = da.where(da >= 0)  # -3 = "no radar coverage" -> NaN
        da = da.expand_dims(time=[np.datetime64(pd.Timestamp(ts))]).astype('float32')
        return ('ok', ts, da)
    except Exception:  # noqa: BLE001
        return ('error', ts, None)


def _read_existing(path: str) -> tuple[set, xr.DataArray | None]:
    """Load an existing per-day shard file's timestamps and precip_rate grid."""
    if not os.path.exists(path):
        return set(), None
    try:
        with xr.open_dataset(path) as _ds:
            existing = _ds.load()
        return set(pd.to_datetime(existing['time'].values)), existing['precip_rate']
    except Exception:  # noqa: BLE001
        return set(), None


def _read_existing_times(path: str) -> set:
    """Cheap variant of _read_existing.

    just the set of already-stored timestamps, without loading the (potentially
    large) precip_rate grid.
    """
    if not os.path.exists(path):
        return set()
    try:
        with xr.open_dataset(path) as _ds:
            return set(pd.to_datetime(_ds['time'].values))
    except Exception:  # noqa: BLE001
        return set()


def _write_day(
    path: str, existing_da: xr.DataArray | None, new_slices: list[xr.DataArray],
) -> int:
    """Merge existing + newly-fetched slices for one day and write the shard file.

    Returns the total number of stored timestamps.
    """
    parts = []
    if existing_da is not None:
        parts.append(existing_da)
    if new_slices:
        parts.append(xr.concat(new_slices, dim='time'))
    if not parts:
        return 0
    da_all = xr.concat(parts, dim='time').sortby('time').drop_duplicates('time')
    da_all = da_all.assign_coords(
        time=pd.DatetimeIndex(pd.to_datetime(da_all['time'].values)),
    )
    ds_out = da_all.to_dataset(name='precip_rate')
    enc = {
        'precip_rate': {'zlib': True, 'complevel': 4, 'dtype': 'float32'},
        'time': {'units': "minutes since 2000-01-01 00:00:00", 'dtype': 'float64'},
    }
    tmp = path + '.tmp'
    ds_out.to_netcdf(tmp, encoding=enc)
    os.replace(tmp, path)
    return int(da_all['time'].size)


def _run_pass(
    days: list[pd.Timestamp],
    todo_by_day: dict[pd.Timestamp, list],
    bbox: tuple[float, float, float, float],
    fs: Any | None,
    mask_negative: bool,
    pool: ThreadPoolExecutor,
    bar: tqdm | None,
    shard_dir: str,
    max_workers: int,
    session: requests.Session | None = None,
) -> tuple[dict, list, dict]:
    """Fetch every (day, timestamp) using one rolling submission window across
    all days so pool continuously saturated across day boundaries.

    Returns (results_by_day, new_missing_timestamps, transient_fail_by_day).
    """
    flat = [(d, t) for d in days for t in (todo_by_day.get(d) or [])]
    results: dict = {}
    if not flat:
        return results, [], {}

    queue_window = max(max_workers * 4, 64)
    it = iter(flat)
    day_buffers: dict[pd.Timestamp, list] = {}
    day_remaining = {
        d: len(todo_by_day.get(d) or []) for d in days if todo_by_day.get(d)
    }
    day_stats = {d: {'new': 0, 'missing': 0, 'errors': 0} for d in day_remaining}
    new_missing: list = []
    fail_by_day: dict[pd.Timestamp, list] = {}

    fut_to_dt: dict = {}
    pending: set = set()

    def _submit_next() -> None:
        """Submit the next queued (day, timestamp) fetch, if any remain."""
        item = next(it, None)
        if item is None:
            return
        d, t = item
        fut = pool.submit(_process_one, t, bbox, fs, mask_negative, session)
        fut_to_dt[fut] = (d, t)
        pending.add(fut)

    for _ in range(queue_window):
        _submit_next()
        if not pending:
            break

    def _flush_day(d: pd.Timestamp) -> None:
        """Write day d's buffered slices to its shard file and record results."""
        path = os.path.join(shard_dir, f'pr_{d:%Y%m%d}.nc')
        _, existing_da = _read_existing(path)
        n_stored = _write_day(path, existing_da, day_buffers.pop(d, []))
        stats = day_stats[d]
        results[d] = {
            'date': f'{d:%Y-%m-%d}',
            'stored': n_stored,
            'new': stats['new'],
            'missing': stats['missing'],
            'errors': stats['errors'],
        }

    while pending:
        done, pending = _futures_wait(pending, return_when=FIRST_COMPLETED)
        for fut in done:
            d, t = fut_to_dt.pop(fut)
            status, ts, da = fut.result()
            if status == 'ok':
                day_buffers.setdefault(d, []).append(da)
                day_stats[d]['new'] += 1
            elif status == 'missing':
                day_stats[d]['missing'] += 1
                new_missing.append(pd.Timestamp(ts))
            else:
                day_stats[d]['errors'] += 1
                fail_by_day.setdefault(d, []).append(pd.Timestamp(ts))

            day_remaining[d] -= 1
            if day_remaining[d] == 0:
                _flush_day(d)

            if bar is not None:
                bar.set_postfix(
                    day=f'{d:%Y-%m-%d}',
                    miss=day_stats[d]['missing'],
                    err=day_stats[d]['errors'],
                    refresh=False,
                )
                bar.update(1)

            _submit_next()

    return results, new_missing, fail_by_day


def build_store(
    unique_times: Iterable,
    bbox: tuple[float, float, float, float],
    out_dir: str | Path,
    max_workers: int = 12,
    mask_negative: bool = True,
    use_aws: bool = True,
    verbose: bool = True,
    max_retries: int = 3,
) -> dict:
    """Download + bbox-subset every timestamp."""
    shard_dir = os.path.join(out_dir, 'shards')
    os.makedirs(shard_dir, exist_ok=True)
    missing_csv = os.path.join(out_dir, 'known_missing.csv')
    manifest_csv = os.path.join(out_dir, 'build_manifest.csv')
    transient_csv = os.path.join(out_dir, 'transient_errors.csv')

    times = pd.DatetimeIndex(pd.to_datetime(list(unique_times))).sort_values()

    if os.path.exists(missing_csv):
        known = pd.DatetimeIndex(pd.to_datetime(pd.read_csv(missing_csv)['time']))
        n0 = len(times)
        times = times.difference(known)
        if verbose and n0 != len(times):
            log.info('Skipping %d timestamps already known missing.', n0 - len(times))

    by_day = {}
    for t in times:
        by_day.setdefault(t.normalize(), []).append(t)
    days = sorted(by_day)

    todo_by_day, already = {}, 0
    for d in days:
        path = os.path.join(shard_dir, f'pr_{d:%Y%m%d}.nc')
        have = _read_existing_times(path)
        todo_by_day[d] = [t for t in by_day[d] if pd.Timestamp(t) not in have]
        already += len(have & set(by_day[d]))
    total_todo = sum(len(v) for v in todo_by_day.values())

    if verbose:
        log.info(
            '%d timestamps across %d UTC days | %d already stored, %d to fetch -> %s',
            len(times),
            len(days),
            already,
            total_todo,
            shard_dir,
        )
    if total_todo == 0:
        if verbose:
            log.info('Nothing to do -- all timestamps already present.')
        return {
            'days': len(days),
            'to_fetch': 0,
            'stored_new': 0,
            'confirmed_missing': 0,
            'transient_errors': 0,
            'shard_dir': shard_dir,
        }

    fs = None
    if use_aws and s3fs is not None:
        try:
            fs = s3fs.S3FileSystem(
                anon=True,
                config_kwargs={'max_pool_connections': max(max_workers * 2, 50)},
            )
        except RuntimeError as e:
            if verbose:
                log.warning('AWS unavailable (%s); Iowa State only.', e)

    # Pooled, keep-alive session for the Iowa State fallback
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=max_workers, pool_maxsize=max_workers)
    session.mount('https://', adapter)

    try:
        bar = tqdm(total=total_todo, desc='files', unit='file', disable=not verbose)
    except RuntimeError:
        bar = None

    pool = ThreadPoolExecutor(max_workers=max_workers)
    try:
        results, new_missing, fail_by_day = _run_pass(
            days,
            todo_by_day,
            bbox,
            fs,
            mask_negative,
            pool,
            bar,
            shard_dir,
            max_workers,
            session,
        )

        retry_round = 0
        while fail_by_day and retry_round < max_retries:
            retry_round += 1
            n_retry = sum(len(v) for v in fail_by_day.values())
            if verbose:
                log.warning(
                    'Retry %d/%d: %d transient failure(s)...',
                    retry_round,
                    max_retries,
                    n_retry,
                )
            retry_days = sorted(fail_by_day)
            retry_bar = (
                tqdm(
                    total=n_retry,
                    desc=f"retry {retry_round}",
                    unit='file',
                    disable=not verbose,
                )
                if bar is not None
                else None
            )
            r_results, r_missing, fail_by_day = _run_pass(
                retry_days,
                fail_by_day,
                bbox,
                fs,
                mask_negative,
                pool,
                retry_bar,
                shard_dir,
                max_workers,
                session,
            )
            if retry_bar is not None:
                retry_bar.close()
            new_missing.extend(r_missing)
            for d, r in r_results.items():
                results[d]['stored'] = r['stored']
                results[d]['new'] += r['new']
                results[d]['missing'] += r['missing']
                results[d]['errors'] = r['errors']
    finally:
        pool.shutdown(wait=True)
        session.close()
        if bar is not None:
            bar.close()

    if new_missing:
        prev = []
        if os.path.exists(missing_csv):
            prev = list(pd.to_datetime(pd.read_csv(missing_csv)['time']))
        allm = (
            pd.DatetimeIndex(pd.to_datetime(prev + new_missing)).unique().sort_values()
        )
        pd.DataFrame({'time': allm}).to_csv(missing_csv, index=False)

    persistent_fails = sorted(ts for tss in fail_by_day.values() for ts in tss)
    if persistent_fails:
        pd.DataFrame({'time': persistent_fails}).to_csv(transient_csv, index=False)
    elif os.path.exists(transient_csv):
        os.remove(transient_csv)

    pd.DataFrame(list(results.values())).to_csv(manifest_csv, index=False)

    summary = {
        'days': len(days),
        'to_fetch': int(total_todo),
        'stored_new': int(sum(r['new'] for r in results.values())),
        'confirmed_missing': int(len(new_missing)),
        'transient_errors': int(len(persistent_fails)),
        'shard_dir': shard_dir,
        'manifest': manifest_csv,
    }

    if verbose:
        log.info('Done: %s', summary)
        if summary['transient_errors']:
            log.warning(
                '-> %d still failing after %d retries, logged to %s '
                '(will retry again next build_store() call).',
                summary['transient_errors'],
                max_retries,
                transient_csv,
            )
    return summary


#### extraction: 2-min area-weighted rate -> 15-min catchment depth


def open_shards(shard_dir: Path) -> xr.Dataset:
    """Open all MRMS day shards in shard_dir as one lazily-concatenated dataset."""
    files = sorted(Path(shard_dir).glob('pr_*.nc'))
    if not files:
        raise FileNotFoundError(f"no day shards in {shard_dir}")
    return xr.open_mfdataset(
        [str(f) for f in files],
        combine='nested',
        concat_dim='time',
        engine='netcdf4',
        data_vars='minimal',
        coords='minimal',
        compat='override',
    ).sortby('time')


def storm_catchment_rate_2min(
    pts_all: np.ndarray,
    times_all: pd.DatetimeIndex,
    cell_col: dict,
    win_start: pd.Timestamp,
    win_end: pd.Timestamp,
    divide_ids: Iterable[str],
    frac_cw: pd.DataFrame,
    min_coverage: float = 0.95,
) -> tuple[pd.DataFrame | None, str | None]:
    """Extract storm catchment rates from pre-loaded MRMS cell data.

    ``pts_all``/``times_all``/``cell_col`` come from extract_all's one-time
    point-selection + load of just the cells frac_cw references (see there
    for why) -- this only ever does in-memory numpy slicing, no per-event
    dataset access.
    """
    mask = (times_all >= win_start) & (times_all <= win_end)
    actual_n = int(mask.sum())
    if actual_n == 0:
        return None, "no shard data for window"

    expected_n = int((win_end - win_start) / pd.Timedelta(minutes=2)) + 1
    if actual_n < min_coverage * expected_n:
        present_days = set(times_all[mask].normalize())
        all_days = set(
            pd.date_range(win_start.normalize(), win_end.normalize(), freq='D'),
        )
        missing_days = sorted(d.date() for d in (all_days - present_days))
        return None, (
            f"incomplete window coverage ({actual_n}/{expected_n} timestamps, "
            f"{actual_n / expected_n:.0%}); missing day(s): {missing_days}"
        )

    cw = frac_cw[frac_cw['divide_id'].isin(divide_ids)]
    if len(cw) == 0:
        return None, "no fractional_crosswalk match"

    col_idx = np.array([cell_col[cid] for cid in cw['cell_id']], dtype=np.int64)
    values = pts_all[np.ix_(mask, col_idx)]  # (time, cell)
    times = times_all[mask]

    # area-weighted catchment mean via sparse matmul, renormalized per-timestep
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
    num = W @ np.nan_to_num(values, nan=0.0).T  # (n_cat, n_time)
    den = W @ valid.astype(np.float32).T  # (n_cat, n_time)
    with np.errstate(invalid='ignore', divide='ignore'):
        rate = (num / den).T
    rate[den.T == 0] = np.nan

    return pd.DataFrame(rate, index=times, columns=cats), None


def to_depth_15(rate2min: pd.DataFrame) -> pd.DataFrame:
    """Convert a 2-min precip rate series to 15-min depth."""
    rate15 = rate2min.resample('15min', label='left', closed='left').mean()
    return rate15 * 0.25


def extract_all(
    manifest: pd.DataFrame,
    event_catchment_windows: pd.DataFrame,
    frac_cw: pd.DataFrame,
    shard_dir: Path,
    out_nc: Path,
    max_steps: int = 481,
    zero_precip_threshold_mm: float = 1.0,
) -> None:
    """Extract per-event 15-min MRMS catchment precipitation to out_nc."""
    ds = open_shards(shard_dir)
    cats_by_event = event_catchment_windows.groupby('storm_index')['divide_id'].apply(
        list,
    )

    # Pre-select (once) just the unique MRMS grid cells this VPU-shard's
    # fractional crosswalk actually references -- typically a small fraction
    # of the shard's full bounding-box grid -- and load them for the whole
    # time range up front. Reused across every event below via plain numpy
    # slicing in storm_catchment_rate_2min: avoids both the per-event lazy
    # multi-file open/read overhead (thousands of events each re-touching
    # several of the underlying day-files) and the memory blowup of loading
    # the *entire* (often much larger) bounding-box grid at every timestamp,
    # most of which no single event ever touches.
    uniq_cells = (
        frac_cw.drop_duplicates('cell_id')[['cell_id', 'lat', 'lon_360']]
        .reset_index(drop=True)
    )
    cell_col = {cid: i for i, cid in enumerate(uniq_cells['cell_id'])}
    log.info(
        'pre-loading %d unique MRMS cell(s) referenced by this VPU-shard '
        '(of a larger bounding-box grid) across the full time range...',
        len(uniq_cells),
    )
    pts = ds['precip_rate'].sel(
        latitude=xr.DataArray(uniq_cells['lat'].values, dims='cell'),
        longitude=xr.DataArray(uniq_cells['lon_360'].values, dims='cell'),
        method='nearest',
    ).load()
    times_all = pd.DatetimeIndex(pts['time'].values)
    pts_all = pts.values  # (time, cell)
    ds.close()

    records, flagged, failed = [], [], []
    for _, row in tqdm(manifest.iterrows(), total=len(manifest), desc='extract'):
        sid = row['storm_index']
        divide_ids = cats_by_event.get(sid)
        if not divide_ids:
            failed.append(
                {
                    'storm_id': sid,
                    'gauge_id': row['recording_gage_STAID'],
                    'reason': "no catchments in event_catchment_windows",
                },
            )
            continue
        depth15, reason = storm_catchment_rate_2min(
            pts_all,
            times_all,
            cell_col,
            row['win_start'],
            row['win_end'],
            divide_ids,
            frac_cw,
        )
        if depth15 is None or depth15.empty:
            failed.append(
                {
                    'storm_id': sid,
                    'gauge_id': row['recording_gage_STAID'],
                    'reason': reason or "empty result",
                },
            )
            continue
        depth15 = to_depth_15(depth15).iloc[:max_steps]
        vals = depth15.values.astype('float32')
        records.append(
            {
                'storm_id': int(sid),
                'divide_ids': list(depth15.columns),
                'n_steps': len(depth15),
                'ts_start': depth15.index[0],
                'ts_end': depth15.index[-1],
                'depth': vals,
            },
        )

        has_data = ~np.isnan(vals).all(axis=0)
        basin_mean_total = (
            float(np.nanmean(np.nansum(vals[:, has_data], axis=0)))
            if has_data.any()
            else np.nan
        )
        if np.isnan(basin_mean_total) or basin_mean_total < zero_precip_threshold_mm:
            flagged.append(
                {
                    'storm_id': int(sid),
                    'gauge_id': row['recording_gage_STAID'],
                    'ts_start': depth15.index[0],
                    'ts_end': depth15.index[-1],
                    'n_catchments': len(divide_ids),
                    'n_catchments_with_data': int(has_data.sum()),
                    'basin_mean_total_mm': basin_mean_total,
                },
            )
    if not records:
        raise RuntimeError("no events extracted")

    if flagged:
        flagged_csv = Path(out_nc).with_name('zero_precip_events.csv')
        pd.DataFrame(flagged).sort_values('ts_start').to_csv(flagged_csv, index=False)
        log.warning(
            '%d / %d events have basin-mean total precip < %s mm over their full '
            'window -- logged to %s',
            len(flagged),
            len(records),
            zero_precip_threshold_mm,
            flagged_csv,
        )
    if failed:
        failed_csv = Path(out_nc).with_name('extraction_failed_events.csv')
        pd.DataFrame(failed).to_csv(failed_csv, index=False)
        log.warning(
            '%d events could not be extracted at all -- logged to %s',
            len(failed),
            failed_csv,
        )

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
        (np.datetime64(r['ts_start']) - epoch) / np.timedelta64(1, 'm') for r in records
    ]
    v_te = nc.createVariable('ts_end', 'f8', ('event',))
    v_te.units = 'minutes since 1970-01-01 00:00:00 UTC'
    v_te[:] = [
        (np.datetime64(r['ts_end']) - epoch) / np.timedelta64(1, 'm') for r in records
    ]

    # CSR/ragged catchment layout
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
        chunksizes=(min(total_entries, 4096), max_steps),
    )
    v_p.units = 'mm [15 min]-1'
    v_p.long_name = 'MRMS precipitation depth'

    # Write per event so no array spanning all events is ever held in memory.
    for i, r in enumerate(tqdm(records, desc='write')):
        lo, hi = int(cat_ptr[i]), int(cat_ptr[i + 1])
        v_cat[lo:hi] = np.array(r['divide_ids'], dtype=object)
        n = r['n_steps']
        block = np.full((hi - lo, max_steps), np.nan, dtype=np.float32)
        block[:, :n] = r['depth'].T
        v_p[lo:hi, :] = block

    nc.close()
    log.info(
        'wrote %s: %d events x %d steps, %d event-catchment entries (ragged)',
        out_nc,
        n_ev,
        max_steps,
        total_entries,
    )


#### merge per-VPU files into one combined NetCDF


def merge_parts(part_paths: Iterable[Path], out_nc: Path) -> None:
    """Concatenate ragged (CSR) per-event MRMS part files into out_nc."""
    part_paths = list(part_paths)
    ncs = [netCDF4.Dataset(p, 'r') for p in part_paths]

    n_ev = sum(nc.dimensions['event'].size for nc in ncs)
    max_steps = max(nc.dimensions['time_step'].size for nc in ncs)
    total_entries = sum(nc.dimensions['entry'].size for nc in ncs)

    Path(out_nc).parent.mkdir(parents=True, exist_ok=True)
    out = netCDF4.Dataset(out_nc, 'w', format='NETCDF4')
    out.createDimension('event', n_ev)
    out.createDimension('ptr', n_ev + 1)
    out.createDimension('entry', total_entries)
    out.createDimension('time_step', max_steps)

    v_sid = out.createVariable('storm_id', 'i4', ('event',))
    v_ns = out.createVariable('n_steps', 'i4', ('event',))
    v_ts = out.createVariable('ts_start', 'f8', ('event',))
    v_ts.units = "minutes since 1970-01-01 00:00:00 UTC"
    v_te = out.createVariable('ts_end', 'f8', ('event',))
    v_te.units = "minutes since 1970-01-01 00:00:00 UTC"
    v_ptr = out.createVariable('cat_ptr', 'i8', ('ptr',))
    v_cat = out.createVariable('divide_id', str, ('entry',))
    v_p = out.createVariable(
        'P',
        'f4',
        ('entry', 'time_step'),
        fill_value=np.nan,
        zlib=True,
        complevel=4,
        chunksizes=(min(total_entries, 4096), max_steps),
    )
    v_p.units = 'mm [15 min]-1'
    v_p.long_name = 'MRMS precipitation depth'

    e_off, p_off = 0, 0
    for nc in tqdm(ncs, desc='merge parts'):
        n = nc.dimensions['event'].size
        ne = nc.dimensions['entry'].size
        ns = nc.dimensions['time_step'].size

        v_sid[e_off : e_off + n] = nc.variables['storm_id'][:]
        v_ns[e_off : e_off + n] = nc.variables['n_steps'][:]
        v_ts[e_off : e_off + n] = nc.variables['ts_start'][:]
        v_te[e_off : e_off + n] = nc.variables['ts_end'][:]
        # Drop each part's own trailing sentinel (total entries of that part)
        # and rebase its offsets onto this output's running entry offset.
        v_ptr[e_off : e_off + n] = np.asarray(nc.variables['cat_ptr'][:n]) + p_off
        v_cat[p_off : p_off + ne] = nc.variables['divide_id'][:]

        if ns == max_steps:
            v_p[p_off : p_off + ne, :] = nc.variables['P'][:, :]
        else:
            block = np.full((ne, max_steps), np.nan, dtype=np.float32)
            block[:, :ns] = nc.variables['P'][:, :]
            v_p[p_off : p_off + ne, :] = block

        e_off += n
        p_off += ne
        nc.close()

    v_ptr[e_off] = p_off  # final CSR sentinel

    out.close()
    log.info(
        'merged %d part(s) -> %s: %d events, %d event-catchment entries '
        '(ragged, no catchment union)',
        len(part_paths),
        out_nc,
        n_ev,
        total_entries,
    )
