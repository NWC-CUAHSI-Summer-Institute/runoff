import logging
import sqlite3
from collections import deque

import geopandas as gpd
import pandas as pd
import xarray as xr
from exactextract import exact_extract
from exactextract.raster import NumPyRasterSource

from flash_preprocess.paths import HYDROFABRIC_GPKG

log = logging.getLogger('Utils')

HF_PATH_DEFAULT = str(HYDROFABRIC_GPKG)


def build_upstream_graph(hf_path: str) -> dict[str, list[str]]:
    """Build a dict mapping each catchment to its immediate upstream catchments.

    Parameters
    ----------
    hf_path
        Path to conus_nextgen.gpkg (SQLite geopackage).

    Returns
    -------
    dict
        Mapping of divide_id -> list of upstream divide_ids.
    """
    log.info('Building upstream graph from hydrofabric...')
    conn = sqlite3.connect(hf_path)
    divides = pd.read_sql("SELECT divide_id, toid FROM divides", conn)
    nexus = pd.read_sql("SELECT id, toid FROM nexus", conn)
    flowpaths = pd.read_sql("SELECT id, divide_id FROM flowpaths", conn)
    conn.close()

    nex_to_wb = nexus.set_index('id')['toid'].to_dict()
    wb_to_cat = flowpaths.set_index('id')['divide_id'].to_dict()

    upstream = {}
    for row in divides.itertuples(index=False):
        wb_ds = nex_to_wb.get(row.toid)
        cat_ds = wb_to_cat.get(wb_ds) if wb_ds else None
        if cat_ds:
            upstream.setdefault(cat_ds, []).append(row.divide_id)
    return upstream


def expand_upstream(
    seed_cats: list[str] | set[str],
    upstream_graph: dict[str, list[str]],
) -> set[str]:
    """BFS from seed catchments to collect all upstream catchment IDs.

    Parameters
    ----------
    seed_cats
        Seed catchment IDs.
    upstream_graph
        Mapping of catchment IDs to their immediate upstreams.

    Returns
    -------
    set
        Set of all catchment IDs in the upstream network.
    """
    visited = set(seed_cats)
    queue = deque(seed_cats)
    while queue:
        cat = queue.popleft()
        for up in upstream_graph.get(cat, []):
            if up not in visited:
                visited.add(up)
                queue.append(up)
    return visited


def get_cell_weights(
    raster: xr.Dataset,
    gdf: gpd.GeoDataFrame,
    wkt: str,
) -> pd.DataFrame:
    """From CIROH-UA/NGIAB_data_preprocess.

    Get the cell weights (coverage) for each cell in a divide. Coverage is
    defined as the fraction (a float in [0,1]) of a raster cell that overlaps
    with the polygon in the passed gdf.

    Parameters
    ----------
    raster
        One timestep of a gridded forcings dataset.
    gdf
        A GeoDataFrame with a polygon feature.
    wkt
        Well-known text (WKT) representation of gdf's coordinate reference
        system (CRS).

    Returns
    -------
    pd.DataFrame
        DataFrame indexed by divide_id with coverage info for each raster
        cell in the gridded forcing file.
    """
    xmin = min(raster.x)
    xmax = max(raster.x)
    ymin = min(raster.y)
    ymax = max(raster.y)
    data_vars = list(raster.data_vars)
    rastersource = NumPyRasterSource(
        raster[data_vars[0]],
        srs_wkt=wkt,
        xmin=xmin,
        xmax=xmax,
        ymin=ymin,
        ymax=ymax,
    )
    output: pd.DataFrame = exact_extract(
        rastersource,
        gdf,
        ['cell_id', 'coverage'],
        include_cols=['divide_id'],
        output='pandas',
    )  # type: ignore
    return output.set_index('divide_id')
