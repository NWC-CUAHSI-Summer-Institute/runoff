#!/usr/bin/env python3
"""
mrms_stats.py - MRMS rainfall over each flash flood episode footprint.

For every selected episode in data/episodes/episodes.csv (built by
build_catalog.py) this computes, from the MRMS archive on AWS
(noaa-mrms-pds, registry: https://registry.opendata.aws/noaa-mrms-pds/):

  1. Rolling accumulations from hourly QPE grids cropped to the episode
     bounding box: the WORST 6, 12, 24, and 72 hour rainfall total at any
     grid cell, over windows ending inside the episode. The 72 hour lookback
     starts before the episode begins, so antecedent rainfall counts.
  2. The max return period: the most severe FLASH QPE ARI (Average
     Recurrence Interval) value in the footprint at any hour of the episode,
     across the 30 min / 1 h / 3 h / 6 h / 24 h ARI products. ARI compares
     MRMS accumulations against NOAA Atlas 14 frequency tables and is
     expressed in years ("this was a 100 year rainfall here"). Products:
     CONUS/FLASH_QPE_ARI30M_00.00 ... FLASH_QPE_ARI24H_00.00, files every
     2 minutes with a top-of-hour file every hour, available from 2021 on.
  3. Optional (--timeseries): an hourly footprint-mean and footprint-max
     rainfall table for the padded episode window.

QPE product choices (--product):
  pass2  MultiSensor_QPE_01H_Pass2_00.00  gauge corrected, ~12 h latency (default)
  pass1  MultiSensor_QPE_01H_Pass1_00.00  gauge corrected, ~1 h latency
  radar  RadarOnly_QPE_01H_00.00          radar only, near real time

Fetch order is AWS first, then the Iowa State mtarchive mirror for QPE
(ARI exists on AWS only). Decoding is gzip -> cfgrib, the same approach as
the rest of the RUNOFF engine.

Cost: one episode needs (duration + 72 + 6) hourly QPE grids plus
5 x duration ARI grids; roughly 1-2 s per grid. Start with your shortlist
(--min-events, --states, or explicit --episode ids), not with --all.
Runs are resumable: an episode with an existing mrms_summary.json is
skipped unless --force.

After running, rerun engine/episodes/build_catalog.py: it merges every
mrms_summary.json into assets/data/episodes.js so the rainfall filters and
episode cards on episodes.html show the numbers.

Usage:
  python engine/episodes/mrms_stats.py --episode 191899 --product pass2
  python engine/episodes/mrms_stats.py --states TX --min-events 10
  python engine/episodes/mrms_stats.py --all            # the full catalog, long
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import tempfile
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parents[2]

QPE_PRODUCTS = {
    "pass2": "MultiSensor_QPE_01H_Pass2_00.00",
    "pass1": "MultiSensor_QPE_01H_Pass1_00.00",
    "radar": "RadarOnly_QPE_01H_00.00",
}
ARI_PRODUCTS = {
    "30m": "FLASH_QPE_ARI30M_00.00",
    "1h": "FLASH_QPE_ARI01H_00.00",
    "3h": "FLASH_QPE_ARI03H_00.00",
    "6h": "FLASH_QPE_ARI06H_00.00",
    "24h": "FLASH_QPE_ARI24H_00.00",
}
AWS = "https://noaa-mrms-pds.s3.amazonaws.com"
ISU = "https://mtarchive.geol.iastate.edu"
ROLL_HOURS = [6, 12, 24, 72]
PAD_DEG = 0.10          # spatial pad around the episode bbox
PAD_POST_H = 6          # hours after the episode end still eligible as window ends
MIN_COVERAGE = 0.8      # fraction of hours a rolling window needs to count


def urls_for(product: str, dt: pd.Timestamp, qpe: bool):
    """Candidate URLs for one hour of one product: AWS, then the ISU mirror for QPE."""
    d = dt.strftime("%Y%m%d")
    hms = dt.strftime("%H") + "0000"
    urls = [f"{AWS}/CONUS/{product}/{d}/MRMS_{product}_{d}-{hms}.grib2.gz"]
    if qpe:
        stem = product.replace("_00.00", "")
        urls.append(f"{ISU}/{dt.year}/{dt.strftime('%m')}/{dt.strftime('%d')}"
                    f"/mrms/ncep/{stem}/{product}_{d}-{hms}.grib2.gz")
    return urls


def fetch_grid(product: str, dt: pd.Timestamp, bbox, qpe: bool):
    """Cropped 2D array for one hour, or None (missing hour).

    bbox is (min_lat, min_lon, max_lat, max_lon) in -180..180 longitudes.
    """
    import xarray as xr
    raw = None
    for url in urls_for(product, dt, qpe):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "runoff-engine"})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
            break
        except Exception:                                        # noqa: BLE001
            continue
    if raw is None:
        return None
    tmp = None
    try:
        grib = gzip.decompress(raw)
        fd, tmp = tempfile.mkstemp(suffix=".grib2")
        with os.fdopen(fd, "wb") as f:
            f.write(grib)
        ds = xr.load_dataset(tmp, engine="cfgrib", decode_timedelta=False,
                             backend_kwargs={"indexpath": ""})
        da = ds[list(ds.data_vars)[0]]
        da = da.where(da >= 0)                       # mask fill values (-1, -3, ...)
        lon0, lon1 = bbox[1] % 360, bbox[3] % 360
        sub = da.sel(latitude=slice(bbox[2] + PAD_DEG, bbox[0] - PAD_DEG),
                     longitude=slice(lon0 - PAD_DEG, lon1 + PAD_DEG))
        return sub.values.astype("float32")
    except Exception as e:                                       # noqa: BLE001
        print(f"    decode failed {product} {dt}: {e}")
        return None
    finally:
        if tmp and os.path.exists(tmp):
            base = os.path.basename(tmp)
            d = os.path.dirname(tmp)
            for f in os.listdir(d):                  # cfgrib side files (.idx)
                if f.startswith(base):
                    try:
                        os.remove(os.path.join(d, f))
                    except OSError:
                        pass


def episode_rolling(ep, product, timeseries=False):
    """Rolling-accumulation maxima + optional hourly series for one episode."""
    t0 = pd.Timestamp(ep.begin_utc).floor("h")
    t1 = pd.Timestamp(ep.end_utc).floor("h")
    hours = pd.date_range(t0 - pd.Timedelta(hours=72), t1 + pd.Timedelta(hours=PAD_POST_H),
                          freq="h")
    bbox = (ep.min_lat, ep.min_lon, ep.max_lat, ep.max_lon)

    shape = None
    prefix = []          # per-pixel cumulative sums, one entry per hour
    avail = []
    everseen = None
    series = []
    run = None
    for hr in hours:
        g = fetch_grid(product, hr, bbox, qpe=True)
        if g is not None and shape is None:
            shape = g.shape
        if g is not None and shape is not None and g.shape != shape:
            g = None                                  # grid definition changed; treat as gap
        if shape is None:
            avail.append(False)
            prefix.append(None)
            continue
        if g is None:
            avail.append(False)
            contrib = np.zeros(shape, dtype="float64")
            seen = np.zeros(shape, dtype=bool)
        else:
            nan = np.isnan(g)
            contrib = np.where(nan, 0.0, g).astype("float64")
            seen = ~nan
            avail.append(True)
            if timeseries:
                valid = g[~nan]
                if valid.size:
                    series.append({"datetime_utc": hr.strftime("%Y-%m-%d %H:%M"),
                                   "mean_mm": round(float(valid.mean()), 3),
                                   "max_mm": round(float(valid.max()), 3)})
        run = contrib if run is None else run + contrib
        everseen = seen if everseen is None else (everseen | seen)
        prefix.append(run.copy())
    if shape is None or everseen is None or not everseen.any():
        return None, series

    avail = np.array(avail)
    n = len(hours)
    in_ep = [(h >= t0) and (h <= t1 + pd.Timedelta(hours=PAD_POST_H)) for h in hours]
    result = {}
    for k in ROLL_HOURS:
        best = None
        for j in range(n):
            if not in_ep[j] or j - k < -1 or prefix[j] is None:
                continue
            cov = avail[max(0, j - k + 1):j + 1].mean() if k else 0
            if cov < MIN_COVERAGE:
                continue
            base = prefix[j - k] if j - k >= 0 and prefix[j - k] is not None else 0.0
            S = prefix[j] - base
            m = float(np.max(S[everseen])) if everseen.any() else None
            if m is not None and (best is None or m > best):
                best = m
        result[f"h{k}"] = round(best, 1) if best is not None else None
    hours_used = int(avail.sum())
    return {"max_rolling_mm": result, "hours_with_data": hours_used,
            "hours_total": n}, series


def episode_ari(ep):
    """Most severe FLASH ARI cell in the bbox at any hour of the raw window."""
    t0 = pd.Timestamp(ep.begin_utc).floor("h")
    t1 = pd.Timestamp(ep.end_utc).floor("h")
    bbox = (ep.min_lat, ep.min_lon, ep.max_lat, ep.max_lon)
    best = None
    for hr in pd.date_range(t0, t1, freq="h"):
        for dur, product in ARI_PRODUCTS.items():
            g = fetch_grid(product, hr, bbox, qpe=False)
            if g is None:
                continue
            valid = g[~np.isnan(g)]
            if not valid.size:
                continue
            m = float(valid.max())
            if best is None or m > best[0]:
                best = (m, dur, hr.strftime("%Y-%m-%d %H:%M"))
    if best is None:
        return None
    return {"years": round(best[0], 1), "duration": best[1], "hour_utc": best[2]}


def main() -> None:
    """Compute MRMS rolling accumulations and FLASH ARI stats per episode."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalog", type=Path, default=HERE / "data" / "episodes" / "episodes.csv")
    ap.add_argument("--out", type=Path, default=HERE / "data" / "episodes")
    ap.add_argument("--episode", nargs="*", type=int, help="specific episode id(s)")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--states", nargs="*", help="only episodes touching these states")
    ap.add_argument("--min-events", type=int, default=0)
    ap.add_argument("--product", choices=list(QPE_PRODUCTS), default="pass2")
    ap.add_argument("--no-ari", action="store_true", help="skip the FLASH ARI pass")
    ap.add_argument("--timeseries", action="store_true",
                    help="also write hourly_rain.csv per episode")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if not args.catalog.exists():
        raise SystemExit(f"{args.catalog} not found; run engine/episodes/build_catalog.py first")
    cat = pd.read_csv(args.catalog)
    sel = cat
    if args.episode:
        sel = sel[sel.episode_id.isin(args.episode)]
    elif not args.all:
        if args.states:
            pat = "|".join(s.upper() for s in args.states)
            sel = sel[sel.states.fillna("").str.contains(pat)]
        if args.min_events:
            sel = sel[sel.n_events >= args.min_events]
        if not args.states and not args.min_events:
            raise SystemExit("choose episodes: --episode ids, --states/--min-events, or --all")
    if not len(sel):
        raise SystemExit("no episodes match the selection")

    product = QPE_PRODUCTS[args.product]
    print(f"{len(sel)} episode(s), QPE product {product}")
    for _, ep in sel.iterrows():
        epid = int(ep.episode_id)
        out_dir = args.out / str(epid)
        summary_path = out_dir / "mrms_summary.json"
        if summary_path.exists() and not args.force:
            print(f"episode {epid}: cached, skipping")
            continue
        print(f"episode {epid}: {ep.begin_utc} .. {ep.end_utc} "
              f"({ep.states}, {ep.n_events} events)")
        roll, series = episode_rolling(ep, product, timeseries=args.timeseries)
        ari = None if args.no_ari else episode_ari(ep)
        summary = {
            "episode_id": epid, "product": product,
            "computed_utc": pd.Timestamp.now("UTC").strftime("%Y-%m-%d %H:%M"),
            "bbox": [float(ep.min_lat), float(ep.min_lon),
                     float(ep.max_lat), float(ep.max_lon)],
            "max_rolling_mm": (roll or {}).get("max_rolling_mm"),
            "hours_with_data": (roll or {}).get("hours_with_data"),
            "hours_total": (roll or {}).get("hours_total"),
            "max_ari": ari,
        }
        out_dir.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, indent=1), encoding="utf-8")
        if args.timeseries and series:
            pd.DataFrame(series).to_csv(out_dir / "hourly_rain.csv", index=False)
        if roll:
            mm = roll["max_rolling_mm"]
            print(f"  max rolling mm: 6h {mm['h6']} | 12h {mm['h12']} | "
                  f"24h {mm['h24']} | 72h {mm['h72']} "
                  f"({roll['hours_with_data']}/{roll['hours_total']} hours had data)")
        if ari:
            print(f"  max return period: {ari['years']} yr ({ari['duration']} at {ari['hour_utc']} UTC)")

    print("\nDONE. Rerun engine/episodes/build_catalog.py to merge these stats "
          "into the episodes.html payload.")


if __name__ == "__main__":
    main()
