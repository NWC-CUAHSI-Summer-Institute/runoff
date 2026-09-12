#!/usr/bin/env python3
"""
fetch_lsrs.py - NWS Local Storm Reports for the RUNOFF episode catalog.

LSRs are the real-time reports NWS offices relay from spotters, emergency
managers, law enforcement, and the public: a point, a UTC time, a type
(FLASH FLOOD, FLOOD, DEBRIS FLOW, HEAVY RAIN, ...), a source, and a remark.
They complement the Storm Events Database: Storm Events is the curated
post-event record organized into episodes and events; LSRs are the raw
ground-truth points, and many flash flood impacts appear only here.

Source: Iowa Environmental Mesonet (IEM) archive API,
  https://mesonet.agron.iastate.edu/cgi-bin/request/gis/lsr.py
  params: state=XX  sts=YYYY-MM-DDTHH:MM:SSZ  ets=...  fmt=csv
Timestamps in and out are UTC. The CSV has unescaped commas inside REMARK on
some rows, so it is parsed with QUOTE_NONE and bad lines are skipped (the
same handling the LSRs tutorial notebook settled on).

Fetches are chunked per state per water year and cached to one CSV each, so
the pull is resumable: rerun and only missing chunks are requested.

Usage:
  python engine/lsr/fetch_lsrs.py --wy 2021 2025                 # all CONUS
  python engine/lsr/fetch_lsrs.py --wy 2021 2025 --states TX OK
  python engine/lsr/fetch_lsrs.py --start 2025-07-01 --end 2025-07-10 --states TX

Output: data/lsr/lsr_<STATE>_wy<YYYY>.csv (or lsr_<STATE>_<start>_<end>.csv),
columns as served by IEM: VALID, VALID2, LAT, LON, MAG, WFO, TYPECODE,
TYPETEXT, CITY, COUNTY, STATE, SOURCE, REMARK, UGC, UGCNAME, QUALIFIER.
All report types are kept on disk; type filtering happens downstream in
engine/episodes/build_catalog.py so the archive never has to be re-pulled.
"""
from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

import pandas as pd
import requests

API = "https://mesonet.agron.iastate.edu/cgi-bin/request/gis/lsr.py"

CONUS = ["AL","AZ","AR","CA","CO","CT","DE","DC","FL","GA","ID","IL","IN","IA",
         "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH",
         "NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX",
         "UT","VT","VA","WA","WV","WI","WY"]

HERE = Path(__file__).resolve().parents[2]          # repository root
OUT_DIR = HERE / "data" / "lsr"


def fetch_chunk(state: str, sts: str, ets: str, out_path: Path,
                retries: int = 3, insecure: bool = False) -> int:
    """One state x window pull -> CSV on disk. Returns row count (-1 = failed)."""
    if out_path.exists():
        return -2  # cached
    params = {"state": state, "sts": sts, "ets": ets, "fmt": "csv"}
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(API, params=params, timeout=120, verify=not insecure)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            text = r.text
            if len(text.strip()) < 60:          # header only or empty
                out_path.write_text("", encoding="utf-8")
                return 0
            df = pd.read_csv(io.StringIO(text), quoting=3, on_bad_lines="skip",
                             na_values=["", "null", "None"], dtype=str)
            df = df.dropna(subset=["LAT", "LON", "VALID"])
            df = df.drop_duplicates(subset=["VALID", "LAT", "LON", "TYPETEXT", "WFO"])
            df.to_csv(out_path, index=False, encoding="utf-8")
            return len(df)
        except Exception as e:                                    # noqa: BLE001
            if attempt == retries:
                print(f"  {state} {sts[:10]}..{ets[:10]} FAILED: {e}")
                return -1
            time.sleep(2 * attempt)
    return -1


def main() -> None:
    """Chunked, resumable LSR pull from the IEM archive."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wy", nargs=2, type=int, metavar=("FIRST", "LAST"),
                    help="water year range, e.g. --wy 2021 2025 "
                         "(WY y = Oct 1 of y-1 through Sep 30 of y)")
    ap.add_argument("--start", help="explicit start date YYYY-MM-DD (UTC)")
    ap.add_argument("--end", help="explicit end date YYYY-MM-DD (UTC, inclusive)")
    ap.add_argument("--states", nargs="*", default=None,
                    help="two-letter state codes; default all CONUS + DC")
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS verification (some managed networks intercept it)")
    args = ap.parse_args()

    states = [s.upper() for s in (args.states or CONUS)]
    args.out.mkdir(parents=True, exist_ok=True)

    chunks = []  # (state, sts, ets, filename)
    if args.wy:
        for wy in range(args.wy[0], args.wy[1] + 1):
            sts = f"{wy-1}-10-01T00:00:00Z"
            ets = f"{wy}-09-30T23:59:59Z"
            for st in states:
                chunks.append((st, sts, ets, f"lsr_{st}_wy{wy}.csv"))
    elif args.start and args.end:
        sts = f"{args.start}T00:00:00Z"
        ets = f"{args.end}T23:59:59Z"
        for st in states:
            chunks.append((st, sts, ets, f"lsr_{st}_{args.start}_{args.end}.csv"))
    else:
        ap.error("give either --wy FIRST LAST or --start/--end")

    print(f"{len(chunks)} chunk(s) -> {args.out}")
    got = cached = failed = 0
    for i, (st, sts, ets, name) in enumerate(chunks, 1):
        n = fetch_chunk(st, sts, ets, args.out / name, insecure=args.insecure)
        if n == -2:
            cached += 1
        elif n == -1:
            failed += 1
        else:
            got += 1
            time.sleep(0.5)                       # be polite to the IEM
        if i % 25 == 0 or i == len(chunks):
            print(f"  {i}/{len(chunks)}  fetched={got} cached={cached} failed={failed}")

    if failed:
        print(f"\n{failed} chunk(s) failed; rerun this command to retry just those.")
        sys.exit(1)
    print("\nDONE. Next: python engine/episodes/build_catalog.py "
          "--stormevents <dir> --lsr-dir data/lsr")


if __name__ == "__main__":
    main()
