#!/usr/bin/env python3
"""
prep_events.py - county and HUC8 flash flood impact records for records.html.

Aggregates the NOAA Storm Events Database (2015-2025 by default) into the two
payloads the Flash Flood Records page reads: county_stats.js (CSTATS) and
huc8_stats.js (HSTATS). Each unit carries, per year, [episodes, events,
fatalities, injuries, property damage USD, crop damage USD] plus a flood cause
histogram. Weather-caused flash floods only: dam and levee failure reports are
removed. Lower 48 states and DC.

Inputs
  --stormevents DIR   StormEvents_details-ftp_v1.0_dYYYY*.csv.gz files from
                      https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/
  --huc8-shp PATH     USGS WBD HUC8 shapefile (full resolution) for the
                      point in polygon assignment of events to watersheds

    python engine/records/prep_events.py --stormevents data/stormevents --huc8-shp data/geo/HUC8_US.shp
"""
from __future__ import annotations

import argparse
import glob
import json
import re
from pathlib import Path

import geopandas as gpd
import pandas as pd

HERE = Path(__file__).resolve().parents[2]
CONUS_FIPS = set(range(1, 57)) - {2, 15}   # 48 states + DC; territories are > 56
USE = ["EPISODE_ID", "EVENT_ID", "STATE", "STATE_FIPS", "YEAR", "EVENT_TYPE", "CZ_TYPE",
       "CZ_FIPS", "CZ_NAME", "BEGIN_DATE_TIME", "DEATHS_DIRECT", "DEATHS_INDIRECT",
       "INJURIES_DIRECT", "INJURIES_INDIRECT", "DAMAGE_PROPERTY", "DAMAGE_CROPS",
       "FLOOD_CAUSE", "BEGIN_LAT", "BEGIN_LON"]


def dollars(s) -> float:
    """Parse a StormEvents damage string such as 10.00K or 2.5M into dollars."""
    if pd.isna(s) or s == "":
        return 0.0
    m = re.match(r"^([0-9.]+)\s*([KMBkmb]?)$", str(s).strip())
    if not m:
        return 0.0
    return float(m.group(1)) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2).upper()]


def load_events(stormevents: Path, years: list[int]) -> pd.DataFrame:
    """Flash flood events, county reports, CONUS, weather-caused, with damage parsed."""
    frames = []
    for f in sorted(glob.glob(str(stormevents / "StormEvents_details-ftp_v1.0_d20*.csv.gz"))):
        m = re.search(r"_d(\d{4})", f)
        if not m or int(m.group(1)) not in years:
            continue
        df = pd.read_csv(f, usecols=USE, dtype={"CZ_FIPS": "Int64", "STATE_FIPS": "Int64"})
        frames.append(df[df.EVENT_TYPE == "Flash Flood"])
    if not frames:
        raise SystemExit(f"no StormEvents details files for {years[0]}-{years[-1]} under {stormevents}")
    ff = pd.concat(frames, ignore_index=True)
    print("flash flood rows:", len(ff))

    ff = ff[(ff.CZ_TYPE == "C") & (ff.STATE_FIPS.isin(CONUS_FIPS))]
    cause = ff.FLOOD_CAUSE.fillna("Not specified")
    drop = cause.str.contains("dam|levee", case=False)
    print("dropped dam/levee:", int(drop.sum()))
    ff = ff[~drop].copy()
    ff["cause"] = cause[~drop].values
    ff["deaths"] = ff.DEATHS_DIRECT.fillna(0) + ff.DEATHS_INDIRECT.fillna(0)
    ff["inj"] = ff.INJURIES_DIRECT.fillna(0) + ff.INJURIES_INDIRECT.fillna(0)
    ff["dp"] = ff.DAMAGE_PROPERTY.map(dollars)
    ff["dc"] = ff.DAMAGE_CROPS.map(dollars)
    ff["geoid"] = (ff.STATE_FIPS.astype(int) * 1000 + ff.CZ_FIPS.astype(int)) \
        .astype(str).str.zfill(5)
    print("after filters:", len(ff), "| episodes:", ff.EPISODE_ID.nunique())
    return ff


