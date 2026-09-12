#!/usr/bin/env python3
"""
prep_geo.py - simplified boundary payloads for the map pages.

Writes three script-tag GeoJSON files under assets/data/:
  counties_geo.js  var COUNTYGJ, CONUS counties dissolved by FIPS (props f, n)
  states_geo.js    var STATESGJ, state outlines (prop s)
  huc8_geo.js      var HUC8GJ, HUC8 watersheds (props h, n)

Inputs: the NWS AWIPS county shapefile (c_16ap26 or later) and the USGS WBD
HUC8 shapefile. Coordinates are rounded to 3 decimals (2 for states) after a
topology preserving simplification, which keeps each payload to a few MB.

    python engine/records/prep_geo.py --counties-shp data/geo/c_16ap26.shp --huc8-shp data/geo/HUC8_US.shp
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import geopandas as gpd

HERE = Path(__file__).resolve().parents[2]
CONUS_AB = {"AL", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "ID", "IL", "IN",
            "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE",
            "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
            "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"}


def emit(gdf: gpd.GeoDataFrame, props: dict, var: str, path: Path, nd: int = 3) -> None:
    """Write gdf as 'var <var>=<FeatureCollection>;' with rounded coordinates."""
    def rnd(c):
        if isinstance(c[0], (int, float)):
            return [round(c[0], nd), round(c[1], nd)]
        return [rnd(x) for x in c]

    feats = []
    for _, r in gdf.iterrows():
        g = json.loads(json.dumps(r.geometry.__geo_interface__))
        g["coordinates"] = rnd(g["coordinates"])
        feats.append({"type": "Feature",
                      "properties": {k: r[v] for k, v in props.items()},
                      "geometry": g})
    js = "var " + var + "=" + json.dumps({"type": "FeatureCollection", "features": feats},
                                        separators=(",", ":")) + ";"
    path.write_text(js, encoding="utf-8")
    print(f"{path.name}: {path.stat().st_size / 1e6:.2f} MB, {len(feats)} features")


def main() -> None:
    """Build the county, state and HUC8 boundary payloads."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--counties-shp", type=Path, required=True)
    ap.add_argument("--huc8-shp", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=HERE / "assets" / "data")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    c = gpd.read_file(args.counties_shp)
    c = c[c.STATE.isin(CONUS_AB)].copy()
    c["geoid"] = c.FIPS.astype(str).str.zfill(5)
    c = c.dissolve(by="geoid", aggfunc={"COUNTYNAME": "first", "STATE": "first"}).reset_index()
    c["nm"] = c.COUNTYNAME.str.title() + ", " + c.STATE
    c["geometry"] = c.geometry.simplify(0.004, preserve_topology=True)
    c = c[~c.geometry.is_empty & c.geometry.notna()]
    emit(c, {"f": "geoid", "n": "nm"}, "COUNTYGJ", args.out / "counties_geo.js")

    s = c.dissolve(by="STATE").reset_index()
    s["geometry"] = s.geometry.simplify(0.02, preserve_topology=True)
    emit(s, {"s": "STATE"}, "STATESGJ", args.out / "states_geo.js", nd=2)

    h = gpd.read_file(args.huc8_shp)[["HUC8", "NAME", "geometry"]]
    h["HUC8"] = h.HUC8.astype(str).str.zfill(8)
    h["geometry"] = h.geometry.simplify(0.012, preserve_topology=True)
    h = h[~h.geometry.is_empty & h.geometry.notna()]
    emit(h, {"h": "HUC8", "n": "NAME"}, "HUC8GJ", args.out / "huc8_geo.js")


if __name__ == "__main__":
    main()
