#!/usr/bin/env python3
"""
export_dataset.py - the RUNOFF episodes dataset as one folder of flat tables.

Everything the engine computed for the general modeling path, assembled into a
single self-contained folder that a modeler can load with pandas and join on two
keys, episode_id and huc8:

    episodes            one row per storm episode: window, impacts, footprint, MRMS
                        rainfall and return periods, FLASH models and guidance, the
                        NWS episode narrative
    episode_huc8        one row per (episode, HUC8 watershed): the modeling table.
                        Rainfall, return period coverage, FLASH products and gages
                        for that watershed in that episode, the reports that fell
                        inside it, and the label "flooded" (any report in the unit)
    events              NOAA Storm Events flash flood reports with the event narrative
    lsrs                NWS Local Storm Reports with the remark as narrative
    hourly              hourly footprint series per episode: rain, 1 h ARI, FLASH
    huc8                static watershed attributes (name, states, area, gages, and
                        any extra columns you supply, for example radar coverage)
    gages, episode_gages  RUNOFF USGS gages (under 1000 km2) and which episode
                        footprints contain them
    README.md, DATA_DICTIONARY.csv

Each table is written as Parquet (types preserved, narratives safe) and as CSV.

Inputs (all produced by the scripts in this folder and engine/gages)
    data/episodes/episodes.csv, episode_events.csv, episode_lsrs.csv,
    episode_huc8.csv, huc8_lookup.csv, data/gages/gages.csv,
    assets/data/ep/<id>.json
    optional: data/geo/huc8_wbd.geojson  (assigns each report to its HUC8; needs
              geopandas; without it the per watershed impact columns stay empty)
    optional: data/episodes/huc8_attributes.csv  any extra per-HUC8 columns keyed
              by huc8 (radar_coverage from huc8_radar_coverage.py, soils, ...);
              they are joined into huc8 and episode_huc8 as they are

    python engine/episodes/export_dataset.py                    # -> storm_episodes/ (data/, README, dictionary)
    python engine/episodes/export_dataset.py --zip              # also storm_episodes.zip next to it
    python engine/episodes/export_dataset.py --out /path/to/dir --version 1.1

Layout of the output folder (committed to the repository as storm_episodes/):
    README.md, DATA_DICTIONARY.csv     what the tables are, every column
    data/<table>.parquet + .csv        the eight tables
    notebooks/                         worked examples (kept in the repository, not
                                       touched by this script)

QPE/FFG note: FLASH records written before the percent fix in precompute_flash.py
hold the ratio x100 and a coverage computed against the wrong threshold. They are
rescaled here and their coverage columns left empty; rerun precompute_flash.py
--force to fill them.
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parents[2]
EP_DIR = HERE / "data" / "episodes"
JSON_DIR = HERE / "assets" / "data" / "ep"
GAGES_CSV = HERE / "data" / "gages" / "gages.csv"
WBD = HERE / "data" / "geo" / "huc8_wbd.geojson"
ATTRS = EP_DIR / "huc8_attributes.csv"
# the hydrologic radar coverage layer (percent of the drainage area upstream of each
# pixel covered by radar); sampled at gages and reports when present
RADAR = [HERE / "storm_episodes" / "data" / "radar_cov.tif", HERE / "data" / "geo" / "radar_cov.tif"]

DURS = [1, 3, 6]
MODELS = ["crest", "sac", "hp"]
FFGS = ["ffg1", "ffg3", "ffg6", "ffgmax"]
FFG_NAME = {"ffg1": "ffg1h", "ffg3": "ffg3h", "ffg6": "ffg6h", "ffgmax": "ffgmax"}
LEGACY_COLS = ["max_roll_6h_mm", "max_roll_12h_mm", "max_roll_24h_mm", "max_roll_72h_mm",
               "max_ari_years", "max_ari_duration", "mrms_product"]


# ----------------------------------------------------------------------------- helpers
def g(d, *keys):
    """Nested get that tolerates missing dicts: g(d, 'fp', 'max1', 'max')."""
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def flat_mrms(stats: dict, thresholds: list, prefix: str = "") -> dict:
    """Flatten one MRMS statistics block (footprint, county union or HUC8) to columns."""
    out = {}
    out[prefix + "rain_ep_mean_mm"] = g(stats, "rain", "ep", "mean")
    out[prefix + "rain_ep_max_mm"] = g(stats, "rain", "ep", "max")
    out[prefix + "rain_pre5h_mean_mm"] = g(stats, "rain", "pre", "mean")
    out[prefix + "rain_pre5h_max_mm"] = g(stats, "rain", "pre", "max")
    for d in DURS:
        out[f"{prefix}max{d}h_peak_mm"] = g(stats, f"max{d}", "max")
        out[f"{prefix}max{d}h_mean_of_peaks_mm"] = g(stats, f"max{d}", "mean")
        t = g(stats, f"max{d}", "t")
        if t is not None:
            out[f"{prefix}max{d}h_peak_time_utc"] = t
    for d in DURS:
        out[f"{prefix}ari{d}h_peak_yr"] = g(stats, f"ari{d}", "max")
        out[f"{prefix}ari{d}h_valid_frac"] = g(stats, f"ari{d}", "valid")
        cov = g(stats, f"ari{d}", "cov") or []
        for i, t in enumerate(thresholds):
            out[f"{prefix}ari{d}h_cov_ge{t}yr"] = cov[i] if i < len(cov) else None
    return out


def flat_flash_fp(fl: dict) -> dict:
    """Flatten the footprint FLASH block; rescales records from before the percent fix."""
    out = {}
    if not fl or not fl.get("fp"):
        return out
    legacy = fl.get("ffg_scale") != "ratio"
    scale = 0.01 if legacy else 1.0
    fp = fl["fp"]
    for k in MODELS:
        out[f"{k}_peak_m3s_km2"] = g(fp, k, "max")
        out[f"{k}_mean_of_peaks_m3s_km2"] = g(fp, k, "mean_peak")
        out[f"{k}_peak_time_utc"] = g(fp, k, "t")
    for k in FFGS:
        n = FFG_NAME[k]
        mx, mn = g(fp, k, "max"), g(fp, k, "mean_peak")
        out[f"{n}_peak_ratio"] = None if mx is None else round(mx * scale, 3)
        out[f"{n}_mean_of_peaks_ratio"] = None if mn is None else round(mn * scale, 3)
        out[f"{n}_peak_time_utc"] = g(fp, k, "t")
        out[f"{n}_share_ge1"] = None if legacy else g(fp, k, "cov1")
    out["flash_hours"] = fl.get("hours")
    out["flash_missing_product_hours"] = sum((fl.get("missing") or {}).values())
    out["flash_legacy_scale"] = int(legacy)
    return out


def flat_flash_huc(fl: dict, code: str) -> dict:
    """Per HUC8 FLASH peaks for one watershed of one episode."""
    out = {}
    s = g(fl, "huc8", code)
    if not s:
        return out
    legacy = fl.get("ffg_scale") != "ratio"
    scale = 0.01 if legacy else 1.0
    for k in MODELS:
        out[f"{k}_peak_m3s_km2"] = s.get(k)
    for k in FFGS:
        v = s.get(k)
        out[f"{FFG_NAME[k]}_peak_ratio"] = None if v is None else round(v * scale, 3)
    out["ffgmax_share_ge1"] = None if legacy else s.get("ffgmax_cov1")
    return out


_WBD_CACHE = {}


def assign_huc8(df: pd.DataFrame, wbd_path: Path) -> pd.Series:
    """HUC8 containing each (lat, lon) row via the WBD polygons, or all-NA without geopandas."""
    try:
        import geopandas as gpd
    except ImportError:
        print("  geopandas not installed: reports are not assigned to watersheds")
        return pd.Series([pd.NA] * len(df), index=df.index, dtype="string")
    if not wbd_path.exists():
        print(f"  {wbd_path} not found: reports are not assigned to watersheds")
        return pd.Series([pd.NA] * len(df), index=df.index, dtype="string")
    if "huc" not in _WBD_CACHE:                     # the 60 MB WBD file is read once
        huc = gpd.read_file(wbd_path)[["huc8", "geometry"]]
        huc["huc8"] = huc.huc8.astype(str).str.zfill(8)
        huc = huc[huc.geometry.notna() & ~huc.geometry.is_empty].set_crs("EPSG:4326", allow_override=True)
        _WBD_CACHE["huc"] = huc
    huc = _WBD_CACHE["huc"]
    pts = gpd.GeoDataFrame(df[[]].copy(), geometry=gpd.points_from_xy(df.lon, df.lat), crs="EPSG:4326")
    hit = gpd.sjoin(pts, huc, how="left", predicate="within")
    hit = hit[~hit.index.duplicated(keep="first")]
    return hit["huc8"].reindex(df.index).astype("string")


def sample_radar(df: pd.DataFrame, lat_col: str = "lat", lon_col: str = "lon"):
    """Radar layer value at each point, or None when the layer or rasterio is absent.

    The layer is a hydrologic coverage, so the value at a point is the percent of the
    drainage area upstream of that point that weather radar covers.
    """
    path = next((p for p in RADAR if p.exists()), None)
    if path is None:
        return None
    try:
        import rasterio
    except ImportError:
        print("  rasterio not installed: radar coverage not sampled at points")
        return None
    lat = pd.to_numeric(df[lat_col], errors="coerce")
    lon = pd.to_numeric(df[lon_col], errors="coerce")
    ok = lat.notna() & lon.notna()
    vals = np.full(len(df), np.nan)
    with rasterio.open(path) as src:
        got = np.array([v[0] for v in src.sample(list(zip(lon[ok], lat[ok])))], dtype="float64")
        nodata = src.nodata
    got[~np.isfinite(got)] = np.nan
    if nodata is not None:
        got[got == nodata] = np.nan
    vals[ok.values] = got
    return np.round(vals, 2)


TABLES = ["episodes", "episode_huc8", "events", "lsrs", "hourly", "huc8", "gages", "episode_gages"]


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


def write(df: pd.DataFrame, out: Path, name: str, sizes: dict) -> None:
    """Parquet + CSV for one table."""
    df.to_parquet(out / f"{name}.parquet", index=False)
    df.to_csv(out / f"{name}.csv", index=False)
    sizes[name] = (len(df), (out / f"{name}.parquet").stat().st_size / 1e6,
                   (out / f"{name}.csv").stat().st_size / 1e6)


# ----------------------------------------------------------------------------- build
def main() -> None:
    """Assemble the dataset folder."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=None, help="dataset folder, default storm_episodes")
    ap.add_argument("--version", default="1", help="dataset version tag written into the README")
    ap.add_argument("--zip", action="store_true", help="also write <out>.zip next to the folder")
    args = ap.parse_args()
    root = args.out or (HERE / "storm_episodes")
    out = root / "data"
    out.mkdir(parents=True, exist_ok=True)
    check_writable(*[out / f"{n}.{ext}" for n in TABLES for ext in ("parquet", "csv")],
                   root / "README.md", root / "DATA_DICTIONARY.csv",
                   *([root.with_suffix(".zip")] if args.zip else []))
    if importlib.util.find_spec("pyarrow") is None and importlib.util.find_spec("fastparquet") is None:
        raise SystemExit("no parquet engine installed; run:  pip install pyarrow   then start again")
    sizes = {}

    # ---- base tables -------------------------------------------------------------
    ep = pd.read_csv(EP_DIR / "episodes.csv", dtype={"county_fips": str, "states": str})
    ep = ep.drop(columns=[c for c in LEGACY_COLS if c in ep.columns])
    ep["episode_id"] = ep.episode_id.astype(int)
    ev = pd.read_csv(EP_DIR / "episode_events.csv", dtype={"county_fips": str, "state": str})
    ls = pd.read_csv(EP_DIR / "episode_lsrs.csv", dtype={"county_fips": str, "wfo": str})
    eh = pd.read_csv(EP_DIR / "episode_huc8.csv", dtype={"huc8": str})
    eh = eh[eh.kept == 1].drop(columns=["kept"]).copy()
    lookup = pd.read_csv(EP_DIR / "huc8_lookup.csv", dtype={"huc8": str})
    print(f"episodes {len(ep):,} | events {len(ev):,} | lsrs {len(ls):,} | "
          f"episode-huc8 rows {len(eh):,} | huc8 {len(lookup):,}")

    # ---- reports -> watershed ------------------------------------------------------
    print("assigning reports to watersheds ...")
    ev["huc8"] = assign_huc8(ev, WBD)
    ls["huc8"] = assign_huc8(ls, WBD)
    # hydrologic radar coverage at the report location
    rc_ev, rc_ls = sample_radar(ev), sample_radar(ls)
    if rc_ev is not None:
        ev["radar_coverage_at_report"] = rc_ev
        ls["radar_coverage_at_report"] = rc_ls
        print(f"  radar coverage sampled at {int(np.isfinite(rc_ev).sum()):,} events and "
              f"{int(np.isfinite(rc_ls).sum()):,} LSRs")

    # ---- per episode JSON: MRMS, FLASH, hourly -------------------------------------
    print("reading per episode statistics ...")
    thresholds = [1, 2, 5, 10, 20, 25, 50, 100, 200]
    fp_rows, huc_rows, hourly_rows = {}, {}, []
    for p in sorted(glob.glob(str(JSON_DIR / "*.json"))):
        d = json.load(open(p, encoding="utf-8"))
        eid = int(d["id"])
        thresholds = g(d, "ari", "thresholds") or thresholds
        fl = d.get("flash") or {}
        row = {"n_huc8": len(d.get("huc8") or []), "footprint_km2": g(d, "fp", "area_km2"),
               "mrms_hours": d.get("hours"),
               "qpe_products": ";".join(f"{k}:{v}" for k, v in (g(d, "qpe", "products") or {}).items()),
               "qpe_missing_hours": len(g(d, "qpe", "missing") or []),
               "ari_missing_duration_hours": sum(len(v) for v in (g(d, "ari", "missing") or {}).values()),
               "artifact_cells": g(d, "qpe", "artifact_cells"),
               "artifact_cells_in_footprint": g(d, "qpe", "artifact_cells_in_footprint")}
        row.update(flat_mrms(d.get("fp") or {}, thresholds))
        row.update(flat_flash_fp(fl))
        fp_rows[eid] = row
        for h in d.get("huc8") or []:
            r = flat_mrms(h, thresholds)
            r.update(flat_flash_huc(fl, h["h"]))
            huc_rows[(eid, h["h"])] = r
        # hourly: rain series + flash series merged on the hour
        fs = {}
        if fl.get("series"):
            fields = fl.get("series_fields") or []
            legacy = fl.get("ffg_scale") != "ratio"
            for r in fl["series"]:
                rec = dict(zip(fields, r))
                if legacy:
                    for k in list(rec):
                        if k.startswith("ffg") and rec[k] is not None:
                            rec[k] = round(rec[k] / 100.0, 3)
                fs[rec.pop("hour")] = rec
        for r in d.get("series") or []:
            hr = {"episode_id": eid, "hour_utc": r[0], "rain_mean_mm": r[1],
                  "rain_max_mm": r[2], "ari1h_max_yr": r[3]}
            for k, v in (fs.get(r[0]) or {}).items():
                hr[k.replace("ffg1_", "ffg1h_").replace("ffg3_", "ffg3h_").replace("ffg6_", "ffg6h_")] = v
            hourly_rows.append(hr)

    # ---- gages ---------------------------------------------------------------------
    gages = pd.DataFrame(columns=["id", "name", "lat", "lon", "area_km2", "huc8", "n_events", "basin"])
    if GAGES_CSV.exists():
        gages = pd.read_csv(GAGES_CSV, dtype={"id": str, "huc8": str})
    gages = gages.rename(columns={"id": "site_no", "name": "station_name", "area_km2": "drainage_area_km2",
                                  "n_events": "n_runoff_events", "basin": "basin_polygon"})
    if len(gages):
        rc_g = sample_radar(gages)
        if rc_g is not None:
            gages["radar_coverage_at_gage"] = rc_g     # coverage of the basin above the gage
    gages_by_huc = gages.groupby("huc8").site_no.apply(list).to_dict() if len(gages) else {}

    # ---- episode_huc8: the modeling table ------------------------------------------
    print("building episode_huc8 ...")
    eh["episode_id"] = eh.episode_id.astype(int)
    eh = eh.merge(lookup.rename(columns={"name": "huc8_name", "area_km2": "huc8_area_km2"})
                  [["huc8", "huc8_name", "states", "huc8_area_km2"]], on="huc8", how="left")
    eh = eh.rename(columns={"inter_km2": "inside_counties_km2", "frac_huc8": "inside_counties_frac",
                            "frac_footprint": "share_of_footprint"})
    imp = ev.dropna(subset=["huc8"]).groupby(["episode_id", "huc8"]).agg(
        fatalities=("deaths", "sum"), injuries=("injuries", "sum"), damage_usd=("damage_usd", "sum"))
    eh = eh.merge(imp, left_on=["episode_id", "huc8"], right_index=True, how="left")
    for c in ["fatalities", "injuries", "damage_usd"]:
        eh[c] = eh[c].fillna(0) if ev.huc8.notna().any() else np.nan
    eh["n_reports"] = eh.n_events + eh.n_lsr
    eh["flooded"] = (eh.n_reports > 0).astype(int)
    if "radar_coverage_at_report" in ev.columns:
        # hydrologic coverage where the flooding was reported, averaged over the reports of the unit
        pts = pd.concat([ev[["episode_id", "huc8", "radar_coverage_at_report"]],
                         ls[["episode_id", "huc8", "radar_coverage_at_report"]]]).dropna()
        rc = pts.groupby(["episode_id", "huc8"]).radar_coverage_at_report.mean().round(2)
        eh = eh.merge(rc.rename("radar_coverage_at_reports_mean"),
                      left_on=["episode_id", "huc8"], right_index=True, how="left")
    eh["n_gages"] = eh.huc8.map(lambda h: len(gages_by_huc.get(h, [])))
    eh["gage_ids"] = eh.huc8.map(lambda h: ";".join(gages_by_huc.get(h, [])))
    stats = pd.DataFrame.from_dict(huc_rows, orient="index")
    stats.index = pd.MultiIndex.from_tuples(stats.index, names=["episode_id", "huc8"])
    eh = eh.merge(stats, left_on=["episode_id", "huc8"], right_index=True, how="left")

    # ---- episodes ------------------------------------------------------------------
    fp = pd.DataFrame.from_dict(fp_rows, orient="index")
    fp.index.name = "episode_id"
    ep = ep.merge(fp, left_on="episode_id", right_index=True, how="left")
    ep["duration_h"] = (pd.to_datetime(ep.end_utc, format="%Y-%m-%d %H") -
                        pd.to_datetime(ep.begin_utc, format="%Y-%m-%d %H")).dt.total_seconds() / 3600
    ep["huc8_list"] = ep.episode_id.map(eh.groupby("episode_id").huc8.apply(lambda s: ";".join(sorted(s))))
    ep["n_gages"] = ep.episode_id.map(eh.groupby("episode_id").n_gages.sum()).fillna(0).astype(int)
    ep["n_huc8"] = ep.n_huc8.fillna(ep.episode_id.map(eh.groupby("episode_id").size())).fillna(0).astype(int)
    narr = ep.pop("episode_narrative")
    ep["episode_narrative"] = narr
    front = ["episode_id", "begin_utc", "end_utc", "duration_h", "states", "n_events", "n_lsr",
             "fatalities", "injuries", "damage_usd", "n_counties", "county_fips", "n_huc8",
             "huc8_list", "footprint_km2", "n_gages", "min_lat", "min_lon", "max_lat", "max_lon"]
    ep = ep[front + [c for c in ep.columns if c not in front]]

    # ---- huc8 static ---------------------------------------------------------------
    huc = lookup.copy()
    huc["n_gages"] = huc.huc8.map(lambda h: len(gages_by_huc.get(h, [])))
    huc["gage_ids"] = huc.huc8.map(lambda h: ";".join(gages_by_huc.get(h, [])))
    per = eh.groupby("huc8").agg(n_episodes=("episode_id", "nunique"), n_events_total=("n_events", "sum"),
                                 n_lsr_total=("n_lsr", "sum"), n_flooded_episodes=("flooded", "sum"))
    huc = huc.merge(per, on="huc8", how="left")
    for c in per.columns:
        huc[c] = huc[c].fillna(0).astype(int)
    extra_cols = []
    if ATTRS.exists():
        extra = pd.read_csv(ATTRS, dtype={"huc8": str})
        extra["huc8"] = extra.huc8.str.zfill(8)
        extra_cols = [c for c in extra.columns if c != "huc8"]
        huc = huc.merge(extra, on="huc8", how="left")
        eh = eh.merge(extra, on="huc8", how="left")
        print(f"extra HUC8 attributes joined: {extra_cols}")

    # ---- episode_gages, hourly -----------------------------------------------------
    eg = eh[eh.n_gages > 0][["episode_id", "huc8", "gage_ids"]].copy()
    eg["site_no"] = eg.gage_ids.str.split(";")
    eg = eg.explode("site_no")[["episode_id", "huc8", "site_no"]].reset_index(drop=True)
    hourly = pd.DataFrame(hourly_rows).sort_values(["episode_id", "hour_utc"]).reset_index(drop=True)

    # ---- write ---------------------------------------------------------------------
    ev = ev.rename(columns={"deaths": "fatalities", "event_narrative": "narrative"})
    radar_pt = [c for c in ["radar_coverage_at_report"] if c in ev.columns]
    ev_front = ["episode_id", "event_id", "huc8", "state", "county_fips", "county_name",
                "begin_utc", "end_utc", "lat", "lon", "fatalities", "injuries", "damage_usd", "flood_cause"] + radar_pt + ["narrative"]
    ev = ev[ev_front]
    ls_front = ["episode_id", "huc8", "valid_utc", "lat", "lon", "typetext", "source", "wfo",
                "county_fips", "county", "city"] + radar_pt + ["narrative"]
    ls = ls[ls_front]
    eh_front = ["episode_id", "huc8", "huc8_name", "states", "huc8_area_km2", "inside_counties_km2",
                "inside_counties_frac", "share_of_footprint", "flooded", "n_reports", "n_events", "n_lsr",
                "fatalities", "injuries", "damage_usd", "n_gages", "gage_ids"]
    eh = eh[eh_front + [c for c in eh.columns if c not in eh_front]]

    write(ep, out, "episodes", sizes)
    write(eh, out, "episode_huc8", sizes)
    write(ev, out, "events", sizes)
    write(ls, out, "lsrs", sizes)
    write(hourly, out, "hourly", sizes)
    write(huc, out, "huc8", sizes)
    write(gages, out, "gages", sizes)
    write(eg, out, "episode_gages", sizes)
    write_docs(root, args.version, ep, eh, ev, ls, hourly, huc, gages, thresholds, extra_cols, sizes)

    for n, (rows, pq, csv) in sizes.items():
        print(f"  {n:14s} {rows:>8,} rows   parquet {pq:6.1f} MB   csv {csv:6.1f} MB")
    print(f"written to {root} (tables in {out.name}/, README.md and DATA_DICTIONARY.csv at the top)")
    if args.zip:
        z = shutil.make_archive(str(root), "zip", root_dir=root.parent, base_dir=root.name)
        print(f"zip: {z} ({Path(z).stat().st_size / 1e6:.1f} MB)")


