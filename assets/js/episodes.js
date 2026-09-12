/* RUNOFF episode browser: the general flash flood modeling path.
   Data contracts (built by engine/episodes/build_catalog.py):

   assets/data/episodes.js ->
     var EPCAT={built:"YYYY-MM-DD", wy:[2021,2025], product:"...",
       fields:["id","t0","t1","states","nev","nlsr","deaths","inj","dmg",
               "fips","bbox","acc6","acc12","acc24","acc72","ari","aridur"],
       rows:[[...],...]};
     t0/t1 are UTC "YYYY-MM-DD HH". nlsr is -1 when LSRs were not fetched.
     acc*/ari are null until engine/episodes/mrms_stats.py fills them.

   assets/data/episode_points.js ->
     var EPPTS={"<id>":{ev:[[lat,lon,"YYYY-MM-DD HH:MM",deaths,dmgUSD],...],
                        lsr:[[lat,lon,"YYYY-MM-DD HH:MM","TYPETEXT","SOURCE"],...]},...};

   The page degrades cleanly: no payload -> build notice; no LSR fetch -> LSR
   filter hidden; no MRMS run yet -> rainfall filters hidden. */
"use strict";
var el=function(id){return document.getElementById(id);};
var fmt$=function(v){ if(v>=1e9)return "$"+(v/1e9).toFixed(2)+"B";
  if(v>=1e6)return "$"+(v/1e6).toFixed(1)+"M"; if(v>=1e3)return "$"+Math.round(v/1e3)+"K";
  return "$"+Math.round(v); };
var fmtN=function(n){ return n.toLocaleString("en-US"); };
var esc=function(s){ return String(s===null||s===undefined?"":s)
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); };
var MONTHS=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

/* ---------------- map boots regardless of payload ---------------- */
var map=L.map("map",{preferCanvas:true,zoomSnap:.5}).setView([38.6,-95.8],4.5);
var BASE=BASEMAPS.attach(map,"dark");
document.querySelectorAll("#basebox input").forEach(function(r){
  r.addEventListener("change",function(){ BASE.set(this.value); });
});
map.createPane("statepane"); map.getPane("statepane").style.zIndex=430;
map.getPane("statepane").style.pointerEvents="none";
L.geoJSON(STATESGJ,{pane:"statepane",interactive:false,
  style:{color:"#9aa5b3",weight:1,opacity:.4,fill:false}}).addTo(map);

/* ---------------- payload check ---------------- */
var HAVE_CAT=(typeof EPCAT!=="undefined")&&EPCAT&&EPCAT.rows&&EPCAT.rows.length>0;
var HAVE_PTS=(typeof EPPTS!=="undefined")&&!!EPPTS;
if(!HAVE_CAT){
  el("nobuild").hidden=false;
}else{
  el("filters").hidden=false;
  boot();
}

