#!/usr/bin/env python3

from __future__ import annotations
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

import usgs_events as ue

warnings.filterwarnings("ignore")

'''
Full event extraction pipeline 
- Extracts flood events from USGS hydrograph at gages inside HUC8s of choice

Full event extraction pipeline dependencies (available in inputs folder): 
- usgs_events.py (open in the same folder)
- CONUS HUC8 shapefile
- Gage csv with columns STAID, LAT_GAGE, LNG_GAGE
'''
# ===========================================================================
# USER SETTINGS
# ===========================================================================

# --- Water years -------------------------------------------------------------
# Starting in WY 2021 reflects MRMS advancements made that year
# recommended to start in WY 2021 if completing full model pipeline with MRMS precip
WY_START = 2021
WY_END   = 2025

# --- Exceedance threshold ----------------------------------------------------
# Events are kept only if their peak >= Q at this exceedance probability.
# 25 = Q25 (exceeded only 25% of the time) — strict, larger events only
Q_EXCEEDANCE_PCT = 25   # <-- change to any value 0-100

# --- Output ------------------------------------------------------------------
OUTDIR       = ".../event_extraction_outputs"  # output folder for per-site CSVs
COMBINED_OUT = "combined_events.csv" # name for merged file written inside OUTDIR

# --- Site selection -----------------------------------------------------------
# Add as many HUC8 IDs as user wants — the pipeline runs each in turn and
# writes a separate combined CSV per HUC8 (e.g. huc8_03020201_combined_events.csv).
HUC8_LIST = [
"03020201"
]
HUC8_SHP  = ".../RUNOFF_interface/event_separation/event_extraction_inputs/huc8_conus/HUC8_US.shp"
GAGES_CSV = ".../RUNOFF_interface/event_separation/event_extraction_inputs/gages2_lt1000km2.csv"





# ===========================================================================
'''
Flash-flood-tuned event detection parameters:

-These have been edited to apply less smoothing to the hydrograph and capture
"flashier" events. These can be changed depending on outputs user requires.

-the default parameters from NOAA-OWPHydroTools are in usgs_events.py but 
get overridden here. 
'''
DETECT_KWARGS = dict(
    halflife="1h",
    window="2D",
    minimum_event_duration="1h",
    start_radius="2h",
)

# ---------------------------------------------------------------------------
# HUC8 gage selection
# ---------------------------------------------------------------------------
def gages_in_huc8(huc8_id: str, huc8_shp: str | Path, gages_csv: str | Path,
                  out_csv: str | Path | None = None) -> list[str]:
    """
    Spatial join to find all USGS gages (from gages_csv) inside a HUC8 polygon
    (from huc8_shp). Returns a list of zero-padded site codes.

    Parameters
    ----------
    huc8_id   : 8-digit HUC8 string, e.g. "11010002"
    huc8_shp  : path to the HUC8 shapefile (e.g. huc8_conus/HUC8_US.shp)
    gages_csv : path to gages CSV with columns STAID, LAT_GAGE, LNG_GAGE
    out_csv   : if given, save the station list here (can be reused as --stations)
    """
    import geopandas as gpd

    huc8_id = str(huc8_id).zfill(8)
    print(f"Loading HUC8 polygons from {huc8_shp} ...")
    huc8_gdf = gpd.read_file(huc8_shp)
    huc8_gdf["HUC8"] = huc8_gdf["HUC8"].astype(str).str.zfill(8)

    target = huc8_gdf.loc[huc8_gdf["HUC8"] == huc8_id].copy()
    if target.empty:
        raise ValueError(f"HUC8 {huc8_id} not found in {huc8_shp}.")

    print(f"Loading gages from {gages_csv} ...")
    gages = pd.read_csv(gages_csv, dtype={"STAID": str})
    gages_gdf = gpd.GeoDataFrame(
        gages,
        geometry=gpd.points_from_xy(gages["LNG_GAGE"], gages["LAT_GAGE"]),
        crs="EPSG:4326",
    ).to_crs(target.crs)

    inside = gpd.sjoin(gages_gdf, target[["geometry"]], how="inner", predicate="within")
    inside = inside[["STAID", "LAT_GAGE", "LNG_GAGE"]].dropna(subset=["STAID"]).drop_duplicates("STAID")
    inside["STAID"] = inside["STAID"].astype(str).str.zfill(8)
    inside = inside.sort_values("STAID").reset_index(drop=True)

    site_list = inside[["STAID"]].rename(columns={"STAID": "site_code"})

    if out_csv is not None:
        site_list.to_csv(out_csv, index=False)
        print(f"Saved {len(site_list)} gages to {out_csv}")

    # Build coords lookup: {site_code: (lat, lon)}
    coords = {
        row["STAID"]: (row["LAT_GAGE"], row["LNG_GAGE"])
        for _, row in inside.iterrows()
    }

    print(f"Found {len(site_list)} gage(s) in HUC8 {huc8_id}: "
          f"{', '.join(inside['STAID'].tolist())}")
    return inside["STAID"].tolist(), coords


