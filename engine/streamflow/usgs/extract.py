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

from runoff import (
    EVENTS_CSV as _DEFAULT_EVENTS_CSV,
    STUDY_START as _DEFAULT_STUDY_START,
    STUDY_END as _DEFAULT_STUDY_END,
)
from runoff.usgs import download_all, load_gage_ids, to_utc_15min

log = logging.getLogger('usgs-extract')


# CONFIG -------------------------- #
# Flash flood event registry.
#   None = default path set in runoff config.
EVENTS_CSV = None
STAID_COL = 'STAID'

# Study period.
#   None = default start/end set in runoff config.
STUDY_START = None
STUDY_END = None

# NWIS parameter code (00060 = discharge).
PARAMETER_CODE = '00060'

# Optional path to cache the raw (pre-UTC) download, reused if it already
# exists.
#   None -- always re-download.
RAW_CACHE = None

# Output CSV path.
#   None -- defaults to EVENTS_CSV's own directory.
OUTPUT_CSV = None
# -------------------------- #

EVENTS_CSV = EVENTS_CSV or _DEFAULT_EVENTS_CSV
STUDY_START = STUDY_START or _DEFAULT_STUDY_START
STUDY_END = STUDY_END or _DEFAULT_STUDY_END
OUTPUT_CSV = OUTPUT_CSV or (EVENTS_CSV.parent / 'usgs_discharge.csv')


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
