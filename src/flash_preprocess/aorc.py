"""Core AORC spatial extraction and disaggregation utilities."""

import atexit
from multiprocessing.pool import ThreadPool

import dask
import numpy as np
import s3fs
import xarray as xr
from scipy.sparse import csr_matrix

_pool = ThreadPool(16)
dask.config.set(pool=_pool)
atexit.register(_pool.terminate)


VARIABLE_LIST = [
    "APCP_surface",
    "DSWRF_surface",
    "TMP_2maboveground",
    "DLWRF_surface",
    "PRES_surface",
    "SPFH_2maboveground",
    "UGRD_10maboveground",
    "VGRD_10maboveground",
]

ACCUM_VARS = {"APCP_surface"}

_AORC_NCOLS = 8401  # full CONUS grid width, constant across all years


def open_aorc(start: np.datetime64, end: np.datetime64) -> xr.Dataset:
    """Open AORC v1.1 zarr stores for every calendar year spanned by [start, end].

    Parameters
    ----------
    start
        Start of range, inclusive, datetime64[h].
    end
        End of range, inclusive, datetime64[h].

    Returns
    -------
    xr.Dataset
        Lazily opened dataset with dims (time, latitude, longitude),
        latitude sorted descending, sliced to [start, end].
    """
    y0 = int(str(start)[:4])
    y1 = int(str(end)[:4])
    _s3 = s3fs.S3FileSystem(anon=True)
    stores = [
        s3fs.S3Map(root=f"s3://noaa-nws-aorc-v1-1-1km/{y}.zarr", s3=_s3, check=False)
        for y in range(y0, y1 + 1)
    ]
    ds = xr.open_mfdataset(stores, engine="zarr", parallel=True, consolidated=True)
    ds = ds.sortby("latitude", ascending=False)
    return ds.sel(time=slice(start, end))