function boot(){

/* ---------------- unpack catalog ---------------- */
var EF={}; EPCAT.fields.forEach(function(f,i){EF[f]=i;});
var EPS=EPCAT.rows.map(function(r){
  var o={};
  EPCAT.fields.forEach(function(f,i){o[f]=r[i];});
  o.year=parseInt(String(o.t0).slice(0,4),10);
  o.c=[(o.bbox[0]+o.bbox[2])/2,(o.bbox[1]+o.bbox[3])/2];
  return o;
});
var hasLSR=EPS.some(function(e){return e.nlsr>=0;});
var hasMRMS=EPS.some(function(e){return e.acc24!==null&&e.acc24!==undefined;});
if(!hasLSR){ el("lab-lsr").style.display="none"; el("f-lsr").style.display="none"; }
if(hasMRMS){ el("mrmsfilters").hidden=false; }

/* damage slider: log scale from $10K to the catalog max */
var DMG_MAX=1e5;
EPS.forEach(function(e){ if(e.dmg>DMG_MAX) DMG_MAX=e.dmg; });
function dmgFromSlider(v){ if(v<=0) return 0;
  return Math.round(Math.pow(10, 4+(v/100)*(Math.log10(DMG_MAX)-4))); }

/* year + state controls from the data itself */
var years=EPS.map(function(e){return e.year;});
var Y0=Math.min.apply(null,years), Y1=Math.max.apply(null,years);
function fillYears(id,val){
  var s=el(id);
  for(var y=Y0;y<=Y1;y++){ var o=document.createElement("option");
    o.value=y; o.textContent=y; s.appendChild(o); }
  s.value=val;
  s.addEventListener("change",refresh);
  return s;
}
var sy0=fillYears("y-from",Y0), sy1=fillYears("y-to",Y1);
var stSet={};
EPS.forEach(function(e){ String(e.states||"").split(",").forEach(function(s){
  s=s.trim(); if(s.length===2) stSet[s]=1; });});
var stSel=el("sel-states");
Object.keys(stSet).sort().forEach(function(s){
  var o=document.createElement("option"); o.value=s; o.textContent=s; stSel.appendChild(o);
});
stSel.addEventListener("change",refresh);
el("st-clear").addEventListener("click",function(){
  for(var i=0;i<stSel.options.length;i++) stSel.options[i].selected=false;
  refresh();
});

/* sliders */
function slider(id,labId,fmt){
  var s=el(id);
  s.addEventListener("input",function(){
    el(labId).textContent=fmt(parseInt(s.value,10)); refresh();
  });
  el(labId).textContent=fmt(parseInt(s.value,10));
  return s;
}
var sEv=slider("f-ev","v-ev",function(v){return String(v);});
var sLsr=slider("f-lsr","v-lsr",function(v){return String(v);});
var sDeath=slider("f-death","v-death",function(v){return String(v);});
var sDmg=slider("f-dmg","v-dmg",function(v){return fmt$(dmgFromSlider(v));});
var sA24=slider("f-a24","v-a24",function(v){return v+" mm";});
var sA72=slider("f-a72","v-a72",function(v){return v+" mm";});
var sAri=slider("f-ari","v-ari",function(v){return v<=0?"any":(">= "+v+" yr");});
el("sel-sort").addEventListener("change",refresh);

/* ---------------- filtering + sorting ---------------- */
function selectedStates(){
  var out={},any=false;
  for(var i=0;i<stSel.options.length;i++){
    if(stSel.options[i].selected){ out[stSel.options[i].value]=1; any=true; }
  }
  return any?out:null;
}
function matches(e){
  if(e.year<parseInt(sy0.value,10)||e.year>parseInt(sy1.value,10)) return false;
  var st=selectedStates();
  if(st){
    var hit=false;
    String(e.states||"").split(",").forEach(function(s){ if(st[s.trim()]) hit=true; });
    if(!hit) return false;
  }
  if(e.nev<parseInt(sEv.value,10)) return false;
  if(hasLSR && parseInt(sLsr.value,10)>0){
    if(e.nlsr<0 || e.nlsr<parseInt(sLsr.value,10)) return false;
  }
  if(e.deaths<parseInt(sDeath.value,10)) return false;
  if(e.dmg<dmgFromSlider(parseInt(sDmg.value,10))) return false;
  if(hasMRMS){
    /* a rainfall threshold above zero excludes episodes whose MRMS stats
       have not been computed yet (unknown is not the same as low) */
    var a24=parseInt(sA24.value,10), a72=parseInt(sA72.value,10), ari=parseInt(sAri.value,10);
    if(a24>0 && !(e.acc24>=a24)) return false;
    if(a72>0 && !(e.acc72>=a72)) return false;
    if(ari>0 && !(e.ari>=ari)) return false;
  }
  return true;
}
function sortKey(e,k){
  if(k==="date") return e.t0;
  var v=e[k];
  return (v===null||v===undefined)?-1:v;
}
var matched=[], dotsSig="";
function refresh(){
  var k=el("sel-sort").value;
  matched=EPS.filter(matches);
  matched.sort(function(a,b){
    var av=sortKey(a,k), bv=sortKey(b,k);
    return av<bv?1:(av>bv?-1:0);
  });
  var t={ev:0,d:0,dmg:0};
  matched.forEach(function(e){t.ev+=e.nev;t.d+=e.deaths;t.dmg+=e.dmg;});
  el("rescount").innerHTML="<b>"+fmtN(matched.length)+"</b> of "+fmtN(EPS.length)+
    " episodes match: "+fmtN(t.ev)+" events, "+fmtN(t.d)+" fatalities, "+fmt$(t.dmg)+" damage."+
    (hasMRMS?"":" Rainfall filters appear once MRMS stats are built.");
  renderList();
  renderDots();
  if(SEL && matched.indexOf(SEL)<0) clearSelection();
}

/* ---------------- results list ---------------- */
var LISTN=200;
function epTitle(e){
  var d=String(e.t0);
  var mon=MONTHS[parseInt(d.slice(5,7),10)-1]||"";
  return mon+" "+d.slice(8,10)+", "+d.slice(0,4)+" | "+(e.states||"-");
}
function renderList(){
  var box=el("eplist"), html="";
  matched.slice(0,LISTN).forEach(function(e,i){
    var extra="";
    if(hasMRMS&&e.acc24!==null&&e.acc24!==undefined)
      extra=" | 24h max "+Math.round(e.acc24)+" mm"+(e.ari?(", "+Math.round(e.ari)+"-yr ARI"):"");
    html+="<div class='eprow' data-i='"+i+"'><b>Episode "+e.id+"</b> "+epTitle(e)+
      "<div class='m'>"+e.nev+" events"+
      (e.nlsr>=0?(", "+e.nlsr+" LSRs"):"")+
      (e.deaths?(", "+e.deaths+" deaths"):"")+
      (e.dmg?(", "+fmt$(e.dmg)):"")+extra+"</div></div>";
  });
  if(matched.length>LISTN)
    html+="<div class='eprow' style='cursor:default;color:var(--dim)'>Showing the top "+
      LISTN+" of "+fmtN(matched.length)+"; tighten the filters to narrow down.</div>";
  if(!matched.length)
    html="<div class='eprow' style='cursor:default'>Nothing matches. Loosen a filter.</div>";
  box.innerHTML=html;
  box.querySelectorAll(".eprow[data-i]").forEach(function(row){
    row.addEventListener("click",function(){ select(matched[parseInt(row.dataset.i,10)],row); });
  });
}

/* ---------------- map layers ---------------- */
var rend=L.canvas({padding:0.4});
var dotsG=L.layerGroup().addTo(map);
var footG=L.layerGroup().addTo(map);
var evG=L.layerGroup().addTo(map);
var lsrG=L.layerGroup().addTo(map);
function renderDots(){
  /* skip the rebuild when the matched set is unchanged (slider drags fire fast) */
  var sig=matched.length+":"+(matched.length?matched[0].id+"-"+matched[matched.length-1].id:"");
  if(sig===dotsSig) return;
  dotsSig=sig;
  dotsG.clearLayers();
  matched.forEach(function(e){
    var m=L.circleMarker(e.c,{renderer:rend,radius:Math.min(9,2+Math.sqrt(e.nev)),
      weight:0,fillOpacity:.5,fillColor:"#8b95a5"});
    m.bindTooltip("Episode "+e.id+" | "+epTitle(e)+" | "+e.nev+" events",{sticky:true});
    m.on("click",function(){ select(e,null); });
    dotsG.addLayer(m);
  });
}

/* county footprint index: 5-digit FIPS -> geojson feature */
var FIPSIDX={};
COUNTYGJ.features.forEach(function(f){ FIPSIDX[f.properties.f]=f; });

var SEL=null;
function clearSelection(){
  SEL=null; footG.clearLayers(); evG.clearLayers(); lsrG.clearLayers();
  el("detail").hidden=true;
  document.querySelectorAll(".eprow.on").forEach(function(r){r.classList.remove("on");});
}
function select(e,row){
  clearSelection();
  SEL=e;
  if(row) row.classList.add("on");
  /* footprint: the counties this episode touched */
  var feats=(e.fips||[]).map(function(f){return FIPSIDX[f];}).filter(Boolean);
  if(feats.length){
    footG.addLayer(L.geoJSON({type:"FeatureCollection",features:feats},{
      style:{color:"#f2b705",weight:1.3,fillColor:"#f2b705",fillOpacity:.14},
      onEachFeature:function(f,lyr){ lyr.bindTooltip(f.properties.n,{sticky:true}); }
    }));
  }
  /* points */
  var pts=HAVE_PTS?(EPPTS[String(e.id)]||EPPTS[e.id]||null):null;
  if(pts){
    (pts.ev||[]).forEach(function(p){
      var m=L.circleMarker([p[0],p[1]],{renderer:rend,radius:4,weight:1,
        color:"#161308",fillOpacity:.95,fillColor:"#f2b705"});
      m.bindPopup("<b>Storm Events flash flood</b><br>"+p[2]+" UTC"+
        (p[3]?("<br>Fatalities: "+p[3]):"")+(p[4]?("<br>Damage: "+fmt$(p[4])):""));
      evG.addLayer(m);
    });
    (pts.lsr||[]).forEach(function(p){
      var m=L.circleMarker([p[0],p[1]],{renderer:rend,radius:3.4,weight:1,
        color:"#0a1220",fillOpacity:.95,fillColor:"#5b8dd9"});
      m.bindPopup("<b>Local Storm Report: "+esc(p[3]||"FLASH FLOOD")+"</b><br>"+esc(p[2])+" UTC"+
        (p[4]?("<br>Source: "+esc(p[4])):""));
      lsrG.addLayer(m);
    });
  }
  map.fitBounds([[e.bbox[0],e.bbox[1]],[e.bbox[2],e.bbox[3]]],{padding:[36,36],maxZoom:9});
  renderDetail(e,pts);
}

/* ---------------- detail card + package ---------------- */
function renderDetail(e,pts){
  var d=el("detail");
  var html="<h3>Episode "+e.id+"</h3><div class='per'>"+e.t0+" to "+e.t1+" UTC | "+(e.states||"-")+"</div>";
  html+="<table>";
  html+="<tr><td>Flash flood events</td><td>"+e.nev+"</td></tr>";
  if(e.nlsr>=0) html+="<tr><td>Local Storm Reports</td><td>"+e.nlsr+"</td></tr>";
  html+="<tr><td>Fatalities / injuries</td><td>"+e.deaths+" / "+e.inj+"</td></tr>";
  html+="<tr><td>Reported damage</td><td>"+fmt$(e.dmg)+"</td></tr>";
  html+="<tr><td>Counties touched</td><td>"+(e.fips?e.fips.length:0)+"</td></tr>";
  html+="</table>";
  if(e.acc24!==null&&e.acc24!==undefined){
    html+="<div class='sec'>MRMS rainfall over the footprint ("+(EPCAT.product||"QPE")+")</div><table>";
    html+="<tr><td>Max rolling 6 h</td><td>"+Math.round(e.acc6)+" mm</td></tr>";
    html+="<tr><td>Max rolling 12 h</td><td>"+Math.round(e.acc12)+" mm</td></tr>";
    html+="<tr><td>Max rolling 24 h</td><td>"+Math.round(e.acc24)+" mm</td></tr>";
    html+="<tr><td>Max rolling 72 h</td><td>"+Math.round(e.acc72)+" mm</td></tr>";
    if(e.ari) html+="<tr><td>Max return period</td><td>"+Math.round(e.ari)+" yr ("+(e.aridur||"-")+")</td></tr>";
    html+="</table>";
  }else{
    html+="<div class='sec'>MRMS rainfall</div><div style='color:var(--dim);font-size:12px'>"+
      "Not computed yet for this episode. The download package includes the exact command.</div>";
  }
  html+="<div class='btnrow'>"+
    "<button class='btn2 gold' id='dl-ep'>Download episode package</button>"+
    "<button class='btn2' id='zoom-ep'>Zoom to footprint</button></div>";
  d.innerHTML=html; d.hidden=false;
  el("zoom-ep").addEventListener("click",function(){
    map.fitBounds([[e.bbox[0],e.bbox[1]],[e.bbox[2],e.bbox[3]]],{padding:[36,36],maxZoom:10});
  });
  el("dl-ep").addEventListener("click",function(){ downloadPackage(e,pts); });
}

function csvEsc(v){
  v=(v===null||v===undefined)?"":String(v);
  return (/[",\n]/.test(v))?('"'+v.replace(/"/g,'""')+'"'):v;
}
function toCsv(header,rows){
  var out=header.join(",")+"\n";
  rows.forEach(function(r){ out+=r.map(csvEsc).join(",")+"\n"; });
  return out;
}
function downloadPackage(e,pts){
  var zip=new JSZip();
  var summary={episode_id:e.id, window_utc:[e.t0,e.t1], states:e.states,
    n_events:e.nev, n_lsr:(e.nlsr>=0?e.nlsr:null), fatalities:e.deaths,
    injuries:e.inj, damage_usd:e.dmg, county_fips:e.fips, bbox:e.bbox,
    mrms:(e.acc24!==null&&e.acc24!==undefined)?{product:EPCAT.product,
      max_rolling_mm:{h6:e.acc6,h12:e.acc12,h24:e.acc24,h72:e.acc72},
      max_ari_years:e.ari,max_ari_duration:e.aridur}:null,
    source:"NOAA NCEI Storm Events Database; NWS Local Storm Reports via IEM; NOAA MRMS on AWS",
    note:"Event coordinates are NWS report locations, not storm centers. The bbox and county footprint describe where the episode was reported."};
  zip.file("episode_"+e.id+"/episode_summary.json",JSON.stringify(summary,null,2));
  if(pts&&pts.ev&&pts.ev.length)
    zip.file("episode_"+e.id+"/events.csv",
      toCsv(["lat","lon","begin_utc","deaths","damage_usd"],pts.ev));
  if(pts&&pts.lsr&&pts.lsr.length)
    zip.file("episode_"+e.id+"/lsrs.csv",
      toCsv(["lat","lon","valid_utc","typetext","source"],pts.lsr));
  var sh="#!/bin/bash\n"+
    "# RUNOFF general modeling package, episode "+e.id+"\n"+
    "# 1. clone the repository\n"+
    "git clone https://github.com/NWC-CUAHSI-Summer-Institute/runoff.git\ncd runoff\n\n"+
    "# 2. build the episode catalog once (StormEvents csv.gz files from\n"+
    "#    https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/ )\n"+
    "python engine/episodes/build_catalog.py --stormevents data/stormevents\n\n"+
    "# 3. MRMS rainfall over this episode footprint (rolling 6/12/24/72 h + FLASH ARI)\n"+
    "#    --product: pass2 (gauge corrected, default) | pass1 | radar\n"+
    "python engine/episodes/mrms_stats.py --episode "+e.id+" --product pass2\n\n"+
    "# 4. optional hourly footprint rainfall series for the same episode\n"+
    "python engine/episodes/mrms_stats.py --episode "+e.id+" --product pass2 --timeseries --force\n\n"+
    "# 5. refresh Local Storm Reports for the episode window (optional)\n"+
    "python engine/lsr/fetch_lsrs.py --start "+String(e.t0).slice(0,10)+" --end "+String(e.t1).slice(0,10)+
    " --states "+String(e.states||"").split(",").join(" ")+"\n";
  zip.file("episode_"+e.id+"/get_mrms.sh",sh);
  zip.file("episode_"+e.id+"/README.md",
    "# RUNOFF episode "+e.id+"\n\n"+
    "Weather-caused flash flood episode, "+e.t0+" to "+e.t1+" UTC ("+(e.states||"-")+").\n\n"+
    "Files:\n"+
    "- episode_summary.json: totals, footprint counties, bbox, MRMS stats if built\n"+
    "- events.csv: NOAA Storm Events flash flood reports in this episode\n"+
    "- lsrs.csv: NWS Local Storm Reports matched to this episode (time window + county footprint)\n"+
    "- get_mrms.sh: exact commands to build the MRMS rainfall tables for this episode\n\n"+
    "Event coordinates are report locations, not storm centers; use the footprint\n"+
    "and the MRMS grids to characterize the storm itself.\n");
  zip.generateAsync({type:"blob"}).then(function(blob){
    var a=document.createElement("a");
    a.href=URL.createObjectURL(blob);
    a.download="runoff_episode_"+e.id+".zip";
    document.body.appendChild(a); a.click();
    setTimeout(function(){URL.revokeObjectURL(a.href);a.remove();},800);
  });
}

/* ---------------- layer toggles ---------------- */
el("ly-dots").addEventListener("change",function(e){
  e.target.checked?dotsG.addTo(map):map.removeLayer(dotsG); });
el("ly-ev").addEventListener("change",function(e){
  e.target.checked?evG.addTo(map):map.removeLayer(evG); });
el("ly-lsr").addEventListener("change",function(e){
  e.target.checked?lsrG.addTo(map):map.removeLayer(lsrG); });

/* ---------------- boot ---------------- */
refresh();

}
