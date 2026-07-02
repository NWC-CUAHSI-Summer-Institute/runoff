r"""Build an area-weighted AORC index using exactextract conservative regridding.

Unlike the point-in-polygon approach in index_hf.py (equal weight for every
pixel whose center falls inside a catchment), this script computes the
fractional overlap between each AORC 1km grid cell and each catchment
polygon, producing proper area-weighted averages at catchment boundaries.

The weight computation uses exactextract and downloads only one timestep of
one variable from S3 (~140 MB) to establish the AORC grid geometry.

Selection (mutually exclusive; default: all CONUS catchments)
  --gpkg PATH           All divides in a geopackage
  --catchment-ids IDS   Explicit catchment IDs
  --csv PATH            CSV with a column of catchment IDs (default col: gage_cat-id)

Optional
  --upstream            Also include every upstream catchment (reads the
                        hydrofabric network via sqlite).

Output
------
  Pickle compatible with extract.py's --index, containing:
    station_ids  np.ndarray[str]        shape (N,)
    cell_ids     list[np.ndarray[i64]]  flat grid index per catchment pixel
    weights      list[np.ndarray[f32]]  coverage fraction (0-1) per pixel
    rs_row, rs_col  4201, 8401 (AORC grid shape)

  Flat index convention: row 0 = highest latitude (~55°N), consistent with
  sortby(latitude, ascending=False). cell_id = row * 8401 + col.

Usage
-----
    python engine/forcing/aorc/index_hf_weighted.py \\
        --gpkg /path/to/vpu-13_subset.gpkg --output /path/to/weighted_index.pkl

    python engine/forcing/aorc/index_hf_weighted.py \\
        --catchment-ids cat-1000 cat-2000 --upstream \\
        --output /path/to/custom_weighted_index.pkl
"""

import argparse
import multiprocessing
import os
import pickle
import sqlite3

import geopandas as gpd
import numpy as np
import pandas as pd
import xarray as xr

from flash_preprocess.utils import (
    build_upstream_graph,
    expand_upstream,
    get_cell_weights,
    HF_PATH_DEFAULT,
)


# Default output path for the weighted index dictionary.
OUT_DEFAULT = '/Users/leoglonz/Desktop/noaa/data/weighted_index_dict.pkl'


# AORC v1.1 1km grid — fixed for all years 1979-2025.
# lat descending (row 0 = ~55N) to match sortby(latitude, ascending=False)
# used in aorc_extract_hourly.py.
AORC_NROWS = 4201
AORC_NCOLS = 8401
AORC_LAT0 = 20.0  # southernmost latitude (bottom of grid)
AORC_LON0 = -130.0  # westernmost longitude
AORC_DLAT = 1.0 / 120.0  # ~0.008333° spacing (~1 km)


def build_aorc_grid() -> xr.Dataset:
    """Construct the AORC 1km grid as an xr.Dataset.

    The grid dimensions and coordinates are identical for every AORC year
    (1979-2025), so no S3 download is needed. Latitude is sorted descending
    (row 0 = ~55N) to match the flat cell_id convention used in extraction.
    exactextract expects coordinate names 'x' and 'y'.
    """
    # descending latitude: highest lat first
    lat = np.linspace(
        AORC_LAT0 + (AORC_NROWS - 1) * AORC_DLAT,
        AORC_LAT0,
        AORC_NROWS,
    )
    lon = np.linspace(
        AORC_LON0,
        AORC_LON0 + (AORC_NCOLS - 1) * AORC_DLAT,
        AORC_NCOLS,
    )
    dummy = np.zeros((AORC_NROWS, AORC_NCOLS), dtype=np.float32)
    grid = xr.Dataset(
        {'dummy': (['y', 'x'], dummy)},
        coords={'y': lat, 'x': lon},
    )
    print(
        f"AORC grid: {AORC_NROWS} rows x {AORC_NCOLS} cols  "
        f"(lat {lat[-1]:.2f}N - {lat[0]:.2f}N, "
        f"lon {lon[0]:.2f}E - {lon[-1]:.2f}E)",
    )
    return grid


