r"""Download USGS 15-minute instantaneous discharge for gages in an events CSV.

Outputs:
    - 15-min resolution discharge CSV for all gages.

Edit the CONFIG block at the top of this file to set all options, or
override per-invocation via CLI flags (see below).

@drworm
"""

import argparse
import logging
from pathlib import Path

import pandas as pd
import requests
from tqdm import tqdm

from flash_preprocess.paths import EVENTS_CSV as _EVENTS_CSV
from flash_preprocess.paths import STUDY_START as _STUDY_START
from flash_preprocess.paths import STUDY_END as _STUDY_END

log = logging.getLogger('USGS-Extract')


# CONFIG -------------------------- #
# Events/gages CSV with a STAID column.
EVENTS_CSV = _EVENTS_CSV
STAID_COL = 'STAID'

# Study period.
STUDY_START = _STUDY_START
STUDY_END = _STUDY_END

# NWIS parameter code (00060 = discharge).
PARAMETER_CODE = '00060'

# Optional path to cache the raw (pre-UTC) download, reused if it already
# exists.
#   None -- always re-download.
RAW_CACHE = None

# Output CSV path.
OUTPUT_CSV = EVENTS_CSV.parent / 'usgs_discharge.csv'

# NWIS Instantaneous Values service URL for USGS.
_NWIS_URL = 'https://waterservices.usgs.gov/nwis/iv/'
# -------------------------- #


def load_gage_ids(events_csv: Path, staid_col: str) -> list[str]:
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
    parameter_code: str,
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
    r = requests.get(_NWIS_URL, params=params, timeout=60)
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
    parameter_code: str,
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


def parse_args():
    """Parse command-line overrides for the CONFIG block above."""
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        '--events',
        type=Path,
        default=EVENTS_CSV,
        help='Events/gages CSV with a STAID column (default: %(default)s)',
    )
    p.add_argument(
        '--staid-col',
        default=STAID_COL,
        help='STAID column name (default: %(default)s)',
    )
    p.add_argument(
        '--start',
        default=STUDY_START,
        help='Study period start date, e.g. 2021-01-01 (default: %(default)s)',
    )
    p.add_argument(
        '--end',
        default=STUDY_END,
        help='Study period end date, e.g. 2025-12-31 (default: %(default)s)',
    )
    p.add_argument(
        '--parameter-code',
        default=PARAMETER_CODE,
        help='NWIS parameter code (default: %(default)s)',
    )
    p.add_argument(
        '--raw-cache',
        type=Path,
        default=RAW_CACHE,
        help='Optional path to cache raw (pre-UTC) download, reused if it already exists',
    )
    p.add_argument(
        '--output',
        type=Path,
        default=OUTPUT_CSV,
        help='Output CSV path (default: %(default)s)',
    )
    return p.parse_args()


def usgs_extract():
    """Run the discharge download + resample pipeline."""
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    args = parse_args()

    gage_ids = load_gage_ids(args.events, args.staid_col)
    log.info('Found %d unique gages in %s', len(gage_ids), args.events)

    if args.raw_cache and args.raw_cache.exists():
        log.info('Loading cached raw download: %s', args.raw_cache)
        raw = pd.read_csv(args.raw_cache, dtype={'STAID': str})
    else:
        raw = download_all(gage_ids, args.start, args.end, args.parameter_code)
        if args.raw_cache:
            args.raw_cache.parent.mkdir(parents=True, exist_ok=True)
            raw.to_csv(args.raw_cache, index=False)
            log.info('Cached raw download: %s', args.raw_cache)

    discharge = to_utc_15min(raw, args.start, args.end)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    discharge.to_csv(args.output, index=False)
    log.info(
        'Wrote %s (%d rows, %d gages)',
        args.output,
        len(discharge),
        discharge['STAID'].nunique(),
    )


if __name__ == '__main__':
    usgs_extract()