# ---------------------------------------------------------------------------
# FDC helpers
# ---------------------------------------------------------------------------
def compute_fdc(q: pd.Series) -> pd.DataFrame:
    """Flow-duration curve with columns [exceedance_percent, discharge_cfs]."""
    q_clean = q.dropna()
    q_clean = q_clean[q_clean > 0]
    flows_sorted = np.sort(q_clean.values)[::-1]
    n = len(flows_sorted)
    rank = np.arange(1, n + 1)
    exceedance = 100.0 * rank / (n + 1)
    return pd.DataFrame({"exceedance_percent": exceedance, "discharge_cfs": flows_sorted})


def q_exceedance(fdc: pd.DataFrame, percent: float) -> float:
    """Discharge at a given exceedance probability (%) via linear interpolation."""
    return float(np.interp(percent, fdc["exceedance_percent"], fdc["discharge_cfs"]))


# ---------------------------------------------------------------------------
# event detection with fallback
# ---------------------------------------------------------------------------
def _clean_for_events(q: pd.Series) -> pd.Series:
    """Trim first/last timestep to prevent open events at record edges,
    which cause the unequal starts/ends error in hydrotools."""
    q = q.dropna()
    if len(q) > 2:
        q = q.iloc[1:-1]
    return q


def detect_events_with_fallback(q: pd.Series, **detect_kwargs) -> pd.DataFrame:
    """
    Try hydrotools event detection; if it fails due to data gaps, retry with
    a gap-filled series (interpolated + ffill + bfill) before giving up.
    """
    # Attempt 1: trim edges and run hydrotools
    try:
        return ue.detect_events(_clean_for_events(q), **detect_kwargs)
    except Exception as err:
        print(f"  hydrotools failed ({err}); retrying with gap-filled series ...")

    # Attempt 2: fill NaNs caused by data gaps and retry
    try:
        q_filled = q.interpolate(method="time").ffill().bfill()
        return ue.detect_events(_clean_for_events(q_filled), **detect_kwargs)
    except Exception as err2:
        print(f"  hydrotools failed again ({err2}); skipping site.")
        return pd.DataFrame(columns=["start", "end", "peak", "t_peak"])