def compute_weights_parallel(gdf_4326, grid_ds, n_workers=None):
    """Compute exactextract cell_id/coverage for every catchment in gdf_4326.

    Returns a DataFrame indexed by divide_id with columns [cell_id, coverage].
    """
    if n_workers is None:
        n_workers = max(1, multiprocessing.cpu_count() - 1)

    wkt = gdf_4326.crs.to_wkt()
    chunks = np.array_split(gdf_4326, n_workers)

    print(
        f"  Computing area weights ({len(gdf_4326)} catchments, {n_workers} workers)...",
    )

    with multiprocessing.Pool(n_workers) as pool:
        args = [(grid_ds, chunk, wkt) for chunk in chunks]
        results = pool.starmap(get_cell_weights, args)

    return pd.concat(results)


def build_and_save(target_ids, grid_ds, hydrofabric_path, output_path):
    """Compute area-weighted AORC cell weights for `target_ids` and write to `output_path`."""
    # load catchment polygons, reproject to EPSG:4326 to match AORC grid
    print("Loading divides from hydrofabric...")
    conn = sqlite3.connect(hydrofabric_path)
    id_list = sorted(target_ids)
    placeholders = ','.join(f"'{c}'" for c in id_list)
    df = pd.read_sql(
        f"SELECT divide_id FROM divides WHERE divide_id IN ({placeholders})",
        conn,
    )
    conn.close()

    found_ids = set(df['divide_id'])
    missing = sorted(target_ids - found_ids)
    if missing:
        print(f"  WARNING: {len(missing)} IDs not found in hydrofabric (skipped)")
        for m in missing[:10]:
            print(f"    {m}")

    gdf = gpd.read_file(
        hydrofabric_path,
        layer="divides",
        where=f"divide_id IN ({placeholders})",
    )
    gdf = gdf[['divide_id', 'geometry']].copy()
    gdf = gdf.to_crs("EPSG:4326")
    print(f"  {len(gdf)} catchments loaded, reprojected to EPSG:4326")

    weights_df = compute_weights_parallel(gdf, grid_ds)

    # assemble per-catchment lists (sorted for reproducibility)
    station_ids, cell_ids_list, weights_list = [], [], []
    for cat_id in id_list:
        if cat_id not in weights_df.index:
            continue
        row = weights_df.loc[cat_id]
        station_ids.append(cat_id)
        cell_ids_list.append(np.asarray(row['cell_id'], dtype=np.int64))
        w = np.asarray(row['coverage'], dtype=np.float32)
        weights_list.append(w / w.sum())

    out = {
        'station_ids': np.array(station_ids),
        'cell_ids': cell_ids_list,
        'weights': weights_list,
        'rs_row': len(grid_ds.y),
        'rs_col': len(grid_ds.x),
    }

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, 'wb') as f:
        pickle.dump(out, f)
    print(f"Saved {len(station_ids)} catchments -> {output_path}")


def main():
    """Parse CLI args and build the area-weighted AORC index."""
    parser = argparse.ArgumentParser(
        description="Build an area-weighted AORC catchment index via exactextract.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--hydrofabric', default=HF_PATH_DEFAULT)
    parser.add_argument('--output', default=OUT_DEFAULT)
    parser.add_argument(
        '--upstream',
        action='store_true',
        help="Expand selection to all upstream catchments",
    )
    parser.add_argument(
        '--workers',
        type=int,
        default=None,
        help="Parallel workers for weight computation (default: cpu_count-1)",
    )

    sel = parser.add_mutually_exclusive_group()
    sel.add_argument(
        '--gpkg',
        default=None,
        help="Select all divides from this geopackage",
    )
    sel.add_argument(
        '--catchment-ids',
        nargs='+',
        metavar='ID',
        help="Explicit catchment IDs, e.g. cat-100 cat-200",
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

    # seed selection
    if args.gpkg:
        gdf = gpd.read_file(args.gpkg, layer="divides")
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
        print(f"No selection — using all divides in {args.hydrofabric}")
        conn = sqlite3.connect(args.hydrofabric)
        df = pd.read_sql("SELECT divide_id FROM divides", conn)
        conn.close()
        seed_ids = set(df['divide_id'].tolist())
        print(f"  {len(seed_ids)} total divides")

    # upstream expansion
    if args.upstream:
        print(f"Expanding {len(seed_ids)} seeds upstream...")
        graph = build_upstream_graph(args.hydrofabric)
        target_ids = expand_upstream(seed_ids, graph)
        print(f"  {len(seed_ids)} -> {len(target_ids)} catchments")
    else:
        target_ids = seed_ids

    grid_ds = build_aorc_grid()
    build_and_save(target_ids, grid_ds, args.hydrofabric, args.output)


if __name__ == '__main__':
    main()
