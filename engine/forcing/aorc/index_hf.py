r"""Subset a HydroFabric v2.2 AORC index to a specific catchment set.

The CONUS index (index_dict.pkl) maps each NextGen catchment to the AORC
1km grid pixels (unstructured mesh) that fall inside it. This script filters
that index down to a chosen set of catchments.

Selection (mutually exclusive; default: all CONUS catchments)
  --gpkg PATH           All divides in a geopackage
  --catchment-ids IDS    Explicit list of catchment IDs
  --csv PATH             CSV with a column of catchment IDs (default col: gage_cat-id)

Optional
  --upstream             Also include every catchment upstream of the
                         selection (reads the full hydrofabric network).

Output: ./subset_index_dict.pkl by default; override with --output.

Usage
-----
    # All catchments in a VPU geopackage
    python engine/forcing/aorc/index_hf.py --gpkg /path/to/vpu-13_subset.gpkg

    # Specific outlets + everything upstream
    python engine/forcing/aorc/index_hf.py \\
        --catchment-ids cat-1000 cat-2000 --upstream
"""

import argparse
import pickle
import sqlite3

import numpy as np
import pandas as pd
import geopandas as gpd

from flash_preprocess.utils import (
    build_upstream_graph,
    expand_upstream,
    HF_PATH_DEFAULT,
)


CONUS_INDEX = '/Users/leoglonz/Desktop/noaa/data/index_dict.pkl'
DEFAULT_OUT = '/Users/leoglonz/Desktop/noaa/data/subset_index_dict.pkl'


def _read_table(conn, sql):
    return pd.read_sql(sql, conn)


def filter_and_save(target_ids, conus_index_path, output_path):
    """Filter the CONUS AORC index to `target_ids` and write it to `output_path`."""
    print("Loading CONUS index...")
    with open(conus_index_path, 'rb') as f:
        conus = pickle.load(f)

    id_to_pos = {sid: i for i, sid in enumerate(conus['station_ids'])}

    found, missing = [], []
    for cid in sorted(target_ids):
        (found if cid in id_to_pos else missing).append(cid)

    if missing:
        print(f"WARNING: {len(missing)} catchments not in CONUS index (skipped):")
        for m in missing[:20]:
            print(f"  {m}")
        if len(missing) > 20:
            print(f"  ... and {len(missing) - 20} more")

    print(f"Writing {len(found)} catchments -> {output_path}")
    out = {
        'station_ids': np.array(found),
        'unique_polygon_ids': np.arange(len(found)),
        'row_list': [conus['row_list'][id_to_pos[c]] for c in found],
        'col_list': [conus['col_list'][id_to_pos[c]] for c in found],
        'index_list': [conus['index_list'][id_to_pos[c]] for c in found],
        'rs_row': conus['rs_row'],
        'rs_col': conus['rs_col'],
    }
    with open(output_path, 'wb') as f:
        pickle.dump(out, f)
    print(f"Done. Grid: {conus['rs_row']} rows x {conus['rs_col']} cols.")


def main():
    """Parse CLI args and run the AORC index subsetting."""
    parser = argparse.ArgumentParser(
        description="Subset the CONUS AORC index to a set of NextGen catchments.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        '--hydrofabric',
        default=HF_PATH_DEFAULT,
        help="Path to conus_nextgen.gpkg (default: %(default)s)",
    )
    parser.add_argument(
        '--conus-index',
        default=CONUS_INDEX,
        help="Path to full CONUS index_dict.pkl (default: %(default)s)",
    )
    parser.add_argument(
        '--output',
        default=DEFAULT_OUT,
        help="Output path for the subset index pkl (default: %(default)s)",
    )
    parser.add_argument(
        '--upstream',
        action='store_true',
        help="Expand selection to include all upstream catchments",
    )

    sel = parser.add_mutually_exclusive_group()
    sel.add_argument(
        '--gpkg',
        default=None,
        help="Select all divides from this geopackage (reads 'divides' layer)",
    )
    sel.add_argument(
        '--catchment-ids',
        nargs='+',
        metavar='ID',
        help="Explicit catchment IDs, e.g. --catchment-ids cat-100 cat-200",
    )
    sel.add_argument(
        '--csv',
        default=None,
        metavar='PATH',
        help="CSV file with catchment IDs in a column",
    )
    parser.add_argument(
        '--csv-column',
        default="gage_cat-id",
        metavar="COL",
        help="Column name in --csv that contains catchment IDs "
        "(default: %(default)s). Bare integers are prefixed with 'cat-'.",
    )

    args = parser.parse_args()

    # determine seed set
    if args.gpkg:
        gdf = gpd.read_file(args.gpkg, layer='divides')
        seed_ids = set(gdf['divide_id'].tolist())
        print(f"Loaded {len(seed_ids)} catchments from {args.gpkg}")
    elif args.catchment_ids:
        seed_ids = set(args.catchment_ids)
        print(f"Using {len(seed_ids)} explicitly provided catchment IDs")
    elif args.csv:
        df_csv = pd.read_csv(args.csv)
        raw = df_csv[args.csv_column].astype(str).tolist()
        seed_ids = {v if v.startswith('cat-') else f"cat-{v}" for v in raw}
        print(
            f"Loaded {len(seed_ids)} catchments from {args.csv} (col: {args.csv_column})",
        )
    else:
        print(f"No selection specified — using all divides in {args.hydrofabric}")
        conn = sqlite3.connect(args.hydrofabric)
        df = _read_table(conn, 'SELECT divide_id FROM divides')
        conn.close()
        seed_ids = set(df['divide_id'].tolist())
        print(f"  {len(seed_ids)} total divides")

    # optional upstream expansion
    if args.upstream:
        print(f"Expanding {len(seed_ids)} seeds upstream...")
        graph = build_upstream_graph(args.hydrofabric)
        target_ids = expand_upstream(seed_ids, graph)
        print(
            f"  {len(seed_ids)} -> {len(target_ids)} catchments after upstream expansion",
        )
    else:
        target_ids = seed_ids

    filter_and_save(target_ids, args.conus_index, args.output)


if __name__ == '__main__':
    main()