# ---------------------------------------------------------------------------
# per-site pipeline
# ---------------------------------------------------------------------------
def process_site(site: str, wy_start: int, wy_end: int,
                 coords: dict | None = None,
                 q_exceedance_pct: float = 50.0) -> pd.DataFrame | None:
    """Full pipeline for one site. Returns the event table, or None on failure."""
    print(f"\n[{site}] fetching WY{wy_start}-{wy_end} ...")
    try:
        q = ue.load_usgs_discharge(site, wy_start, wy_end, freq="15min")
    except Exception as exc:
        print(f"[{site}] ERROR retrieving data: {exc}")
        return None

    print(f"[{site}] {len(q):,} steps | min {q.min():.2f}  mean {q.mean():.1f}  "
          f"max {q.max():,.0f} cfs")

    # FDC + Qx threshold
    fdc   = compute_fdc(q)
    q_thr = q_exceedance(fdc, q_exceedance_pct)
    print(f"[{site}] Q{q_exceedance_pct:.0f} = {q_thr:.2f} cfs")

    # Event detection (with fallback)
    events = detect_events_with_fallback(q, **DETECT_KWARGS)
    n_total = len(events)
    print(f"[{site}] {n_total} events detected")

    # Filter events with peak < Qx
    events = events[events["peak"] >= q_thr].reset_index(drop=True)
    n_removed = n_total - len(events)
    print(f"[{site}] removed {n_removed} events (peak < Q{q_exceedance_pct:.0f}); {len(events)} remain")

    if events.empty:
        print(f"[{site}] no events after filtering; skipping.")
        return None

    # Build event table
    table = ue.build_event_table(q, events, site)

    # Attach gage coordinates from input gages file
    lat, lon = (coords or {}).get(site, (None, None))
    table["gage_lat"] = lat
    table["gage_lon"] = lon

    print(f"[{site}] {len(table)} events ready")
    return table


# ---------------------------------------------------------------------------
# combine per-site CSVs into one master file
# ---------------------------------------------------------------------------
def combine_tables(tables: dict[str, pd.DataFrame], out_path: Path) -> pd.DataFrame:
    """Concatenate per-site tables, add a sequential event_id, and save."""
    master = pd.concat(tables.values(), ignore_index=True)
    master.insert(0, "event_id", range(1, len(master) + 1))
    master.to_csv(out_path, index=False)
    print(f"\nCombined {len(tables)} site(s) → {out_path}  ({len(master):,} total events)")
    return master


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------
def run(
    outdir: str = ".",
    wy_start: int = 2021,
    wy_end: int = 2025,
    combined_out: str = "combined_events.csv",
    huc8: str | None = None,
    huc8_shp: str | None = None,
    gages_csv: str | None = None,
    q_exceedance_pct: float = 50.0,
) -> dict[str, pd.DataFrame]:

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    if huc8 is None or huc8_shp is None or gages_csv is None:
        raise ValueError("HUC8, HUC8_SHP, and GAGES_CSV must all be set.")

    # --- resolve site list -------------------------------------------
    station_csv_path = outdir / f"stations_huc8_{str(huc8).zfill(8)}.csv"
    sites, coords = gages_in_huc8(huc8, huc8_shp, gages_csv, out_csv=station_csv_path)

    print(f"\n{len(sites)} site(s): {', '.join(sites)} | WY{wy_start}-{wy_end}")

    # --- per-site processing --------------------------------------
    results: dict[str, pd.DataFrame] = {}
    for site in sites:
        table = process_site(site, wy_start, wy_end, coords=coords,
                              q_exceedance_pct=q_exceedance_pct)
        if table is not None:
            results[site] = table

    print(f"\n{len(results)}/{len(sites)} sites processed successfully.")

    # --- combine into one master CSV ---------------------------------
    if results:
        if huc8 is not None:
            stem, suffix = combined_out.rsplit(".", 1) if "." in combined_out else (combined_out, "csv")
            combined_out = f"huc8_{str(huc8).zfill(8)}_{stem}.{suffix}"
        combined_path = outdir / combined_out
        combine_tables(results, combined_path)

    return results


if __name__ == "__main__":
    if not HUC8_LIST:
        raise ValueError("HUC8_LIST is empty — add at least one HUC8 ID.")
    if not HUC8_SHP or not GAGES_CSV:
        raise ValueError("HUC8_SHP and GAGES_CSV must be set.")
    for huc8_id in HUC8_LIST:
        print(f"\n{'='*60}")
        print(f"  HUC8: {huc8_id}")
        print(f"{'='*60}")
        run(
            outdir=OUTDIR,
            wy_start=WY_START,
            wy_end=WY_END,
            combined_out=COMBINED_OUT,
            huc8=huc8_id,
            huc8_shp=HUC8_SHP,
            gages_csv=GAGES_CSV,
            q_exceedance_pct=Q_EXCEEDANCE_PCT,
        )
