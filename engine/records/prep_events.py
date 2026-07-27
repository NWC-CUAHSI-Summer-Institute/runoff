"""StormEvents 2015-2025 flash flood aggregation for the storm_events site.
Outputs county and HUC8 stats keyed by unit id with per-year metric arrays."""
import json, glob, re
import pandas as pd, numpy as np, geopandas as gpd

D="/sessions/peaceful-great-rubin/mnt/SI_2026/Teams_work/Team_dontrunoff/runoff_portal/detailed_events/storm_events_data"
OUT="/var/tmp/se/out"
YEARS=list(range(2015,2026))
CONUS_FIPS=set(range(1,57))-{2,15}  # 48 states + DC, drop AK HI; territories are >56

USE=["EPISODE_ID","EVENT_ID","STATE","STATE_FIPS","YEAR","EVENT_TYPE","CZ_TYPE",
     "CZ_FIPS","CZ_NAME","BEGIN_DATE_TIME","DEATHS_DIRECT","DEATHS_INDIRECT",
     "INJURIES_DIRECT","INJURIES_INDIRECT","DAMAGE_PROPERTY","DAMAGE_CROPS",
     "FLOOD_CAUSE","BEGIN_LAT","BEGIN_LON"]

def dollars(s):
    if pd.isna(s) or s=="" : return 0.0
    s=str(s).strip()
    m=re.match(r"^([0-9.]+)\s*([KMBkmb]?)$",s)
    if not m: return 0.0
    v=float(m.group(1)); suf=m.group(2).upper()
    return v*{"":1,"K":1e3,"M":1e6,"B":1e9}[suf]

frames=[]
for f in sorted(glob.glob(f"{D}/StormEvents_details-ftp_v1.0_d20*.csv.gz")):
    df=pd.read_csv(f,usecols=USE,dtype={"CZ_FIPS":"Int64","STATE_FIPS":"Int64"})
    df=df[df.EVENT_TYPE=="Flash Flood"]
    frames.append(df)
ff=pd.concat(frames,ignore_index=True)
print("flash flood rows 2015-2025:",len(ff))

ff=ff[ff.CZ_TYPE=="C"]
ff=ff[ff.STATE_FIPS.isin(CONUS_FIPS)]
cause=ff.FLOOD_CAUSE.fillna("Not specified")
drop=cause.str.contains("dam|levee",case=False)
print("dropped dam/levee:",int(drop.sum()))
ff=ff[~drop].copy()
ff["cause"]=cause[~drop].values
ff["deaths"]=ff.DEATHS_DIRECT.fillna(0)+ff.DEATHS_INDIRECT.fillna(0)
ff["inj"]=ff.INJURIES_DIRECT.fillna(0)+ff.INJURIES_INDIRECT.fillna(0)
ff["dp"]=ff.DAMAGE_PROPERTY.map(dollars)
ff["dc"]=ff.DAMAGE_CROPS.map(dollars)
ff["geoid"]=(ff.STATE_FIPS.astype(int)*1000+ff.CZ_FIPS.astype(int)).astype(str).str.zfill(5)
print("after filters:",len(ff),"| episodes:",ff.EPISODE_ID.nunique())

# ---- HUC8 point-in-polygon join (full-resolution WBD) ----
huc=gpd.read_file("/var/tmp/se/HUC8_CONUS/HUC8_US.shp")[["HUC8","NAME","geometry"]]
huc["HUC8"]=huc.HUC8.astype(str).str.zfill(8)
pts=gpd.GeoDataFrame(ff,geometry=gpd.points_from_xy(ff.BEGIN_LON,ff.BEGIN_LAT),crs=4326)
j=gpd.sjoin(pts,huc.set_crs(4326,allow_override=True),how="left",predicate="within")
j=j[~j.index.duplicated(keep="first")]
ff["huc8"]=j["HUC8"]
ff["huc8_name"]=j["NAME"]
print("no-huc8 (offshore or bad coords):",int(ff.huc8.isna().sum()))

def build(stats_key,name_of):
    out={}
    g=ff.dropna(subset=[stats_key]).groupby([stats_key,"YEAR"])
    agg=g.agg(ep=("EPISODE_ID","nunique"),ev=("EVENT_ID","count"),
              d=("deaths","sum"),i=("inj","sum"),dp=("dp","sum"),dc=("dc","sum"))
    cc=ff.dropna(subset=[stats_key]).groupby([stats_key,"cause"]).size()
    for (uid,yr),r in agg.iterrows():
        u=out.setdefault(str(uid),{"n":name_of.get(str(uid),str(uid)),"y":{},"c":{}})
        u["y"][int(yr)]=[int(r.ep),int(r.ev),int(r.d),int(r.i),int(round(r.dp)),int(round(r.dc))]
    for (uid,cz),n in cc.items():
        out[str(uid)]["c"][str(cz)]=int(n)
    return out

cnames=(ff.groupby("geoid").agg(nm=("CZ_NAME","first"),st=("STATE","first")))
county_name={i:f"{r.nm.title()}, {r.st.title()}" for i,r in cnames.iterrows()}
huc_name=ff.dropna(subset=["huc8"]).groupby("huc8")["huc8_name"].first().to_dict()

CS=build("geoid",county_name)
HS=build("huc8",huc_name)
print("county units:",len(CS),"| huc8 units:",len(HS))

open(f"{OUT}/county_stats.js","w").write("var YEARS="+json.dumps(YEARS)+";\nvar CSTATS="+json.dumps(CS,separators=(",",":"))+";")
open(f"{OUT}/huc8_stats.js","w").write("var HSTATS="+json.dumps(HS,separators=(",",":"))+";")

tot=ff.agg(ev=("EVENT_ID","count")) if False else None
print("TOTALS: events",len(ff),"| episodes",ff.EPISODE_ID.nunique(),
      "| deaths",int(ff.deaths.sum()),"| injuries",int(ff.inj.sum()),
      "| property $%.2fB"%(ff.dp.sum()/1e9),"| crops $%.0fM"%(ff.dc.sum()/1e6))
print("cause totals:",ff.cause.value_counts().to_dict())
import os
print("sizes: county_stats %.2fMB, huc8_stats %.2fMB"%(
    os.path.getsize(f"{OUT}/county_stats.js")/1e6,os.path.getsize(f"{OUT}/huc8_stats.js")/1e6))
