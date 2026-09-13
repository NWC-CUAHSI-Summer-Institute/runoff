#!/usr/bin/env python3
"""
precompute_flash.py - FLASH model output and flash flood guidance over each episode.

A second, resumable pass over the episodes already computed by precompute_mrms.py.
For every hour of the episode window (t0 through t1 + 1 h, top of the hour files,
nearest within 10 minutes) it reads seven NSSL FLASH products from the MRMS archive
and summarizes them over the episode footprint (union of the touched HUC8) and per
HUC8:

  unit streamflow (m3/s/km2), the FLASH distributed hydrologic models:
    CREST_MAXUNITSTREAMFLOW   CREST
    SAC_MAXUNITSTREAMFLOW     SAC-SMA
    HP_MAXUNITSTREAMFLOW      hydrophobic (burn scar) model
  QPE to flash flood guidance ratio (1.0 = rainfall reached the guidance):
    QPE_FFG01H, QPE_FFG03H, QPE_FFG06H, QPE_FFGMAX

Per product: the episode peak at any cell (with its hour), the area weighted mean
of the per cell peak, the hourly footprint maximum and mean (series), and for the
ratio products the share of the footprint area whose peak ratio reached 1.0. Per
HUC8: the peak of each product and the share above guidance for the max window.
Only hours inside the episode window are read; there is no lookback, since these
products already carry their own accumulation.

Sources: Iowa Environmental Mesonet MRMS archive (mtarchive) first, NOAA MRMS bucket
on AWS second, the same fetch order and GRIB2 PNG decoding as precompute_mrms.py.
Missing cells (negative fill values) are masked. Product directories can be edited
in PRODUCTS below if the archive names differ from the ones listed.

Results are merged into assets/data/ep/<id>.json under the key "flash"; episodes
that already have it are skipped unless --force. Then rebuild the catalog payload:
    python engine/episodes/build_mrms_payload.py

    python engine/episodes/precompute_flash.py                  # all computed episodes
    python engine/episodes/precompute_flash.py --states TX --min-events 5
    python engine/episodes/precompute_flash.py --episode 204934 --force
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from precompute_mrms import (
    AWS, DLL, IEM, LAT_TOP, LON_LEFT, Fetcher, crop_window, decode_grib2_png, to_values,
)

HERE = Path(__file__).resolve().parents[2]
EP_DIR = HERE / "data" / "episodes"
OUT_DIR = HERE / "assets" / "data" / "ep"
GEO = HERE / "data" / "geo" / "huc8_wbd.geojson"

# key -> (archive product name, kind); kind "uq" = unit streamflow, "ffg" = QPE/FFG ratio
PRODUCTS = {
    "crest": ("CREST_MAXUNITSTREAMFLOW", "uq"),
    "sac": ("SAC_MAXUNITSTREAMFLOW", "uq"),
    "hp": ("HP_MAXUNITSTREAMFLOW", "uq"),
    "ffg1": ("QPE_FFG01H", "ffg"),
    "ffg3": ("QPE_FFG03H", "ffg"),
    "ffg6": ("QPE_FFG06H", "ffg"),
    "ffgmax": ("QPE_FFGMAX", "ffg"),
}
LABELS = {"crest": "CREST unit streamflow", "sac": "SAC-SMA unit streamflow",
          "hp": "hydrophobic unit streamflow", "ffg1": "QPE/FFG ratio 1 h",
          "ffg3": "QPE/FFG ratio 3 h", "ffg6": "QPE/FFG ratio 6 h", "ffgmax": "QPE/FFG ratio max"}


def r2(x):
    """Round to two decimals; None for None or NaN."""
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), 2)


def fetch_product(f: Fetcher, prod: str, h: datetime):
    """One FLASH product at hour h: IEM top of hour, IEM nearest, AWS top of hour, AWS nearest."""
    d = h.strftime("%Y/%m/%d")
    ts = h.strftime("%Y%m%d-%H0000")
    b = f._get(f"{IEM}/{d}/mrms/ncep/FLASH/{prod}/{prod}_00.00_{ts}.grib2.gz")
    src = "iem"
    if not b:
        names = f._listing(("iem", prod, d), f"{IEM}/{d}/mrms/ncep/FLASH/{prod}/",
                           rf"{prod}_00\.00_(\d{{8}}-\d{{6}})\.grib2\.gz")
        near = Fetcher._nearest(names, h)
        if near:
            b = f._get(f"{IEM}/{d}/mrms/ncep/FLASH/{prod}/{prod}_00.00_{near}.grib2.gz")
            src = f"iem@{near[-6:]}"
    if not b:
        b = f._get(f"{AWS}/FLASH_{prod}_00.00/{h:%Y%m%d}/MRMS_FLASH_{prod}_00.00_{ts}.grib2.gz")
        src = "aws"
        if not b:
            lst = f._get("https://noaa-mrms-pds.s3.amazonaws.com/?list-type=2&prefix=CONUS/"
                         f"FLASH_{prod}_00.00/{h:%Y%m%d}/MRMS_FLASH_{prod}_00.00_{h:%Y%m%d-%H}&max-keys=60")
            names = sorted(set(re.findall(rf"MRMS_FLASH_{prod}_00\.00_(\d{{8}}-\d{{6}})\.grib2\.gz",
                                          lst.decode("utf-8", "ignore")))) if lst else []
            near = Fetcher._nearest(names, h)
            if near:
                b = f._get(f"{AWS}/FLASH_{prod}_00.00/{h:%Y%m%d}/MRMS_FLASH_{prod}_00.00_{near}.grib2.gz")
                src = f"aws@{near[-6:]}"
    if not b:
        return None, None
    try:
        return decode_grib2_png(b), src
    except Exception as e:  # noqa: BLE001
        f.log(f"decode failed {prod} {h}: {e}")
        return None, None


class FlashEpisode:
    """Per cell peaks and hourly footprint statistics of the FLASH products for one episode."""

    def __init__(self, row, hucs, huc_geoms):
        from rasterio import features
        from rasterio.transform import from_origin

        self.id = int(row.episode_id)
        self.t0 = datetime.strptime(row.begin_utc, "%Y-%m-%d %H")
        self.t1 = datetime.strptime(row.end_utc, "%Y-%m-%d %H")
        self.h_last = self.t1 + timedelta(hours=1)
        self.hucs = list(hucs.huc8)
        geoms = [huc_geoms[h] for h in self.hucs]
        b = np.array([g.bounds for g in geoms])
        self.r0, self.r1, self.c0, self.c1 = crop_window(b[:, 1].min(), b[:, 0].min(),
                                                         b[:, 3].max(), b[:, 2].max())
        rows, cols = self.r1 - self.r0, self.c1 - self.c0
        tr = from_origin(LON_LEFT + self.c0 * DLL, LAT_TOP - self.r0 * DLL, DLL, DLL)
        self.label = features.rasterize([(g, i + 1) for i, g in enumerate(geoms)],
                                        out_shape=(rows, cols), transform=tr, fill=0, dtype="int32")
        self.fp = self.label > 0
        lat = LAT_TOP - (np.arange(self.r0, self.r1) + 0.5) * DLL
        self.w = np.repeat(np.cos(np.deg2rad(lat))[:, None], cols, axis=1).astype(np.float32)
        self.cellmax = {k: np.full((rows, cols), np.nan, np.float32) for k in PRODUCTS}
        self.peak = dict.fromkeys(PRODUCTS, (None, None))   # (value, hour)
        self.series = []
        self.missing = dict.fromkeys(PRODUCTS, 0)
        self.sources = {k: {} for k in PRODUCTS}
        self.n_hours = 0

    def window(self):
        """Crop bounds (r0, r1, c0, c1) on the CONUS grid."""
        return self.r0, self.r1, self.c0, self.c1

    def _wmean(self, v, mask):
        ok = mask & ~np.isnan(v)
        return float((v[ok] * self.w[ok]).sum() / self.w[ok].sum()) if ok.any() else None

    def take(self, h, crops, sources):
        """Ingest one hour: crops[key] is a float32 crop (NaN = missing) or None."""
        self.n_hours += 1
        row = [h.strftime("%Y-%m-%d %H")]
        for k in PRODUCTS:
            v = crops.get(k)
            if v is None:
                self.missing[k] += 1
                row += [None, None]
                continue
            src = sources.get(k, "")
            self.sources[k][src] = self.sources[k].get(src, 0) + 1
            np.fmax(self.cellmax[k], v, out=self.cellmax[k])
            fpv = v[self.fp]
            ok = ~np.isnan(fpv)
            mx = float(fpv[ok].max()) if ok.any() else None
            if mx is not None and (self.peak[k][0] is None or mx > self.peak[k][0]):
                self.peak[k] = (mx, h.strftime("%Y-%m-%d %H"))
            row += [r2(mx), r2(self._wmean(v, self.fp))]
        self.series.append(row)

    def _stats(self, mask):
        out = {}
        wsum = float(self.w[mask].sum())
        for k, (_, kind) in PRODUCTS.items():
            cm = self.cellmax[k]
            ok = mask & ~np.isnan(cm)
            s = {"max": r2(float(cm[ok].max())) if ok.any() else None,
                 "mean_peak": r2(self._wmean(cm, mask))}
            if kind == "ffg" and wsum > 0:
                s["cov1"] = round(float(self.w[ok & (cm >= 1.0)].sum() / wsum), 3)
            out[k] = s
        return out

    def result(self):
        """The 'flash' record merged into the episode JSON."""
        fp = self._stats(self.fp)
        for k in PRODUCTS:
            fp[k]["t"] = self.peak[k][1]
        hucs = {}
        for i, code in enumerate(self.hucs):
            st = self._stats(self.label == (i + 1))
            hucs[code] = {k: st[k]["max"] for k in PRODUCTS}
            hucs[code]["ffgmax_cov1"] = st["ffgmax"].get("cov1")
        return {
            "computed_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "hours": self.n_hours,
            "products": {k: PRODUCTS[k][0] for k in PRODUCTS},
            "labels": LABELS,
            "units": {"uq": "m3/s/km2", "ffg": "ratio (1.0 = rainfall reached the guidance)"},
            "missing": self.missing, "sources": self.sources,
            "fp": fp, "huc8": hucs,
            "series_fields": ["hour"] + [f"{k}_{s}" for k in PRODUCTS for s in ("max", "mean")],
            "series": self.series,
            "definition": "FLASH products at the top of each hour t0 .. t1 + 1 h over the episode "
                          "footprint; per cell peak over those hours; cov1 = share of the area "
                          "whose peak QPE/FFG ratio reached 1.0 (cos latitude weighted)",
        }


def main() -> None:
    """Hour by hour sweep of the FLASH products for the selected episodes."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", type=int, nargs="*", help="episode ids")
    ap.add_argument("--states", nargs="*")
    ap.add_argument("--min-events", type=int, default=0)
    ap.add_argument("--start", help="episodes beginning on/after YYYY-MM-DD")
    ap.add_argument("--end", help="episodes beginning before YYYY-MM-DD")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4, help="parallel product downloads per hour")
    ap.add_argument("--force", action="store_true", help="recompute episodes that have 'flash'")
    args = ap.parse_args()

    logf = open(EP_DIR / "flash_run.log", "a")
    loglock = threading.Lock()

    def log(msg):
        with loglock:
            logf.write(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} {msg}\n")
            logf.flush()

    ep = pd.read_csv(EP_DIR / "episodes.csv", dtype={"county_fips": str, "states": str})
    ep["t0dt"] = pd.to_datetime(ep.begin_utc, format="%Y-%m-%d %H")
    if args.episode:
        ep = ep[ep.episode_id.isin(args.episode)]
    if args.states:
        st = {s.upper() for s in args.states}
        ep = ep[ep.states.fillna("").apply(lambda s: bool(set(s.split(",")) & st))]
    if args.min_events:
        ep = ep[ep.n_events >= args.min_events]
    if args.start:
        ep = ep[ep.t0dt >= pd.Timestamp(args.start)]
    if args.end:
        ep = ep[ep.t0dt < pd.Timestamp(args.end)]

    def has_json(i):
        return (OUT_DIR / f"{i}.json").exists()

    def has_flash(i):
        p = OUT_DIR / f"{i}.json"
        return p.exists() and '"flash":' in p.read_text(encoding="utf-8")

    n_before = len(ep)
    ep = ep[ep.episode_id.apply(has_json)]
    if n_before != len(ep):
        print(f"{n_before - len(ep)} episode(s) skipped: no MRMS JSON yet (run precompute_mrms.py first)")
    if not args.force:
        ep = ep[~ep.episode_id.apply(has_flash)]
    ep = ep.sort_values(["t0dt", "episode_id"])
    if args.limit:
        ep = ep.head(args.limit)
    if ep.empty:
        print("nothing to do")
        return
    print(f"{len(ep)} episodes to compute", flush=True)

    eh = pd.read_csv(EP_DIR / "episode_huc8.csv", dtype={"huc8": str})
    eh = eh[eh.kept == 1]
    eh_groups = {int(k): g for k, g in eh.groupby("episode_id")}

    from shapely.geometry import shape
    need = set(eh[eh.episode_id.isin(ep.episode_id)].huc8)
    huc_geoms = {}
    for f in json.load(open(GEO))["features"]:
        h = str(f["properties"]["huc8"]).zfill(8)
        if h in need:
            huc_geoms[h] = shape(f["geometry"]).buffer(0)
    print(f"geometry loaded: {len(huc_geoms)} HUC8", flush=True)

    rows, by_hour = {}, {}
    for r in ep.itertuples(index=False):
        rows[int(r.episode_id)] = r
        t0 = datetime.strptime(r.begin_utc, "%Y-%m-%d %H")
        t1 = datetime.strptime(r.end_utc, "%Y-%m-%d %H") + timedelta(hours=1)
        h = t0
        while h <= t1:
            by_hour.setdefault(h, []).append(int(r.episode_id))
            h += timedelta(hours=1)
    hours = sorted(by_hour)
    print(f"{len(hours)} unique hours x {len(PRODUCTS)} products", flush=True)

    fetcher = Fetcher(log)
    pool = ThreadPoolExecutor(max_workers=args.workers)
    active, done, t_start, last_print = {}, 0, time.time(), 0.0
    for hi, h in enumerate(hours):
        for eid in list(by_hour[h]):
            if eid not in active:
                hucs = eh_groups.get(eid)
                if hucs is None or hucs.empty:
                    log(f"episode {eid}: no HUC8, skipped")
                    by_hour[h].remove(eid)
                    continue
                active[eid] = FlashEpisode(rows[eid], hucs, huc_geoms)
        if not by_hour[h]:
            continue
        futs = {k: pool.submit(fetch_product, fetcher, PRODUCTS[k][0], h) for k in PRODUCTS}
        grids, sources = {}, {}
        for k, fut in futs.items():
            g, src = fut.result()
            if g is not None:
                grids[k] = g
                sources[k] = src
        for eid in list(by_hour[h]):
            e = active.get(eid)
            if e is None:
                continue
            r0, r1, c0, c1 = e.window()
            crops = {}
            for k, (raw, R, E, D) in grids.items():
                v = to_values(raw[r0:r1, c0:c1], R, E, D)
                v[v < 0] = np.nan
                crops[k] = v
            e.take(h, crops, sources)
            if h == e.h_last:
                p = OUT_DIR / f"{eid}.json"
                j = json.loads(p.read_text(encoding="utf-8"))
                j["flash"] = e.result()
                p.write_text(json.dumps(j, separators=(",", ":")), encoding="utf-8")
                del active[eid]
                done += 1
        miss = [k for k in PRODUCTS if k not in grids]
        log(f"{h:%Y-%m-%d %H} ok={len(grids)} missing={miss} n_ep={len(by_hour[h])}")
        if time.time() - last_print > 30 or hi == len(hours) - 1:
            last_print = time.time()
            print(f"{h:%Y-%m-%d %H} | hour {hi + 1}/{len(hours)} | episodes done {done}/{len(ep)} | "
                  f"active {len(active)} | products {len(grids)}/{len(PRODUCTS)} | "
                  f"{(time.time() - t_start) / 60:.1f} min", flush=True)
    pool.shutdown()
    print(f"finished: {done} episodes in {(time.time() - t_start) / 60:.1f} min")
    print("next: python engine/episodes/build_mrms_payload.py")


if __name__ == "__main__":
    main()
