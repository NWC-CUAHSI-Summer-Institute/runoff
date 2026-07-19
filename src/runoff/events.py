"""Flash-flood event extraction from USGS hydrographs.

Given a HUC8 watershed, finds all gages inside it, downloads discharge
(via runoff.usgs), separates hydrographs into individual flood events
(NOAA-OWP hydrotools), and computes per-event metrics. Shared by
engine/events/extract.py.

@drworm
"""

import logging

import geopandas as gpd
import numpy as np
import pandas as pd

from runoff.usgs import load_discharge_series, normalize_staid

log = logging.getLogger('runoff-events')


def _trapz(y, x) -> float:
    """Trapezoidal integral, independent of NumPy version (np.trapz was renamed
    np.trapezoid in NumPy 2.0 and may be absent), so we integrate explicitly.
    """
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    if y.size < 2:
        return 0.0
    return float(np.sum((y[1:] + y[:-1]) * 0.5 * np.diff(x)))


def water_year(ts) -> int:
    """USGS water year: Oct 1 - Sep 30, named for the calendar year it ends in."""
    ts = pd.Timestamp(ts)
    return ts.year + 1 if ts.month >= 10 else ts.year


def gages_in_huc8(huc8_id: str, huc8_shp, gages_csv) -> tuple[list[str], dict]:
    """Spatial join to find all USGS gages (from gages_csv) inside a HUC8
    polygon (from huc8_shp).

    Parameters
    ----------
    huc8_id
        8-digit HUC8 string, e.g. '11010002'.
    huc8_shp
        Path to the HUC8 shapefile.
    gages_csv
        Path to gages CSV with columns STAID, LAT_GAGE, LNG_GAGE.

    Returns
    -------
    list[str]
        Zero-padded gage STAIDs found inside the HUC8.
    dict
        Mapping of STAID -> (lat, lon).
    """
    huc8_id = str(huc8_id).zfill(8)
    log.info('Loading HUC8 polygons from %s ...', huc8_shp)
    huc8_gdf = gpd.read_file(huc8_shp)
    huc8_gdf['HUC8'] = huc8_gdf['HUC8'].astype(str).str.zfill(8)

    target = huc8_gdf.loc[huc8_gdf['HUC8'] == huc8_id].copy()
    if target.empty:
        raise ValueError(f'HUC8 {huc8_id} not found in {huc8_shp}.')

    log.info('Loading gages from %s ...', gages_csv)
    gages = pd.read_csv(gages_csv, dtype={'STAID': str})
    gages_gdf = gpd.GeoDataFrame(
        gages,
        geometry=gpd.points_from_xy(gages['LNG_GAGE'], gages['LAT_GAGE']),
        crs='EPSG:4326',
    ).to_crs(target.crs)

    inside = gpd.sjoin(gages_gdf, target[['geometry']], how='inner', predicate='within')
    inside = inside[['STAID', 'LAT_GAGE', 'LNG_GAGE']].dropna(subset=['STAID'])
    inside['STAID'] = inside['STAID'].apply(normalize_staid)
    inside = inside.drop_duplicates('STAID').sort_values('STAID').reset_index(drop=True)

    coords = {
        row['STAID']: (row['LAT_GAGE'], row['LNG_GAGE']) for _, row in inside.iterrows()
    }
    log.info(
        'Found %d gage(s) in HUC8 %s: %s',
        len(inside),
        huc8_id,
        ', '.join(inside['STAID']),
    )
    return inside['STAID'].tolist(), coords


def compute_fdc(q: pd.Series) -> pd.DataFrame:
    """Flow-duration curve with columns [exceedance_percent, discharge_cfs]."""
    q_clean = q.dropna()
    q_clean = q_clean[q_clean > 0]
    flows_sorted = np.sort(q_clean.values)[::-1]
    n = len(flows_sorted)
    rank = np.arange(1, n + 1)
    exceedance = 100.0 * rank / (n + 1)
    return pd.DataFrame(
        {'exceedance_percent': exceedance, 'discharge_cfs': flows_sorted},
    )


def q_exceedance(fdc: pd.DataFrame, percent: float) -> float:
    """Discharge at a given exceedance probability (%) via linear interpolation."""
    return float(np.interp(percent, fdc['exceedance_percent'], fdc['discharge_cfs']))


def _clean_for_events(q: pd.Series) -> pd.Series:
    """Trim first/last timestep to prevent open events at record edges, which
    cause the unequal starts/ends error in hydrotools.
    """
    q = q.dropna()
    if len(q) > 2:
        q = q.iloc[1:-1]
    return q


def detect_events(
    series: pd.Series,
    halflife: str = '6h',
    window: str = '7D',
    minimum_event_duration: str = '6h',
    start_radius: str = '7h',
) -> pd.DataFrame:
    """Separate a continuous hydrograph into events (NOAA-OWP hydrotools).

    Returns start/end plus peak (cfs) and t_peak per event.
    """
    from hydrotools.events.event_detection import decomposition as ev

    events = ev.list_events(
        series,
        halflife=halflife,
        window=window,
        minimum_event_duration=minimum_event_duration,
        start_radius=start_radius,
    )
    events['peak'] = events.apply(lambda e: series.loc[e.start : e.end].max(), axis=1)
    events['t_peak'] = events.apply(
        lambda e: series.loc[e.start : e.end].idxmax(),
        axis=1,
    )
    return events.reset_index(drop=True)


