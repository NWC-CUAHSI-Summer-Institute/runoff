"""Download MRMS PrecipRate and extract 15-min catchment precipitation for a
set of flash flood events.

Outputs:
    - 15-min resolution MRMS precipitation NetCDF for all events (window
      centered on each event's centroid or peak flow time)

Edit the CONFIG block at the top of this file to set all options, or
override per-invocation via CLI flags (see below).

@drworm
"""

import argparse
import logging
import shutil
from pathlib import Path

import pandas as pd

from flash_preprocess.mrms import (
    load_hydrofabric,
    build_crosswalk,
    build_manifest,
    build_fractional_crosswalk,
    build_store,
    extract_all,
    merge_parts,
)
from flash_preprocess.paths import CACHE_DIR as _CACHE_DIR
from flash_preprocess.paths import EVENTS_CSV as _EVENTS_CSV

log = logging.getLogger('MRMS-Extract')


# CONFIG -------------------------- #
# Flash flood event registry.
#   None = all events in EVENTS_CSV; else e.g.
EVENTS_CSV = _EVENTS_CSV
EVENT_IDS = None

# VPUs to process in this runtime.
#   None = every VPU in EVENTS_CSV; else e.g. ['01', '03N']
VPU_SUBSET = None

# Appended to every per-VPU cache/output path so concurrent instances
# touching the same VPU (e.g. sharded runs) don't collide.
#   '' (default) -- reproduces the original single-instance naming.
#   Else, disables end-of-run auto-merge.
TAG_SUFFIX = ''

# Where to cache per-VPU windows, timesteps, and NetCDF shards. 
CACHE_DIR = _CACHE_DIR

# Output NetCDF path for merged 15-min MRMS precipitation.
OUT_NC = _CACHE_DIR / 'mrms_15min.nc'

# Margin (degrees) added around each VPU's bbox before downloading.
BBOX_MARGIN_DEG = 0.1

# More workers == faster. Make sure you have enough CPUs (=workers) and RAM.
MAX_WORKERS = 100

# Total width of each event's forcing window (days), centered on CENTROID.
#    Must match WINDOW_DAYS used for the AORC run.
WINDOW_DAYS = 6.0

# Event window centroid method.
#   'midpoint' -- center between begin and end times.
#   'peak' (Recommended) -- window centered on the event's reported peak time.
CENTROID = 'peak'

# Caching
#   True -- ignore cached per-VPU windows/timesteps and rebuild from scratch.
#   Doesn't touch the hydrofabric or crosswalk caches (static geometry,
#   expensive to rebuild). Needed after any change to WINDOW_DAYS/CENTROID.
FRESH_START = False
# -------------------------- #


def parse_args():
    """Parse command-line overrides for the CONFIG block above."""
    p = argparse.ArgumentParser(description="MRMS download + extraction pipeline")
    p.add_argument('--events-csv', type=Path, default=EVENTS_CSV)
    p.add_argument(
        '--vpu-subset',
        default=None,
        help="Comma-separated VPU codes, e.g. '03N,02'. Unset -> every VPU "
        "present in --events-csv (the VPU_SUBSET default).",
    )
    p.add_argument(
        '--tag-suffix',
        default=TAG_SUFFIX,
        help="Appended to per-VPU cache/output paths; non-empty disables "
        "auto-merge at the end of this run (see TAG_SUFFIX above).",
    )
    p.add_argument('--cache-dir', type=Path, default=CACHE_DIR)
    p.add_argument('--out-nc', type=Path, default=OUT_NC)
    p.add_argument('--max-workers', type=int, default=MAX_WORKERS)
    p.add_argument('--window-days', type=float, default=WINDOW_DAYS)
    p.add_argument('--centroid', choices=['midpoint', 'peak'], default=CENTROID)
    p.add_argument('--fresh-start', action='store_true', default=FRESH_START)
    return p.parse_args()