def attach_huc8(ff: pd.DataFrame, huc8_shp: Path) -> pd.DataFrame:
    """Assign each event to the HUC8 containing its begin coordinates."""
    huc = gpd.read_file(huc8_shp)[["HUC8", "NAME", "geometry"]]
    huc["HUC8"] = huc.HUC8.astype(str).str.zfill(8)
    pts = gpd.GeoDataFrame(ff, geometry=gpd.points_from_xy(ff.BEGIN_LON, ff.BEGIN_LAT), crs=4326)
    j = gpd.sjoin(pts, huc.set_crs(4326, allow_override=True), how="left", predicate="within")
    j = j[~j.index.duplicated(keep="first")]
    ff["huc8"] = j["HUC8"]
    ff["huc8_name"] = j["NAME"]
    print("no-huc8 (offshore or bad coords):", int(ff.huc8.isna().sum()))
    return ff


def build(ff: pd.DataFrame, stats_key: str, name_of: dict) -> dict:
    """Per unit: name, per-year [ep, ev, deaths, inj, prop, crop], cause histogram."""
    out = {}
    g = ff.dropna(subset=[stats_key]).groupby([stats_key, "YEAR"])
    agg = g.agg(ep=("EPISODE_ID", "nunique"), ev=("EVENT_ID", "count"),
                d=("deaths", "sum"), i=("inj", "sum"), dp=("dp", "sum"), dc=("dc", "sum"))
    cc = ff.dropna(subset=[stats_key]).groupby([stats_key, "cause"]).size()
    for (uid, yr), r in agg.iterrows():
        u = out.setdefault(str(uid), {"n": name_of.get(str(uid), str(uid)), "y": {}, "c": {}})
        u["y"][int(yr)] = [int(r.ep), int(r.ev), int(r.d), int(r.i),
                           int(round(r.dp)), int(round(r.dc))]
    for (uid, cz), n in cc.items():
        out[str(uid)]["c"][str(cz)] = int(n)
    return out


def main() -> None:
    """Aggregate StormEvents into the records page payloads."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stormevents", type=Path, required=True)
    ap.add_argument("--huc8-shp", type=Path, required=True)
    ap.add_argument("--years", nargs=2, type=int, default=[2015, 2025])
    ap.add_argument("--out", type=Path, default=HERE / "assets" / "data")
    args = ap.parse_args()

    years = list(range(args.years[0], args.years[1] + 1))
    ff = load_events(args.stormevents, years)
    ff = attach_huc8(ff, args.huc8_shp)

    cnames = ff.groupby("geoid").agg(nm=("CZ_NAME", "first"), st=("STATE", "first"))
    county_name = {i: f"{r.nm.title()}, {r.st.title()}" for i, r in cnames.iterrows()}
    huc_name = ff.dropna(subset=["huc8"]).groupby("huc8")["huc8_name"].first().to_dict()

    cs = build(ff, "geoid", county_name)
    hs = build(ff, "huc8", huc_name)
    print("county units:", len(cs), "| huc8 units:", len(hs))

    args.out.mkdir(parents=True, exist_ok=True)
    p_c = args.out / "county_stats.js"
    p_h = args.out / "huc8_stats.js"
    p_c.write_text("var YEARS=" + json.dumps(years) + ";\nvar CSTATS="
                   + json.dumps(cs, separators=(",", ":")) + ";", encoding="utf-8")
    p_h.write_text("var HSTATS=" + json.dumps(hs, separators=(",", ":")) + ";", encoding="utf-8")

    print(f"TOTALS: events {len(ff)} | episodes {ff.EPISODE_ID.nunique()} | "
          f"deaths {int(ff.deaths.sum())} | injuries {int(ff.inj.sum())} | "
          f"property ${ff.dp.sum() / 1e9:.2f}B | crops ${ff.dc.sum() / 1e6:.0f}M")
    print("cause totals:", ff.cause.value_counts().to_dict())
    print(f"sizes: county_stats {p_c.stat().st_size / 1e6:.2f} MB, "
          f"huc8_stats {p_h.stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
