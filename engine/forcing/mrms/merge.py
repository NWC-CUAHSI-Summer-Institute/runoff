"""Merge per-VPU mrms_15min_part.nc files into one combined NetCDF.

Edit the CONFIG block at the top of this file to set all options, or
override per-invocation via CLI flags (see below).

@drworm
"""

import argparse
import logging
from pathlib import Path

from flash_preprocess.mrms import merge_parts
from flash_preprocess.paths import CACHE_DIR as _CACHE_DIR

log = logging.getLogger('MRMS-Merge')


# CONFIG -------------------------- #
# Per-VPU mrms_15min_part.nc files to merge.
PARTS = sorted((_CACHE_DIR / 'vpu_runs').glob('*/mrms_15min_part.nc'))

# Output NetCDF path for the merged 15-min MRMS precipitation.
OUT_NC = _CACHE_DIR / 'mrms_15min.nc'
# -------------------------- #


def parse_args():
    """Parse command-line overrides for the CONFIG block above."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        '--parts',
        nargs='+',
        type=Path,
        default=PARTS,
        help="per-VPU mrms_15min_part.nc files (default: CACHE_DIR/vpu_runs/*/mrms_15min_part.nc)",
    )
    ap.add_argument('--out', type=Path, default=OUT_NC)
    return ap.parse_args()


def mrms_merge() -> None:
    """Merge per-VPU MRMS part files into one combined NetCDF."""
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    args = parse_args()

    if not args.parts:
        raise SystemExit('No part files found/given -- nothing to merge.')

    merge_parts(args.parts, args.out)


if __name__ == '__main__':
    mrms_merge()
