"""
Build an area-weighted AORC index using exactextract conservative regridding.

Unlike the point-in-polygon approach in index_hf22.py (which gives equal weight
to every pixel whose center falls inside a catchment), this script computes the
fractional overlap between each AORC 1km grid cell and each catchment polygon,
producing proper area-weighted averages at catchment boundaries.

The weight computation uses exactextract (the same library used by
NGIAB_data_preprocess) and downloads only one timestep of one variable from
S3 (~140 MB) to establish the AORC grid geometry.

Output pkl format (compatible with aorc_extract_hourly.py --weighted):
  station_ids:  np.ndarray[str]   shape (N,)
  cell_ids:     list[np.ndarray[int64]]   flat grid index per catchment pixel
  weights:      list[np.ndarray[float32]] coverage fraction (0-1) per pixel
  rs_row:       4201
  rs_col:       8401

Flat index convention: row 0 = highest latitude (~55°N), consistent with
sortby(latitude, ascending=False) used in aorc_extract_hourly.py.
cell_id = row * 8401 + col.

Selection modes (mutually exclusive):
  --gpkg PATH           All divides in a geopackage
  --catchment-ids IDS   Explicit catchment IDs
  (neither)             All CONUS catchments

Optional:
  --upstream            Also include every upstream catchment (reads hydrofabric
                        network via SQLite).

Usage examples:

  python build_aorc_index_weighted.py \\
      --gpkg /path/to/vpu-13_subset.gpkg \\
      --output data/vpu13_weighted_index.pkl

  python build_aorc_index_weighted.py \\
      --catchment-ids cat-1000 cat-2000 --upstream \\
      --output data/custom_weighted_index.pkl
"""

import argparse
import multiprocessing
import pickle
import sqlite3
from collections import deque

import geopandas as gpd
import numpy as np
import pandas as pd
import s3fs
import xarray as xr
from data_processing.forcings import get_cell_weights


HYDROFABRIC = "/Users/leoglonz/.ngiab/hydrofabric/v2.2/conus_nextgen.gpkg"
DEFAULT_OUT = "/Users/leoglonz/Desktop/noaa/data/weighted_index_dict.pkl"
AORC_YEAR = 2022



def build_upstream_graph(hydrofabric_path):
    print("  Reading network tables via SQLite...")
    conn = sqlite3.connect(hydrofabric_path)
    divides = pd.read_sql("SELECT divide_id, toid FROM divides", conn)
    nexus = pd.read_sql("SELECT id, toid FROM nexus", conn)
    flowpaths = pd.read_sql("SELECT id, divide_id FROM flowpaths", conn)
    conn.close()

    nex_to_wb = nexus.set_index("id")["toid"].to_dict()
    wb_to_cat = flowpaths.set_index("id")["divide_id"].to_dict()

    upstream = {}
    for row in divides.itertuples(index=False):
        wb_ds  = nex_to_wb.get(row.toid)
        cat_ds = wb_to_cat.get(wb_ds) if wb_ds else None
        if cat_ds:
            upstream.setdefault(cat_ds, []).append(row.divide_id)
    return upstream


def expand_upstream(seed_cats, upstream_graph):
    visited = set(seed_cats)
    queue   = deque(seed_cats)
    while queue:
        cat = queue.popleft()
        for up in upstream_graph.get(cat, []):
            if up not in visited:
                visited.add(up)
                queue.append(up)
    return visited



def fetch_aorc_grid(year=AORC_YEAR):
    """
    Download one timestep of one variable from AORC S3 to get the grid
    geometry. Returns a 2D xr.Dataset with x/y coords in EPSG:4326,
    latitude descending (row 0 = highest lat, matching raster convention).
    """
    print(f"Fetching AORC grid geometry from S3 (year={year}, one timestep)...")
    _s3   = s3fs.S3FileSystem(anon=True)
    store = s3fs.S3Map(root=f"s3://noaa-nws-aorc-v1-1-1km/{year}.zarr",
                       s3=_s3, check=False)
    ds = xr.open_dataset(store, engine="zarr", consolidated=True)

    # Sort latitude descending: row 0 = highest lat (~55°N) — standard raster
    # convention, and consistent with the flat cell_id used in extraction.
    ds = ds.sortby("latitude", ascending=False)

    # exactextract expects coords named 'x' and 'y'
    ds = ds.rename({"latitude": "y", "longitude": "x"})

    # One timestep, one variable (values unused — only grid geometry matters)
    var = list(ds.data_vars)[0]
    grid = ds[[var]].isel(time=0).compute()
    ds.close()

    print(f"  Grid: {len(grid.y)} rows x {len(grid.x)} cols  "
          f"(lat {float(grid.y.min()):.2f}°N – {float(grid.y.max()):.2f}°N, "
          f"lon {float(grid.x.min()):.2f}°E – {float(grid.x.max()):.2f}°E)")
    return grid



