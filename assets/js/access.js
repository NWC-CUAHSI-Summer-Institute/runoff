/* RUNOFF access wizard v3. Vanilla JS + Leaflet. Data: huc8.js (HUC8GJ) + data.js (RUNOFF).
   Package targets the RUNOFF engine (engine/events, engine/forcing, engine/geo). */
"use strict";
var F={}; RUNOFF.fields.forEach(function(f,i){F[f]=i;});
var GAGES=RUNOFF.stations;
var el=function(id){return document.getElementById(id);};
var fmt$=function(v){ if(v>=1e9)return "$"+(v/1e9).toFixed(2)+"B";
  if(v>=1e6)return "$"+(v/1e6).toFixed(1)+"M"; if(v>=1e3)return "$"+Math.round(v/1e3)+"K";
  return "$"+Math.round(v); };

/* ---------------- state ---------------- */
var STEPS=["Geography","Experiment scope","Experiment criteria","USGS data","Forcing","Package"];
var S={
  step:0, maxStep:0,
  geo:"conus", states:{},
  scope:"runoff",
  fEv:0,fDeath:0,fDmgV:0,fStn:0,
  product:"events", qexc:25, wy0:2021, wy1:2025,
  forcing:"radar_15min", byoPath:"", byoFmt:"zarr", byoFreq:15, byoUnits:"mm"
};
var MRMS_REGISTRY="https://registry.opendata.aws/noaa-mrms-pds/";
var MRMS_BUCKET="https://noaa-mrms-pds.s3.amazonaws.com/index.html";
var FORCING_META={
  radar_2min:{label:"Radar-only MRMS PrecipRate, 2-minute + AORC met",
    product:"CONUS/PrecipRate_00.00",timestep_min:2,gc:false,ready:true,
    note:"native PrecipRate cadence"},
  radar_10min:{label:"Radar-only MRMS PrecipRate, 10-minute + AORC met",
    product:"CONUS/PrecipRate_00.00",timestep_min:10,gc:false,ready:true,
    note:"accumulated from 2-minute scans by the engine"},
  radar_15min:{label:"Radar-only MRMS PrecipRate, 15-minute + AORC met",
    product:"CONUS/PrecipRate_00.00",timestep_min:15,gc:false,ready:true,
    note:"the engine default"},
  radar_30min:{label:"Radar-only MRMS PrecipRate, 30-minute + AORC met",
    product:"CONUS/PrecipRate_00.00",timestep_min:30,gc:false,ready:true,
    note:"accumulated from 2-minute scans by the engine"},
  radar_60min:{label:"Radar-only MRMS RadarOnly_QPE_01H, 1-hour + AORC met",
    product:"CONUS/RadarOnly_QPE_01H_00.00",timestep_min:60,gc:false,ready:true,
    note:"hourly accumulation grids direct from AWS, no 2-minute download"},
  pass1_60min:{label:"MRMS MultiSensor Pass 1, 1-hour + AORC met",
    product:"CONUS/MultiSensor_QPE_01H_Pass1_00.00",timestep_min:60,gc:true,ready:true,
    note:"gauge-corrected, about 1 h latency"},
  pass2_60min:{label:"MRMS MultiSensor Pass 2, 1-hour + AORC met",
    product:"CONUS/MultiSensor_QPE_01H_Pass2_00.00",timestep_min:60,gc:true,ready:true,
    note:"gauge-corrected, about 12 h latency"},
  byo:{label:"User-supplied QPE/QPF brought to AORC format",
    product:"user",timestep_min:null,gc:null,ready:true,
    note:"declared frequency and units drive the converter"}
};
var BYO_FORMATS=["zarr","netcdf (.nc)","hdf5 (.h5)","grib2","geotiff","csv","other"];
var DMG_MAX=3.2e8;
function dmgFromSlider(v){ if(v<=0) return 0;
  return Math.round(Math.pow(10, 4 + (v/100)*(Math.log10(DMG_MAX)-4))); }

/* HUC8 props + ranges */
var HP={}; HUC8GJ.features.forEach(function(f){HP[f.properties.h]=f.properties;});
var MAXE=0,MAXD=0,MAXG=0;
Object.keys(HP).forEach(function(h){var p=HP[h];
  if(p.e>MAXE)MAXE=p.e; if(p.d>MAXD)MAXD=p.d; if(p.g>MAXG)MAXG=p.g;});
var US_STATES={};
Object.keys(HP).forEach(function(h){ (HP[h].st||"").split(",").forEach(function(s){
  s=s.trim(); if(s.length===2 && s!=="CN") US_STATES[s]=1; });});
var STATE_LIST=Object.keys(US_STATES).sort();

