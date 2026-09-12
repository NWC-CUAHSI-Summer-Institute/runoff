"""HUC8 footprint of every episode: which 8-digit watersheds the storm episode touched.

Rule (from the project brief): any HUC8 touched by the episode footprint belongs to the
episode. The episode footprint is the union of the counties that reported flash flooding
in the episode (Storm Events county FIPS list). A HUC8 is kept when its intersection with
that county union is at least 1 percent of the HUC8 area or at least 10 km2, or when a
storm event or a Local Storm Report of the episode falls inside it. The small threshold
only removes boundary slivers created by the simplified county polygons.

Inputs
    data/episodes/episodes.csv          master episode table (build_catalog.py)
    data/episodes/episode_events.csv    events with coordinates
    data/episodes/episode_lsrs.csv      matched LSRs with coordinates
    data/geo/huc8_wbd.geojson           USGS WBD HUC8 (fetch_huc8_wbd.py)
    assets/data/counties_geo.js         county polygons of the site (NWS c_16ap26, simplified)

Outputs
    data/episodes/episode_huc8.csv      one row per (episode, HUC8) with intersection area,
                                        fraction of the HUC8 and of the county union, event
                                        and LSR counts inside the HUC8, and the kept flag
    data/episodes/huc8_lookup.csv       one row per HUC8: name, states, area km2, bbox

    python engine/episodes/build_huc8_footprints.py
"""
import json
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point
from shapely.ops import unary_union

HERE = Path(__file__).resolve().parents[2]
EP_DIR = HERE / "data" / "episodes"
GEO = HERE / "data" / "geo" / "huc8_wbd.geojson"
COUNTIES_JS = HERE / "assets" / "data" / "counties_geo.js"
EQUAL_AREA = "EPSG:5070"      # CONUS Albers, km2 after /1e6
MIN_FRAC = 0.01               # of the HUC8 area
MIN_KM2 = 10.0
CONUS_HUC2 = {f"{i:02d}" for i in range(1, 19)}


def load_js_geojson(path, var):
    """Parse a 'var X = {...};' site payload file as GeoJSON (var is informational)."""
    txt = path.read_text()
    txt = txt[txt.index("{"):].rstrip().rstrip(";")
    return json.loads(txt)


