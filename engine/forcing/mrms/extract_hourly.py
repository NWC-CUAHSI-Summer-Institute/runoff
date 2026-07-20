"""Download hourly MRMS QPE products and extract per-event catchment
precipitation for a set of flash flood events.

Products (all on noaa-mrms-pds, mirrored at Iowa State):
    radar_1h  RadarOnly_QPE_01H_00.00            radar-only 1-h accumulation
    pass1     MultiSensor_QPE_01H_Pass1_00.00    gauge-corrected, ~1 h latency
    pass2     MultiSensor_QPE_01H_Pass2_00.00    gauge-corrected, ~12 h latency

These grids are already hourly depths, so no 2-min accumulation is needed:
each hourly grid in the event window is downloaded, sampled at the crosswalk
cells, and area-weighted onto NextGen catchments.

Pair with:  aorc/extract.py --timestep-min 60   and   merge_15min.py --timestep-min 60

Edit the CONFIG block at the top of this file to set all options, or
override per-invocation via CLI flags (see below).

@drworm
"""

import argparse
import logging
import shutil
from pathlib import Path

import pandas as pd

from runoff.mrms import (
    load_hydrofabric,
    build_crosswalk,
    build_manifest,
    build_fractional_crosswalk,
    merge_parts,
)
from runoff.mrms_hourly import HOURLY_PRODUCTS, extract_all_hourly
from runoff import CACHE_DIR as _DEFAULT_CACHE_DIR, EVENTS_CSV as _DEFAULT_EVENTS_CSV

log = logging.getLogger('mrms-hourly-extract')


# CONFIG -------------------------- #
# Flash flood event registry.
#   None = default path set in runoff config.
EVENTS_CSV = None
EVENT_IDS = None

# Hourly product: 'radar_1h', 'pass1' or 'pass2'.
PRODUCT = 'radar_1h'

# VPUs to process in this runtime.
#   None = every VPU in EVENTS_CSV; else e.g. ['01', '03N']
VPU_SUBSET = None

# Where to cache per-VPU windows and NetCDF parts.
#   None = default path set in runoff config.
CACHE_DIR = None

# More workers == faster download. Hourly grids are few; 8 is plenty.
MAX_WORKERS = 8

# Total width of each event's forcing window (days), centered on CENTROID.
#    Must match WINDOW_DAYS used for the AORC run.
WINDOW_DAYS = 6.0

# Event window centroid method ('midpoint' or 'peak').
CENTROID = 'peak'

# Caching: True -- rebuild manifests and parts from scratch.
FRESH_START = False
# -------------------------- #

EVENTS_CSV = EVENTS_CSV or _DEFAULT_EVENTS_CSV
CACHE_DIR = CACHE_DIR or _DEFAULT_CACHE_DIR

# Output NetCDF path for merged hourly MRMS precipitation.
OUT_NC = CACHE_DIR / 'mrms_60min.nc'


def parse_args():
    """Parse command-line overrides for the CONFIG block above."""
    p = argparse.ArgumentParser(
        description='Hourly MRMS QPE download + extraction pipeline',
    )
    p.add_argument('--events-csv', type=Path, default=EVENTS_CSV)
    p.add_argument(
        '--product',
        choices=sorted(HOURLY_PRODUCTS),
        default=PRODUCT,
        help='hourly MRMS product (default: %(default)s)',
    )
    p.add_argument(
        '--vpu-subset',
        default=None,
        help="Comma-separated VPU codes, e.g. '03N,02'. Unset -> every VPU "
        'present in --events-csv.',
    )
    p.add_argument('--cache-dir', type=Path, default=CACHE_DIR)
    p.add_argument('--out-nc', type=Path, default=OUT_NC)
    p.add_argument('--max-workers', type=int, default=MAX_WORKERS)
    p.add_argument('--window-days', type=float, default=WINDOW_DAYS)
    p.add_argument('--centroid', choices=['midpoint', 'peak'], default=CENTROID)
    p.add_argument('--fresh-start', action='store_true', default=FRESH_START)
    return p.parse_args()


def mrms_hourly_extract():
    """Run the hourly MRMS download and extraction pipeline."""
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    args = parse_args()
    vpu_subset = args.vpu_subset.split(',') if args.vpu_subset else None
    cache_dir = args.cache_dir
    product = args.product

    catchments_master, network, flowpaths, nexus = load_hydrofabric(cache_dir)
    log.info('hydrofabric: %d catchments', len(catchments_master))

    events = pd.read_csv(args.events_csv, dtype={'STAID': str})
    if EVENT_IDS is not None:
        events = events[events['event_id'].isin(EVENT_IDS)]

    cat_vpu = catchments_master.set_index('divide_id')['vpuid']
    events = events.assign(vpuid=events['gage_cat-id'].map(cat_vpu))
    vpus = sorted(events['vpuid'].dropna().unique())
    if vpu_subset is not None:
        vpus = [v for v in vpus if v in vpu_subset]
    log.info('events: %d across %d VPU(s): %s', len(events), len(vpus), vpus)
    log.info(
        "window: %s day(s) centered on '%s'  |  product: %s",
        args.window_days,
        args.centroid,
        HOURLY_PRODUCTS[product],
    )

    crosswalk = build_crosswalk(catchments_master, cache_dir, vpus=vpus)
    log.info('crosswalk: %d MRMS cells', len(crosswalk))

    max_steps = int(round(args.window_days * 24)) + 1

    part_ncs = []
    for vpu in vpus:
        tag = f'{vpu}_{product}'
        log.info('=== VPU %s  (product: %s) ===', vpu, product)
        vpu_events = events[events['vpuid'] == vpu]
        vpu_dir = cache_dir / 'mrms_hourly_runs' / tag

        if args.fresh_start and vpu_dir.exists():
            shutil.rmtree(vpu_dir)

        part_nc = vpu_dir / f'mrms_60min_{product}_part.nc'
        if not args.fresh_start and part_nc.exists():
            log.info('%s already exists -- skipping VPU %s', part_nc, vpu)
            part_ncs.append(part_nc)
            continue

        manifest, event_catchment_windows = build_manifest(
            vpu_events,
            cache_dir,
            tag=tag,
            window_days=args.window_days,
            centroid=args.centroid,
        )
        log.info('manifest: %d events', len(manifest))

        divide_ids = event_catchment_windows['divide_id'].unique()
        frac_cw = build_fractional_crosswalk(
            divide_ids,
            catchments_master,
            crosswalk,
            cache_dir,
        )
        log.info('fractional crosswalk: %d cell/catchment pairs', len(frac_cw))

        extract_all_hourly(
            manifest,
            event_catchment_windows,
            frac_cw,
            part_nc,
            product_key=product,
            max_steps=max_steps,
            max_workers=args.max_workers,
        )
        part_ncs.append(part_nc)

    if vpu_subset is None:
        merge_parts(part_ncs, args.out_nc)
        log.info('Done -> %s', args.out_nc)
    else:
        log.info('VPU subset %s done -> %s', vpu_subset, part_ncs)


if __name__ == '__main__':
    mrms_hourly_extract()