# ----------------------------------------------------------------------------- docs
def write_docs(out, version, ep, eh, ev, ls, hourly, huc, gages, thresholds, extra_cols, sizes):
    """README.md and DATA_DICTIONARY.csv from the tables actually written."""
    n_flash = int(ep.get("flash_hours", pd.Series(dtype=float)).notna().sum()) if "flash_hours" in ep else 0
    n_legacy = int(ep.get("flash_legacy_scale", pd.Series(dtype=float)).fillna(0).sum()) if "flash_legacy_scale" in ep else 0
    wy0 = pd.to_datetime(ep.begin_utc, format="%Y-%m-%d %H").min()
    wy1 = pd.to_datetime(ep.begin_utc, format="%Y-%m-%d %H").max()
    pos = int(eh.flooded.sum())

    def desc(table, col):
        return DICT.get((table, col)) or DICT.get(("*", col)) or ""

    rows = []
    for name, df in [("episodes", ep), ("episode_huc8", eh), ("events", ev), ("lsrs", ls),
                     ("hourly", hourly), ("huc8", huc), ("gages", gages)]:
        for c in df.columns:
            rows.append({"table": name, "column": c, "dtype": str(df[c].dtype),
                         "description": desc(name, c)})
    def extra_desc(c):
        if c.startswith("radar_coverage_"):
            suffix = c[len("radar_coverage_"):]
            base = ("hydrologic radar coverage from data/radar_cov.tif (at each 0.01 degree pixel, the percent of "
                    "the drainage area upstream of that pixel covered by weather radar, 0 to 100), summarized over "
                    "the pixels of the watershed")
            if suffix == "mean":
                return base + ": area weighted mean, percent"
            if suffix == "min":
                return base + ": lowest pixel, percent"
            if suffix == "max":
                return base + ": highest pixel, percent"
            if suffix == "valid_frac":
                return base + ": share of the watershed inside the raster (0 = open water or outside the lower 48)"
            if suffix.startswith("frac_ge"):
                return base + f": share of the watershed area with a value at or above {suffix[7:]} percent"
        return "user supplied HUC8 attribute (data/episodes/huc8_attributes.csv)"

    for c in extra_cols:
        for r in rows:
            if r["column"] == c and not r["description"]:
                r["description"] = extra_desc(c)
    rows.append({"table": "episode_gages", "column": "episode_id, huc8, site_no", "dtype": "",
                 "description": "one row per RUNOFF gage inside a watershed of the episode footprint"})
    pd.DataFrame(rows).to_csv(out / "DATA_DICTIONARY.csv", index=False)

    readme = f"""# RUNOFF episodes dataset, version {version}

Flash flood storm episodes over the lower 48 states, {wy0:%B %Y} to {wy1:%B %Y}, with the
NOAA Storm Events reports and NWS Local Storm Reports that make them up, the HUC8
watersheds they touched, MRMS rainfall and FLASH return periods over those watersheds,
FLASH model output and flash flood guidance ratios, and the RUNOFF USGS gages inside them.
No stream gage is required anywhere: this is the storm-first view of the dataset, built for
flash flood guidance style detection models.

Built by engine/episodes/export_dataset.py in the RUNOFF repository
(https://github.com/NWC-CUAHSI-Summer-Institute/runoff). Website: episodes.html.

## Getting the data

This folder is the dataset. Clone the repository, or download the folder from GitHub, or take
the zip attached to the latest release. Inside:

    README.md, DATA_DICTIONARY.csv    this file and every column explained
    RUNOFF_storm_episodes_overview.pdf  15 slides: what is in the dataset, how it was built,
                                      the notebook, the radar coverage layer
    data/                             the eight tables, each as .parquet and .csv
    data/radar_cov.tif                the hydrologic radar coverage layer (GeoTIFF, 0.01 degree)
    notebooks/quickstart.ipynb        a worked tour: load, filter, join, model, map

Reading a table needs only pandas (`pd.read_parquet("data/episode_huc8.parquet")`; add pyarrow
for Parquet, or read the CSV with `dtype={{"huc8": str}}` so the leading zeros survive).

## Tables

| table | rows | one row is |
|---|---|---|
| episodes | {sizes['episodes'][0]:,} | one storm episode (NOAA EPISODE_ID) |
| episode_huc8 | {sizes['episode_huc8'][0]:,} | one HUC8 watershed inside one episode footprint: the modeling table |
| events | {sizes['events'][0]:,} | one Storm Events flash flood report |
| lsrs | {sizes['lsrs'][0]:,} | one Local Storm Report matched to an episode |
| hourly | {sizes['hourly'][0]:,} | one hour of one episode, footprint statistics |
| huc8 | {sizes['huc8'][0]:,} | one HUC8 watershed, static attributes |
| gages | {sizes['gages'][0]:,} | one RUNOFF USGS gage (drainage area under 1000 km2) |
| episode_gages | {sizes['episode_gages'][0]:,} | one gage inside one episode footprint |

Keys: `episode_id` (integer) joins everything to episodes; `huc8` (8 character string,
keep the leading zero) joins episode_huc8, events, lsrs, huc8 and gages; `site_no` joins
gages and episode_gages. Times are UTC strings, `YYYY-MM-DD HH` for episode hours and
`YYYY-MM-DD HH:MM` for reports. Every table exists as Parquet and as CSV; read the Parquet
when you can (types and narratives survive), the CSV when you need Excel.

## Definitions

- Episode: one NOAA Storm Events EPISODE_ID with at least one weather-caused flash flood
  report in a county of the lower 48 or DC (dam and levee failures excluded). The window is
  the earliest report start to the latest report end, in UTC, truncated to the hour.
- Footprint: every HUC8 (USGS WBD) that intersects the reporting counties by at least 1
  percent of its area or 10 km2, or that holds a report. `share_of_footprint` and
  `inside_counties_frac` tell how much of a watershed the counties cover.
- Rain: MRMS MultiSensor QPE 1 h Pass 2 (gauge corrected; Pass 1 or radar only where Pass 2
  is absent, listed in `qpe_products`). `max1h/3h/6h_peak_mm` is the highest rolling
  accumulation of that length reached by any 1 km cell in the unit, windows ending from the
  episode start hour to one hour after its end; `_mean_of_peaks_mm` is the area weighted mean
  of the cell peaks; `rain_ep_*` is the total over the same window; `rain_pre5h_*` the 5 hours
  before the start.
- Return period: FLASH QPE_ARI 1H/3H/6H, the average recurrence interval in years of the
  observed accumulation at each cell (NOAA Atlas 14 based, capped at 200), sampled at the
  top of every hour; `_peak_yr` is the cell maximum over the episode, `_cov_geTyr` the share
  of the unit area at or above T years at any hour (cos latitude weighted; cells without an
  ARI value count as not exceeding; `_valid_frac` says how much of the area had a value).
- FLASH models: `crest/sac/hp_peak_m3s_km2` is the episode peak unit streamflow of the
  CREST, SAC-SMA and hydrophobic (burn scar) models at any cell, m3/s/km2.
- Guidance: `ffg1h/3h/6h/max_peak_ratio` is the peak MRMS QPE to NWS flash flood guidance
  ratio (1.0 = rain reached the guidance); `_share_ge1` the share of the unit area whose
  peak ratio reached 1.0 during the episode. FLASH products are read at the top of each
  hour of the window only, no lookback.
- Label: `flooded` = 1 when at least one Storm Events report or Local Storm Report fell
  inside the watershed during the episode (`n_reports` = `n_events` + `n_lsr`). It is a
  report based label: {pos:,} of {sizes['episode_huc8'][0]:,} episode-watershed rows are
  positive, and the negatives are watersheds inside a storm footprint where nobody reported
  flooding, not watersheds with no storm. Reports happen where people and roads are.
- Coordinates in events and lsrs are NWS report locations, not storm centers. The footprint
  and the MRMS columns are what characterize the storm.

## Coverage of this build

- FLASH products present for {n_flash:,} of {sizes['episodes'][0]:,} episodes.
{"- " + str(n_legacy) + " FLASH records predate the percent fix: their QPE/FFG ratios were rescaled and their guidance coverage (_share_ge1) is empty until precompute_flash.py --force is rerun." if n_legacy else "- QPE/FFG values are ratios in every record."}
- Reports assigned to watersheds: {int(ev.huc8.notna().sum()):,} of {len(ev):,} events, {int(ls.huc8.notna().sum()):,} of {len(ls):,} LSRs.
- Extra HUC8 attributes joined: {", ".join(extra_cols) if extra_cols else "none (drop a huc8_attributes.csv next to the engine outputs, for example radar coverage, and re-export)"}.
{RADAR_README if any(c.startswith("radar_coverage_") for c in extra_cols) else ""}

## Sources

NOAA NCEI Storm Events Database; NWS Local Storm Reports via the Iowa Environmental
Mesonet; NOAA MRMS archive (IEM mirror, AWS noaa-mrms-pds fallback); NSSL FLASH products
(QPE ARI, CREST, SAC-SMA, hydrophobic unit streamflow, QPE/FFG); USGS Watershed Boundary
Dataset (HUC8); USGS GAGES-II, NWIS and NLDI for the gages.

See DATA_DICTIONARY.csv for every column and notebooks/quickstart.ipynb for the worked tour
(load, filter, join, summarize, map, build a training matrix, use the narratives).
"""
    (out / "README.md").write_text(readme, encoding="utf-8")


