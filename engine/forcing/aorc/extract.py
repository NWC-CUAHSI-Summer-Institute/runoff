"""Extract hourly AORC (Analysis Of Record for Calibration) forcing for a set
of flash flood events.

Outputs:
    - 15-min resolution forcing NetCDF for all events (window centered on
      each event's centroid or peak flow time)
    - 1-hr resolution forcing NetCDF for all events (antecedent days
      preceding the 15-min window)

Edit the CONFIG block at the top of this file to set all options, or
override per-invocation via CLI flags (see below).

@drworm
"""

import argparse
import logging
import shutil
from pathlib import Path

import pandas as pd

from flash_preprocess.mrms import load_hydrofabric, build_manifest
from flash_preprocess.aorc import (
    build_weighted_crosswalk,
    build_shards,
    extract_all,
    merge_hr_parts,
    merge_15min_parts,
)
from flash_preprocess.paths import CACHE_DIR as _CACHE_DIR
from flash_preprocess.paths import EVENTS_CSV as _EVENTS_CSV

log = logging.getLogger('AORC-Extract')


# CONFIG -------------------------- #
# Flash flood event registry.
#   None = all events in EVENTS_CSV; else e.g. [1266, 4703]
EVENT_PATH = _EVENTS_CSV
EVENT_IDS = None

# VPUs to process in this runtime.
#   None = every VPU in EVENTS_CSV; else e.g. ['01', '03N']
#   Merge with forcing/mrms/merge.py
VPU_SUBSET = None

# Where to cache per-VPU AORC windows, weights, and NetCDF shards.
CACHE_DIR = _CACHE_DIR

# Output NetCDF paths for 1) merged hourly and 2) 15-min AORC forcing.
OUT_HR_NC = _CACHE_DIR / 'aorc_hr.nc'
OUT_15MIN_NC = _CACHE_DIR / 'aorc_15min.nc'

# More workers == faster. Make sure you have enough CPUs (=workers) and RAM.
MAX_WORKERS = 32

# Total width of each event's forcing window (days), centered on event.
#    Must match WINDOW_DAYS used for MRMS run.
WINDOW_DAYS = 6.0

# Event window centroid method.
#   'midpoint' -- center between begin and end times.
#   'peak' (Recommended) -- window centered on the event's reported peak time.
CENTROID = 'peak'

# Hourly warmup window (days) preceding each event's WINDOW_DAYS window.
ANTECEDENT_DAYS = 30.0

# Caching
#   True -- ignore cached per-VPU windows/weights/shards and rebuild.
#   Needed after any change to WINDOW_DAYS/CENTROID/ANTECEDENT_DAYS.
FRESH_START = False
# -------------------------- #


def parse_args():
    """Parse command-line overrides for the CONFIG block above."""
    p = argparse.ArgumentParser(description='AORC forcing extraction pipeline')
    p.add_argument('--events-csv', type=Path, default=EVENT_PATH)
    p.add_argument(
        '--vpu-subset',
        default=None,
        help="Comma-separated VPU codes, e.g. '03N,02'. Unset -> every VPU "
        'present in --events-csv (the VPU_SUBSET default).',
    )
    p.add_argument('--cache-dir', type=Path, default=CACHE_DIR)
    p.add_argument('--out-hr-nc', type=Path, default=OUT_HR_NC)
    p.add_argument('--out-15min-nc', type=Path, default=OUT_15MIN_NC)
    p.add_argument('--max-workers', type=int, default=MAX_WORKERS)
    p.add_argument('--window-days', type=float, default=WINDOW_DAYS)
    p.add_argument('--centroid', choices=['midpoint', 'peak'], default=CENTROID)
    p.add_argument('--antecedent-days', type=float, default=ANTECEDENT_DAYS)
    p.add_argument('--fresh-start', action='store_true', default=FRESH_START)
    return p.parse_args()


