r"""Extract and subset the CONUS NextGen HydroFabric for flash-flood event sims.

Reads divide IDs from a CSV column (or an explicit list), subsets the
HydroFabric to those catchments plus their full upstream network, derives
the river topology DAG, locates USGS gages within the region, and writes
everything needed for dMG runtime (see
https://github.com/mhpi/generic_deltamodel).

Output
------
  {output-dir}/divides.gpkg
      Catchment divide geometries + key attributes
  {output-dir}/topology.json
      River network DAG:
        {
            "nodes": [int, ...],
            "edges": [[upstream_int, downstream_int], ...],
            "gage_hf": {"USGS_gage_id": outlet_int_divide_id, ...}
        }
  {output-dir}/gauges.csv
      USGS gage metadata: STAID, DRAIN_SQKM, divide_id

Usage
-----
    # From a CSV column (e.g. gage_divide_id)
    python engine/geo/extract_hf.py \\
        --csv /path/to/events_and_gages.csv \\
        --gpkg /path/to/conus_nextgen.gpkg \\
        --output-dir /path/to/output/

    # From an explicit list of divide IDs
    python engine/geo/extract_hf.py \\
        --divide-ids cat-251968 cat-251969 cat-251970 \\
        --gpkg /path/to/conus_nextgen.gpkg \\
        --output-dir /path/to/output/
"""

import argparse
import json
import logging
import sqlite3
from pathlib import Path

import geopandas as gpd
import pandas as pd

from flash_preprocess.utils import build_upstream_graph, expand_upstream

log = logging.getLogger('ExtractHF')


DEFAULT_COLUMN_DIVIDE_ID = 'gage_cat-id'


def _cat_to_int(cat_id: str) -> int:
    """Convert 'cat-XXXXX' to integer XXXXX."""
    return int(cat_id.split('-', 1)[1])


def _int_to_cat(n: int) -> str:
    """Convert integer XXXXX to 'cat-XXXXX'."""
    return f'cat-{n}'


def collect_divide_ids_from_csv(csv_path: Path, column: str) -> list[str]:
    """Return sorted unique divide_ids found in a CSV column.

    Parameters
    ----------
    csv_path
        Path to a CSV file containing a column of divide IDs.
    column
        Name of the column holding divide IDs (cat-XXXXX strings).

    Returns
    -------
    list[str]
        Sorted list of unique divide IDs found in the column.
    """
    df = pd.read_csv(csv_path, usecols=[column])
    ids = set(df[column].dropna().tolist())
    log.info('Found %d unique divide IDs in %s[%s]', len(ids), csv_path, column)
    return sorted(ids)


def build_full_upstream_network(
    hf_path: str,
    divide_ids: list[str],
) -> tuple[set[int], list[tuple[int, int]]]:
    """Walk upstream from divides and return all reachable nodes and edges.

    Parameters
    ----------
    hf_path
        Path to the hydrofabric GeoPackage.
    divide_ids
        List of divide IDs (cat-XXXXX strings) to start from.

    Returns
    -------
    set[int]
        Set of all integer divide IDs in the subgraph (seeds + all upstream).
    list[tuple[int, int]]
        List of (upstream_int, downstream_int) tuples representing flow direction.
    """
    # graph: downstream divide_id -> [upstream divide_ids], joined directly
    # via divides/nexus/flowpaths instead of a LIKE-filtered self-join on
    # the much larger denormalized `network` table.
    graph = build_upstream_graph(hf_path)
    visited = expand_upstream(divide_ids, graph)

    subgraph_edges = [
        (_cat_to_int(up), _cat_to_int(dn))
        for dn, ups in graph.items()
        if dn in visited
        for up in ups
        if up in visited
    ]
    nodes = {_cat_to_int(d) for d in visited}
    log.info(
        'Subgraph: %d nodes, %d edges (seeds: %d)',
        len(nodes),
        len(subgraph_edges),
        len(divide_ids),
    )
    return nodes, subgraph_edges


def find_gages_in_region(
    conn: sqlite3.Connection,
    node_ids: set[int],
) -> pd.DataFrame:
    """Return USGS gages whose outlet waterbody falls within node_ids.

    Parameters
    ----------
    conn
        Open SQLite connection to the hydrofabric GeoPackage.
    node_ids
        Integer divide IDs in the study subgraph.

    Returns
    -------
    DataFrame
        columns: STAID, divide_id (int), DRAIN_SQKM.
    """
    # Split the OR join into two indexed queries and UNION — a single JOIN with
    # OR prevents SQLite from using any index, causing a full cross-scan.
    rows = conn.execute(
        """
        SELECT h.hl_link                         AS STAID,
               CAST(SUBSTR(h.id, 4) AS INTEGER)  AS divide_id,
               d.tot_drainage_areasqkm           AS DRAIN_SQKM
        FROM   hydrolocations h
        JOIN   divides d ON d.divide_id = h.nex_id
        WHERE  h.hl_reference IN ('gages', 'usgs-gage')
          AND  h.id LIKE 'wb-%'

        UNION

        SELECT h.hl_link,
               CAST(SUBSTR(h.id, 4) AS INTEGER),
               d.tot_drainage_areasqkm
        FROM   hydrolocations h
        JOIN   divides d ON 'wb' || SUBSTR(d.divide_id, 4) = h.id
        WHERE  h.hl_reference IN ('gages', 'usgs-gage')
          AND  h.id LIKE 'wb-%'
        """,
    ).fetchall()

    df = pd.DataFrame(rows, columns=['STAID', 'divide_id', 'DRAIN_SQKM'])
    df = (
        df[df['divide_id'].isin(node_ids)]
        .drop_duplicates('STAID')
        .reset_index(drop=True)
    )
    log.info('Found %d USGS gages in study region', len(df))
    return df