RADAR_README = """- Radar coverage: `data/radar_cov.tif` is a hydrologic coverage layer on the 0.01 degree
  MRMS grid. At every pixel the value is the percent of the drainage area upstream of that
  pixel that weather radar covers (0 to 100). Read it that way: the value at a gage is the
  coverage of the basin above the gage; the value at a flash flood report is the coverage of
  the basin draining to that place. The dataset carries it three ways: sampled at each gage
  (`radar_coverage_at_gage` in gages), at each report (`radar_coverage_at_report` in events
  and lsrs, averaged per watershed as `radar_coverage_at_reports_mean` in episode_huc8), and
  summarized over every pixel of a watershed (`radar_coverage_mean/min/max/valid_frac/frac_ge*`
  in huc8 and episode_huc8, from engine/episodes/huc8_radar_coverage.py). Over the lower 48,
  56 percent of land pixels read 100, 37 percent read 0, the rest are partial."""

DICT = {
    ("*", "episode_id"): "NOAA Storm Events EPISODE_ID",
    ("gages", "radar_coverage_at_gage"): "hydrologic radar coverage at the gage: percent of the basin above the gage covered by weather radar (data/radar_cov.tif)",
    ("events", "radar_coverage_at_report"): "hydrologic radar coverage at the report location: percent of the drainage area upstream of that point covered by weather radar",
    ("lsrs", "radar_coverage_at_report"): "hydrologic radar coverage at the report location: percent of the drainage area upstream of that point covered by weather radar",
    ("episode_huc8", "radar_coverage_at_reports_mean"): "mean of radar_coverage_at_report over the reports inside the watershed during the episode (empty when none)",
    ("*", "huc8"): "8 digit hydrologic unit code (USGS WBD), string with leading zero",
    ("episodes", "begin_utc"): "episode start, UTC, truncated to the hour",
    ("episodes", "end_utc"): "episode end, UTC, truncated to the hour",
    ("episodes", "duration_h"): "end minus begin, hours",
    ("episodes", "states"): "states with reports, comma separated",
    ("episodes", "n_events"): "Storm Events flash flood reports in the episode",
    ("episodes", "n_lsr"): "Local Storm Reports matched to the episode",
    ("episodes", "fatalities"): "direct plus indirect deaths, Storm Events",
    ("episodes", "injuries"): "direct plus indirect injuries, Storm Events",
    ("episodes", "damage_usd"): "property plus crop damage, US dollars, NWS estimates",
    ("episodes", "n_counties"): "counties with reports",
    ("episodes", "county_fips"): "county FIPS codes, semicolon separated",
    ("episodes", "n_huc8"): "watersheds in the footprint",
    ("episodes", "huc8_list"): "HUC8 codes of the footprint, semicolon separated",
    ("episodes", "footprint_km2"): "area of the footprint (union of the HUC8), km2",
    ("episodes", "n_gages"): "RUNOFF USGS gages inside the footprint",
    ("episodes", "min_lat"): "bounding box of the report coordinates",
    ("episodes", "min_lon"): "bounding box of the report coordinates",
    ("episodes", "max_lat"): "bounding box of the report coordinates",
    ("episodes", "max_lon"): "bounding box of the report coordinates",
    ("episodes", "mrms_hours"): "hourly QPE fields in the window (start to end + 1 h)",
    ("episodes", "qpe_products"): "QPE product used per hour count, pass2/pass1/radar (@aws = AWS fallback)",
    ("episodes", "qpe_missing_hours"): "window hours with no QPE in either archive",
    ("episodes", "ari_missing_duration_hours"): "ARI duration-hours missing from the archives",
    ("episodes", "artifact_cells"): "cells removed as radar artifacts (persistent hot cells)",
    ("episodes", "artifact_cells_in_footprint"): "of which inside the footprint",
    ("episodes", "episode_narrative"): "NWS narrative of the whole episode, Storm Events",
    ("episodes", "flash_hours"): "hours of FLASH products read (window only)",
    ("episodes", "flash_missing_product_hours"): "FLASH product-hours missing from the archives",
    ("episodes", "flash_legacy_scale"): "1 if the FLASH record predates the percent fix (ratios rescaled, coverage empty)",
    ("*", "rain_ep_mean_mm"): "episode rain, area weighted mean of the unit, mm",
    ("*", "rain_ep_max_mm"): "episode rain, wettest cell of the unit, mm",
    ("*", "rain_pre5h_mean_mm"): "rain in the 5 h before the start, unit mean, mm",
    ("*", "rain_pre5h_max_mm"): "rain in the 5 h before the start, wettest cell, mm",
    ("episode_huc8", "huc8_name"): "watershed name (WBD)",
    ("episode_huc8", "states"): "states the watershed spans (WBD)",
    ("episode_huc8", "huc8_area_km2"): "watershed area, km2",
    ("episode_huc8", "inside_counties_km2"): "area of the watershed inside the reporting counties, km2",
    ("episode_huc8", "inside_counties_frac"): "share of the watershed inside the reporting counties",
    ("episode_huc8", "share_of_footprint"): "share of the county union that this watershed covers",
    ("episode_huc8", "flooded"): "label: 1 if any Storm Events report or LSR fell inside the watershed during the episode",
    ("episode_huc8", "n_reports"): "n_events + n_lsr",
    ("episode_huc8", "n_events"): "Storm Events reports inside the watershed",
    ("episode_huc8", "n_lsr"): "Local Storm Reports inside the watershed",
    ("episode_huc8", "fatalities"): "deaths of the reports inside the watershed (empty if reports were not assigned)",
    ("episode_huc8", "injuries"): "injuries of the reports inside the watershed",
    ("episode_huc8", "damage_usd"): "damage of the reports inside the watershed, US dollars",
    ("episode_huc8", "n_gages"): "RUNOFF USGS gages in the watershed",
    ("episode_huc8", "gage_ids"): "their site numbers, semicolon separated",
    ("episode_huc8", "ffgmax_share_ge1"): "share of the watershed whose peak QPE/FFG ratio (max window) reached 1.0",
    ("events", "event_id"): "NOAA Storm Events EVENT_ID",
    ("events", "state"): "state abbreviation",
    ("events", "county_fips"): "county FIPS of the report",
    ("events", "county_name"): "county name",
    ("events", "begin_utc"): "report begin, UTC",
    ("events", "end_utc"): "report end, UTC",
    ("events", "lat"): "report location (NWS begin coordinates), not the storm center",
    ("events", "lon"): "report location (NWS begin coordinates), not the storm center",
    ("events", "fatalities"): "direct plus indirect deaths of this report",
    ("events", "injuries"): "direct plus indirect injuries of this report",
    ("events", "damage_usd"): "property plus crop damage of this report, US dollars",
    ("events", "flood_cause"): "NWS flood cause (heavy rain, tropical system, burn area, snowmelt, ice jam)",
    ("events", "narrative"): "NWS event narrative, Storm Events",
    ("lsrs", "valid_utc"): "report time, UTC",
    ("lsrs", "lat"): "report location",
    ("lsrs", "lon"): "report location",
    ("lsrs", "typetext"): "LSR type (FLASH FLOOD)",
    ("lsrs", "source"): "who reported (trained spotter, law enforcement, public, ...)",
    ("lsrs", "wfo"): "issuing NWS office",
    ("lsrs", "county_fips"): "county FIPS from the UGC code, empty if the report had none",
    ("lsrs", "county"): "county name as reported",
    ("lsrs", "city"): "place text as reported",
    ("lsrs", "narrative"): "LSR remark",
    ("hourly", "hour_utc"): "valid hour, UTC; the QPE hour covers (h-1, h]",
    ("hourly", "rain_mean_mm"): "footprint mean of the hourly QPE, mm",
    ("hourly", "rain_max_mm"): "footprint maximum of the hourly QPE, mm",
    ("hourly", "ari1h_max_yr"): "footprint maximum of the 1 h ARI at that hour, years",
    ("hourly", "crest_max"): "footprint maximum CREST unit streamflow, m3/s/km2",
    ("hourly", "crest_mean"): "footprint mean CREST unit streamflow, m3/s/km2",
    ("hourly", "sac_max"): "footprint maximum SAC-SMA unit streamflow",
    ("hourly", "sac_mean"): "footprint mean SAC-SMA unit streamflow",
    ("hourly", "hp_max"): "footprint maximum hydrophobic unit streamflow",
    ("hourly", "hp_mean"): "footprint mean hydrophobic unit streamflow",
    ("hourly", "ffg1h_max"): "footprint maximum QPE/FFG ratio, 1 h",
    ("hourly", "ffg1h_mean"): "footprint mean QPE/FFG ratio, 1 h",
    ("hourly", "ffg3h_max"): "footprint maximum QPE/FFG ratio, 3 h",
    ("hourly", "ffg3h_mean"): "footprint mean QPE/FFG ratio, 3 h",
    ("hourly", "ffg6h_max"): "footprint maximum QPE/FFG ratio, 6 h",
    ("hourly", "ffg6h_mean"): "footprint mean QPE/FFG ratio, 6 h",
    ("hourly", "ffgmax_max"): "footprint maximum QPE/FFG ratio, max window",
    ("hourly", "ffgmax_mean"): "footprint mean QPE/FFG ratio, max window",
    ("huc8", "name"): "watershed name (WBD)",
    ("huc8", "states"): "states the watershed spans",
    ("huc8", "area_km2"): "watershed area, km2",
    ("huc8", "min_lon"): "bounding box", ("huc8", "min_lat"): "bounding box",
    ("huc8", "max_lon"): "bounding box", ("huc8", "max_lat"): "bounding box",
    ("huc8", "n_gages"): "RUNOFF USGS gages in the watershed",
    ("huc8", "gage_ids"): "their site numbers, semicolon separated",
    ("huc8", "n_episodes"): "episodes whose footprint contains the watershed",
    ("huc8", "n_events_total"): "Storm Events reports inside the watershed, all episodes",
    ("huc8", "n_lsr_total"): "Local Storm Reports inside the watershed, all episodes",
    ("huc8", "n_flooded_episodes"): "episodes with at least one report inside the watershed",
    ("gages", "site_no"): "USGS site number, string with leading zero",
    ("gages", "station_name"): "NWIS station name",
    ("gages", "lat"): "gage location", ("gages", "lon"): "gage location",
    ("gages", "drainage_area_km2"): "drainage area, km2 (GAGES-II)",
    ("gages", "huc8"): "HUC8 of the gage",
    ("gages", "n_runoff_events"): "cataloged events linked to the gage in the RUNOFF gaged path (WY 2021-2025)",
    ("gages", "basin_polygon"): "1 if the NLDI drainage basin polygon is available (assets/data/basin/<site_no>.json)",
}
for _d in DURS:
    DICT[("*", f"max{_d}h_peak_mm")] = f"highest rolling {_d} h accumulation reached by any cell of the unit, mm"
    DICT[("*", f"max{_d}h_mean_of_peaks_mm")] = f"area weighted mean of the cell peak rolling {_d} h accumulations, mm"
    DICT[("*", f"max{_d}h_peak_time_utc")] = f"hour (UTC, window end) of the unit peak {_d} h accumulation"
    DICT[("*", f"ari{_d}h_peak_yr")] = f"maximum FLASH {_d} h ARI at any cell during the episode, years (capped at 200)"
    DICT[("*", f"ari{_d}h_valid_frac")] = f"share of the unit area with a valid {_d} h ARI value"
    for _t in [1, 2, 5, 10, 20, 25, 50, 100, 200]:
        DICT[("*", f"ari{_d}h_cov_ge{_t}yr")] = f"share of the unit area whose {_d} h ARI reached {_t} years at any hour"
