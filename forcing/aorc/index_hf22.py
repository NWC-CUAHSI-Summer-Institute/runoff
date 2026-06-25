"""Build a HydroFabric v2.2 index for of catchments and upstream areas.

The CONUS index (index_dict.pkl) maps each NextGen catchment to AORC 1km
grid pixels (unstructured mesh) that fall inside it. This script allows
subsetting for a specific catchment set.

Selection:
  --gpkg <PATH>        All divides in a geopackage
  --catchment-ids IDS  Explicit list of catchment IDs
  (none)               All CONUS catchments

Optional:
  --upstream           Include every catchment upstream of the selection.
                       Reads the full hydrofabric network to trace upstream.

Usage examples:

  # All catchments in a VPU geopackage:
  python build_aorc_index.py --gpkg /path/to/vpu-13_subset.gpkg

  # Specific outlets + everything upstream:
  python build_aorc_index.py --catchment-ids cat-1000 cat-2000 --upstream

  # Default output path is ./subset_index_dict.pkl; override with --output.
"""

import argparse
import pickle
import sqlite3
from collections import deque

import numpy as np
import pandas as pd
import geopandas as gpd


HYDROFABRIC  = "/Users/leoglonz/.ngiab/hydrofabric/v2.2/conus_nextgen.gpkg"
CONUS_INDEX  = "/Users/leoglonz/Desktop/noaa/data/index_dict.pkl"
DEFAULT_OUT  = "/Users/leoglonz/Desktop/noaa/data/subset_index_dict.pkl"



def _read_table(conn, sql):
    return pd.read_sql(sql, conn)


def build_upstream_graph(hydrofabric_path):
    """
    Return dict: cat_id -> [direct upstream cat_ids].

    Flow chain (NextGen hydrofabric):
      cat-A --(divides.toid)--> nex-A --(nexus.toid)--> wb-B
      wb-B  --(flowpaths.divide_id)--> cat-B   (cat-B is downstream of cat-A)

    We invert this to get upstream neighbours.
    """
    print("  Reading network tables via SQLite...")
    conn = sqlite3.connect(hydrofabric_path)
    divides   = _read_table(conn, "SELECT divide_id, toid FROM divides")
    nexus     = _read_table(conn, "SELECT id, toid FROM nexus")
    flowpaths = _read_table(conn, "SELECT id, divide_id FROM flowpaths")
    conn.close()

    nex_to_wb  = nexus.set_index("id")["toid"].to_dict()
    wb_to_cat  = flowpaths.set_index("id")["divide_id"].to_dict()

    upstream = {}
    for row in divides.itertuples(index=False):
        wb_ds  = nex_to_wb.get(row.toid)
        if wb_ds is None:
            continue
        cat_ds = wb_to_cat.get(wb_ds)
        if cat_ds is None:
            continue
        upstream.setdefault(cat_ds, []).append(row.divide_id)

    return upstream


def expand_upstream(seed_cats, upstream_graph):
    """BFS from seed catchments; return the full set including seeds."""
    visited = set(seed_cats)
    queue   = deque(seed_cats)
    while queue:
        cat = queue.popleft()
        for up in upstream_graph.get(cat, []):
            if up not in visited:
                visited.add(up)
                queue.append(up)
    return visited



def filter_and_save(target_ids, conus_index_path, output_path):
    print(f"Loading CONUS index...")
    with open(conus_index_path, "rb") as f:
        conus = pickle.load(f)

    id_to_pos = {sid: i for i, sid in enumerate(conus["station_ids"])}

    found, missing = [], []
    for cid in sorted(target_ids):          # sorted for reproducible output order
        (found if cid in id_to_pos else missing).append(cid)

    if missing:
        print(f"WARNING: {len(missing)} catchments not in CONUS index (skipped):")
        for m in missing[:20]:
            print(f"  {m}")
        if len(missing) > 20:
            print(f"  ... and {len(missing) - 20} more")

    print(f"Writing {len(found)} catchments -> {output_path}")
    out = {
        "station_ids":        np.array(found),
        "unique_polygon_ids": np.arange(len(found)),
        "row_list":           [conus["row_list"][id_to_pos[c]]   for c in found],
        "col_list":           [conus["col_list"][id_to_pos[c]]   for c in found],
        "index_list":         [conus["index_list"][id_to_pos[c]] for c in found],
        "rs_row":             conus["rs_row"],
        "rs_col":             conus["rs_col"],
    }
    with open(output_path, "wb") as f:
        pickle.dump(out, f)
    print(f"Done. Grid: {conus['rs_row']} rows x {conus['rs_col']} cols.")



def main():
    parser = argparse.ArgumentParser(
        description="Subset the CONUS AORC index to a set of NextGen catchments.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--hydrofabric", default=HYDROFABRIC,
                        help="Path to conus_nextgen.gpkg (default: %(default)s)")
    parser.add_argument("--conus-index", default=CONUS_INDEX,
                        help="Path to full CONUS index_dict.pkl (default: %(default)s)")
    parser.add_argument("--output", default=DEFAULT_OUT,
                        help="Output path for the subset index pkl (default: %(default)s)")
    parser.add_argument("--upstream", action="store_true",
                        help="Expand selection to include all upstream catchments")

    sel = parser.add_mutually_exclusive_group()
    sel.add_argument("--gpkg", default=None,
                     help="Select all divides from this geopackage (reads 'divides' layer)")
    sel.add_argument("--catchment-ids", nargs="+", metavar="ID",
                     help="Explicit catchment IDs, e.g. --catchment-ids cat-100 cat-200")

    args = parser.parse_args()

    # --- Determine seed set ---
    if args.gpkg:
        gdf = gpd.read_file(args.gpkg, layer="divides")
        seed_ids = set(gdf["divide_id"].tolist())
        print(f"Loaded {len(seed_ids)} catchments from {args.gpkg}")
    elif args.catchment_ids:
        seed_ids = set(args.catchment_ids)
        print(f"Using {len(seed_ids)} explicitly provided catchment IDs")
    else:
        print(f"No selection specified — using all divides in {args.hydrofabric}")
        conn = sqlite3.connect(args.hydrofabric)
        df   = _read_table(conn, "SELECT divide_id FROM divides")
        conn.close()
        seed_ids = set(df["divide_id"].tolist())
        print(f"  {len(seed_ids)} total divides")

    # --- Optional upstream expansion ---
    if args.upstream:
        print(f"Expanding {len(seed_ids)} seeds upstream...")
        graph      = build_upstream_graph(args.hydrofabric)
        target_ids = expand_upstream(seed_ids, graph)
        print(f"  {len(seed_ids)} -> {len(target_ids)} catchments after upstream expansion")
    else:
        target_ids = seed_ids

    filter_and_save(target_ids, args.conus_index, args.output)


if __name__ == "__main__":
    main()