def compute_weights_parallel(gdf_4326, grid_ds, n_workers=None):
    """
    Compute exactextract cell_id/coverage for every catchment in gdf_4326.

    Returns a DataFrame indexed by divide_id with columns [cell_id, coverage].
    """
    if n_workers is None:
        n_workers = max(1, multiprocessing.cpu_count() - 1)

    wkt    = gdf_4326.crs.to_wkt()
    chunks = np.array_split(gdf_4326, n_workers)

    print(f"  Computing area weights ({len(gdf_4326)} catchments, "
          f"{n_workers} workers)...")

    with multiprocessing.Pool(n_workers) as pool:
        args    = [(grid_ds, chunk, wkt) for chunk in chunks]
        results = pool.starmap(get_cell_weights, args)

    return pd.concat(results)



def build_and_save(target_ids, grid_ds, hydrofabric_path, output_path):
    # Load catchment polygons, reproject to EPSG:4326 to match AORC grid
    print(f"Loading divides from hydrofabric...")
    conn     = sqlite3.connect(hydrofabric_path)
    id_list  = sorted(target_ids)
    placeholders = ",".join(f"'{c}'" for c in id_list)
    df = pd.read_sql(
        f"SELECT divide_id FROM divides WHERE divide_id IN ({placeholders})", conn
    )
    conn.close()

    # Use geopandas to get the geometry
    found_ids = set(df["divide_id"])
    missing   = sorted(target_ids - found_ids)
    if missing:
        print(f"  WARNING: {len(missing)} IDs not found in hydrofabric (skipped)")
        for m in missing[:10]:
            print(f"    {m}")

    gdf = gpd.read_file(hydrofabric_path, layer="divides",
                        where=f"divide_id IN ({placeholders})")
    gdf = gdf[["divide_id", "geometry"]].copy()
    gdf = gdf.to_crs("EPSG:4326")
    print(f"  {len(gdf)} catchments loaded, reprojected to EPSG:4326")

    weights_df = compute_weights_parallel(gdf, grid_ds)

    # Assemble per-catchment lists (sorted for reproducibility).
    # exact_extract returns one row per catchment with array-valued cell_id/coverage.
    station_ids, cell_ids_list, weights_list = [], [], []
    for cat_id in id_list:
        if cat_id not in weights_df.index:
            continue    # no AORC pixels intersected this catchment
        row = weights_df.loc[cat_id]
        station_ids.append(cat_id)
        cell_ids_list.append(np.asarray(row["cell_id"], dtype=np.int64))
        w = np.asarray(row["coverage"], dtype=np.float32)
        weights_list.append(w / w.sum())  # normalise so weights sum to 1

    out = {
        "station_ids": np.array(station_ids),
        "cell_ids":    cell_ids_list,
        "weights":     weights_list,
        "rs_row":      len(grid_ds.y),
        "rs_col":      len(grid_ds.x),
    }

    with open(output_path, "wb") as f:
        pickle.dump(out, f)
    print(f"Saved {len(station_ids)} catchments -> {output_path}")



def main():
    parser = argparse.ArgumentParser(
        description="Build an area-weighted AORC catchment index via exactextract.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--hydrofabric", default=HYDROFABRIC)
    parser.add_argument("--output", default=DEFAULT_OUT)
    parser.add_argument("--aorc-year", type=int, default=AORC_YEAR,
                        help="Year used to fetch the AORC grid geometry (default: %(default)s)")
    parser.add_argument("--upstream", action="store_true",
                        help="Expand selection to all upstream catchments")
    parser.add_argument("--workers", type=int, default=None,
                        help="Parallel workers for weight computation (default: cpu_count-1)")

    sel = parser.add_mutually_exclusive_group()
    sel.add_argument("--gpkg", default=None,
                     help="Select all divides from this geopackage")
    sel.add_argument("--catchment-ids", nargs="+", metavar="ID",
                     help="Explicit catchment IDs, e.g. cat-100 cat-200")

    args = parser.parse_args()

    # --- Seed selection ---
    if args.gpkg:
        gdf = gpd.read_file(args.gpkg, layer="divides")
        seed_ids = set(gdf["divide_id"].tolist())
        print(f"Loaded {len(seed_ids)} catchments from {args.gpkg}")
    elif args.catchment_ids:
        seed_ids = set(args.catchment_ids)
        print(f"Using {len(seed_ids)} explicitly provided catchment IDs")
    else:
        print(f"No selection — using all divides in {args.hydrofabric}")
        conn     = sqlite3.connect(args.hydrofabric)
        df       = pd.read_sql("SELECT divide_id FROM divides", conn)
        conn.close()
        seed_ids = set(df["divide_id"].tolist())
        print(f"  {len(seed_ids)} total divides")

    # --- Optional upstream expansion ---
    if args.upstream:
        print(f"Expanding {len(seed_ids)} seeds upstream...")
        graph      = build_upstream_graph(args.hydrofabric)
        target_ids = expand_upstream(seed_ids, graph)
        print(f"  {len(seed_ids)} -> {len(target_ids)} catchments")
    else:
        target_ids = seed_ids

    grid_ds = fetch_aorc_grid(args.aorc_year)
    build_and_save(target_ids, grid_ds, args.hydrofabric, args.output)


if __name__ == "__main__":
    main()
