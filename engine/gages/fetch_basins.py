#!/usr/bin/env python3
"""
fetch_basins.py - USGS gages of the RUNOFF set: station names and upstream basins.

For every RUNOFF gage (the GAGES-II sites under 1000 km2 carried in
assets/data/data.js) this pulls the official station name from the NWIS site
service and the upstream drainage basin polygon from the USGS NLDI, then writes:

    data/gages/gages.csv            id, name, lat, lon, area_km2, huc8, n_events, basin
    assets/data/gages.js            var GAGES2 = {id: [lat, lon, area_km2, name, huc8, basin]}
    assets/data/basin/<id>.json     one GeoJSON Feature per gage, simplified for the map

NLDI endpoint (one request per site, resumable, about 1-2 s each):
    https://api.water.usgs.gov/nldi/linked-data/nwissite/USGS-<id>/basin?f=json
Sites the NLDI does not know (discontinued or not on the NHDPlus network) are kept
in the table with basin = 0 and no polygon.

    python engine/gages/fetch_basins.py              # everything, resumable
    python engine/gages/fetch_basins.py --workers 4  # more parallel NLDI requests
    python engine/gages/fetch_basins.py --no-basins  # names and table only
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests
from shapely.geometry import mapping, shape

HERE = Path(__file__).resolve().parents[2]
DATA_JS = HERE / "assets" / "data" / "data.js"
OUT_CSV = HERE / "data" / "gages" / "gages.csv"
OUT_JS = HERE / "assets" / "data" / "gages.js"
BASIN_DIR = HERE / "assets" / "data" / "basin"
NWIS = "https://waterservices.usgs.gov/nwis/site/"
NLDI = "https://api.water.usgs.gov/nldi/linked-data/nwissite/USGS-{id}/basin?f=json"
UA = {"User-Agent": "RUNOFF gages (CUAHSI NWC Summer Institute 2026)"}
SIMPLIFY_DEG = 0.001          # about 100 m, far below the 0.01 deg MRMS cell

# The NLDI rate limits bursts (HTTP 429). All workers share one pacing clock and one
# pause: at most one request per --interval seconds, and a 429 stops everyone for
# Retry-After seconds (or an escalating 30 s, 60 s, ... up to 10 min).
_clock = threading.Lock()
_last_request = [0.0]
_pause_until = [0.0]
_pauses = [0]


def _wait_turn(min_interval: float) -> None:
    with _clock:
        now = time.time()
        wait = max(_last_request[0] + min_interval, _pause_until[0]) - now
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.time()


def _back_off(seconds: float) -> None:
    with _clock:
        _pause_until[0] = max(_pause_until[0], time.time() + seconds)
        _pauses[0] += 1


def load_stations() -> pd.DataFrame:
    """RUNOFF stations from the site payload: id, lat, lon, area, huc8, n_events."""
    txt = DATA_JS.read_text(encoding="utf-8")
    d = json.loads(txt[txt.index("{"):].rstrip().rstrip(";"))
    fi = {f: i for i, f in enumerate(d["fields"])}
    rows = [{"id": r[fi["staid"]], "lat": r[fi["lat"]], "lon": r[fi["lon"]],
             "area_km2": r[fi["area"]], "huc8": r[fi["huc8"]], "n_events": r[fi["n_events"]]}
            for r in d["stations"]]
    return pd.DataFrame(rows)


def nwis_names(ids: list[str], insecure: bool = False) -> dict:
    """Station names from the NWIS site service, 100 sites per request."""
    out = {}
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        params = {"format": "rdb", "sites": ",".join(chunk), "siteStatus": "all"}
        for attempt in range(3):
            try:
                r = requests.get(NWIS, params=params, headers=UA, timeout=60, verify=not insecure)
                r.raise_for_status()
                lines = [ln for ln in r.text.splitlines() if ln and not ln.startswith("#")]
                if len(lines) < 2:
                    break
                hdr = lines[0].split("\t")
                si, ni = hdr.index("site_no"), hdr.index("station_nm")
                for ln in lines[2:]:
                    p = ln.split("\t")
                    if len(p) > max(si, ni):
                        out[p[si].strip()] = p[ni].strip().title()
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    print(f"  NWIS chunk {i} failed: {e}")
                time.sleep(2)
        if (i // 100) % 10 == 0:
            print(f"  names: {min(i + 100, len(ids))}/{len(ids)}", flush=True)
    return out


def rnd(c, nd=4):
    """Round nested coordinate lists."""
    if isinstance(c[0], (int, float)):
        return [round(c[0], nd), round(c[1], nd)]
    return [rnd(x, nd) for x in c]


def fetch_basin(site_id: str, name: str, area_km2: float, insecure: bool = False,
                min_interval: float = 1.0) -> str:
    """One NLDI basin -> assets/data/basin/<id>.json. Returns ok | cached | none | failed."""
    out = BASIN_DIR / f"{site_id}.json"
    if out.exists():
        return "cached"
    url = NLDI.format(id=site_id)
    for attempt in range(8):
        try:
            _wait_turn(min_interval)
            r = requests.get(url, headers=UA, timeout=90, verify=not insecure)
            if r.status_code == 429:
                try:
                    pause = float(r.headers.get("Retry-After", 0))
                except ValueError:
                    pause = 0.0
                if pause <= 0:
                    pause = min(600.0, 30.0 * (2 ** attempt))
                print(f"  rate limited on {site_id}; pausing all requests {pause:.0f} s", flush=True)
                _back_off(pause)
                continue
            if r.status_code in (404, 400):
                return "none"
            r.raise_for_status()
            fc = r.json()
            feats = fc.get("features") or []
            if not feats or not feats[0].get("geometry"):
                return "none"
            geom = shape(feats[0]["geometry"]).buffer(0).simplify(SIMPLIFY_DEG, preserve_topology=True)
            if geom.is_empty:
                return "none"
            g = mapping(geom)
            g = {"type": g["type"], "coordinates": rnd(json.loads(json.dumps(g["coordinates"])))}
            feat = {"type": "Feature",
                    "properties": {"id": site_id, "name": name, "area_km2": area_km2},
                    "geometry": g}
            out.write_text(json.dumps(feat, separators=(",", ":")), encoding="utf-8")
            return "ok"
        except Exception as e:  # noqa: BLE001
            if attempt == 7:
                print(f"  {site_id} failed: {e}")
                return "failed"
            time.sleep(3 * (attempt + 1))
    print(f"  {site_id} failed: still rate limited after 8 attempts")
    return "failed"


def main() -> None:
    """Names for every RUNOFF gage, basins from the NLDI, table and site payload."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=2, help="parallel NLDI requests (pacing is shared)")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="minimum seconds between two NLDI requests, all workers together")
    ap.add_argument("--no-basins", action="store_true")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification")
    args = ap.parse_args()

    st = load_stations()
    print(f"{len(st)} RUNOFF gages")
    names = nwis_names(list(st.id), insecure=args.insecure)
    st["name"] = st.id.map(names).fillna("")
    print(f"names resolved: {int((st.name != '').sum())}")

    BASIN_DIR.mkdir(parents=True, exist_ok=True)
    status = {"ok": 0, "cached": 0, "none": 0, "failed": 0}
    if not args.no_basins:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(fetch_basin, r.id, r.name, float(r.area_km2), args.insecure,
                                args.interval): r.id
                    for r in st.itertuples(index=False)}
            for n, fut in enumerate(as_completed(futs), 1):
                status[fut.result()] += 1
                if n % 200 == 0 or n == len(futs):
                    print(f"  basins {n}/{len(futs)} {status}", flush=True)
    st["basin"] = st.id.map(lambda i: int((BASIN_DIR / f"{i}.json").exists()))

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    st[["id", "name", "lat", "lon", "area_km2", "huc8", "n_events", "basin"]] \
        .sort_values("id").to_csv(OUT_CSV, index=False)
    payload = {r.id: [round(float(r.lat), 4), round(float(r.lon), 4), round(float(r.area_km2), 1),
                      r.name, str(r.huc8), int(r.basin)]
               for r in st.itertuples(index=False)}
    OUT_JS.write_text("var GAGES2=" + json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + ";",
                      encoding="utf-8")
    print(f"wrote {OUT_CSV} and {OUT_JS} ({OUT_JS.stat().st_size / 1e6:.2f} MB); "
          f"basins on disk: {int(st.basin.sum())} of {len(st)}")
    if _pauses[0]:
        print(f"rate limit pauses honored: {_pauses[0]}")
    if status["failed"]:
        print(f"{status['failed']} basins failed; rerun to retry them (cached ones are skipped), "
              f"or slow down with --interval 2")
    print("next: python engine/episodes/build_mrms_payload.py  (adds gage counts to the catalog)")


if __name__ == "__main__":
    main()
