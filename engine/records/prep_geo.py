"""Geometry payloads: CONUS counties (dissolved by FIPS), states outline, HUC8."""
import json
import geopandas as gpd
import pandas as pd

OUT="/var/tmp/se/out"
CONUS_AB={"AL","AZ","AR","CA","CO","CT","DE","DC","FL","GA","ID","IL","IN","IA","KS",
"KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC",
"ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY"}

def emit(gdf, props, var, path, nd=3):
    feats=[]
    for _,r in gdf.iterrows():
        g=json.loads(json.dumps(r.geometry.__geo_interface__))
        def rnd(c):
            if isinstance(c[0],(int,float)): return [round(c[0],nd),round(c[1],nd)]
            return [rnd(x) for x in c]
        g["coordinates"]=rnd(g["coordinates"])
        feats.append({"type":"Feature","properties":{k:r[v] for k,v in props.items()},"geometry":g})
    js="var "+var+"="+json.dumps({"type":"FeatureCollection","features":feats},separators=(",",":"))+";"
    open(path,"w").write(js)
    import os; print(f"{path.split('/')[-1]}: {os.path.getsize(path)/1e6:.2f}MB, {len(feats)} features")

# ---- counties ----
c=gpd.read_file("/var/tmp/se/counties/c_16ap26.shp")
c=c[c.STATE.isin(CONUS_AB)].copy()
c["geoid"]=c.FIPS.astype(str).str.zfill(5)
c=c.dissolve(by="geoid",aggfunc={"COUNTYNAME":"first","STATE":"first"}).reset_index()
c["nm"]=c.COUNTYNAME.str.title()+", "+c.STATE
c["geometry"]=c.geometry.simplify(0.004,preserve_topology=True)
c=c[~c.geometry.is_empty & c.geometry.notna()]
emit(c,{"f":"geoid","n":"nm"},"COUNTYGJ",f"{OUT}/counties_geo.js")

# ---- states outline (context layer) ----
s=c.dissolve(by="STATE").reset_index()
s["geometry"]=s.geometry.simplify(0.02,preserve_topology=True)
emit(s,{"s":"STATE"},"STATESGJ",f"{OUT}/states_geo.js",nd=2)

# ---- HUC8 ----
h=gpd.read_file("/var/tmp/se/HUC8_CONUS/HUC8_US.shp")[["HUC8","NAME","geometry"]]
h["HUC8"]=h.HUC8.astype(str).str.zfill(8)
h["geometry"]=h.geometry.simplify(0.012,preserve_topology=True)
h=h[~h.geometry.is_empty & h.geometry.notna()]
emit(h,{"h":"HUC8","n":"NAME"},"HUC8GJ",f"{OUT}/huc8_geo.js")
