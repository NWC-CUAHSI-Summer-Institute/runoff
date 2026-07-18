#!/usr/bin/env python3

'''
Dependency file for event_extraction_pipeline.py
'''

from __future__ import annotations
import argparse
import warnings
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

CFS_TO_ACREFT_PER_S = 1.0 / 43560.0     # 1 cubic foot = 1/43560 acre-foot
SITE_COL_CANDIDATES = ["site_code", "site_no", "station_id", "staid", "site", "siteid"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _trapz(y, x) -> float:
    """Trapezoidal integral, independent of NumPy version (np.trapz was renamed
    np.trapezoid in NumPy 2.0 and may be absent), so we integrate explicitly."""
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    if y.size < 2:
        return 0.0
    return float(np.sum((y[1:] + y[:-1]) * 0.5 * np.diff(x)))


def normalize_site(value) -> str:
    """USGS site numbers are zero-padded strings; restore an 8-digit minimum."""
    s = str(value).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s.zfill(8)


def read_station_csv(path: str | Path) -> list[str]:
    """Read site code(s) from a CSV. Accepts a column named site_code/site_no/
    station_id/staid/site (case-insensitive); otherwise uses the first column."""
    df = pd.read_csv(path, dtype=str, comment="#")
    df.columns = [c.strip().lower() for c in df.columns]
    col = next((c for c in SITE_COL_CANDIDATES if c in df.columns), df.columns[0])
    return [normalize_site(v) for v in df[col].dropna()]


def water_year(ts) -> int:
    ts = pd.Timestamp(ts)
    return ts.year + 1 if ts.month >= 10 else ts.year


# ---------------------------------------------------------------------------
# 1. retrieve discharge for the water years (chunked = robust)
# ---------------------------------------------------------------------------
def load_usgs_discharge(site: str, wy_start: int = 2021, wy_end: int = 2025,
                        param: str = "00060", freq: str = "15min") -> pd.Series:
    """Instantaneous discharge (cfs) for WY wy_start..wy_end, UTC, resampled to
    `freq`. Pulled one water year at a time so large multi-year requests are
    reliable. Returns a Series named 'value' on a tz-aware UTC index."""
    from dataretrieval import nwis
    frames = []
    for wy in range(wy_start, wy_end + 1):
        start = f"{wy-1}-10-01T00:00Z"
        end = f"{wy}-09-30T23:59Z"
        df, _ = nwis.get_iv(sites=site, parameterCd=param, start=start, end=end)
        if df is not None and len(df) and param in df.columns:
            frames.append(df[param].astype(float))
    if not frames:
        raise ValueError(f"No {param} data for site {site} in WY{wy_start}-{wy_end}")
    q = pd.concat(frames)
    q.index = q.index.tz_convert("UTC")
    q = q[~q.index.duplicated(keep="first")].sort_index()
    q = q.resample(freq).first().ffill().rename("value")
    return q


# ---------------------------------------------------------------------------
# 2. hydrograph separation (NOAA-OWP hydrotools event detection)
# ---------------------------------------------------------------------------
def detect_events(series: pd.Series, halflife="6h", window="7D",
                  minimum_event_duration="6h", start_radius="7h") -> pd.DataFrame:
    """Separate a continuous hydrograph into events. Returns start/end plus
    peak (cfs) and t_peak per event."""
    from hydrotools.events.event_detection import decomposition as ev
    events = ev.list_events(series, halflife=halflife, window=window,
                            minimum_event_duration=minimum_event_duration,
                            start_radius=start_radius)
    events["peak"] = events.apply(lambda e: series.loc[e.start:e.end].max(), axis=1)
    events["t_peak"] = events.apply(lambda e: series.loc[e.start:e.end].idxmax(), axis=1)
    return events.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 3. per-event metrics
# ---------------------------------------------------------------------------
def volume_acreft(series: pd.Series, start, end) -> float:
    """Runoff volume (acre-ft) = integral of discharge (cfs) over [start, end]."""
    s = series.loc[start:end].dropna()
    if len(s) < 2:
        return 0.0
    secs = (s.index - s.index[0]).total_seconds().to_numpy()
    return _trapz(s.to_numpy(), secs) * CFS_TO_ACREFT_PER_S


def flashiness_index(series: pd.Series, start, end) -> float:
    """Richards-Baker Flashiness Index over [start, end]: sum(|dQ|)/sum(Q)."""
    s = series.loc[start:end].dropna().to_numpy()
    denom = s.sum()
    return float(np.abs(np.diff(s)).sum() / denom) if denom > 0 else np.nan


def build_event_table(series: pd.Series, events: pd.DataFrame, staid: str) -> pd.DataFrame:
    """One row per event with the requested columns (time fields are UTC)."""
    rows = []

    for e in events.itertuples():
        start = pd.Timestamp(e.start)
        end = pd.Timestamp(e.end)
        peak_time = pd.Timestamp(e.t_peak)

        rows.append({
            "STAID": staid,
            "BEGIN_DATE_TIME": start,
            "END_DATE_TIME": end,
            "peak_time": peak_time,
            "peak_flow_cfs": float(e.peak),
            "volume_acreft": volume_acreft(series, start, end),
            "flashiness_index": flashiness_index(series, start, end),
            "YEAR": start.year,
            "month": start.month,
            "day": start.day,
            "water_year": water_year(start),
            "duration_hours": (end - start).total_seconds() / 3600.0,
            "time_to_peak_h": (peak_time - start).total_seconds() / 3600.0,
        })

    cols = [
       "STAID", "BEGIN_DATE_TIME", "END_DATE_TIME", "peak_time", "peak_flow_cfs",
        "volume_acreft", "flashiness_index", "YEAR", "month", "day",
        "water_year", "duration_hours", "time_to_peak_h"
    ]

    return pd.DataFrame(rows, columns=cols)

# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------
def run(stations_csv="station.csv", outdir=".", wy_start=2021, wy_end=2025,
        **detect_kwargs) -> dict[str, pd.DataFrame]:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    sites = read_station_csv(stations_csv)
    print(f"{len(sites)} site(s): {', '.join(sites)} | water years {wy_start}-{wy_end}")
    results = {}
    for site in sites:
        print(f"\n[{site}] retrieving WY{wy_start}-{wy_end} ...")
        q = load_usgs_discharge(site, wy_start, wy_end)
        print(f"[{site}] {len(q):,} hourly steps, peak {q.max():,.0f} cfs; detecting events ...")
        events = detect_events(q, **detect_kwargs)
        table = build_event_table(q, events)
        out = outdir / f"{site}_events_WY{wy_start}_{wy_end}.csv"
        table.to_csv(out, index=False)
        print(f"[{site}] {len(table)} events -> {out.name}")
        results[site] = table
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stations", default="station.csv", help="CSV with a site code column")
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--wy-start", type=int, default=2021)
    ap.add_argument("--wy-end", type=int, default=2025)
    a = ap.parse_args()
    run(a.stations, a.outdir, a.wy_start, a.wy_end)


if __name__ == "__main__":
    main()