def build_topology_json(
    nodes: set[int],
    edges: list[tuple[int, int]],
    gages_df: pd.DataFrame,
) -> dict:
    """Assemble the topology dict expected by FlashHydroLoader / MtsHydroLoader.

    Parameters
    ----------
    nodes
        Set of integer divide IDs in the subgraph.
    edges
        List of (upstream_int, downstream_int) tuples representing flow
        direction.
    gages_df
        DataFrame of USGS gages in the region, with columns STAID and divide_id

    Returns
    -------
    dict
        dict with keys 'nodes', 'edges', and 'gage_hf' for JSON serialization.
    """
    gage_hf = {
        str(row['STAID']).zfill(8): int(row['divide_id'])
        for _, row in gages_df.iterrows()
    }
    return {
        'nodes': sorted(nodes),
        'edges': [[int(u), int(d)] for u, d in edges],
        'gage_hf': gage_hf,
    }


def main():
    """Parse CLI args and run the HydroFabric extraction."""
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')

    # Parse command-line arguments
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument(
        '--csv',
        type=Path,
        help='CSV file containing a column of divide IDs',
    )
    src.add_argument(
        '--divide-ids',
        nargs='+',
        metavar='cat-XXXXX',
        help='Explicit list of seed divide IDs',
    )
    parser.add_argument(
        '--csv-column',
        default=DEFAULT_COLUMN_DIVIDE_ID,
        help="Column in --csv holding divide IDs (default: %(default)s)",
    )
    parser.add_argument(
        '--gpkg',
        type=Path,
        required=True,
        help='Path to conus_nextgen.gpkg',
    )
    parser.add_argument(
        '--output-dir',
        type=Path,
        required=True,
        help='Output directory',
    )
    parser.add_argument(
        '--upstream',
        action='store_true',
        default=True,
        help='Expand to full upstream network (default: on)',
    )
    parser.add_argument(
        '--no-upstream',
        dest='upstream',
        action='store_false',
        help='Only keep seed divide IDs, no upstream expansion',
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    # collect seed divide ids
    if args.csv:
        seed_ids = collect_divide_ids_from_csv(args.csv, args.csv_column)
    else:
        seed_ids = args.divide_ids

    log.info('Seed divide IDs: %d', len(seed_ids))

    conn = sqlite3.connect(args.gpkg)

    # build network subgraph
    if args.upstream:
        nodes, edges = build_full_upstream_network(str(args.gpkg), seed_ids)
    else:
        nodes = {_cat_to_int(d) for d in seed_ids}
        edges = []

    # find gages
    gages_df = find_gages_in_region(conn, nodes)

    # extract divide geometries
    log.info('Extracting divide geometries ...')
    gpkg_path = str(args.gpkg)
    cat_ids = [_int_to_cat(n) for n in sorted(nodes)]
    try:
        gdf = gpd.read_file(
            gpkg_path,
            layer='divides',
            where=f"divide_id IN ({','.join(repr(c) for c in cat_ids)})",
        )
    except Exception:  # noqa: BLE001 -- driver-specific error type varies by backend (pyogrio/fiona)
        log.warning('WHERE clause failed, falling back to full read + filter')
        gdf = gpd.read_file(gpkg_path, layer='divides')
        gdf = gdf[gdf['divide_id'].isin(set(cat_ids))].reset_index(drop=True)

    conn.close()

    # write outputs
    divides_path = args.output_dir / 'divides.gpkg'
    gdf.to_file(divides_path, driver='GPKG')
    log.info('Wrote %s (%d features)', divides_path, len(gdf))

    topo_path = args.output_dir / 'topology.json'
    topo = build_topology_json(nodes, edges, gages_df)
    with open(topo_path, 'w') as f:
        json.dump(topo, f)
    log.info(
        'Wrote %s (%d nodes, %d edges, %d gages)',
        topo_path,
        len(topo['nodes']),
        len(topo['edges']),
        len(topo['gage_hf']),
    )

    gauges_path = args.output_dir / 'gauges.csv'
    gages_df.to_csv(gauges_path, index=False)
    log.info('Wrote %s', gauges_path)

    log.info('Done.')


if __name__ == '__main__':
    main()
