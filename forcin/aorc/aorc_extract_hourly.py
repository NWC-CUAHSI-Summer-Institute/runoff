"""Extract hourly AORC forcing data for a set of NextGen catchments.

Reads the NOAA AORC v1.1 1km zarr from S3, spatially averages each variable
over catchment pixels, and saves one .npy file per variable with shape
(num_catchments, num_hours).

Time range: specify --start / --end (ISO 8601 dates or datetimes). The script
opens one yearly zarr per calendar year spanned and slices to the exact range.
Use --year YYYY as a shorthand for a full calendar year.

Supports two index formats (auto-detected):

  Equal-weight (built by index_hf22.py):
    Averages all AORC pixels whose centre falls inside the catchment polygon.

  Area-weighted (built by build_aorc_index_weighted.py):
    Applies exactextract coverage fractions so edge pixels contribute in
    proportion to their overlap with the catchment polygon.

Usage:
    # Full year
    python aorc_extract_hourly.py --year 2022 --index /path/to/index.pkl

    # Arbitrary range (can span multiple years)
    python aorc_extract_hourly.py \\
        --start 2019-10-01 --end 2022-09-30T23:00 \\
        --index /path/to/index.pkl --output-dir /path/to/output/

Output files saved to <output-dir>/:
    APCP_surface.npy       shape: (basins, hours)
    DSWRF_surface.npy
    ...
    time.npy               UTC timestamps as numpy datetime64[h], shape: (hours,)

Row i corresponds to station_ids[i] from the index pkl.
"""

import os
import time
import argparse
import pickle
import s3fs
import xarray as xr
import numpy as np
from multiprocessing.pool import ThreadPool
import dask
dask.config.set(pool=ThreadPool(4))


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



def groupby_mean_equal(data, interval):
    """
    Equal-weight average of pixel rows within groups.
    data: (total_pixels, hours), interval: per-catchment pixel counts.
    Returns (num_catchments, hours).
    """
    bins    = np.insert(np.cumsum(interval), 0, 0)[:-1]
    mask    = np.isnan(data)
    data    = data.copy()
    data[mask] = 0.0
    g_count = np.add.reduceat(~mask, bins, axis=0)
    g_sum   = np.add.reduceat(data,  bins, axis=0)
    return g_sum / g_count


def weighted_mean(flat_raster, cell_ids_list, weights_list, num_catchments, num_hours):
    """
    Area-weighted average using exactextract coverage fractions.
    flat_raster: (hours, rs_row*rs_col) — row 0 = highest latitude.
    Returns (num_catchments, hours).
    """
    out = np.full((num_catchments, num_hours), np.nan, dtype=np.float32)
    for i, (cids, w) in enumerate(zip(cell_ids_list, weights_list)):
        cols    = flat_raster[:, cids]
        has_nan = np.isnan(cols).any(axis=1)
        out[i]  = np.nansum(cols * w, axis=1) / w.sum()
        out[i, has_nan] = np.nan
    return out



def open_aorc(start: np.datetime64, end: np.datetime64) -> xr.Dataset:
    """
    Open AORC zarr stores for every calendar year spanned by [start, end],
    concatenate along time, and return the sliced dataset.
    """
    y0 = int(str(start)[:4])
    y1 = int(str(end)[:4])
    years = range(y0, y1 + 1)

    _s3    = s3fs.S3FileSystem(anon=True)
    stores = [
        s3fs.S3Map(root=f"s3://noaa-nws-aorc-v1-1-1km/{y}.zarr", s3=_s3, check=False)
        for y in years
    ]
    ds = xr.open_mfdataset(stores, engine="zarr", parallel=True, consolidated=True)
    ds = ds.sortby("latitude", ascending=False)
    ds = ds.sel(time=slice(start, end))
    return ds



