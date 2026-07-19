"""Extract flash flood events from USGS hydrographs for gages inside a HUC8.

Finds all gages inside each HUC8 in HUC8_LIST, downloads discharge, and
separates hydrographs into individual flood events. Only events with a
peak at or above the Q_EXCEEDANCE_PCT flow-duration threshold are kept.

Outputs:
    - stations_huc8_<ID>.csv: gages found inside each HUC8.
    - huc8_<ID>_combined_events.csv: all events across all sites in that HUC8.

Edit the CONFIG block at the top of this file to set all options, or
override per-invocation via CLI flags (see below).

@drworm
"""

import argparse
import logging
from pathlib import Path

import pandas as pd

from runoff import (
    GAGES_CSV as _DEFAULT_GAGES_CSV,
    HUC8_SHP as _DEFAULT_HUC8_SHP,
    EVENT_OUTPUT_DIR as _DEFAULT_OUTPUT_DIR,
)
from runoff.events import combine_tables, gages_in_huc8, process_site

log = logging.getLogger('runoff-events-extract')


# CONFIG -------------------------- #
# 8-digit HUC8 watershed IDs to process, one at a time.
HUC8_LIST = ['03020201']

# HUC8 boundary shapefile and gage inventory CSV (STAID, LAT_GAGE, LNG_GAGE).
#   None = defaults set in runoff config.
HUC8_SHP = None
GAGES_CSV = None

# Output directory and combined-file name (per HUC8: huc8_<ID>_<COMBINED_OUT>).
#   None = default path set in runoff config.
OUTPUT_DIR = None
COMBINED_OUT = 'combined_events.csv'

# Water years to pull discharge for. WY2021+ reflects MRMS advancements made
# that year -- recommended if completing the full model pipeline with MRMS.
WY_START = 2021
WY_END = 2025

# Events are kept only if their peak >= Q at this exceedance probability.
#   25 = Q25 (exceeded only 25% of the time) -- stricter, larger events only.
Q_EXCEEDANCE_PCT = 25

# Flash-flood-tuned event detection parameters (NOAA-OWP hydrotools), applying
# less smoothing than the hydrotools defaults to capture "flashier" events.
DETECT_KWARGS = {
    'halflife': '1h',
    'window': '2D',
    'minimum_event_duration': '1h',
    'start_radius': '2h',
}
# -------------------------- #

HUC8_SHP = HUC8_SHP or _DEFAULT_HUC8_SHP
GAGES_CSV = GAGES_CSV or _DEFAULT_GAGES_CSV
OUTPUT_DIR = OUTPUT_DIR or _DEFAULT_OUTPUT_DIR


def parse_args():
    """Parse command-line overrides for the CONFIG block above."""
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--huc8', nargs='+', default=HUC8_LIST, metavar='huc8_id')
    p.add_argument('--huc8-shp', type=Path, default=HUC8_SHP)
    p.add_argument('--gages-csv', type=Path, default=GAGES_CSV)
    p.add_argument('--output-dir', type=Path, default=OUTPUT_DIR)
    p.add_argument('--combined-out', default=COMBINED_OUT)
    p.add_argument('--wy-start', type=int, default=WY_START)
    p.add_argument('--wy-end', type=int, default=WY_END)
    p.add_argument('--q-exceedance-pct', type=float, default=Q_EXCEEDANCE_PCT)
    return p.parse_args()


def run(
    huc8: str,
    huc8_shp: Path,
    gages_csv: Path,
    output_dir: Path,
    combined_out: str,
    wy_start: int,
    wy_end: int,
    q_exceedance_pct: float,
) -> dict:
    """Run the event extraction pipeline for one HUC8."""
    output_dir.mkdir(parents=True, exist_ok=True)
    huc8 = str(huc8).zfill(8)

    sites, coords = gages_in_huc8(huc8, huc8_shp, gages_csv)
    pd.DataFrame({'STAID': sites}).to_csv(
        output_dir / f'stations_huc8_{huc8}.csv', index=False,
    )
    log.info('%d site(s): %s | WY%d-%d', len(sites), ', '.join(sites), wy_start, wy_end)

    results = {}
    for site in sites:
        table = process_site(
            site, wy_start, wy_end,
            coords=coords, q_exceedance_pct=q_exceedance_pct, **DETECT_KWARGS,
        )
        if table is not None:
            results[site] = table

    log.info('%d/%d sites processed successfully.', len(results), len(sites))

    if results:
        combined_path = output_dir / f'huc8_{huc8}_{combined_out}'
        combine_tables(results, combined_path)

    return results


def event_extract() -> None:
    """Run event extraction for every HUC8 in HUC8_LIST."""
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    args = parse_args()

    for huc8_id in args.huc8:
        log.info('=== HUC8: %s ===', huc8_id)
        run(
            huc8=huc8_id,
            huc8_shp=args.huc8_shp,
            gages_csv=args.gages_csv,
            output_dir=args.output_dir,
            combined_out=args.combined_out,
            wy_start=args.wy_start,
            wy_end=args.wy_end,
            q_exceedance_pct=args.q_exceedance_pct,
        )


if __name__ == '__main__':
    event_extract()