for _m, _n in [("crest", "CREST"), ("sac", "SAC-SMA"), ("hp", "hydrophobic")]:
    DICT[("*", f"{_m}_peak_m3s_km2")] = f"episode peak {_n} unit streamflow at any cell, m3/s/km2"
    DICT[("*", f"{_m}_mean_of_peaks_m3s_km2")] = f"area weighted mean of the cell peak {_n} unit streamflow"
    DICT[("*", f"{_m}_peak_time_utc")] = f"hour of the {_n} peak, UTC"
for _f, _w in [("ffg1h", "1 h"), ("ffg3h", "3 h"), ("ffg6h", "6 h"), ("ffgmax", "max window")]:
    DICT[("*", f"{_f}_peak_ratio")] = f"peak QPE/FFG ratio ({_w}) at any cell during the episode; 1.0 = rain reached the guidance"
    DICT[("*", f"{_f}_mean_of_peaks_ratio")] = f"area weighted mean of the cell peak QPE/FFG ratios ({_w})"
    DICT[("*", f"{_f}_peak_time_utc")] = f"hour of the peak QPE/FFG ratio ({_w}), UTC"
    DICT[("*", f"{_f}_share_ge1")] = f"share of the unit area whose peak QPE/FFG ratio ({_w}) reached 1.0"


if __name__ == "__main__":
    main()
