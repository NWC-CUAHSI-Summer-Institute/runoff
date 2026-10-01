#!/usr/bin/env python3
"""
huc8_radar_coverage.py - zonal statistics of a raster per HUC8 watershed.

Turns a gridded field where every pixel has a value (radar coverage, beam height,
a quality index, anything) into one row per HUC8 with the area weighted mean, the
minimum, the maximum, the share of the watershed at or above a threshold, and the
share of the watershed that had a value at all. The result is written to
data/episodes/huc8_attributes.csv, which export_dataset.py joins into the huc8 and
episode_huc8 tables on the next run. Running it again with another raster and
another --name adds columns; existing ones are kept. Only the lower 48 watersheds
(HUC2 01 to 18) are written unless --all-hucs is given; watersheds with no pixel
inside the raster (open water, the Canadian side of the Great Lakes) keep empty
statistics and a valid share of 0.

The RUNOFF radar layer, storm_episodes/data/radar_cov.tif, is a HYDROLOGIC
coverage: at every 0.01 degree pixel the value is the percent of the drainage
area upstream of that pixel that weather radar covers. So the value at a gage is
the coverage of the basin above the gage, and the value at a flash flood report is
the coverage of the basin draining to that place. export_dataset.py samples the
layer at gages and reports for that reason; the per watershed statistics here are
the summary over all pixels of the watershed.

Inputs
    --raster   any raster rasterio can open: GeoTIFF, or a NetCDF variable written
               as NETCDF:"path/file.nc":variable (quote it in the shell)
    --band     band index, default 1
    --nodata   value to treat as missing (default: the raster's own nodata)
    --threshold  optional value; adds <name>_frac_ge<threshold>
    --name     column prefix, default radar_coverage
    --wbd      HUC8 polygons, default data/geo/huc8_wbd.geojson (fetch_huc8_wbd.py)

    python engine/episodes/huc8_radar_coverage.py --raster data/geo/radar_cov.tif --threshold 50 100
    python engine/episodes/huc8_radar_coverage.py --raster 'NETCDF:"data/geo/rqi.nc":rqi' --name rqi

Method: the watersheds are reprojected to the raster's CRS and burned onto the
raster grid (each pixel gets the watershed whose polygon covers its centre), so the
statistics are exact on the raster's own grid, no resampling. Pixels are weighted
by cos(latitude) when the raster is in geographic coordinates, by nothing otherwise.
Needs rasterio, geopandas, shapely, numpy, pandas.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio import features

HERE = Path(__file__).resolve().parents[2]
WBD = HERE / "data" / "geo" / "huc8_wbd.geojson"
OUT = HERE / "data" / "episodes" / "huc8_attributes.csv"


def check_writable(*paths: Path) -> None:
    """Stop before any work when an output file is locked (usually open in Excel)."""
    locked = []
    for p in paths:
        if p.exists():
            try:
                with open(p, "r+b"):
                    pass
            except PermissionError:
                locked.append(p)
    if locked:
        names = "\n  ".join(str(p) for p in locked)
        raise SystemExit(f"cannot write, the file is open in another program (Excel?):\n  {names}\n"
                         "close it and run again")


def save_csv(df: pd.DataFrame, path: Path) -> Path:
    """Write the CSV; when the target is locked, keep the result next to it as <name>.new.csv."""
    try:
        df.to_csv(path, index=False)
        return path
    except PermissionError:
        alt = path.with_name(path.stem + ".new.csv")
        df.to_csv(alt, index=False)
        print(f"{path.name} is open in another program; result saved as {alt.name}. "
              f"Close the file, then rename {alt.name} to {path.name}.")
        return alt


def main() -> None:
    """Per HUC8 mean, min, max, threshold share and valid share of one raster band."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raster", required=True)
    ap.add_argument("--band", type=int, default=1)
    ap.add_argument("--nodata", type=float, default=None)
    ap.add_argument("--threshold", type=float, nargs="*", default=None,
                    help="one or more values; adds <name>_frac_ge<t> for each")
    ap.add_argument("--name", default="radar_coverage")
    ap.add_argument("--wbd", type=Path, default=WBD)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--all-hucs", action="store_true",
                    help="keep Alaska, Hawaii, Caribbean and Pacific watersheds too (default: HUC2 01 to 18)")
    args = ap.parse_args()
    check_writable(args.out)

    with rasterio.open(args.raster) as src:
        arr = src.read(args.band).astype("float64")
        nodata = args.nodata if args.nodata is not None else src.nodata
        transform, crs, shape = src.transform, src.crs, arr.shape
    valid = np.isfinite(arr)
    if nodata is not None:
        valid &= arr != nodata
    print(f"raster {shape[0]} x {shape[1]}, crs {crs}, valid pixels {valid.mean():.1%}")

    huc = gpd.read_file(args.wbd)[["huc8", "geometry"]]
    huc["huc8"] = huc.huc8.astype(str).str.zfill(8)
    huc = huc[huc.geometry.notna() & ~huc.geometry.is_empty]
    if not args.all_hucs:
        huc = huc[huc.huc8.str[:2].isin([f"{i:02d}" for i in range(1, 19)])]
    huc = huc.reset_index(drop=True)
    huc = huc.set_crs("EPSG:4326", allow_override=True)
    if crs is not None and crs.to_string() not in ("EPSG:4326", "OGC:CRS84"):
        huc = huc.to_crs(crs)
    codes = features.rasterize(((geom, i + 1) for i, geom in enumerate(huc.geometry)),
                               out_shape=shape, transform=transform, fill=0, dtype="int32")
    n = len(huc) + 1

    # pixel weights: cos(latitude) on a geographic grid, 1 otherwise
    if crs is None or crs.is_geographic:
        rows = np.arange(shape[0])
        lat = transform.f + (rows + 0.5) * transform.e
        w = np.repeat(np.cos(np.deg2rad(lat))[:, None], shape[1], axis=1)
    else:
        w = np.ones(shape)

    lab = codes.ravel()
    wv = (w * valid).ravel()
    wall = w.ravel()
    vals = np.where(valid, arr, 0.0).ravel()
    w_valid = np.bincount(lab, weights=wv, minlength=n)
    w_all = np.bincount(lab, weights=wall, minlength=n)
    s = np.bincount(lab, weights=wv * vals, minlength=n)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = s / w_valid
        valid_frac = w_valid / w_all
    ok = valid.ravel()
    mins = np.full(n, np.inf)
    maxs = np.full(n, -np.inf)
    np.minimum.at(mins, lab[ok], vals[ok])
    np.maximum.at(maxs, lab[ok], vals[ok])
    mins[~np.isfinite(mins)] = np.nan
    maxs[~np.isfinite(maxs)] = np.nan
    out = pd.DataFrame({"huc8": huc.huc8,
                        f"{args.name}_mean": np.round(mean[1:], 4),
                        f"{args.name}_min": np.round(mins[1:], 4),
                        f"{args.name}_max": np.round(maxs[1:], 4),
                        f"{args.name}_valid_frac": np.round(valid_frac[1:], 4)})
    for t in (args.threshold or []):
        above = np.bincount(lab, weights=wv * (vals >= t), minlength=n)
        with np.errstate(invalid="ignore", divide="ignore"):
            out[f"{args.name}_frac_ge{t:g}"] = np.round((above / w_valid)[1:], 4)
    covered = int(np.isfinite(mean[1:]).sum())
    print(f"{covered} of {len(huc)} watersheds have a value")

    if args.out.exists():
        old = pd.read_csv(args.out, dtype={"huc8": str})
        old["huc8"] = old.huc8.str.zfill(8)
        old = old.drop(columns=[c for c in old.columns if c in out.columns and c != "huc8"])
        out = old.merge(out, on="huc8", how="outer")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    written = save_csv(out, args.out)
    print(f"wrote {written} ({len(out)} rows, columns: {', '.join(c for c in out.columns if c != 'huc8')})")
    print("next: python engine/episodes/export_dataset.py   (joins these columns into huc8 and episode_huc8)")


if __name__ == "__main__":
    main()