def detect_events_with_fallback(q: pd.Series, **detect_kwargs) -> pd.DataFrame:
    """Try hydrotools event detection; if it fails due to data gaps, retry
    with a gap-filled series (interpolated + ffill + bfill) before giving up.
    """
    try:
        return detect_events(_clean_for_events(q), **detect_kwargs)
    except Exception as err:  # noqa: BLE001 -- retry with gap-filled series below
        log.warning('hydrotools failed (%s); retrying with gap-filled series ...', err)

    try:
        q_filled = q.interpolate(method='time').ffill().bfill()
        return detect_events(_clean_for_events(q_filled), **detect_kwargs)
    except Exception as err2:  # noqa: BLE001 -- give up, return an empty event table
        log.warning('hydrotools failed again (%s); skipping site.', err2)
        return pd.DataFrame(columns=['start', 'end', 'peak', 't_peak'])


def volume_acreft(series: pd.Series, start, end) -> float:
    """Runoff volume (acre-ft) = integral of discharge (cfs) over [start, end]."""
    s = series.loc[start:end].dropna()
    if len(s) < 2:
        return 0.0
    secs = (s.index - s.index[0]).total_seconds().to_numpy()
    return _trapz(s.to_numpy(), secs) / 43560.0  # 1 cubic foot = 1/43560 acre-foot


def flashiness_index(series: pd.Series, start, end) -> float:
    """Richards-Baker Flashiness Index over [start, end]: sum(|dQ|)/sum(Q)."""
    s = series.loc[start:end].dropna().to_numpy()
    denom = s.sum()
    return float(np.abs(np.diff(s)).sum() / denom) if denom > 0 else np.nan


def build_event_table(
    series: pd.Series,
    events: pd.DataFrame,
    staid: str,
) -> pd.DataFrame:
    """One row per event with the requested columns (time fields are UTC)."""
    rows = []
    for e in events.itertuples():
        start = pd.Timestamp(e.start)
        end = pd.Timestamp(e.end)
        peak_time = pd.Timestamp(e.t_peak)
        rows.append(
            {
                'STAID': staid,
                'BEGIN_DATE_TIME': start,
                'END_DATE_TIME': end,
                'peak_time': peak_time,
                'peak_flow_cfs': float(e.peak),
                'volume_acreft': volume_acreft(series, start, end),
                'flashiness_index': flashiness_index(series, start, end),
                'YEAR': start.year,
                'month': start.month,
                'day': start.day,
                'water_year': water_year(start),
                'duration_hours': (end - start).total_seconds() / 3600.0,
                'time_to_peak_h': (peak_time - start).total_seconds() / 3600.0,
            },
        )

    cols = [
        'STAID',
        'BEGIN_DATE_TIME',
        'END_DATE_TIME',
        'peak_time',
        'peak_flow_cfs',
        'volume_acreft',
        'flashiness_index',
        'YEAR',
        'month',
        'day',
        'water_year',
        'duration_hours',
        'time_to_peak_h',
    ]
    return pd.DataFrame(rows, columns=cols)


def process_site(
    site: str,
    wy_start: int,
    wy_end: int,
    coords: dict | None = None,
    q_exceedance_pct: float = 50.0,
    **detect_kwargs,
) -> pd.DataFrame | None:
    """Full event-extraction pipeline for one site. Returns the event table,
    or None on failure / no events after filtering.
    """
    log.info('[%s] fetching WY%d-%d ...', site, wy_start, wy_end)
    try:
        q = load_discharge_series(site, wy_start, wy_end, freq='15min')
    except Exception as exc:  # noqa: BLE001 -- log and skip this site, continue the batch
        log.warning('[%s] ERROR retrieving data: %s', site, exc)
        return None

    log.info(
        '[%s] %d steps | min %.2f  mean %.1f  max %.0f cfs',
        site,
        len(q),
        q.min(),
        q.mean(),
        q.max(),
    )

    fdc = compute_fdc(q)
    q_thr = q_exceedance(fdc, q_exceedance_pct)
    log.info('[%s] Q%.0f = %.2f cfs', site, q_exceedance_pct, q_thr)

    events = detect_events_with_fallback(q, **detect_kwargs)
    n_total = len(events)
    log.info('[%s] %d events detected', site, n_total)

    events = events[events['peak'] >= q_thr].reset_index(drop=True)
    log.info(
        '[%s] removed %d events (peak < Q%.0f); %d remain',
        site,
        n_total - len(events),
        q_exceedance_pct,
        len(events),
    )
    if events.empty:
        log.info('[%s] no events after filtering; skipping.', site)
        return None

    table = build_event_table(q, events, site)
    lat, lon = (coords or {}).get(site, (None, None))
    table['gage_lat'] = lat
    table['gage_lon'] = lon

    log.info('[%s] %d events ready', site, len(table))
    return table


def combine_tables(tables: dict, out_path) -> pd.DataFrame:
    """Concatenate per-site tables, add a sequential event_id, and save."""
    master = pd.concat(tables.values(), ignore_index=True)
    master.insert(0, 'event_id', range(1, len(master) + 1))
    master.to_csv(out_path, index=False)
    log.info(
        'Combined %d site(s) -> %s (%d total events)',
        len(tables),
        out_path,
        len(master),
    )
    return master
