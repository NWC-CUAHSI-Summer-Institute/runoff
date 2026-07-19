"""USGS NWIS discharge download and resampling.

Fetches raw instantaneous discharge (parameter 00060 by default) from the
USGS NWIS Instantaneous Values service and resamples to a fixed UTC grid.
Shared by engine/streamflow/usgs/extract.py and engine/events/extract.py.

@drworm
"""

import logging

import pandas as pd
import requests
from tqdm.auto import tqdm

log = logging.getLogger('runoff-usgs')

NWIS_IV_URL = 'https://waterservices.usgs.gov/nwis/iv/'


def normalize_staid(value) -> str:
    """Zero-pad a USGS site number to at least 8 digits."""
    s = str(value).strip()
    if s.endswith('.0'):
        s = s[:-2]
    return s.zfill(8)


def load_gage_ids(events_csv, staid_col: str) -> list[str]:
    """Read unique, zero-padded gage STAIDs from an events/gages CSV."""
    df = pd.read_csv(events_csv, dtype={staid_col: str})
    return (
        df[staid_col]
        .dropna()
        .astype(str)
        .str.strip()
        .str.replace('.0', '', regex=False)
        .str.zfill(8)
        .drop_duplicates()
        .tolist()
    )


def fetch_discharge(
    site: str,
    start_date: str,
    end_date: str,
    parameter_code: str = '00060',
) -> pd.DataFrame:
    """Fetch raw instantaneous discharge for one gage from the NWIS IV service."""
    params = {
        'format': 'json',
        'sites': site,
        'parameterCd': parameter_code,
        'startDT': start_date,
        'endDT': end_date,
        'siteStatus': 'all',
    }
    r = requests.get(NWIS_IV_URL, params=params, timeout=60)
    r.raise_for_status()

    series = r.json()['value'].get('timeSeries', [])
    if not series:
        return pd.DataFrame()

    rows = []
    for ts in series:
        info = ts['sourceInfo']
        site_no = info['siteCode'][0]['value']
        site_name = info['siteName']
        lat = info['geoLocation']['geogLocation']['latitude']
        lon = info['geoLocation']['geogLocation']['longitude']

        for obs in ts['values'][0].get('value', []):
            val = obs.get('value')
            rows.append(
                {
                    'STAID': site_no,
                    'site_name': site_name,
                    'datetime': obs['dateTime'],
                    'discharge_cfs': float(val) if val not in (None, '') else None,
                    'latitude': lat,
                    'longitude': lon,
                },
            )

    return pd.DataFrame(rows)


def download_all(
    gage_ids: list[str],
    start_date: str,
    end_date: str,
    parameter_code: str = '00060',
) -> pd.DataFrame:
    """Download and concatenate raw discharge for all gages."""
    all_data = []
    empty, failed = [], []

    for gage in tqdm(gage_ids, desc='Downloading gages'):
        try:
            df = fetch_discharge(gage, start_date, end_date, parameter_code)
        except Exception as e:  # noqa: BLE001 - report and continue past per-gage failures
            failed.append((gage, str(e)))
            log.warning('%s: %s', gage, e)
            continue

        if df.empty:
            empty.append(gage)
        else:
            all_data.append(df)

    log.info(
        'Successful gages: %d | Empty: %d | Failed: %d',
        len(all_data),
        len(empty),
        len(failed),
    )
    if empty:
        log.info('Empty gages: %s', empty)
    if failed:
        log.info('Failed gages: %s', failed)

    if not all_data:
        raise RuntimeError('No discharge data retrieved for any gage.')

    discharge = pd.concat(all_data, ignore_index=True)
    sort_key = pd.to_datetime(discharge['datetime'], utc=True)
    discharge = (
        discharge.assign(_sort_key=sort_key)
        .sort_values(['STAID', '_sort_key'])
        .drop(columns='_sort_key')
    )
    return discharge.reset_index(drop=True)


def to_utc_15min(
    discharge: pd.DataFrame,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """Convert mixed-offset local timestamps to UTC and resample to a 15-min grid."""
    discharge = discharge.copy()
    discharge['datetime'] = pd.to_datetime(
        discharge['datetime'],
        errors='coerce',
        utc=True,
    )
    discharge = discharge.dropna(subset=['datetime'])
    discharge['datetime'] = discharge['datetime'].dt.tz_localize(None)

    study_start = pd.Timestamp(start_date)
    study_end = pd.Timestamp(end_date) + pd.Timedelta(hours=23, minutes=45)
    discharge = discharge[
        (discharge['datetime'] >= study_start) & (discharge['datetime'] <= study_end)
    ].copy()

    resampled = (
        discharge.set_index('datetime')
        .groupby('STAID')
        .resample('15min')[['discharge_cfs', 'latitude', 'longitude']]
        .max()
        .reset_index()
    )

    site_names = discharge[['STAID', 'site_name']].drop_duplicates()
    resampled = resampled.merge(site_names, on='STAID', how='left')

    return resampled[
        ['STAID', 'site_name', 'datetime', 'discharge_cfs', 'latitude', 'longitude']
    ].sort_values(['STAID', 'datetime'])


def load_discharge_series(
    site: str,
    wy_start: int,
    wy_end: int,
    parameter_code: str = '00060',
    freq: str = '15min',
) -> pd.Series:
    """Continuous discharge (cfs) for one site over WY wy_start..wy_end, UTC,
    resampled to `freq`. Pulled one water year at a time so large multi-year
    requests stay reliable. Returns a Series named 'value' on a tz-aware UTC
    index.
    """
    frames = []
    for wy in range(wy_start, wy_end + 1):
        start = f'{wy - 1}-10-01'
        end = f'{wy}-09-30'
        df = fetch_discharge(site, start, end, parameter_code)
        if not df.empty:
            frames.append(df)
    if not frames:
        raise ValueError(f'No {parameter_code} data for site {site} in WY{wy_start}-{wy_end}')

    raw = pd.concat(frames, ignore_index=True)
    raw['datetime'] = pd.to_datetime(raw['datetime'], utc=True)
    q = raw.set_index('datetime')['discharge_cfs'].sort_index()
    q = q[~q.index.duplicated(keep='first')]
    return q.resample(freq).first().ffill().rename('value')
