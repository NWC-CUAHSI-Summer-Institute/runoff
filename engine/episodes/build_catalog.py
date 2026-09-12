#!/usr/bin/env python3
"""
build_catalog.py - the RUNOFF episode catalog for general flash flood modeling.

Groups every weather-caused flash flood event in the NOAA Storm Events
Database into its storm EPISODE, attaches NWS Local Storm Reports (LSRs) to
each episode, merges any MRMS rainfall stats already computed by
engine/episodes/mrms_stats.py, and writes both the analysis tables (CSV) and
the site payloads that light up episodes.html.

This is the path for users who do NOT need a stream gage in the basin: they
select episodes by rainfall and impact criteria, then take the events, LSRs,
and MRMS tables into their own modeling.

Inputs
  --stormevents DIR   folder of StormEvents_details-ftp_v1.0_dYYYY*.csv.gz
                      from https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/
                      (calendar years covering the water year range)
  --lsr-dir DIR       output of engine/lsr/fetch_lsrs.py (optional; without it
                      the catalog is built with LSR counts marked "not fetched")
  --wy FIRST LAST     water year window, default 2021 2025 (dual-pol MRMS era,
                      and the era the FLASH ARI archive covers)

Outputs
  data/episodes/episodes.csv            one row per episode
  data/episodes/episode_events.csv      Storm Events rows with episode_id
  data/episodes/episode_lsrs.csv        LSR rows matched to episodes
  assets/data/episodes.js               site payload (var EPCAT)
  assets/data/episode_points.js         site payload (var EPPTS)

Episode/LSR matching: an LSR belongs to an episode when its type is in
--lsr-types, its time falls in [episode start - 3 h, episode end + 6 h], and
either its UGC county code is one of the episode's counties or, when the UGC
is absent, its point falls in the episode bounding box padded 0.25 degrees.

Rerun this script after any mrms_stats.py run: it re-reads every
data/episodes/<id>/mrms_summary.json and refreshes the site payload, so the
rainfall filters on episodes.html pick up the new numbers.

Note for Flash Flood Guidance style users: Storm Events coordinates are the
NWS report locations, not storm centers. The episode footprint (counties,
bbox) and the MRMS grids are what characterize the storm itself.
"""
from __future__ import annotations

import argparse
import glob
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parents[2]          # repository root

CONUS_FIPS = set(range(1, 57)) - {2, 15}            # 48 states + DC
FIPS2AB = {1:"AL",4:"AZ",5:"AR",6:"CA",8:"CO",9:"CT",10:"DE",11:"DC",12:"FL",
    13:"GA",16:"ID",17:"IL",18:"IN",19:"IA",20:"KS",21:"KY",22:"LA",23:"ME",
    24:"MD",25:"MA",26:"MI",27:"MN",28:"MS",29:"MO",30:"MT",31:"NE",32:"NV",
    33:"NH",34:"NJ",35:"NM",36:"NY",37:"NC",38:"ND",39:"OH",40:"OK",41:"OR",
    42:"PA",44:"RI",45:"SC",46:"SD",47:"TN",48:"TX",49:"UT",50:"VT",51:"VA",
    53:"WA",54:"WV",55:"WI",56:"WY"}
AB2FIPS = {v: k for k, v in FIPS2AB.items()}

USE = ["EPISODE_ID","EVENT_ID","STATE","STATE_FIPS","EVENT_TYPE","CZ_TYPE",
       "CZ_FIPS","CZ_NAME","CZ_TIMEZONE","BEGIN_YEARMONTH","BEGIN_DAY",
       "BEGIN_TIME","END_YEARMONTH","END_DAY","END_TIME","DEATHS_DIRECT",
       "DEATHS_INDIRECT","INJURIES_DIRECT","INJURIES_INDIRECT",
       "DAMAGE_PROPERTY","DAMAGE_CROPS","FLOOD_CAUSE",
       "BEGIN_LAT","BEGIN_LON","END_LAT","END_LON"]