/* ---------------- selection logic ---------------- */
function inGeo(p){
  if(S.geo==="conus") return true;
  var sts=(p.st||"").split(",");
  for(var i=0;i<sts.length;i++){ if(S.states[sts[i].trim()]) return true; }
  return false;
}
function isSelected(p){
  if(!inGeo(p)) return false;
  var inScope=(S.scope==="runoff")?(p.e>0):(p.g>0);
  if(!inScope) return false;
  if(p.e<S.fEv) return false;
  if(p.d<S.fDeath) return false;
  if(p.dmg<dmgFromSlider(S.fDmgV)) return false;
  if(p.g<S.fStn) return false;
  return true;
}
function selectedIds(){ return Object.keys(HP).filter(function(h){return isSelected(HP[h]);}).sort(); }
function selTotals(ids){ var t={e:0,d:0,dmg:0,g:0};
  ids.forEach(function(h){var p=HP[h];t.e+=p.e;t.d+=p.d;t.dmg+=p.dmg;t.g+=p.g;}); return t; }

/* ---------------- map ---------------- */
var map=L.map("map",{preferCanvas:true}).setView([38.6,-96],5);
BASEMAPS.attach(map,"dark");   /* CARTO first, Esri/OSM failover (assets/js/basemaps.js) */
var rend=L.canvas({padding:0.4});
function hucStyle(f){
  var p=f.properties;
  if(isSelected(p)) return {color:"#3f9e6a",weight:1.4,fillColor:"#3f9e6a",fillOpacity:0.28};
  return {color:"#566275",weight:0.7,fillColor:"#000",fillOpacity:0};
}
function popupHtml(p){
  return "<div style='font:13px Segoe UI,sans-serif;min-width:210px'>"+
    "<b>"+p.n+"</b><br>HUC8 "+p.h+" ("+(p.st||"-")+")<hr style='border-color:#242c3a'>"+
    "Flood events: <b>"+p.e+"</b><br>Fatalities: <b>"+p.d+"</b><br>"+
    "Damage: <b>"+fmt$(p.dmg)+"</b><br>USGS stations (&lt;1000 km2): <b>"+p.g+"</b><br>"+
    "Event-linked gages: <b>"+p.eg+"</b></div>";
}
var hucLayer=L.geoJSON(HUC8GJ,{style:hucStyle,
  onEachFeature:function(f,lyr){ lyr.on("click",function(){lyr.bindPopup(popupHtml(f.properties)).openPopup();}); }
}).addTo(map);
var layAllG=L.layerGroup().addTo(map), layEvG=L.layerGroup().addTo(map);
GAGES.forEach(function(r){
  var hasEv=r[F.n_events]>0;
  var m=L.circleMarker([r[F.lat],r[F.lon]],{renderer:rend,
    radius:hasEv?2.7:1.7,weight:0,fillOpacity:hasEv?0.85:0.38,
    fillColor:hasEv?"#f2b705":"#5c6a7d"});
  m.bindTooltip("USGS "+r[F.staid]+" | "+r[F.area].toFixed(0)+" km2"+
    (hasEv?(" | "+r[F.n_events]+" events"):""),{sticky:true});
  (hasEv?layEvG:layAllG).addLayer(m);
});
el("ly-huc").addEventListener("change",function(e){ e.target.checked?hucLayer.addTo(map):map.removeLayer(hucLayer); });
el("ly-gag").addEventListener("change",function(e){
  if(e.target.checked){layAllG.addTo(map);layEvG.addTo(map);} else {map.removeLayer(layAllG);map.removeLayer(layEvG);} });

/* ---------------- step bar ---------------- */
function renderStepbar(){
  el("stepbar").innerHTML=STEPS.map(function(t,i){
    var cls=i===S.step?"cur":(i<S.step?"done":"");
    return '<span class="st '+cls+'" data-i="'+i+'"><span class="n">'+
      (i<S.step?"&#10003;":i)+'</span>'+t+'</span>';
  }).join("");
  el("stepbar").querySelectorAll(".st.done").forEach(function(n){
    n.addEventListener("click",function(){S.step=+n.dataset.i;render();});
  });
}