def spatial_subset_weighted(
    ds: xr.Dataset,
    cell_ids: list,
) -> tuple[xr.Dataset, list]:
    """Subset to the catchment bounding box and remap cell_ids to local indices.

    Parameters
    ----------
    ds
        AORC dataset, dims (time, latitude, longitude).
    cell_ids
        Per-catchment arrays of full-grid flat cell indices.

    Returns
    -------
    ds_sub
        Dataset subsetted to the bounding box.
    local_cell_ids
        cell_ids remapped to the subsetted grid.
    """
    all_cids = np.concatenate(cell_ids)
    rows = all_cids // _AORC_NCOLS
    cols = all_cids % _AORC_NCOLS
    r0, r1 = int(rows.min()), int(rows.max())
    c0, c1 = int(cols.min()), int(cols.max())
    n_sub = c1 - c0 + 1

    print(
        f"  Bbox: rows {r0}-{r1}, cols {c0}-{c1} "
        f"({r1 - r0 + 1} × {n_sub} = {(r1 - r0 + 1) * n_sub:,} pixels, "
        f"was {4201 * 8401:,})",
    )

    ds_sub = ds.isel(latitude=slice(r0, r1 + 1), longitude=slice(c0, c1 + 1))
    local_cids = [
        (cids // _AORC_NCOLS - r0) * n_sub + (cids % _AORC_NCOLS - c0)
        for cids in cell_ids
    ]
    return ds_sub, local_cids


def spatial_subset_equal(
    ds: xr.Dataset,
    row_flat: np.ndarray,
    col_flat: np.ndarray,
) -> tuple[xr.Dataset, list, list]:
    """Subset to the catchment bounding box and return local row/col arrays.

    Parameters
    ----------
    ds
        AORC dataset, dims (time, latitude, longitude).
    row_flat
        Full-grid row indices for all catchment pixels.
    col_flat
        Full-grid column indices for all catchment pixels.

    Returns
    -------
    ds_sub, row_local, col_local
    """
    rows = np.asarray(row_flat)
    cols = np.asarray(col_flat)
    r0, r1 = int(rows.min()), int(rows.max())
    c0, c1 = int(cols.min()), int(cols.max())

    print(
        f"  Bbox: rows {r0}-{r1}, cols {c0}-{c1} "
        f"({r1 - r0 + 1} × {c1 - c0 + 1} = {(r1 - r0 + 1) * (c1 - c0 + 1):,} pixels, "
        f"was {4201 * 8401:,})",
    )

    ds_sub = ds.isel(latitude=slice(r0, r1 + 1), longitude=slice(c0, c1 + 1))
    return ds_sub, (rows - r0).tolist(), (cols - c0).tolist()


def build_weight_matrix(
    cell_ids_list: list,
    weights_list: list,
    n_basins: int,
    n_pixels: int,
) -> csr_matrix:
    """Build a (n_basins, n_pixels) CSR weight matrix (rows sum to 1).

    Parameters
    ----------
    cell_ids_list
        Per-catchment local flat pixel indices into the bbox grid.
    weights_list
        Per-catchment area weights (unnormalised).
    n_basins
        Number of catchments.
    n_pixels
        Total pixels in the bbox (bbox_lat * bbox_lon).

    Returns
    -------
    scipy.sparse.csr_matrix
        Shape (n_basins, n_pixels), float32.
    """
    rows = np.concatenate(
        [np.full(len(c), i, dtype=np.int32) for i, c in enumerate(cell_ids_list)],
    )
    cols = np.concatenate(cell_ids_list).astype(np.int32)
    vals = np.concatenate([w / w.sum() for w in weights_list]).astype(np.float32)
    return csr_matrix(
        (vals, (rows, cols)),
        shape=(n_basins, n_pixels),
        dtype=np.float32,
    )


def weighted_mean(flat_raster: np.ndarray, W: csr_matrix) -> np.ndarray:
    """Area-weighted catchment average via sparse matmul.

    Parameters
    ----------
    flat_raster
        Shape (num_hours, n_pixels_in_bbox).
    W
        Pre-built weight matrix from build_weight_matrix, shape (n_basins, n_pixels).

    Returns
    -------
    np.ndarray
        Shape (num_catchments, num_hours), float32. NaN propagates if any pixel is NaN.
    """
    return np.asarray(W @ flat_raster.T, dtype=np.float32)  # (n_basins, n_hours)


def groupby_mean_equal(data: np.ndarray, interval: np.ndarray) -> np.ndarray:
    """Equal-weight catchment average (point-in-polygon index).

    Parameters
    ----------
    data
        Shape (total_pixels, hours).
    interval
        Per-catchment pixel counts.

    Returns
    -------
    np.ndarray
        Shape (num_catchments, hours).
    """
    bins = np.insert(np.cumsum(interval), 0, 0)[:-1]
    mask = np.isnan(data)
    data = data.copy()
    data[mask] = 0.0
    g_count = np.add.reduceat(~mask, bins, axis=0)
    g_sum = np.add.reduceat(data, bins, axis=0)
    return g_sum / g_count


def disaggregate_to_15min(
    data: np.ndarray,
    var_name: str,
    time_vals: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Disaggregate hourly catchment data to 15-minute intervals.

    Accumulated variables (APCP_surface): uniform split ÷ 4.
    Instantaneous variables: linear interpolation between hourly values.

    Parameters
    ----------
    data
        Shape (n_basins, n_hours).
    var_name
        Variable name, used to select accumulation vs. interpolation.
    time_vals
        Hourly datetime64 array, shape (n_hours,).

    Returns
    -------
    data_15min
        Shape (n_basins, n_hours * 4).
    time_15min
        datetime64[m] array, shape (n_hours * 4,).
    """
    n_hours = data.shape[1]
    n_steps = n_hours * 4

    if var_name in ACCUM_VARS:
        data_15min = np.repeat(data / 4.0, 4, axis=1)
    else:
        x_15 = np.arange(n_steps, dtype=np.float64) * 0.25
        i_low = np.clip(np.floor(x_15).astype(int), 0, n_hours - 1)
        i_high = np.clip(np.ceil(x_15).astype(int), 0, n_hours - 1)
        frac = (x_15 - i_low).astype(np.float32)
        data_15min = data[:, i_low] * (1.0 - frac) + data[:, i_high] * frac

    t0 = time_vals[0].astype("datetime64[m]")
    time_15min = t0 + np.arange(n_steps) * np.timedelta64(15, "m")
    return data_15min, time_15min