def main():
    parser = argparse.ArgumentParser(description="Extract hourly AORC data to catchments.")

    time_grp = parser.add_mutually_exclusive_group(required=True)
    time_grp.add_argument("--year", type=int,
                          help="Full calendar year to extract (e.g. 2022)")
    time_grp.add_argument("--start", metavar="DATETIME",
                          help="Start of time range, ISO 8601 (e.g. 2019-10-01 or "
                               "2019-10-01T06:00). Pair with --end.")

    parser.add_argument("--end", metavar="DATETIME", default=None,
                        help="End of time range, inclusive (ISO 8601). Required with --start.")
    parser.add_argument("--index",
                        default="/Users/leoglonz/Desktop/noaa/data/subset_index_dict.pkl",
                        help="Index pkl: equal-weight (index_hf22.py) or "
                             "area-weighted (build_aorc_index_weighted.py)")
    parser.add_argument("--output-dir",
                        default="/Users/leoglonz/Desktop/noaa/data/forcing_hourly",
                        help="Output directory for .npy files")
    parser.add_argument("--variables", nargs="+", default=VARIABLE_LIST,
                        help="Variables to extract (default: all 8)")
    args = parser.parse_args()

    if args.start and args.end is None:
        parser.error("--end is required when --start is specified")

    # Resolve time bounds
    if args.year:
        start = np.datetime64(f"{args.year}-01-01T00:00", "h")
        end   = np.datetime64(f"{args.year}-12-31T23:00", "h")
    else:
        start = np.datetime64(args.start, "h")
        end   = np.datetime64(args.end,   "h")

    os.makedirs(args.output_dir, exist_ok=True)

    # --- Load index and detect format ---
    print(f"Loading index: {args.index}")
    with open(args.index, "rb") as f:
        idx = pickle.load(f)

    weighted = "weights" in idx

    if weighted:
        cell_ids_list = idx["cell_ids"]
        weights_list  = idx["weights"]
        n_basins      = len(idx["station_ids"])
        print(f"  Area-weighted index: {n_basins} catchments")
    else:
        pixel_counts = [len(r) for r in idx["col_list"]]
        row_flat     = [item for sub in idx["row_list"] for item in sub]
        col_flat     = [item for sub in idx["col_list"] for item in sub]
        n_basins     = len(idx["station_ids"])
        print(f"  Equal-weight index: {n_basins} catchments, "
              f"{sum(pixel_counts)} total pixels")

    # --- Open and slice AORC ---
    print(f"Opening AORC zarr(s) for {start} → {end} ...")
    ds        = open_aorc(start, end)
    time_vals = ds.time.values
    n_hours   = len(time_vals)
    print(f"  {n_hours} hourly timesteps")

    # Save the time axis so outputs are self-describing
    np.save(os.path.join(args.output_dir, "time.npy"),
            time_vals.astype("datetime64[h]"))

    t0 = time.time()

    for var_name in args.variables:
        print(f"Processing {var_name}...", end=" ", flush=True)
        t_var = time.time()

        raw = ds[var_name].compute().values     # (hours, lat, lon)

        if weighted:
            flat_raster = raw.reshape(n_hours, -1)
            result = weighted_mean(flat_raster, cell_ids_list, weights_list,
                                   n_basins, n_hours)
        else:
            raw_sel = raw[..., row_flat, col_flat]  # (hours, total_pixels)
            raw_sel = raw_sel.T                      # (total_pixels, hours)
            result  = groupby_mean_equal(raw_sel, pixel_counts)

        np.save(os.path.join(args.output_dir, f"{var_name}.npy"), result)
        del raw, result
        print(f"done ({time.time() - t_var:.1f}s)")

    ds.close()
    print(f"\nAll variables complete. Total time: {time.time() - t0:.1f}s")
    print(f"Output: {args.output_dir}/")
    print(f"Array shape per file: ({n_basins}, {n_hours})")
    print(f"Row order matches index station_ids: {args.index}")


if __name__ == "__main__":
    main()