/* ---------------- rail content per step ---------------- */
function forcingRadio(key,title,sub){
  var fm=FORCING_META[key];
  var badge='<span style="color:#3f9e6a;font-size:10.5px;font-weight:800;letter-spacing:.5px"> ENGINE-READY</span>';
  return '<label class="radio"><input type="radio" name="forcing" value="'+key+'" '+(S.forcing===key?"checked":"")+'>'+
    '<span><b>'+title+badge+'</b><span>'+sub+'</span></span></label>';
}
function railHtml(){
  var ids=selectedIds(), t=selTotals(ids);
  if(S.step===0) return ''+
    '<h2>Step 0. Geographic scope</h2>'+
    '<p class="hint">Where should the experiment live?</p>'+
    '<label class="radio"><input type="radio" name="geo" value="conus" '+(S.geo==="conus"?"checked":"")+'>'+
      '<span><b>CONUS</b><span>the full conterminous United States</span></span></label>'+
    '<label class="radio"><input type="radio" name="geo" value="state" '+(S.geo==="state"?"checked":"")+'>'+
      '<span><b>State based</b><span>pick one or more states</span></span></label>'+
    (S.geo==="state"?('<div class="statelist">'+STATE_LIST.map(function(st){
      return '<label><input type="checkbox" class="stbox" value="'+st+'" '+(S.states[st]?"checked":"")+'>'+st+'</label>';
    }).join("")+'</div>'):"")+
    '<div class="readout" id="ro">-</div>';
  if(S.step===1) return ''+
    '<h2>Step 1. Experiment scope</h2>'+
    '<p class="hint">Which basins should the experiment consider?</p>'+
    '<label class="radio"><input type="radio" name="scope" value="runoff" '+(S.scope==="runoff"?"checked":"")+'>'+
      '<span><b>RUNOFF basins</b><span>watersheds linked to the 5,097 cataloged flash flood events (recommended)</span></span></label>'+
    '<label class="radio"><input type="radio" name="scope" value="all" '+(S.scope==="all"?"checked":"")+'>'+
      '<span><b>All basins under 1000 km2</b><span>every GAGES-II gage at flash flood scale, with or without cataloged events</span></span></label>'+
    '<div class="readout" id="ro">-</div>';
  if(S.step===2) return ''+
    '<h2>Step 2. Experiment criteria</h2>'+
    '<p class="hint">Move the sliders; the selected basins update on the map in green.</p>'+
    '<label class="f">Min flood events <b id="v-ev">'+S.fEv+'</b></label>'+
    '<input type="range" id="s-ev" min="0" max="'+MAXE+'" step="1" value="'+S.fEv+'">'+
    '<label class="f">Min fatalities <b id="v-death">'+S.fDeath+'</b></label>'+
    '<input type="range" id="s-death" min="0" max="'+MAXD+'" step="1" value="'+S.fDeath+'">'+
    '<label class="f">Min damage <b id="v-dmg">'+fmt$(dmgFromSlider(S.fDmgV))+'</b></label>'+
    '<input type="range" id="s-dmg" min="0" max="100" step="1" value="'+S.fDmgV+'">'+
    '<label class="f">Min USGS stations <b id="v-stn">'+S.fStn+'</b></label>'+
    '<input type="range" id="s-stn" min="0" max="'+MAXG+'" step="1" value="'+S.fStn+'">'+
    '<div class="readout" id="ro">-</div>';
  if(S.step===3) return ''+
    '<h2>Step 3. USGS data</h2>'+
    '<p class="hint">What should the package retrieve at the gages inside your selected basins?</p>'+
    '<label class="radio"><input type="radio" name="product" value="events" '+(S.product==="events"?"checked":"")+'>'+
      '<span><b>Separated flood events</b><span>hydrograph separation with flash-tuned parameters (engine/events)</span></span></label>'+
    '<label class="radio"><input type="radio" name="product" value="series" '+(S.product==="series"?"checked":"")+'>'+
      '<span><b>Entire time series</b><span>full 15-minute discharge record per gage, UTC (engine/streamflow/usgs)</span></span></label>'+
    (S.product==="events"?('<label class="f">Event peak threshold, Q exceedance '+
      '<span class="i" data-tip="Events are kept when the peak exceeds the flow duration curve at this exceedance probability. Q25 keeps larger events; Q50 keeps more, smaller ones.">i</span>'+
      ' <b id="v-q">Q'+S.qexc+'</b></label>'+
      '<input type="range" id="s-q" min="1" max="99" step="1" value="'+S.qexc+'">'):"")+
    '<label class="f">Water years</label>'+
    '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">'+
      '<select id="wy0">'+[2021,2022,2023,2024,2025].map(function(y){return '<option '+(y===S.wy0?"selected":"")+'>'+y+'</option>';}).join("")+'</select>'+
      '<select id="wy1">'+[2021,2022,2023,2024,2025].map(function(y){return '<option '+(y===S.wy1?"selected":"")+'>'+y+'</option>';}).join("")+'</select></div>'+
    '<div class="readout" id="ro">-</div>';
  if(S.step===4) return ''+
    '<h2>Step 4. Forcing (NextGen)</h2>'+
    '<p class="hint">One precipitation product; all other meteorology (temperature, PET inputs) '+
    'always comes from AORC, disaggregated from hourly to the precipitation timestep and '+
    'aggregated onto NextGen HydroFabric catchments by the RUNOFF engine.</p>'+
    '<div style="font-size:12px;color:var(--gold);letter-spacing:.5px;text-transform:uppercase;margin:10px 0 6px">Radar only '+
      '<span class="i" data-tip="MRMS PrecipRate: radar-only rain rate, near real time, no rain gauge correction. Source: NOAA MRMS on AWS (noaa-mrms-pds), CONUS/PrecipRate_00.00.">i</span></div>'+
    forcingRadio("radar_2min","2-minute","native PrecipRate cadence")+
    forcingRadio("radar_10min","10-minute","accumulated from 2-minute scans")+
    forcingRadio("radar_15min","15-minute","engine native: P from MRMS, T and PET from AORC, merged per event")+
    forcingRadio("radar_30min","30-minute","accumulated from 2-minute scans")+
    forcingRadio("radar_60min","1-hour","radar-only at AORC cadence")+
    '<div style="font-size:12px;color:var(--gold);letter-spacing:.5px;text-transform:uppercase;margin:12px 0 6px">Hourly, gauge-corrected '+
      '<span class="i" data-tip="MRMS MultiSensor QPE blends radar with rain gauges. Bucket path: CONUS/MultiSensor_QPE_01H_Pass1_00.00 and Pass2 on noaa-mrms-pds. Pass 1 about 1 h latency, Pass 2 about 12 h.">i</span></div>'+
    forcingRadio("pass1_60min","Pass 1, 1-hour","gauge-corrected, lower latency, fewer gauges")+
    forcingRadio("pass2_60min","Pass 2, 1-hour","gauge-corrected, higher accuracy, about 12 h latency")+
    '<div style="font-size:12px;color:var(--gold);letter-spacing:.5px;text-transform:uppercase;margin:12px 0 6px">Bring your own '+
      '<span class="i" data-tip="Use your own QPE or QPF (forecast) product. The package includes the protocol and a template script to bring it to AORC format so the engine can aggregate it onto the HydroFabric.">i</span></div>'+
    forcingRadio("byo","I have my own QPE/QPF","point the package at your product; conversion protocol included")+
    (S.forcing==="byo"?(''+
      '<label class="f">Path to your data</label>'+
      '<input type="text" id="byo-path" placeholder="/path/to/my_qpe" value="'+S.byoPath.replace(/"/g,"&quot;")+'" style="width:100%;padding:7px 9px;background:var(--panel);border:1px solid var(--line2);border-radius:6px;color:var(--text);font:13px inherit">'+
      '<label class="f">Format</label>'+
      '<select id="byo-fmt">'+BYO_FORMATS.map(function(f2){return '<option '+(f2===S.byoFmt?"selected":"")+'>'+f2+'</option>';}).join("")+'</select>'+
      '<label class="f">Temporal frequency of your data (minutes)</label>'+
      '<input type="number" id="byo-freq" min="1" max="1440" value="'+S.byoFreq+'" style="width:100%;padding:7px 9px;background:var(--panel);border:1px solid var(--line2);border-radius:6px;color:var(--text);font:13px inherit">'+
      '<label class="f">Units of your data</label>'+
      '<select id="byo-units">'+["mm","mm/h","in","in/h","kg m-2"].map(function(u){return '<option '+(u===S.byoUnits?"selected":"")+'>'+u+'</option>';}).join("")+'</select>'):"")+
    '<div class="note" style="margin-top:10px">Every option runs with the scripts in '+
    'engine/forcing: sub-hourly cadences via --timestep-min, hourly products via '+
    'extract_hourly.py, your own product via byo/convert.py. MRMS source: '+
    '<a href="'+MRMS_REGISTRY+'" target="_blank" rel="noopener">noaa-mrms-pds on AWS</a>.</div>';
  /* step 5 */
  var fm=FORCING_META[S.forcing];
  return ''+
    '<h2>Step 5. Package</h2>'+
    '<div class="ready"><b>Your data download package is ready.</b><br>Everything below is bundled in one zip.</div>'+
    '<div class="sumline"><span>Selected basins</span><b>'+ids.length+' HUC8</b></div>'+
    '<div class="sumline"><span>USGS stations inside</span><b>'+t.g+'</b></div>'+
    '<div class="sumline"><span>Cataloged events</span><b>'+t.e+'</b></div>'+
    '<div class="sumline"><span>USGS product</span><b>'+(S.product==="events"?("separated events (Q"+S.qexc+")"):"entire time series")+'</b></div>'+
    '<div class="sumline"><span>Water years</span><b>'+S.wy0+' to '+S.wy1+'</b></div>'+
    '<div class="sumline" style="border-bottom:0"><span>Forcing</span><b style="text-align:right">'+fm.label+'</b></div>'+
    '<div style="display:flex;flex-direction:column;gap:9px;margin-top:14px">'+
    '<button class="btn2 gold" id="dl-zip">Download package (.zip)</button>'+
    '<button class="btn2" id="copy-cmd">Copy engine commands</button></div>'+
    '<p class="hint" style="margin-top:12px">The zip holds RUN_EXPERIMENT.md (step by step), '+
    'experiment_commands.sh, a config.yaml patch, FORCING.json'+
    (S.forcing==="byo"?", the BYO-QPE protocol and a converter template":"")+
    '. Commands target the RUNOFF engine in this repository.</p>';
}

/* ---------------- readout + validation ---------------- */
function updateReadout(){
  var ro=el("ro"); if(!ro) return;
  var ids=selectedIds(), t=selTotals(ids);
  ro.innerHTML="<b>"+ids.length+"</b> selected basins | <b>"+t.g+"</b> USGS stations | <b>"+
    t.e+"</b> events | "+fmt$(t.dmg)+" damage";
}
function nextAllowed(){
  if(S.step===0 && S.geo==="state" && !Object.keys(S.states).some(function(k){return S.states[k];})) return false;
  if(S.step>=2 && selectedIds().length===0) return false;
  return S.step<5;
}
function refreshNav(){
  el("btn-back").disabled=S.step===0;
  el("btn-next").disabled=!nextAllowed();
  el("btn-next").textContent=S.step===4?"Finish":"Next";
  if(S.step===5) el("btn-next").style.display="none"; else el("btn-next").style.display="";
}

/* ---------------- wiring ---------------- */
function wire(){
  document.querySelectorAll("input[name=geo]").forEach(function(r){
    r.addEventListener("change",function(e){S.geo=e.target.value;render();});});
  document.querySelectorAll(".stbox").forEach(function(c){
    c.addEventListener("change",function(e){S.states[e.target.value]=e.target.checked;repaint();});});
  document.querySelectorAll("input[name=scope]").forEach(function(r){
    r.addEventListener("change",function(e){S.scope=e.target.value;repaint();});});
  [["s-ev","fEv","v-ev"],["s-death","fDeath","v-death"],["s-stn","fStn","v-stn"]].forEach(function(cf){
    var n=el(cf[0]); if(!n) return;
    n.addEventListener("input",function(e){S[cf[1]]=+e.target.value;
      el(cf[2]).textContent=e.target.value;repaint();});});
  var sd=el("s-dmg"); if(sd) sd.addEventListener("input",function(e){
    S.fDmgV=+e.target.value; el("v-dmg").textContent=fmt$(dmgFromSlider(S.fDmgV)); repaint();});
  document.querySelectorAll("input[name=product]").forEach(function(r){
    r.addEventListener("change",function(e){S.product=e.target.value;render();});});
  var sq=el("s-q"); if(sq) sq.addEventListener("input",function(e){
    S.qexc=+e.target.value; el("v-q").textContent="Q"+S.qexc;});
  if(el("wy0")) el("wy0").addEventListener("change",function(e){S.wy0=+e.target.value;});
  if(el("wy1")) el("wy1").addEventListener("change",function(e){S.wy1=+e.target.value;});
  document.querySelectorAll("input[name=forcing]").forEach(function(r){
    r.addEventListener("change",function(e){S.forcing=e.target.value;render();});});
  if(el("byo-path")) el("byo-path").addEventListener("input",function(e){S.byoPath=e.target.value;});
  if(el("byo-fmt")) el("byo-fmt").addEventListener("change",function(e){S.byoFmt=e.target.value;});
  if(el("byo-freq")) el("byo-freq").addEventListener("input",function(e){S.byoFreq=+e.target.value||15;});
  if(el("byo-units")) el("byo-units").addEventListener("change",function(e){S.byoUnits=e.target.value;});
  if(el("dl-zip")) el("dl-zip").addEventListener("click",downloadZip);
  if(el("copy-cmd")) el("copy-cmd").addEventListener("click",function(){
    navigator.clipboard.writeText(cmdScript());
    el("copy-cmd").textContent="Copied"; setTimeout(function(){el("copy-cmd").textContent="Copy engine commands";},1200);});
}
function repaint(){ hucLayer.setStyle(hucStyle); updateReadout(); refreshNav(); }
function render(){ renderStepbar(); el("railbody").innerHTML=railHtml(); wire(); repaint(); }
el("btn-back").addEventListener("click",function(){ if(S.step>0){S.step--;render();} });
el("btn-next").addEventListener("click",function(){ if(nextAllowed()){S.step++; if(S.step>S.maxStep)S.maxStep=S.step; render();} });

/* ---------------- package generation (engine-native) ---------------- */
function cmdScript(){
  var ids=selectedIds(), L1=[];
  function P(s){L1.push(s);}
  P("#!/usr/bin/env bash");
  P("# RUNOFF experiment: engine commands (generated by the access portal)");
  P("# Run from the repository root after: uv pip install -e .  (or pip install -e .)");
  P("# and after setting your root path in config.yaml");
  P("set -e");
  P("");
  P("# ---- 1. USGS hydrographs -> "+(S.product==="events"?"flash flood events":"full time series"));
  if(S.product==="events"){
    ids.forEach(function(h){
      P("python engine/events/extract.py --huc8 "+h+" --wy-start "+S.wy0+" --wy-end "+S.wy1);
    });
    P("# event peak threshold: set Q_EXCEEDANCE_PCT = "+S.qexc+" in the CONFIG block");
    P("# (engine/events/extract.py), or pass the matching CLI flag (see --help).");
  } else {
    P("# full 15-minute discharge per gage inside each HUC8 (see script --help):");
    ids.forEach(function(h){
      P("python engine/streamflow/usgs/extract.py --huc8 "+h+" --wy-start "+S.wy0+" --wy-end "+S.wy1);
    });
  }
  P("");
  P("# ---- 2. Subset hydrofabric to event catchments + upstream network");
  P("python engine/geo/extract_hf.py --csv events.csv --gpkg conus_nextgen.gpkg --output-dir data/experiment/");
  P("");
  P("# ---- 3. Snap gages to catchments (adds gage_cat-id, needed by forcing)");
  P("python engine/geo/_gage_to_cat.py --csv events.csv --gpkg conus_nextgen.gpkg");
  P("");
  var fm=FORCING_META[S.forcing];
  if(S.forcing==="byo"){
    P("# ---- 4. Forcing: BRING YOUR OWN QPE/QPF");
    P("# Your product: "+(S.byoPath||"<set path>")+"  ("+S.byoFmt+", every "+S.byoFreq+" min, "+S.byoUnits+")");
    P("python engine/forcing/byo/convert.py \\");
    P("    --in-path "+(S.byoPath||"/path/to/my_qpe")+" --format "+S.byoFmt.split(" ")[0]+" \\");
    P("    --timestep-min "+S.byoFreq+" --units \""+S.byoUnits+"\"");
    P("# Then aggregate + merge with AORC met at your product timestep:");
    P("python engine/forcing/aorc/extract.py --events-csv events.csv --timestep-min "+(60%S.byoFreq===0?S.byoFreq:15));
    P("python engine/forcing/merge_15min.py --timestep-min "+(60%S.byoFreq===0?S.byoFreq:15));
  } else if(fm.timestep_min===60){
    var prodkey = S.forcing==="radar_60min"?"radar_1h":(S.forcing==="pass1_60min"?"pass1":"pass2");
    P("# ---- 4. MRMS hourly product ("+fm.product+"), direct from AWS");
    P("python engine/forcing/mrms/extract_hourly.py --events-csv events.csv --product "+prodkey+" --window-days 6 --centroid peak");
    P("");
    P("# ---- 5. AORC meteorology at the hourly step (30-day hourly warmup included)");
    P("python engine/forcing/aorc/extract.py --events-csv events.csv --timestep-min 60 --window-days 6 --centroid peak");
    P("");
    P("# ---- 6. Merge: P from MRMS, T and PET from AORC");
    P("python engine/forcing/merge_15min.py --timestep-min 60");
  } else {
    P("# ---- 4. MRMS precipitation ("+fm.product+", "+fm.timestep_min+"-minute accumulation)");
    P("python engine/forcing/mrms/extract.py --events-csv events.csv --timestep-min "+fm.timestep_min+" --window-days 6 --centroid peak");
    P("python engine/forcing/mrms/merge.py");
    P("");
    P("# ---- 5. AORC meteorology ("+fm.timestep_min+"-minute, 30-day hourly warmup included)");
    P("python engine/forcing/aorc/extract.py --events-csv events.csv --timestep-min "+fm.timestep_min+" --window-days 6 --centroid peak");
    P("");
    P("# ---- 6. Merge: P from MRMS, T and PET from AORC");
    P("python engine/forcing/merge_15min.py --timestep-min "+fm.timestep_min);
  }
  P("");
  P("# Output: forcing_15min.nc next to your events CSV (see FORCING.json for the spec).");
  return L1.join("\n");
}
function configPatch(){
  return "# RUNOFF config.yaml patch (generated). Merge into config.yaml at repo root.\n"+
  "# Only root is mandatory: every other path derives from it.\n"+
  "root: /path/to/your/workspace/\n\n"+
  "hydrofabric_gpkg: ${root}/data/conus_nextgen.gpkg\n"+
  "#   AWS download: s3://communityhydrofabric/hydrofabrics/community/conus_nextgen.tar.gz\n"+
  "events_csv: ${root}/data/experiment/events.csv\n"+
  "cache_dir: ${root}/data/experiment/cache\n"+
  "study_start: '"+(S.wy0-1)+"-10-01'\n"+
  "study_end: '"+S.wy1+"-09-30'\n"+
  "huc8_shp: ${root}/data/huc8_conus/HUC8_US.shp\n"+
  "gages_csv: ${root}/data/gages2_1000km2.csv\n"+
  "event_output_dir: ${root}/data/experiment/event_extraction\n";
}
function forcingJson(){
  var ids=selectedIds(), fm=FORCING_META[S.forcing];
  return JSON.stringify({
    generated_by:"RUNOFF access portal (experimental)",
    huc8_list: ids,
    water_years:[S.wy0,S.wy1],
    usgs_product: S.product==="events"?"separated_events":"entire_time_series",
    q_exceedance_pct: S.product==="events"?S.qexc:null,
    forcing:{
      selection:S.forcing, label:fm.label,
      mrms_product:fm.product, precip_timestep_minutes:fm.timestep_min,
      gauge_corrected:fm.gc, engine_ready:fm.ready,
      met_source:"AORC (hourly, disaggregated to the precipitation timestep)",
      window_days:6, centroid:"peak", antecedent_days:30,
      aws_registry:MRMS_REGISTRY, aws_bucket:MRMS_BUCKET,
      byo: S.forcing==="byo"?{path:S.byoPath||null,format:S.byoFmt,timestep_min:S.byoFreq,units:S.byoUnits}:null
    },
    output:"forcing_15min.nc: P (MRMS), T (AORC), PET (AORC), ragged per-event NextGen catchments"
  },null,2);
}
function runReadme(){
  var ids=selectedIds(), fm=FORCING_META[S.forcing];
  var s="RUNOFF experiment package\n=========================\n\n"+
  "Generated by the RUNOFF access portal (experimental dataset).\n\n"+
  "Selection\n"+
  "  Basins ("+ids.length+" HUC8): "+ids.join(", ")+"\n"+
  "  Water years: "+S.wy0+" to "+S.wy1+"\n"+
  "  USGS product: "+(S.product==="events"?("separated flood events, Q"+S.qexc+" threshold"):"entire 15-minute time series")+"\n"+
  "  Forcing: "+fm.label+"\n\n"+
  "Setup (once)\n"+
  "  1. git clone https://github.com/NWC-CUAHSI-Summer-Institute/runoff.git && cd runoff\n"+
  "  2. uv pip install -e .        (or: pip install -e .)\n"+
  "  3. Merge config_patch.yaml into config.yaml and set your root path.\n"+
  "  4. Download the NextGen hydrofabric:\n"+
  "     s3://communityhydrofabric/hydrofabrics/community/conus_nextgen.tar.gz\n\n"+
  "Run\n"+
  "  bash experiment_commands.sh   (or run the commands one by one)\n\n"+
  "What the forcing chain does\n"+
  "  - engine/forcing/mrms/extract.py downloads MRMS and aggregates 15-min\n"+
  "    precipitation onto NextGen HydroFabric catchments for each event window\n"+
  "    (6 days centered on the event peak).\n"+
  "  - engine/forcing/aorc/extract.py extracts AORC: hourly for a 30-day warmup\n"+
  "    and 15-min (disaggregated from hourly) over the event window. AORC is\n"+
  "    always hourly at source; the engine transforms it to the MRMS timestep\n"+
  "    before the hydrofabric aggregation.\n"+
  "  - engine/forcing/merge_15min.py joins them per event and per catchment:\n"+
  "    P from MRMS, T and PET from AORC, written as ragged NetCDF\n"+
  "    (forcing_15min.nc). Both extractions must use the same WINDOW_DAYS and\n"+
  "    CENTROID or the merge will refuse.\n\n";
  if(S.forcing==="byo"){
    s+="BRING YOUR OWN QPE/QPF: engine/forcing/byo/convert.py converts your\n"+
    "product ("+S.byoFmt+", every "+S.byoFreq+" min, "+S.byoUnits+") to the AORC convention;\n"+
    "BYO_QPE_PROTOCOL.md documents the convention in detail.\n\n";
  }
  s+="All timestamps are UTC. Dataset and interface are experimental.\n";
  return s;
}
function byoProtocol(){
  return "Bring your own QPE/QPF: AORC-format protocol\n"+
  "============================================\n\n"+
  "Goal: convert your product ("+S.byoFmt+") to the AORC convention so the RUNOFF\n"+
  "engine can treat it exactly like MRMS precipitation.\n"+
  "Declared: every "+S.byoFreq+" minutes, units "+S.byoUnits+".\n\n"+
  "Target convention (per timestep)\n"+
  "  variable   APCP_surface\n"+
  "  units      kg m-2 per timestep (1 kg m-2 = 1 mm of water)\n"+
  "  dims       (time, latitude, longitude) on a regular EPSG:4326 grid\n"+
  "  time       UTC, stamped at the END of each accumulation interval\n"+
  "  no gaps    missing data as NaN, never as zero\n\n"+
  "Steps\n"+
  "  1. Read your product ("+S.byoFmt+"): "+(S.byoPath||"<your path>")+"\n"+
  "  2. If it is a rate (mm/h), convert to depth per timestep:\n"+
  "     depth = rate x (timestep_minutes / 60).\n"+
  "  3. Regrid to a regular lat/lon grid (EPSG:4326) if needed. Conservative\n"+
  "     regridding preserves storm volumes; bilinear smooths convective peaks.\n"+
  "  4. Stamp timestamps in UTC at interval END; document the timestep.\n"+
  "  5. Write NetCDF with the variable and attributes above\n"+
  "     (see byo_to_aorc_template.py).\n"+
  "  6. Point engine/forcing at your files instead of the MRMS download and run\n"+
  "     the same aggregation and merge chain. The engine adjustments for\n"+
  "     user-supplied products are under development; FORCING.json carries your\n"+
  "     product path and format so the adapter can pick them up unchanged.\n\n"+
  "QPF note: forecasts follow the same convention with one extra dimension\n"+
  "(reference_time). Keep lead times as separate files or a lead dimension and\n"+
  "document your choice in FORCING.json.\n";
}
function byoTemplate(){
  return '#!/usr/bin/env python3\n'+
  '"""Template: convert a user QPE/QPF product to AORC-format NetCDF.\n'+
  'Fill in the read_my_product() function for your format ('+S.byoFmt+').\n'+
  'Generated by the RUNOFF access portal.\n'+
  '"""\n'+
  'import numpy as np\n'+
  'import pandas as pd\n'+
  'import xarray as xr\n\n'+
  'IN_PATH  = r"'+(S.byoPath||"/path/to/my_qpe")+'"\n'+
  'OUT_NC   = "my_qpe_aorc_format.nc"\n'+
  'TIMESTEP_MIN = '+S.byoFreq+'            # your product timestep, minutes\n'+
  'IS_RATE_MM_PER_H = '+(S.byoUnits.indexOf('/h')>=0?'True':'False')+'     # from your declared units: '+S.byoUnits+'\n\n\n'+
  'def read_my_product(path):\n'+
  '    """Return an xarray.DataArray precip(time, latitude, longitude).\n\n'+
  '    Examples:\n'+
  '      zarr:    xr.open_zarr(path)["precip"]\n'+
  '      netcdf:  xr.open_dataset(path)["precip"]\n'+
  '      hdf5:    xr.open_dataset(path, engine="h5netcdf")["precip"]\n'+
  '      grib2:   xr.open_dataset(path, engine="cfgrib")["unknown"]\n'+
  '      geotiff: stack per-time rasters with rioxarray, then concat on time\n'+
  '    """\n'+
  '    raise NotImplementedError("read your '+S.byoFmt+' product here")\n\n\n'+
  'def main():\n'+
  '    da = read_my_product(IN_PATH)\n'+
  '    if IS_RATE_MM_PER_H:\n'+
  '        da = da * (TIMESTEP_MIN / 60.0)   # rate -> depth per timestep\n'+
  '    da = da.astype("float32").rename("APCP_surface")\n'+
  '    da.attrs = {\n'+
  '        "long_name": "Total Precipitation",\n'+
  '        "units": "kg/m^2",\n'+
  '        "crs": "EPSG:4326",\n'+
  '        "cell_methods": "time: sum",\n'+
  '        "accumulation_interval_minutes": TIMESTEP_MIN,\n'+
  '        "time_stamp_convention": "valid for the interval ENDING at time (UTC)",\n'+
  '        "source": "user-supplied QPE/QPF via RUNOFF BYO protocol",\n'+
  '    }\n'+
  '    ds = da.to_dataset()\n'+
  '    ds["latitude"].attrs = {"units": "degrees_north", "standard_name": "latitude"}\n'+
  '    ds["longitude"].attrs = {"units": "degrees_east", "standard_name": "longitude"}\n'+
  '    ds.to_netcdf(OUT_NC, encoding={"APCP_surface": {"zlib": True, "complevel": 4}})\n'+
  '    print("wrote", OUT_NC)\n\n\n'+
  'if __name__ == "__main__":\n'+
  '    main()\n';
}
function downloadZip(){
  var zip=new JSZip();
  zip.file("RUN_EXPERIMENT.md", runReadme());
  zip.file("experiment_commands.sh", cmdScript());
  zip.file("config_patch.yaml", configPatch());
  zip.file("FORCING.json", forcingJson());
  if(S.forcing==="byo"){
    zip.file("BYO_QPE_PROTOCOL.md", byoProtocol());
    zip.file("byo_to_aorc_template.py", byoTemplate());
  }
  zip.generateAsync({type:"blob"}).then(function(b){
    var a=document.createElement("a");
    a.href=URL.createObjectURL(b); a.download="runoff_experiment_package.zip"; a.click();
  });
}

render();