def aorc_extract():
    """Run the AORC forcing extraction pipeline."""
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    args = parse_args()
    event_path = args.events_csv
    vpu_subset = args.vpu_subset.split(',') if args.vpu_subset else None
    cache_dir = args.cache_dir
    out_hr_nc = args.out_hr_nc
    out_15min_nc = args.out_15min_nc
    max_workers = args.max_workers
    window_days = args.window_days
    centroid = args.centroid
    antecedent_days = args.antecedent_days
    fresh_start = args.fresh_start

    catchments_master, *_ = load_hydrofabric(cache_dir)
    log.info('hydrofabric: %d catchments', len(catchments_master))

    events = pd.read_csv(event_path, dtype={'STAID': str})
    if EVENT_IDS is not None:
        events = events[events['event_id'].isin(EVENT_IDS)]

    cat_vpu = catchments_master.set_index('divide_id')['vpuid']
    events = events.assign(vpuid=events['gage_cat-id'].map(cat_vpu))
    vpus = sorted(events['vpuid'].dropna().unique())
    if vpu_subset is not None:
        vpus = [v for v in vpus if v in vpu_subset]
    log.info('events: %d across %d VPU(s): %s', len(events), len(vpus), vpus)
    log.info(
        "window: %s day(s) centered on '%s', %sd antecedent",
        window_days,
        centroid,
        antecedent_days,
    )

    # 15-min steps in a window_days-wide window, + a small buffer for the
    # outward hour-grid rounding in build_manifest.
    max_15min_steps = int(round(window_days * 24 * 60 / 15)) + 1

    hr_parts, min15_parts = [], []
    for vpu in vpus:
        log.info('=== VPU %s ===', vpu)
        vpu_events = events[events['vpuid'] == vpu]
        vpu_dir = cache_dir / 'aorc_runs' / vpu

        if fresh_start:
            f_weights = cache_dir / f'aorc_weights_{vpu}.pkl'
            f_weights.unlink(missing_ok=True)
            if vpu_dir.exists():
                shutil.rmtree(vpu_dir)
            log.info('FRESH_START: cleared weight cache and %s', vpu_dir)

        hr_part = vpu_dir / 'aorc_hr_part.nc'
        min15_part = vpu_dir / 'aorc_15min_part.nc'
        if not fresh_start and hr_part.exists() and min15_part.exists():
            log.info(
                '%s and %s already exist -- skipping manifest/weights/shards/extract '
                'for VPU %s (use --fresh-start to force a rebuild).',
                hr_part,
                min15_part,
                vpu,
            )
            hr_parts.append(hr_part)
            min15_parts.append(min15_part)
            continue

        manifest, event_catchment_windows = build_manifest(
            vpu_events,
            cache_dir,
            tag=vpu,
            window_days=window_days,
            centroid=centroid,
        )
        log.info('manifest: %d events resolved to upstream catchments', len(manifest))

        divide_id_of = dict(
            zip(vpu_events['event_id'].astype(str), vpu_events['gage_cat-id']),
        )

        divide_ids = event_catchment_windows['divide_id'].unique()
        weight_idx = build_weighted_crosswalk(
            divide_ids,
            catchments_master,
            cache_dir,
            tag=vpu,
            max_workers=max_workers,
        )
        log.info('weighted crosswalk: %d catchments', len(weight_idx['station_ids']))

        build_shards(manifest, weight_idx, vpu_dir, antecedent_days=antecedent_days)

        extract_all(
            manifest,
            event_catchment_windows,
            weight_idx,
            vpu_dir / 'shards',
            hr_part,
            min15_part,
            divide_id_of,
            antecedent_days=antecedent_days,
            max_15min_steps=max_15min_steps,
        )
        hr_parts.append(hr_part)
        min15_parts.append(min15_part)

    if vpu_subset is None:
        merge_hr_parts(hr_parts, out_hr_nc)
        merge_15min_parts(min15_parts, out_15min_nc)
    else:
        log.info('VPU subset %s done -> %s', vpu_subset, hr_parts + min15_parts)
        log.info(
            'Run other VPU subsets separately, then merge remaining parts together.',
        )


if __name__ == '__main__':
    aorc_extract()