def main():
    """Intersect every episode's county union with the WBD HUC8 layer and write the tables."""
    t0 = time.time()
    huc = gpd.read_file(GEO)
    huc["huc8"] = huc["huc8"].astype(str).str.zfill(8)
    huc = huc[huc.huc8.str[:2].isin(CONUS_HUC2)].copy()
    huc = huc[huc.geometry.notna() & ~huc.geometry.is_empty].copy()
    huc["geometry"] = huc.geometry.buffer(0)
    huc = huc.set_crs("EPSG:4326", allow_override=True)
    huc_ea = huc.to_crs(EQUAL_AREA)
    huc_ea["geometry"] = huc_ea.geometry.make_valid()
    huc["area_km2"] = huc_ea.geometry.area / 1e6
    huc = huc.reset_index(drop=True)
    huc_ea = huc_ea.reset_index(drop=True)
    sidx = huc.sindex
    print(f"{len(huc)} CONUS HUC8 polygons, {time.time()-t0:.0f}s", flush=True)

    b = huc.bounds
    lookup = pd.DataFrame({
        "huc8": huc.huc8, "name": huc["name"], "states": huc["states"],
        "area_km2": huc.area_km2.round(1),
        "min_lon": b.minx.round(4), "min_lat": b.miny.round(4),
        "max_lon": b.maxx.round(4), "max_lat": b.maxy.round(4)})
    lookup.sort_values("huc8").to_csv(EP_DIR / "huc8_lookup.csv", index=False)

    cty = gpd.GeoDataFrame.from_features(load_js_geojson(COUNTIES_JS, "COUNTYGJ")["features"],
                                         crs="EPSG:4326")
    cty["f"] = cty["f"].astype(str).str.zfill(5)
    cty["geometry"] = cty.geometry.buffer(0)
    cty_geom = dict(zip(cty.f, cty.geometry))
    print(f"{len(cty)} counties", flush=True)

    ep = pd.read_csv(EP_DIR / "episodes.csv", dtype={"county_fips": str})
    ev = pd.read_csv(EP_DIR / "episode_events.csv")
    ls = pd.read_csv(EP_DIR / "episode_lsrs.csv")

    def pts_to_huc(df):
        """Map point rows to the HUC8 that contains them (index into huc)."""
        if len(df) == 0:
            return pd.Series([], dtype=object)
        g = gpd.GeoSeries([Point(x, y) for x, y in zip(df.lon, df.lat)], crs="EPSG:4326")
        hit = gpd.sjoin(gpd.GeoDataFrame(geometry=g), huc[["huc8", "geometry"]],
                        how="left", predicate="within")
        hit = hit[~hit.index.duplicated(keep="first")]
        return hit["huc8"].reindex(range(len(df))).values

    ev["huc8"] = pts_to_huc(ev)
    ls["huc8"] = pts_to_huc(ls)
    ev_counts = ev.groupby(["episode_id", "huc8"]).size()
    ls_counts = ls.groupby(["episode_id", "huc8"]).size()
    print(f"points mapped: {ev.huc8.notna().sum()}/{len(ev)} events, "
          f"{ls.huc8.notna().sum()}/{len(ls)} LSRs, {time.time()-t0:.0f}s", flush=True)

    rows = []
    missing_cty = set()
    for k, r in enumerate(ep.itertuples(index=False)):
        fips = [f for f in str(r.county_fips).split(";") if f]
        geoms = [cty_geom[f] for f in fips if f in cty_geom]
        missing_cty.update(f for f in fips if f not in cty_geom)
        if not geoms:
            continue
        u = unary_union(geoms)
        u_ea = gpd.GeoSeries([u], crs="EPSG:4326").to_crs(EQUAL_AREA).make_valid().iloc[0]
        u_km2 = u_ea.area / 1e6
        cand = sidx.query(u, predicate="intersects")
        eid = int(r.episode_id)
        evh = ev_counts.loc[eid] if eid in ev_counts.index.get_level_values(0) else None
        lsh = ls_counts.loc[eid] if eid in ls_counts.index.get_level_values(0) else None
        for i in cand:
            try:
                inter = huc_ea.geometry.iloc[i].intersection(u_ea)
            except Exception:  # noqa: BLE001  rare GEOS topology conflicts
                inter = huc_ea.geometry.iloc[i].buffer(0).intersection(u_ea.buffer(0))
            km2 = inter.area / 1e6 if not inter.is_empty else 0.0
            if km2 <= 0:
                continue
            h = huc.huc8.iloc[i]
            frac = km2 / huc.area_km2.iloc[i]
            n_ev = int(evh.get(h, 0)) if evh is not None else 0
            n_lsr = int(lsh.get(h, 0)) if lsh is not None else 0
            keep = (frac >= MIN_FRAC) or (km2 >= MIN_KM2) or (n_ev + n_lsr > 0)
            rows.append((eid, h, round(km2, 1), round(frac, 4), round(km2 / u_km2, 4),
                         n_ev, n_lsr, int(keep)))
        if k % 500 == 0:
            print(f"  {k}/{len(ep)} episodes, {time.time()-t0:.0f}s", flush=True)

    out = pd.DataFrame(rows, columns=["episode_id", "huc8", "inter_km2", "frac_huc8",
                                      "frac_footprint", "n_events", "n_lsr", "kept"])
    out.to_csv(EP_DIR / "episode_huc8.csv", index=False)
    kept = out[out.kept == 1]
    per = kept.groupby("episode_id").size()
    print(f"wrote {EP_DIR/'episode_huc8.csv'}: {len(out)} rows, {len(kept)} kept, "
          f"{per.index.nunique()} episodes, HUC8 per episode median {per.median():.0f} "
          f"max {per.max()}; {len(missing_cty)} county FIPS without geometry {sorted(missing_cty)[:10]}")


if __name__ == "__main__":
    main()
