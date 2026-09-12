#!/usr/bin/env python3
"""
precompute_mrms.py - MRMS rainfall and return period statistics for every episode footprint,
computed once, locally, and written as static JSON for the website. Nothing runs online.

For each episode (NOAA Storm Events EPISODE_ID, see build_catalog.py) and for each HUC8
watershed the episode touched (build_huc8_footprints.py):

  rain        MRMS MultiSensor QPE 1 h Pass 2 (gauge corrected, 2 h latency), 0.01 deg grid,
              hourly files with valid time h. The hour ending at h covers (h-1h, h].
  windows     rolling 1 h, 3 h and 6 h accumulations with window END times h from the
              episode start hour t0 through t1 + 1 h (the catalogue truncates the episode
              end to the hour). The 5 hours before t0 are read so that the first 6 h
              window is complete (antecedent rain counts, as a flash flood forecaster
              would read it).
  totals      rain_ep   sum of the hourly fields with h in [t0, t1 + 1 h]
              rain_pre  sum of the 5 hourly fields before that (t0 - 5 h .. t0 - 1 h)
  ARI         FLASH QPE_ARI 1H, 3H and 6H (average recurrence interval in years of the
              MRMS QPE accumulation, NOAA Atlas 14 based, capped at 200), sampled at the top
              of every hour h in [t0, t1 + 1 h]; per pixel the maximum over those hours.
              FLASH derives its ARI from the MRMS radar only QPE, so the ARI and the Pass 2
              accumulations are two views of the same storm, not one computed from the other.
  statistics  per footprint (union of touched HUC8), per county union, per HUC8:
              area km2; for each duration the maximum and area weighted mean of the pixel
              maxima and the hour of the footprint maximum; episode totals; for each ARI
              duration the maximum and the fraction of the area at or above 1, 2, 5, 10, 20,
              25, 50, 100 and 200 years (cos latitude weighted; pixels without an ARI value
              count as not exceeding, the valid fraction is reported).
  series      hourly footprint mean and maximum rain and maximum 1 h ARI, for sparklines.

Data source: Iowa Environmental Mesonet MRMS archive (mtarchive.geol.iastate.edu) with the
NOAA MRMS bucket on AWS (noaa-mrms-pds) as fallback, file by file. Pass 2 exists from
14 October 2020; earlier hours fall back to Pass 1, then radar only, and the product used
is recorded per hour. GRIB2 messages are PNG packed (template 5.41); they are decoded
directly with OpenCV, which is about 5 times faster than a generic GRIB library and gives
identical values (checked against ecCodes).

Sweep: hours are visited once in chronological order; every episode active at that hour
receives its cropped fields (download, decode and crop happen in worker threads, the
statistics in the main thread). An episode is written when its last hour has been seen,
so a run can be stopped and resumed: episodes with an existing JSON are skipped.

Outputs
    assets/data/ep/<episode_id>.json    per episode payload read by episodes.html on click
    data/episodes/mrms_run.log          hour log (product used, fallbacks, misses)

Usage
    python engine/episodes/precompute_mrms.py                    # everything, resumable
    python engine/episodes/precompute_mrms.py --episode 191899   # one episode
    python engine/episodes/precompute_mrms.py --states TX --min-events 5
    python engine/episodes/precompute_mrms.py --start 2021-05-01 --end 2021-06-01
    python engine/episodes/precompute_mrms.py --workers 3 --force

Then rebuild the site catalogue payload:
    python engine/episodes/build_mrms_payload.py
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import re
import struct
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

try:
    import cv2
except ImportError:  # pragma: no cover
    sys.exit("OpenCV is required: pip install opencv-python-headless")

HERE = Path(__file__).resolve().parents[2]
EP_DIR = HERE / "data" / "episodes"
OUT_DIR = HERE / "assets" / "data" / "ep"
GEO = HERE / "data" / "geo" / "huc8_wbd.geojson"
COUNTIES_JS = HERE / "assets" / "data" / "counties_geo.js"

# MRMS CONUS grid: 3500 rows x 7000 cols, 0.01 deg, cell centres from (54.995, -129.995)
NROWS, NCOLS, DLL = 3500, 7000, 0.01
LAT_TOP, LON_LEFT = 55.0, -130.0
KM_PER_DEG = 111.32

IEM = "https://mtarchive.geol.iastate.edu"
AWS = "https://noaa-mrms-pds.s3.amazonaws.com/CONUS"
QPE_CHAIN = [  # (label, IEM product dir, AWS product dir)
    ("pass2", "MultiSensor_QPE_01H_Pass2", "MultiSensor_QPE_01H_Pass2_00.00"),
    ("pass1", "MultiSensor_QPE_01H_Pass1", "MultiSensor_QPE_01H_Pass1_00.00"),
    ("radar", "RadarOnly_QPE_01H", "RadarOnly_QPE_01H_00.00"),
]
ARI_DURS = {1: "QPE_ARI01H", 3: "QPE_ARI03H", 6: "QPE_ARI06H"}
ARI_THRESH = [1, 2, 5, 10, 20, 25, 50, 100, 200]
ROLL = [1, 3, 6]
LOOKBACK_H = 5
# persistent hot cell rule: a cell above 100 mm/h in 4 or more of the hours read, or above
# 150 mm/h in 3 or more, is a radar artifact (a stuck bin, clutter), not rain; such cells are
# removed from every statistic and counted in qpe.artifact_cells
HOT_MM, HOT_N, HOT2_MM, HOT2_N = 100.0, 4, 150.0, 3
UA = {"User-Agent": "RUNOFF episode precompute (CUAHSI NWC Summer Institute 2026)"}

_session = threading.local()


def http():
    """One requests.Session per worker thread."""
    s = getattr(_session, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update(UA)
        _session.s = s
    return s


# ----------------------------------------------------------------------------- GRIB2 PNG decode
def decode_grib2_png(gz_bytes):
    """Decode a single message PNG packed MRMS GRIB2 (gzipped).

    Returns (img, R, E, D): img is the raw PNG image, uint16 (rows, cols) for 16 bit packing
    or uint8 (rows, cols, 3) in OpenCV BGR order for 24 bit packing. Convert crops with
    to_values(); converting the whole CONUS grid would cost more than the decode itself.
    """
    buf = gzip.decompress(gz_bytes)
    if buf[:4] != b"GRIB":
        raise ValueError("not a GRIB file")
    total = struct.unpack(">Q", buf[8:16])[0]
    pos, secs = 16, {}
    while pos < total - 4:
        if buf[pos:pos + 4] == b"7777":
            break
        ln = struct.unpack(">I", buf[pos:pos + 4])[0]
        secs[buf[pos + 4]] = buf[pos:pos + ln]
        pos += ln
    s3, s5, s7 = secs[3], secs[5], secs[7]
    ni = struct.unpack(">I", s3[30:34])[0]
    nj = struct.unpack(">I", s3[34:38])[0]
    if struct.unpack(">H", s5[9:11])[0] != 41:
        raise ValueError("expected PNG packing (template 5.41)")
    R = struct.unpack(">f", s5[11:15])[0]
    E = struct.unpack(">h", s5[15:17])[0]
    D = struct.unpack(">h", s5[17:19])[0]
    img = cv2.imdecode(np.frombuffer(s7[5:], np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError("PNG decode failed")
    if img.shape[:2] != (nj, ni):
        raise ValueError(f"grid {img.shape[:2]} != ({nj},{ni})")
    return img, R, E, D


def to_values(img, R, E, D, dtype=np.float32):
    """Physical values of a raw PNG image (or a crop of it), missing codes left as coded."""
    if img.ndim == 3:  # 24 bit values, OpenCV gives BGR
        raw = (img[:, :, 2].astype(np.int32) << 16) | (img[:, :, 1].astype(np.int32) << 8) | img[:, :, 0]
    else:
        raw = img
    return ((R + raw.astype(np.float64) * (2.0 ** E)) / (10.0 ** D)).astype(dtype)


# ----------------------------------------------------------------------------- fetching
class Fetcher:
    """Finds and downloads one MRMS file for a product and valid hour; IEM first, AWS second.

    FLASH products are written every 2 minutes; the top of the hour file is used when it
    exists, otherwise the nearest file within 10 minutes (directory listing, cached per day).
    """

    def __init__(self, log):
        self.log = log
        self.lists = {}
        self.lock = threading.Lock()

    def _get(self, url, timeout=120):
        for attempt in range(3):
            try:
                r = http().get(url, timeout=timeout)
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                return r.content
            except requests.RequestException as e:
                if attempt == 2:
                    self.log(f"GET failed {url}: {e}")
                    return None
                time.sleep(2 * (attempt + 1))
        return None

    def _listing(self, key, url, pattern):
        with self.lock:
            if key in self.lists:
                return self.lists[key]
        txt = self._get(url)
        names = sorted(set(re.findall(pattern, txt.decode("utf-8", "ignore")))) if txt else []
        with self.lock:
            self.lists[key] = names
        return names

    def qpe(self, h):
        """Return (values float32 CONUS, label) for the hourly QPE ending at h, or (None, None)."""
        d = h.strftime("%Y/%m/%d")
        ts = h.strftime("%Y%m%d-%H0000")
        for label, iem_dir, aws_dir in QPE_CHAIN:
            urls = [f"{IEM}/{d}/mrms/ncep/{iem_dir}/{iem_dir}_00.00_{ts}.grib2.gz",
                    f"{AWS}/{aws_dir}/{h:%Y%m%d}/MRMS_{aws_dir}_{ts}.grib2.gz"]
            for k, u in enumerate(urls):
                b = self._get(u)
                if b:
                    try:
                        raw, R, E, D = decode_grib2_png(b)
                    except Exception as e:  # noqa: BLE001
                        self.log(f"decode failed {u}: {e}")
                        continue
                    return (raw, R, E, D), (label + ("" if k == 0 else "@aws"))
        return None, None

    def ari(self, h, dur):
        """Return (values, source) for the FLASH ARI of duration dur nearest the hour h."""
        prod = ARI_DURS[dur]
        d = h.strftime("%Y/%m/%d")
        ts = h.strftime("%Y%m%d-%H0000")
        # 1. IEM top of hour
        u = f"{IEM}/{d}/mrms/ncep/FLASH/{prod}/{prod}_00.00_{ts}.grib2.gz"
        b = self._get(u)
        src = "iem"
        if not b:
            # 2. IEM nearest within 10 min from the day listing
            names = self._listing(("iem", prod, d), f"{IEM}/{d}/mrms/ncep/FLASH/{prod}/",
                                  rf"{prod}_00\.00_(\d{{8}}-\d{{6}})\.grib2\.gz")
            near = self._nearest(names, h)
            if near:
                b = self._get(f"{IEM}/{d}/mrms/ncep/FLASH/{prod}/{prod}_00.00_{near}.grib2.gz")
                src = f"iem@{near[-6:]}"
        if not b:
            # 3. AWS top of hour, then AWS listing
            u = f"{AWS}/FLASH_{prod}_00.00/{h:%Y%m%d}/MRMS_FLASH_{prod}_00.00_{ts}.grib2.gz"
            b = self._get(u)
            src = "aws"
            if not b:
                lst = self._get(f"https://noaa-mrms-pds.s3.amazonaws.com/?list-type=2&prefix=CONUS/FLASH_{prod}_00.00/{h:%Y%m%d}/MRMS_FLASH_{prod}_00.00_{h:%Y%m%d-%H}&max-keys=60")
                names = sorted(set(re.findall(rf"MRMS_FLASH_{prod}_00\.00_(\d{{8}}-\d{{6}})\.grib2\.gz",
                                              lst.decode("utf-8", "ignore")))) if lst else []
                near = self._nearest(names, h)
                if near:
                    b = self._get(f"{AWS}/FLASH_{prod}_00.00/{h:%Y%m%d}/MRMS_FLASH_{prod}_00.00_{near}.grib2.gz")
                    src = f"aws@{near[-6:]}"
        if not b:
            return None, None
        try:
            raw, R, E, D = decode_grib2_png(b)
        except Exception as e:  # noqa: BLE001
            self.log(f"decode failed ARI{dur} {h}: {e}")
            return None, None
        return (raw, R, E, D), src

    @staticmethod
    def _nearest(names, h, tol_min=10):
        best, bestd = None, None
        for n in names:
            try:
                t = datetime.strptime(n, "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            dm = abs((t - h).total_seconds()) / 60
            if dm <= tol_min and (bestd is None or dm < bestd):
                best, bestd = n, dm
        return best


# ----------------------------------------------------------------------------- episode state
def crop_window(min_lat, min_lon, max_lat, max_lon, pad=1):
    """Row and column bounds of a lat/lon box on the MRMS CONUS grid, padded by pad cells."""
    r0 = int(math.floor((LAT_TOP - max_lat) / DLL)) - pad
    r1 = int(math.ceil((LAT_TOP - min_lat) / DLL)) + pad
    c0 = int(math.floor((min_lon - LON_LEFT) / DLL)) - pad
    c1 = int(math.ceil((max_lon - LON_LEFT) / DLL)) + pad
    r0, c0 = max(r0, 0), max(c0, 0)
    r1, c1 = min(r1, NROWS), min(c1, NCOLS)
    return r0, r1, c0, c1


def hours_of(t0, t1):
    """Valid hours read for an episode: t0 - 5 h .. t1 + 1 h (statistics use [t0, t1 + 1 h])."""
    h = t0 - timedelta(hours=LOOKBACK_H)
    end = t1 + timedelta(hours=1)
    out = []
    while h <= end:
        out.append(h)
        h += timedelta(hours=1)
    return out


class Episode:
    """Running statistics of one episode over its cropped MRMS window, fed hour by hour."""

    def __init__(self, row, hucs, huc_geoms, huc_meta, cty_geoms):
        from rasterio import features
        from rasterio.transform import from_origin

        self.id = int(row.episode_id)
        self.t0 = datetime.strptime(row.begin_utc, "%Y-%m-%d %H")
        self.t1 = datetime.strptime(row.end_utc, "%Y-%m-%d %H")
        self.h_first = self.t0 - timedelta(hours=LOOKBACK_H)
        self.h_last = self.t1 + timedelta(hours=1)
        self.row = row
        self.hucs = hucs  # DataFrame rows of episode_huc8 (kept)
        geoms = [huc_geoms[h] for h in hucs.huc8]
        b = np.array([g.bounds for g in geoms])
        self.r0, self.r1, self.c0, self.c1 = crop_window(b[:, 1].min(), b[:, 0].min(),
                                                         b[:, 3].max(), b[:, 2].max())
        rows, cols = self.r1 - self.r0, self.c1 - self.c0
        tr = from_origin(LON_LEFT + self.c0 * DLL, LAT_TOP - self.r0 * DLL, DLL, DLL)
        self.label = features.rasterize(
            [(g, i + 1) for i, g in enumerate(geoms)], out_shape=(rows, cols),
            transform=tr, fill=0, dtype="int32")
        fips = [f for f in str(row.county_fips).split(";") if f in cty_geoms]
        self.cty = features.rasterize(
            [(cty_geoms[f], 1) for f in fips], out_shape=(rows, cols),
            transform=tr, fill=0, dtype="uint8").astype(bool) if fips else np.zeros((rows, cols), bool)
        self.fp = self.label > 0
        lat = LAT_TOP - (np.arange(self.r0, self.r1) + 0.5) * DLL
        self.w = np.repeat(np.cos(np.deg2rad(lat))[:, None], cols, axis=1).astype(np.float32)
        self.cell_km2 = (KM_PER_DEG * DLL) ** 2 * self.w
        self.huc_meta = huc_meta
        shape = (rows, cols)
        self.buf = deque(maxlen=6)          # last 6 hourly fields (float32, NaN = missing)
        self.m = {d: np.full(shape, -1.0, np.float32) for d in ROLL}
        self.m_h = {d: np.full(shape, -1, np.int16) for d in ROLL}   # index into self.hours_seen
        self.hours_seen = []
        self.ari = {d: np.full(shape, np.nan, np.float32) for d in ROLL}
        self.rain_ep = np.zeros(shape, np.float32)
        self.rain_pre = np.zeros(shape, np.float32)
        self.n_ep = 0
        self.hot = np.zeros(shape, np.int16)
        self.hot2 = np.zeros(shape, np.int16)
        self.series = []      # [h, mean, lo_max, hi_idx, hi_val, ari_lo, ari_hi] until result()
        self.qpe_products = {}
        self.qpe_missing = []
        self.ari_missing = {d: [] for d in ROLL}
        self.ari_skipped = 0
        self.ari_src = {d: {} for d in ROLL}

    def window(self):
        """Crop bounds (r0, r1, c0, c1) on the CONUS grid."""
        return self.r0, self.r1, self.c0, self.c1

    def take(self, h, qpe_crop, qpe_label, ari_crops, ari_src, ari_skipped=False):
        """Ingest one hour of cropped fields.

        qpe_crop: float32 crop (NaN where missing) or None; ari_crops: {dur: crop or None};
        ari_skipped: the hour had no rain in any active footprint, ARI not sampled (see work).
        """
        if qpe_crop is None:
            qpe_crop = np.full(self.label.shape, np.nan, np.float32)
            self.qpe_missing.append(h.strftime("%Y-%m-%d %H"))
        else:
            self.qpe_products[qpe_label] = self.qpe_products.get(qpe_label, 0) + 1
        self.buf.append(qpe_crop)
        self.hot += (qpe_crop > HOT_MM)
        self.hot2 += (qpe_crop > HOT2_MM)
        in_window = self.t0 <= h <= self.h_last
        if h < self.t0:
            self.rain_pre += np.nan_to_num(qpe_crop)
        if in_window:
            self.hours_seen.append(h)
            self.rain_ep += np.nan_to_num(qpe_crop)
            self.n_ep += 1
            for d in ROLL:
                k = min(d, len(self.buf))
                if k == 1:
                    s = np.nan_to_num(qpe_crop)
                else:
                    s = np.nansum(np.stack(list(self.buf)[-k:]), axis=0)
                better = s > self.m[d]
                self.m[d][better] = s[better]
                self.m_h[d][better] = len(self.hours_seen) - 1
            for d in ROLL:
                a = ari_crops.get(d)
                if a is None:
                    if ari_skipped:
                        self.ari_skipped += 1
                    else:
                        self.ari_missing[d].append(h.strftime("%Y-%m-%d %H"))
                    continue
                src = ari_src.get(d, "")
                self.ari_src[d][src] = self.ari_src[d].get(src, 0) + 1
                np.fmax(self.ari[d], a, out=self.ari[d])
            a1 = ari_crops.get(1)
            fpm = self.fp
            fpv = np.where(fpm, qpe_crop, np.nan)
            has = fpm.any() and not np.all(np.isnan(fpv))
            hi_idx = np.flatnonzero(fpv > HOT_MM) if has else np.zeros(0, np.int64)
            lo = fpv.copy()
            if hi_idx.size:
                lo.flat[hi_idx] = np.nan
            a1v = np.where(fpm, a1, np.nan) if a1 is not None else None
            a_has = a1v is not None and not np.all(np.isnan(a1v))
            a_lo = a1v.copy() if a_has else None
            if a_has and hi_idx.size:
                a_lo.flat[hi_idx] = np.nan
            self.series.append([
                h.strftime("%Y-%m-%d %H"),
                float(np.nanmean(fpv)) if has else None,
                float(np.nanmax(lo)) if has and not np.all(np.isnan(lo)) else None,
                hi_idx, fpv.flat[hi_idx] if hi_idx.size else np.zeros(0, np.float32),
                float(np.nanmax(a_lo)) if a_has and not np.all(np.isnan(a_lo)) else None,
                a1v.flat[hi_idx] if (a_has and hi_idx.size) else np.zeros(0, np.float32),
            ])

    # ---- statistics
    def stats_for(self, mask):
        """Area, rain totals, rolling maxima and ARI coverage over the cells in mask."""
        w = self.w[mask]
        wsum = float(w.sum()) if w.size else 0.0
        out = {"area_km2": r1(float(self.cell_km2[mask].sum()))}
        if wsum <= 0:
            return out

        def wmean(v):
            ok = ~np.isnan(v)
            return r1(float((v[ok] * w[ok]).sum() / w[ok].sum())) if ok.any() else None

        def vmax(v):
            ok = ~np.isnan(v)
            return r1(float(v[ok].max())) if ok.any() else None

        rep, rpre = self.rain_ep[mask], self.rain_pre[mask]
        out["rain"] = {"ep": {"max": vmax(rep), "mean": wmean(rep)},
                       "pre": {"max": vmax(rpre), "mean": wmean(rpre)}}
        for d in ROLL:
            v = self.m[d][mask].copy()
            v[v < 0] = np.nan
            out[f"max{d}"] = {"max": vmax(v), "mean": wmean(v)}
        for d in ROLL:
            a = self.ari[d][mask]
            ok = ~np.isnan(a)
            cov = [r3(float(w[ok & (a >= t)].sum() / wsum)) for t in ARI_THRESH]
            out[f"ari{d}"] = {"max": vmax(a), "cov": cov, "valid": r3(float(w[ok].sum() / wsum))}
        return out

    def finalize_artifacts(self):
        """Remove persistent hot cells (see HOT_*) from every field and rebuild the series."""
        bad = (self.hot >= HOT_N) | (self.hot2 >= HOT2_N)
        n_bad = int(bad.sum())
        self.n_bad = n_bad
        self.bad_fp = int((bad & self.fp).sum())
        if n_bad:
            for d in ROLL:
                self.m[d][bad] = np.nan
                self.ari[d][bad] = np.nan
            self.rain_ep[bad] = np.nan
            self.rain_pre[bad] = np.nan
        nfp = int(self.fp.sum())
        badflat = bad.ravel()
        out = []
        for hh, mean, lo_max, hi_idx, hi_val, a_lo, a_hi in self.series:
            mx = lo_max
            if hi_idx.size:
                keep = ~badflat[hi_idx]
                if keep.any():
                    v = float(np.max(hi_val[keep]))
                    mx = v if mx is None else max(mx, v)
                if mean is not None and nfp > 0 and (~keep).any():
                    mean = (mean * nfp - float(np.sum(hi_val[~keep]))) / max(nfp - int((~keep).sum()), 1)
            am = a_lo
            if hi_idx.size and a_hi.size:
                keep = ~badflat[hi_idx]
                if keep.any() and not np.all(np.isnan(a_hi[keep])):
                    v = float(np.nanmax(a_hi[keep]))
                    am = v if am is None else max(am, v)
            out.append([hh, r1(mean), r1(mx), r1(am)])
        self.series = out

    def result(self):
        """The per episode JSON record: footprint, county union, every HUC8, hourly series."""
        self.finalize_artifacts()
        fp = self.stats_for(self.fp)
        for d in ROLL:
            # hour of the footprint maximum, artifact cells already NaN in self.m
            v = np.where(self.fp, self.m[d], np.nan)
            t = None
            if not np.all(np.isnan(v)):
                k = int(self.m_h[d].flat[int(np.nanargmax(v))])
                t = self.hours_seen[k] if 0 <= k < len(self.hours_seen) else None
            fp[f"max{d}"]["t"] = t.strftime("%Y-%m-%d %H") if t else None
        cty = self.stats_for(self.cty)
        hucs = []
        for i, r in enumerate(self.hucs.itertuples(index=False)):
            m = self.label == (i + 1)
            st = self.stats_for(m)
            meta = self.huc_meta.get(r.huc8, {})
            st.update({"h": r.huc8, "n": meta.get("name"), "st": meta.get("states"),
                       "huc_km2": meta.get("area_km2"), "inter_km2": float(r.inter_km2),
                       "frac": float(r.frac_huc8), "nev": int(r.n_events), "nlsr": int(r.n_lsr)})
            hucs.append(st)
        row = self.row
        return {
            "id": self.id, "t0": row.begin_utc, "t1": row.end_utc,
            "hours": self.n_ep, "states": row.states,
            "nev": int(row.n_events), "nlsr": (int(row.n_lsr) if pd.notna(row.n_lsr) else -1),
            "deaths": int(row.fatalities), "inj": int(row.injuries), "dmg": int(row.damage_usd),
            "qpe": {"products": self.qpe_products, "missing": self.qpe_missing,
                    "artifact_cells": self.n_bad, "artifact_cells_in_footprint": self.bad_fp,
                    "artifact_rule": f"cell above {HOT_MM:.0f} mm/h in {HOT_N}+ hours read or above "
                                     f"{HOT2_MM:.0f} mm/h in {HOT2_N}+ hours: removed from all statistics",
                    "definition": "MRMS hourly QPE, gauge corrected Pass 2 where available; "
                                  "rolling 1, 3, 6 h windows ending t0 .. t1 + 1 h"},
            "ari": {"sources": {str(d): self.ari_src[d] for d in ROLL},
                    "missing": {str(d): self.ari_missing[d] for d in ROLL},
                    "dry_hours_not_sampled": self.ari_skipped // len(ROLL),
                    "thresholds": ARI_THRESH,
                    "definition": "FLASH QPE_ARI 1H, 3H, 6H at the top of each hour t0 .. t1 + 1 h, "
                                  "pixel maximum over the episode; years, capped at 200"},
            "grid": {"r0": self.r0, "c0": self.c0, "rows": int(self.r1 - self.r0),
                     "cols": int(self.c1 - self.c0), "dll": DLL},
            "fp": fp, "cty": cty, "huc8": hucs, "series": self.series,
        }


def r1(x):
    """Round to one decimal; None for None or NaN."""
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 1)


def r3(x):
    """Round to three decimals."""
    return round(float(x), 3)


# ----------------------------------------------------------------------------- main sweep
def load_js_geojson(path):
    """Parse a 'var X = {...};' site payload file as GeoJSON."""
    txt = path.read_text()
    return json.loads(txt[txt.index("{"):].rstrip().rstrip(";"))


def main():
    """Run the resumable hour by hour sweep for the selected episodes."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", type=int, nargs="*", help="episode ids")
    ap.add_argument("--states", nargs="*", help="two letter state codes (episode touches any)")
    ap.add_argument("--min-events", type=int, default=0)
    ap.add_argument("--start", help="episodes beginning on/after YYYY-MM-DD")
    ap.add_argument("--end", help="episodes beginning before YYYY-MM-DD")
    ap.add_argument("--limit", type=int, default=0, help="stop after N episodes (in time order)")
    ap.add_argument("--workers", type=int, default=3, help="download+decode threads")
    ap.add_argument("--prefetch", type=int, default=6, help="hours in flight")
    ap.add_argument("--force", action="store_true", help="recompute existing JSON")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    logf = open(EP_DIR / "mrms_run.log", "a")
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
    if not args.force:
        ep = ep[~ep.episode_id.apply(lambda i: (OUT_DIR / f"{i}.json").exists())]
    ep = ep.sort_values(["t0dt", "episode_id"])
    if args.limit:
        ep = ep.head(args.limit)
    if ep.empty:
        print("nothing to do")
        return
    print(f"{len(ep)} episodes to compute", flush=True)

    eh = pd.read_csv(EP_DIR / "episode_huc8.csv", dtype={"huc8": str})
    eh = eh[eh.kept == 1]
    lookup = pd.read_csv(EP_DIR / "huc8_lookup.csv", dtype={"huc8": str}).set_index("huc8")
    huc_meta = {h: {"name": r["name"], "states": r["states"], "area_km2": float(r["area_km2"])}
                for h, r in lookup.iterrows()}

    from shapely.geometry import shape
    t = time.time()
    need = set(eh[eh.episode_id.isin(ep.episode_id)].huc8)
    huc_geoms = {}
    for f in json.load(open(GEO))["features"]:
        h = str(f["properties"]["huc8"]).zfill(8)
        if h in need:
            huc_geoms[h] = shape(f["geometry"]).buffer(0)
    cty_geoms = {}
    need_f = {x for s in ep.county_fips for x in str(s).split(";")}
    for f in load_js_geojson(COUNTIES_JS)["features"]:
        if f["properties"]["f"] in need_f:
            cty_geoms[f["properties"]["f"]] = shape(f["geometry"]).buffer(0)
    print(f"geometry loaded: {len(huc_geoms)} HUC8, {len(cty_geoms)} counties, {time.time()-t:.0f}s", flush=True)

    # hour -> episode ids; episodes are built lazily at their first hour
    by_hour = {}
    rows = {}
    for r in ep.itertuples(index=False):
        rows[int(r.episode_id)] = r
        t0 = datetime.strptime(r.begin_utc, "%Y-%m-%d %H")
        t1 = datetime.strptime(r.end_utc, "%Y-%m-%d %H")
        for h in hours_of(t0, t1):
            by_hour.setdefault(h, []).append(int(r.episode_id))
    hours = sorted(by_hour)
    print(f"{len(hours)} unique hours to read", flush=True)

    fetcher = Fetcher(log)
    active = {}
    done = 0
    t_start = time.time()

    eh_groups = {int(k): g for k, g in eh.groupby("episode_id")}

    def build(eid):
        r = rows[eid]
        hucs = eh_groups.get(eid)
        if hucs is None or hucs.empty:
            log(f"episode {eid}: no HUC8, skipped")
            return None
        return Episode(r, hucs, huc_geoms, huc_meta, cty_geoms)

    def work(h, windows, ari_windows):
        """Fetch + decode the hour, return crops per episode id.

        windows: {eid: (r0, r1, c0, c1)} for every active episode (QPE is always read);
        ari_windows: {eid: first} for the episodes whose statistics window contains h, with
        first = True at their first window hour. ARI files are skipped when no episode is
        in its window, and when none is at its first window hour and the Pass 2 field has no
        rain in any of their footprints (the FLASH ARI cannot exceed the previous hour then).
        """
        out = {"h": h, "qpe": {}, "ari": {d: {} for d in ROLL}, "qpe_label": None, "ari_src": {},
               "ari_skipped": False}
        try:
            return _work(h, windows, ari_windows, out)
        except Exception as e:  # noqa: BLE001  one bad hour must not stop a 20,000 hour sweep
            log(f"{h:%Y-%m-%d %H} hour failed: {type(e).__name__}: {e}")
            return out

    def _work(h, windows, ari_windows, out):
        q, label = fetcher.qpe(h)
        out["qpe_label"] = label
        if q is not None:
            raw, R, E, D = q
            for key, (r0, r1_, c0, c1) in windows.items():
                v = to_values(raw[r0:r1_, c0:c1], R, E, D)
                v[v < 0] = np.nan
                out["qpe"][key] = v
        if not ari_windows:
            return out
        if q is not None and not any(ari_windows.values()):
            wet = any(np.nanmax(out["qpe"][k]) > 0 for k in ari_windows if k in out["qpe"]
                      and out["qpe"][k].size and not np.all(np.isnan(out["qpe"][k])))
            if not wet:
                out["ari_skipped"] = True
                return out
        for d in ROLL:
            a, src = fetcher.ari(h, d)
            out["ari_src"][d] = src
            if a is None:
                continue
            raw, R, E, D = a
            for key in ari_windows:
                r0, r1_, c0, c1 = windows[key]
                v = to_values(raw[r0:r1_, c0:c1], R, E, D)
                v[v < 0] = np.nan
                out["ari"][d][key] = v
        return out

    pool = ThreadPoolExecutor(max_workers=args.workers)
    pending = deque()
    i = 0
    last_print = [0.0]

    def submit(i):
        h = hours[i]
        windows = {}
        for eid in list(by_hour[h]):
            if eid not in active:
                e = build(eid)
                if e is None:
                    by_hour[h].remove(eid)
                    continue
                active[eid] = e
            windows[eid] = active[eid].window()
        ari_windows = {eid: (h == active[eid].t0) for eid in windows if active[eid].t0 <= h <= active[eid].h_last}
        pending.append(pool.submit(work, h, windows, ari_windows))

    while i < len(hours) and len(pending) < args.prefetch:
        submit(i)
        i += 1
    while pending:
        res = pending.popleft().result()
        h = res["h"]
        for eid in list(by_hour[h]):
            e = active.get(eid)
            if e is None:
                continue
            e.take(h, res["qpe"].get(eid), res["qpe_label"], {d: res["ari"][d].get(eid) for d in ROLL},
                   res["ari_src"], res["ari_skipped"])
            if h == e.h_last:
                out = e.result()
                json.dump(out, open(OUT_DIR / f"{eid}.json", "w"), separators=(",", ":"))
                del active[eid]
                done += 1
        if i < len(hours):
            submit(i)
            i += 1
        el = time.time() - t_start
        if time.time() - last_print[0] > 30 or h == hours[-1]:
            last_print[0] = time.time()
            print(f"{h:%Y-%m-%d %H} | hour {hours.index(h)+1}/{len(hours)} | episodes done "
                  f"{done}/{len(ep)} | active {len(active)} | qpe {res['qpe_label']} | "
                  f"{el/60:.1f} min", flush=True)
        log(f"{h:%Y-%m-%d %H} qpe={res['qpe_label']} ari={res['ari_src'] or ('skipped' if res['ari_skipped'] else 'not needed')} n_ep={len(by_hour[h])}")
    pool.shutdown()
    print(f"finished: {done} episodes in {(time.time()-t_start)/60:.1f} min")


if __name__ == "__main__":
    main()
