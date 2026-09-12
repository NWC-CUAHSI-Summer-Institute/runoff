#!/usr/bin/env python3
"""
build_mrms_payload.py - merge the precomputed MRMS statistics into the site catalogue payload.

Reads
    assets/data/episodes.js             base catalogue written by build_catalog.py (var EPCAT)
    data/episodes/episode_huc8.csv      HUC8 touched per episode (build_huc8_footprints.py)
    assets/data/ep/<id>.json            per episode statistics (precompute_mrms.py)

Writes
    assets/data/episodes.js             var EPCAT with the fields below (rewritten in place)

EPCAT.fields
    id, t0, t1, states, nev, nlsr, deaths, inj, dmg, fips, bbox    as before (build_catalog.py)
    hucs    list of HUC8 codes touched by the episode (footprint of the modelling path)
    km2     footprint area, km2 (union of the touched HUC8)
    m1, m3, m6   [max, mean] of the pixel maximum rolling 1, 3, 6 h accumulation over the
                 footprint, mm (max = highest cell, mean = area weighted mean of the cell maxima)
    rain    [ep_max, ep_mean, pre_mean] episode rain (windows ending t0 .. t1 + 1 h), mm, and
            the mean of the 5 h before t0
    a1, a3, a6   [max, cov...] maximum FLASH ARI in years (pixel maximum over the episode) and
                 the fraction of the footprint area at or above each threshold in
                 EPCAT.mrms.thresholds, in permille (0 to 1000)
    All MRMS fields are null for an episode not yet computed; hucs and km2 are always present.

    python engine/episodes/build_mrms_payload.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parents[2]
SITE_DATA = HERE / "assets" / "data"
EP_DIR = HERE / "data" / "episodes"
BASE_FIELDS = ["id", "t0", "t1", "states", "nev", "nlsr", "deaths", "inj", "dmg", "fips", "bbox"]
NEW_FIELDS = ["hucs", "km2", "m1", "m3", "m6", "rain", "a1", "a3", "a6"]
QPE_LABEL = "MRMS MultiSensor QPE 01H Pass 2 (gauge corrected); Pass 1 / radar only where Pass 2 is absent"
ARI_LABEL = "FLASH QPE_ARI 1H, 3H, 6H (average recurrence interval, years, capped at 200)"


def load_epcat():
    """Parse assets/data/episodes.js (var EPCAT = {...};) into a dict."""
    txt = (SITE_DATA / "episodes.js").read_text(encoding="utf-8")
    return json.loads(txt[txt.index("{"):].rstrip().rstrip(";"))


def build():
    """Rewrite episodes.js with the HUC8 footprint and MRMS summary fields per episode."""
    cat = load_epcat()
    fi = {f: i for i, f in enumerate(cat["fields"])}
    eh = pd.read_csv(EP_DIR / "episode_huc8.csv", dtype={"huc8": str})
    eh = eh[eh.kept == 1]
    hucs = {int(k): list(g.huc8) for k, g in eh.groupby("episode_id")}
    thresholds = None
    rows, n_done = [], 0
    for r in cat["rows"]:
        eid = int(r[fi["id"]])
        base = [r[fi[f]] for f in BASE_FIELDS]
        p = SITE_DATA / "ep" / f"{eid}.json"
        new = [hucs.get(eid, []), None, None, None, None, None, None, None, None]
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            fp = d.get("fp", {})
            if thresholds is None:
                thresholds = d.get("ari", {}).get("thresholds")
            new[1] = fp.get("area_km2")
            for k, key in ((2, "max1"), (3, "max3"), (4, "max6")):
                s = fp.get(key) or {}
                new[k] = [s.get("max"), s.get("mean")]
            rn = fp.get("rain") or {}
            new[5] = [(rn.get("ep") or {}).get("max"), (rn.get("ep") or {}).get("mean"),
                      (rn.get("pre") or {}).get("mean")]
            for k, key in ((6, "ari1"), (7, "ari3"), (8, "ari6")):
                s = fp.get(key) or {}
                cov = s.get("cov") or []
                new[k] = [s.get("max")] + [int(round(c * 1000)) for c in cov]
            n_done += 1
        rows.append(base + new)
    out = {
        "built": cat.get("built"), "wy": cat.get("wy"),
        "mrms": {"qpe": QPE_LABEL, "ari": ARI_LABEL, "thresholds": thresholds,
                 "n_done": n_done, "n_episodes": len(rows),
                 "built": pd.Timestamp.now("UTC").strftime("%Y-%m-%d"),
                 "definition": "rolling 1, 3, 6 h accumulations and top of hour ARI with window ends "
                               "from the episode start hour t0 through t1 + 1 h; statistics over the "
                               "union of the touched HUC8 watersheds; coverage fractions cos latitude "
                               "weighted"},
        "fields": BASE_FIELDS + NEW_FIELDS, "rows": rows}
    (SITE_DATA / "episodes.js").write_text("var EPCAT=" + json.dumps(out, separators=(",", ":")) + ";",
                                           encoding="utf-8")
    sz = (SITE_DATA / "episodes.js").stat().st_size / 1e6
    print(f"episodes.js rewritten: {len(rows)} episodes, {n_done} with MRMS statistics, {sz:.2f} MB")
    return n_done


if __name__ == "__main__":
    build()
