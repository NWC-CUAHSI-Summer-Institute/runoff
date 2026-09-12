"""Fetch the USGS Watershed Boundary Dataset HUC8 layer as GeoJSON for local geoprocessing.

Source: The National Map WBD map service, layer 4 (8-digit hydrologic units),
https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer/4

The polygons are pulled with a light generalisation (maxAllowableOffset 0.0005 deg,
about 50 m) and 4-decimal coordinates, which is far below the 0.01 deg MRMS cell.
Output: data/geo/huc8_wbd.geojson (about 60 MB, gitignored). Nothing on the website
uses this file; the site keeps its own simplified assets/data/huc8.js for display.

    python engine/episodes/fetch_huc8_wbd.py
"""
import json
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parents[2]
OUT = HERE / "data" / "geo" / "huc8_wbd.geojson"
URL = "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer/4/query"
PAGE = 200


def main():
    """Page through the WBD HUC8 map service and write one GeoJSON file."""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    feats, off = [], 0
    while True:
        params = {"where": "1=1", "outFields": "huc8,name,areasqkm,states", "outSR": 4326,
                  "f": "geojson", "resultOffset": off, "resultRecordCount": PAGE,
                  "geometryPrecision": 4, "maxAllowableOffset": 0.0005}
        for attempt in range(6):
            try:
                r = requests.get(URL, params=params, timeout=300)
                r.raise_for_status()
                d = r.json()
                break
            except Exception as e:  # noqa: BLE001
                print(f"retry offset {off}: {e}", flush=True)
                time.sleep(5 * (attempt + 1))
        else:
            sys.exit(f"gave up at offset {off}")
        fs = d.get("features", [])
        feats.extend(fs)
        print(f"{off:6d} +{len(fs)} -> {len(feats)}", flush=True)
        if len(fs) < PAGE:
            break
        off += PAGE
    json.dump({"type": "FeatureCollection", "features": feats}, open(OUT, "w"))
    print(f"wrote {OUT} ({len(feats)} HUC8 polygons, {OUT.stat().st_size/1e6:.1f} MB)")


if __name__ == "__main__":
    main()
