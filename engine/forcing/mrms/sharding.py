"""Split an events CSV into N ~equal shards for parallel MRMS downloading.

Edit the CONFIG block at the top of this file to set all options, or
override per-invocation via CLI flags (see below).

@drworm
"""

import argparse
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from flash_preprocess.paths import EVENTS_CSV as _EVENTS_CSV

log = logging.getLogger('MRMS-Shard')


# CONFIG -------------------------- #
# Events CSV to split
EVENTS_CSV = _EVENTS_CSV

# Number of shards to split EVENTS_CSV into.
N_SHARDS = 8

# Output directory for the shard CSVs.
#   None -- defaults to EVENTS_CSV's own directory.
OUT_DIR = _EVENTS_CSV.parent / 'cache' / 'event_shards'
# -------------------------- #


def parse_args():
    """Parse command-line overrides for the CONFIG block above."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--events-csv', type=Path, default=EVENTS_CSV)
    ap.add_argument('--n-shards', type=int, default=N_SHARDS)
    ap.add_argument(
        '--out-dir',
        type=Path,
        default=OUT_DIR,
        help="Defaults to --events-csv's own directory.",
    )
    return ap.parse_args()


def mrms_shard() -> None:
    """Split an events CSV into N time-contiguous shards."""
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    args = parse_args()

    out_dir = args.out_dir or args.events_csv.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    events = pd.read_csv(args.events_csv, dtype={'STAID': str})
    log.info('%d events -> %d shard(s)', len(events), args.n_shards)

    sort_key = pd.to_datetime(events['BEGIN_DATE_TIME'], utc=True)
    events = (
        events.assign(_sort_key=sort_key)
        .sort_values('_sort_key')
        .reset_index(drop=True)
    )
    events['shard'] = np.arange(len(events)) * args.n_shards // len(events)

    stem = args.events_csv.stem
    for i in range(args.n_shards):
        shard = events[events['shard'] == i].drop(columns=['_sort_key', 'shard'])
        out_path = out_dir / f'{stem}_shard{i}.csv'
        shard.to_csv(out_path, index=False)
        span = f"{shard['BEGIN_DATE_TIME'].min()} -> {shard['BEGIN_DATE_TIME'].max()}"
        log.info('shard %d: %d events (%s) -> %s', i, len(shard), span, out_path)


if __name__ == '__main__':
    mrms_shard()