def dollars(s) -> float:
    if pd.isna(s) or s == "":
        return 0.0
    m = re.match(r"^([0-9.]+)\s*([KMBkmb]?)$", str(s).strip())
    if not m:
        return 0.0
    return float(m.group(1)) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2).upper()]


def tz_offset_hours(tz) -> int:
    """'CST-6' -> -6, 'EST-5' -> -5. UTC = local - offset."""
    m = re.search(r"([+-]?\d+)$", str(tz))
    return int(m.group(1)) if m else 0


def compose_dt(df, ym, day, hhmm):
    """StormEvents stores YYYYMM / day / HHMM as integers; compose local time."""
    y = (df[ym] // 100).astype(int)
    mo = (df[ym] % 100).astype(int)
    hh = (df[hhmm] // 100).astype(int).clip(0, 23)
    mi = (df[hhmm] % 100).astype(int).clip(0, 59)
    return pd.to_datetime(dict(year=y, month=mo, day=df[day].astype(int),
                               hour=hh, minute=mi), errors="coerce")


def load_events(stormevents_dir: Path, wy0: int, wy1: int) -> pd.DataFrame:
    files = sorted(glob.glob(str(stormevents_dir / "StormEvents_details*d20*.csv.gz")))
    files += sorted(glob.glob(str(stormevents_dir / "StormEvents_details*d20*.csv")))
    years = set(range(wy0 - 1, wy1 + 1))            # calendar years touching the WY window
    frames = []
    for f in files:
        m = re.search(r"_d(\d{4})", f)
        if not m or int(m.group(1)) not in years:
            continue
        df = pd.read_csv(f, usecols=USE, dtype={"CZ_FIPS": "Int64", "STATE_FIPS": "Int64",
                                                "EPISODE_ID": "Int64"})
        frames.append(df[df.EVENT_TYPE == "Flash Flood"])
    if not frames:
        raise SystemExit(f"no StormEvents details files for {sorted(years)} under {stormevents_dir}")
    ff = pd.concat(frames, ignore_index=True)

    ff = ff[(ff.CZ_TYPE == "C") & (ff.STATE_FIPS.isin(CONUS_FIPS))]
    cause = ff.FLOOD_CAUSE.fillna("Not specified")
    ff = ff[~cause.str.contains("dam|levee", case=False)].copy()
    ff["cause"] = cause[~cause.str.contains("dam|levee", case=False)].values
    ff = ff[ff.EPISODE_ID.notna()]

    off = ff.CZ_TIMEZONE.map(tz_offset_hours)
    begin_local = compose_dt(ff, "BEGIN_YEARMONTH", "BEGIN_DAY", "BEGIN_TIME")
    end_local = compose_dt(ff, "END_YEARMONTH", "END_DAY", "END_TIME")
    ff["begin_utc"] = begin_local - pd.to_timedelta(off.values, unit="h")
    ff["end_utc"] = end_local - pd.to_timedelta(off.values, unit="h")
    bad_end = ff.end_utc.isna() | (ff.end_utc < ff.begin_utc)
    ff.loc[bad_end, "end_utc"] = ff.loc[bad_end, "begin_utc"]

    w0 = pd.Timestamp(f"{wy0-1}-10-01")
    w1 = pd.Timestamp(f"{wy1}-10-01")
    ff = ff[(ff.begin_utc >= w0) & (ff.begin_utc < w1)].copy()

    ff["deaths"] = ff.DEATHS_DIRECT.fillna(0) + ff.DEATHS_INDIRECT.fillna(0)
    ff["inj"] = ff.INJURIES_DIRECT.fillna(0) + ff.INJURIES_INDIRECT.fillna(0)
    ff["dmg"] = ff.DAMAGE_PROPERTY.map(dollars) + ff.DAMAGE_CROPS.map(dollars)
    ff["fips"] = (ff.STATE_FIPS.astype(int) * 1000 + ff.CZ_FIPS.astype(int)) \
        .astype(str).str.zfill(5)
    ff["stab"] = ff.STATE_FIPS.astype(int).map(FIPS2AB)
    print(f"flash flood events kept: {len(ff):,} in "
          f"{ff.EPISODE_ID.nunique():,} episodes (WY {wy0}-{wy1}, CONUS, weather-caused)")
    return ff


def load_lsrs(lsr_dir: Path, types: set[str]) -> pd.DataFrame | None:
    files = sorted(glob.glob(str(lsr_dir / "lsr_*.csv")))
    if not files:
        return None
    frames = []
    for f in files:
        try:
            df = pd.read_csv(f, dtype=str, quoting=3, on_bad_lines="skip")
        except pd.errors.EmptyDataError:
            continue
        if not len(df) or "VALID" not in df.columns:
            continue
        frames.append(df)
    if not frames:
        return None
    lsr = pd.concat(frames, ignore_index=True)
    lsr["TYPETEXT"] = lsr.TYPETEXT.fillna("").str.strip().str.upper()
    lsr = lsr[lsr.TYPETEXT.isin(types)].copy()
    lsr["valid"] = pd.to_datetime(lsr.VALID.astype(str).str.slice(0, 12),
                                  format="%Y%m%d%H%M", errors="coerce")
    lsr["lat"] = pd.to_numeric(lsr.LAT, errors="coerce")
    lsr["lon"] = pd.to_numeric(lsr.LON, errors="coerce")
    lsr = lsr.dropna(subset=["valid", "lat", "lon"])
    lsr = lsr.drop_duplicates(subset=["VALID", "lat", "lon", "TYPETEXT", "WFO"])

    def ugc_fips(row):
        u = str(row.UGC) if pd.notna(row.UGC) else ""
        st = str(row.STATE).strip().upper() if pd.notna(row.STATE) else ""
        if len(u) == 6 and u[2] == "C" and u[3:].isdigit() and st in AB2FIPS:
            return f"{AB2FIPS[st]:02d}{u[3:]}"
        return ""
    lsr["fips"] = lsr.apply(ugc_fips, axis=1)
    lsr = lsr.sort_values("valid").reset_index(drop=True)
    print(f"LSRs loaded: {len(lsr):,} of types {sorted(types)}")
    return lsr


def match_lsrs(ep, lsr, vvals, t_pre="3h", t_post="6h", pad=0.25):
    """Row indices of LSRs belonging to one episode (county first, bbox fallback).
    vvals is lsr['valid'].values, materialized once by the caller."""
    w0 = ep["t0dt"] - pd.Timedelta(t_pre)
    w1 = ep["t1dt"] + pd.Timedelta(t_post)
    i0, i1 = np.searchsorted(vvals, np.datetime64(w0)), np.searchsorted(vvals, np.datetime64(w1), "right")
    if i1 <= i0:
        return []
    win = lsr.iloc[i0:i1]
    infips = win.fips.isin(ep["fipsset"])
    nofips = win.fips == ""
    inbox = (win.lat >= ep["bbox"][0] - pad) & (win.lat <= ep["bbox"][2] + pad) & \
            (win.lon >= ep["bbox"][1] - pad) & (win.lon <= ep["bbox"][3] + pad)
    return win.index[infips | (nofips & inbox)].tolist()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stormevents", type=Path, required=True,
                    help="folder holding StormEvents_details*.csv.gz")
    ap.add_argument("--lsr-dir", type=Path, default=HERE / "data" / "lsr")
    ap.add_argument("--lsr-types", nargs="*", default=["FLASH FLOOD"],
                    help='LSR TYPETEXT values to attach (default "FLASH FLOOD"; '
                         'add FLOOD, DEBRIS FLOW, HEAVY RAIN as needed)')
    ap.add_argument("--wy", nargs=2, type=int, default=[2021, 2025])
    ap.add_argument("--out", type=Path, default=HERE / "data" / "episodes")
    ap.add_argument("--site-data", type=Path, default=HERE / "assets" / "data")
    args = ap.parse_args()

    ff = load_events(args.stormevents, args.wy[0], args.wy[1])
    lsr = load_lsrs(args.lsr_dir, {t.upper() for t in args.lsr_types}) \
        if args.lsr_dir and args.lsr_dir.exists() else None
    if lsr is None:
        print("LSR archive not found; catalog will mark LSRs as not fetched "
              "(run engine/lsr/fetch_lsrs.py first to include them)")

    # ---------------- per-episode assembly ----------------
    vvals = lsr["valid"].values if lsr is not None else None
    episodes, points, lsr_rows_out = [], {}, []
    for epid, g in ff.groupby("EPISODE_ID"):
        epid = int(epid)
        lats = pd.concat([g.BEGIN_LAT, g.END_LAT]).dropna()
        lons = pd.concat([g.BEGIN_LON, g.END_LON]).dropna()
        if not len(lats):
            continue
        bbox = [float(np.floor(lats.min() * 100) / 100), float(np.floor(lons.min() * 100) / 100),
                float(np.ceil(lats.max() * 100) / 100), float(np.ceil(lons.max() * 100) / 100)]
        if bbox[2] - bbox[0] < 0.1:
            bbox[0] -= 0.05; bbox[2] += 0.05
        if bbox[3] - bbox[1] < 0.1:
            bbox[1] -= 0.05; bbox[3] += 0.05
        ep = {
            "id": epid,
            "t0dt": g.begin_utc.min(), "t1dt": g.end_utc.max(),
            "states": ",".join(sorted(g.stab.dropna().unique())),
            "nev": int(len(g)),
            "deaths": int(g.deaths.sum()), "inj": int(g.inj.sum()),
            "dmg": int(round(g.dmg.sum())),
            "fips": sorted(g.fips.unique()), "bbox": bbox,
        }
        ep["fipsset"] = set(ep["fips"])
        ep["t0"] = ep["t0dt"].strftime("%Y-%m-%d %H")
        ep["t1"] = ep["t1dt"].strftime("%Y-%m-%d %H")

        ev_pts = []
        for _, r in g.iterrows():
            if pd.isna(r.BEGIN_LAT) or pd.isna(r.BEGIN_LON):
                continue
            ev_pts.append([round(float(r.BEGIN_LAT), 3), round(float(r.BEGIN_LON), 3),
                           r.begin_utc.strftime("%Y-%m-%d %H:%M"),
                           int(r.deaths), int(round(r.dmg))])
        lsr_pts = []
        if lsr is not None:
            idx = match_lsrs(ep, lsr, vvals)
            ep["nlsr"] = len(idx)
            for i in idx:
                rr = lsr.loc[i]
                lsr_pts.append([round(float(rr.lat), 3), round(float(rr.lon), 3),
                                rr.valid.strftime("%Y-%m-%d %H:%M"),
                                rr.TYPETEXT, str(rr.SOURCE) if pd.notna(rr.SOURCE) else ""])
                lsr_rows_out.append({
                    "episode_id": epid, "valid_utc": rr.valid.strftime("%Y-%m-%d %H:%M"),
                    "lat": rr.lat, "lon": rr.lon, "typetext": rr.TYPETEXT,
                    "source": rr.SOURCE, "wfo": rr.WFO, "county_fips": rr.fips,
                    "city": rr.CITY, "remark": rr.REMARK})
        else:
            ep["nlsr"] = -1

        # MRMS stats, if mrms_stats.py already ran for this episode
        ms = args.out / str(epid) / "mrms_summary.json"
        ep.update({"acc6": None, "acc12": None, "acc24": None, "acc72": None,
                   "ari": None, "aridur": None, "product": None})
        if ms.exists():
            s = json.loads(ms.read_text(encoding="utf-8"))
            acc = s.get("max_rolling_mm", {})
            ep["acc6"], ep["acc12"] = acc.get("h6"), acc.get("h12")
            ep["acc24"], ep["acc72"] = acc.get("h24"), acc.get("h72")
            mari = s.get("max_ari") or {}
            ep["ari"], ep["aridur"] = mari.get("years"), mari.get("duration")
            ep["product"] = s.get("product")

        episodes.append(ep)
        points[str(epid)] = {"ev": ev_pts, "lsr": lsr_pts}

    episodes.sort(key=lambda e: e["t0"])
    n_mrms = sum(1 for e in episodes if e["acc24"] is not None)
    products = [e["product"] for e in episodes if e["product"]]
    product = max(set(products), key=products.count) if products else None
    print(f"episodes assembled: {len(episodes):,} | with MRMS stats: {n_mrms:,}")

    # ---------------- CSV tables ----------------
    args.out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{
        "episode_id": e["id"], "begin_utc": e["t0"], "end_utc": e["t1"],
        "states": e["states"], "n_events": e["nev"],
        "n_lsr": (e["nlsr"] if e["nlsr"] >= 0 else ""),
        "fatalities": e["deaths"], "injuries": e["inj"], "damage_usd": e["dmg"],
        "n_counties": len(e["fips"]), "county_fips": ";".join(e["fips"]),
        "min_lat": e["bbox"][0], "min_lon": e["bbox"][1],
        "max_lat": e["bbox"][2], "max_lon": e["bbox"][3],
        "max_roll_6h_mm": e["acc6"], "max_roll_12h_mm": e["acc12"],
        "max_roll_24h_mm": e["acc24"], "max_roll_72h_mm": e["acc72"],
        "max_ari_years": e["ari"], "max_ari_duration": e["aridur"],
        "mrms_product": e["product"],
    } for e in episodes]).to_csv(args.out / "episodes.csv", index=False)

    ff_out = ff[["EPISODE_ID","EVENT_ID","stab","fips","begin_utc","end_utc",
                 "BEGIN_LAT","BEGIN_LON","deaths","inj","dmg","cause","CZ_NAME"]].copy()
    ff_out.columns = ["episode_id","event_id","state","county_fips","begin_utc",
                      "end_utc","lat","lon","deaths","injuries","damage_usd",
                      "flood_cause","county_name"]
    ff_out.to_csv(args.out / "episode_events.csv", index=False)
    if lsr_rows_out:
        pd.DataFrame(lsr_rows_out).to_csv(args.out / "episode_lsrs.csv", index=False)

    # ---------------- site payloads ----------------
    fields = ["id","t0","t1","states","nev","nlsr","deaths","inj","dmg",
              "fips","bbox","acc6","acc12","acc24","acc72","ari","aridur"]
    rows = [[e[f] for f in fields] for e in episodes]
    epcat = {"built": pd.Timestamp.now("UTC").strftime("%Y-%m-%d"),
             "wy": args.wy, "product": product, "fields": fields, "rows": rows}
    args.site_data.mkdir(parents=True, exist_ok=True)
    (args.site_data / "episodes.js").write_text(
        "var EPCAT=" + json.dumps(epcat, separators=(",", ":")) + ";",
        encoding="utf-8")
    (args.site_data / "episode_points.js").write_text(
        "var EPPTS=" + json.dumps(points, separators=(",", ":")) + ";",
        encoding="utf-8")
    sz1 = (args.site_data / "episodes.js").stat().st_size / 1e6
    sz2 = (args.site_data / "episode_points.js").stat().st_size / 1e6
    print(f"site payloads written: episodes.js {sz1:.2f} MB, episode_points.js {sz2:.2f} MB")
    print("open episodes.html to browse; run engine/episodes/mrms_stats.py to add "
          "rainfall stats, then rerun this script to merge them into the payload")


if __name__ == "__main__":
    main()