def mrms_extract():
    """Run the MRMS download and extraction pipeline."""
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    args = parse_args()
    events_csv = args.events_csv
    vpu_subset = args.vpu_subset.split(',') if args.vpu_subset else None
    tag_suffix = args.tag_suffix
    cache_dir = args.cache_dir
    out_nc = args.out_nc
    max_workers = args.max_workers
    window_days = args.window_days
    centroid = args.centroid
    fresh_start = args.fresh_start

    catchments_master, network, flowpaths, nexus = load_hydrofabric(cache_dir)
    log.info('hydrofabric: %d catchments', len(catchments_master))

    events = pd.read_csv(events_csv, dtype={'STAID': str})
    if EVENT_IDS is not None:
        events = events[events['event_id'].isin(EVENT_IDS)]

    cat_vpu = catchments_master.set_index('divide_id')['vpuid']
    events = events.assign(vpuid=events['gage_cat-id'].map(cat_vpu))
    vpus = sorted(events['vpuid'].dropna().unique())
    if vpu_subset is not None:
        vpus = [v for v in vpus if v in vpu_subset]
    log.info(
        'events: %d across %d VPU(s): %s%s',
        len(events),
        len(vpus),
        vpus,
        f' [tag suffix: {tag_suffix!r}]' if tag_suffix else '',
    )
    log.info("window: %s day(s) centered on '%s'", window_days, centroid)

    crosswalk = build_crosswalk(catchments_master, cache_dir, vpus=vpus)
    log.info('crosswalk: %d MRMS cells', len(crosswalk))

    # 15-min steps in a window_days-wide window, + small buffer for the
    # outward 15-min-grid rounding in build_manifest.
    max_steps = int(round(window_days * 24 * 60 / 15)) + 1

    part_ncs = []
    for vpu in vpus:
        vpu_tag = f'{vpu}{tag_suffix}'
        log.info('=== VPU %s  (tag: %s) ===', vpu, vpu_tag)
        vpu_events = events[events['vpuid'] == vpu]
        vpu_dir = cache_dir / 'mrms_runs' / vpu_tag

        if fresh_start:
            f_manifest = cache_dir / f'manifest_out_{vpu_tag}.parquet'
            f_windows = cache_dir / f'event_catchment_windows_{vpu_tag}.parquet'
            for f in (f_manifest, f_windows):
                f.unlink(missing_ok=True)
            if vpu_dir.exists():
                shutil.rmtree(vpu_dir)
            log.info('FRESH_START: cleared manifest cache and %s', vpu_dir)

        part_nc = vpu_dir / 'mrms_15min_part.nc'
        if not fresh_start and part_nc.exists():
            log.info(
                '%s already exists -- skipping manifest/download/extract for VPU %s '
                '(use --fresh-start to force a rebuild).',
                part_nc,
                vpu_tag,
            )
            part_ncs.append(part_nc)
            continue

        manifest, event_catchment_windows = build_manifest(
            vpu_events,
            cache_dir,
            tag=vpu_tag,
            window_days=window_days,
            centroid=centroid,
        )
        log.info(
            'manifest: %d events resolved to upstream catchments',
            len(manifest),
        )

        divide_ids = event_catchment_windows['divide_id'].unique()
        cm4326 = catchments_master[
            catchments_master['divide_id'].isin(divide_ids)
        ].to_crs(4326)
        minx, miny, maxx, maxy = cm4326.total_bounds
        bbox = (
            minx - BBOX_MARGIN_DEG,
            miny - BBOX_MARGIN_DEG,
            maxx + BBOX_MARGIN_DEG,
            maxy + BBOX_MARGIN_DEG,
        )

        manifest = manifest.assign(
            grid_start=manifest['win_start'].dt.floor('2min'),
            grid_end=manifest['win_end'].dt.ceil('2min'),
        )
        times = pd.DatetimeIndex(
            sorted(
                set().union(
                    *[
                        set(pd.date_range(r.grid_start, r.grid_end, freq='2min'))
                        for r in manifest.itertuples()
                    ],
                ),
            ),
        )
        log.info(
            'bbox: %s  |  %d catchments  |  %d timestamps',
            bbox,
            len(divide_ids),
            len(times),
        )

        build_store(
            times,
            bbox,
            str(vpu_dir),
            max_workers=max_workers,
            mask_negative=True,
            use_aws=True,
            verbose=True,
        )

        frac_cw = build_fractional_crosswalk(
            divide_ids,
            catchments_master,
            crosswalk,
            cache_dir,
        )
        log.info('fractional crosswalk: %d cell/catchment pairs', len(frac_cw))

        extract_all(
            manifest,
            event_catchment_windows,
            frac_cw,
            vpu_dir / 'shards',
            part_nc,
            max_steps=max_steps,
        )
        part_ncs.append(part_nc)

    if vpu_subset is None and not tag_suffix:
        merge_parts(part_ncs, out_nc)
    else:
        log.info('VPU subset %s / tag %r done -> %s', vpu_subset, tag_suffix, part_ncs)
        log.info(
            'Run other shards/VPU subsets separately, then merge.py all part files together.',
        )


if __name__ == '__main__':
    mrms_extract()
