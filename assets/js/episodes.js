/* RUNOFF episode browser: the general flash flood modeling path.
   Everything shown here is precomputed; the page only reads static files.

   Data contracts:

   assets/data/episodes.js (engine/episodes/build_catalog.py + build_mrms_payload.py) ->
     var EPCAT={built, wy, mrms:{qpe, ari, thresholds:[1,2,5,10,20,25,50,100,200], n_done,...},
       fields:["id","t0","t1","states","nev","nlsr","deaths","inj","dmg","fips","bbox",
               "hucs","km2","m1","m3","m6","rain","a1","a3","a6"], rows:[[...],...]};
     t0/t1 are UTC "YYYY-MM-DD HH"; nlsr is -1 when LSRs were not fetched;
     hucs = HUC8 codes touched; km2 = footprint area;
     m1/m3/m6 = [max, mean] of the cell peak rolling accumulation, mm;
     rain = [ep_max, ep_mean, pre_mean] mm;
     a1/a3/a6 = [max ARI years, cov permille per threshold...];
     the MRMS fields are null for an episode not computed yet.

   assets/data/episode_points.js ->
     var EPPTS={"<id>":{ev:[[lat,lon,"YYYY-MM-DD HH:MM",deaths,dmgUSD],...],
                        lsr:[[lat,lon,"YYYY-MM-DD HH:MM","TYPETEXT","SOURCE"],...]},...};

   assets/data/ep/<id>.json (engine/episodes/precompute_mrms.py), fetched on click ->
     {id,t0,t1,hours,qpe:{products,missing},ari:{sources,missing,thresholds},
      fp:{area_km2,rain:{ep:{max,mean},pre:{max,mean}},max1:{max,mean,t},max3,max6,
          ari1:{max,cov:[...],valid},ari3,ari6}, cty:{same}, huc8:[{h,n,st,huc_km2,inter_km2,
      frac,nev,nlsr, area_km2,rain,max1..,ari1..},...], series:[[hour,mean,max,ari1max],...]}

   assets/data/huc8.js -> var HUC8GJ (simplified HUC8 polygons, properties.h = code)

   The page degrades cleanly: no payload -> build notice; no LSR fetch -> LSR
   filter hidden; no MRMS statistics at all -> rainfall filters hidden. */
"use strict";
var el=function(id){return document.getElementById(id);};
var fmt$=function(v){ if(v>=1e9)return "$"+(v/1e9).toFixed(2)+"B";
  if(v>=1e6)return "$"+(v/1e6).toFixed(1)+"M"; if(v>=1e3)return "$"+Math.round(v/1e3)+"K";
  return "$"+Math.round(v); };
var fmtN=function(n){ return n.toLocaleString("en-US"); };
var esc=function(s){ return String(s===null||s===undefined?"":s)
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); };
var isNum=function(v){ return typeof v==="number" && isFinite(v); };
var MONTHS=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
var pct=function(f){ return isNum(f)?(f*100).toFixed(f*100<10?1:0)+"%":"-"; };
var mm=function(v){ return isNum(v)?Math.round(v)+" mm":"-"; };
var yr=function(v){ return isNum(v)?(v>=200?"200+ yr":(v<1?"under 1 yr":(v<10?v.toFixed(1):Math.round(v))+" yr")):"-"; };

/* ---------------- map boots regardless of payload ---------------- */
var map=L.map("map",{preferCanvas:true,zoomSnap:.5}).setView([38.6,-95.8],4.5);
var BASE=BASEMAPS.attach(map,"dark");
document.querySelectorAll("#basebox input").forEach(function(r){
  r.addEventListener("change",function(){ BASE.set(this.value); });
});
map.createPane("statepane"); map.getPane("statepane").style.zIndex=430;
map.getPane("statepane").style.pointerEvents="none";
map.createPane("ctypane"); map.getPane("ctypane").style.zIndex=445;
map.getPane("ctypane").style.pointerEvents="none";
/* Every interactive vector (episode dots, watersheds, report points) is drawn by ONE
   canvas renderer in the default overlay pane. Separate canvas panes look right but
   the topmost canvas swallows the clicks meant for the layers below it, so after the
   first selection the dots stop responding. Stacking order is kept with bringToFront. */
L.geoJSON(STATESGJ,{pane:"statepane",interactive:false,
  style:{color:"#9aa5b3",weight:1,opacity:.4,fill:false}}).addTo(map);

/* ---------------- (i) tips: fixed box on the body, never clipped by the panel ---------------- */
(function(){
  var tip=document.createElement("div"); tip.className="tipbox"; tip.hidden=true;
  document.body.appendChild(tip);
  function show(ic){
    tip.textContent=ic.getAttribute("data-tip")||"";
    var r=ic.getBoundingClientRect(), w=Math.min(300,window.innerWidth-16);
    tip.style.width=w+"px";
    tip.style.left=Math.min(Math.max(8,r.left-8),window.innerWidth-w-8)+"px";
    tip.hidden=false;
    var top=r.top-tip.offsetHeight-8; if(top<8) top=r.bottom+8;
    tip.style.top=top+"px";
  }
  document.querySelectorAll(".i[data-tip]").forEach(function(ic){
    ic.addEventListener("mouseenter",function(){ show(ic); });
    ic.addEventListener("mouseleave",function(){ tip.hidden=true; });
    ic.addEventListener("click",function(ev){ ev.preventDefault(); if(tip.hidden) show(ic); else tip.hidden=true; });
  });
  var rail=document.querySelector(".rail");
  if(rail) rail.addEventListener("scroll",function(){ tip.hidden=true; });
})();

/* ---------------- resizable panel: drag the bar between the panel and the map ---------------- */
(function(){
  var wrap=document.querySelector(".wrap"), sp=el("splitter");
  if(!wrap||!sp) return;
  var w=412;
  try{ var s=parseInt(localStorage.getItem("runoff_railw"),10); if(s>=300&&s<=760) w=s; }catch(e){}
  function apply(){ if(window.innerWidth>980) wrap.style.gridTemplateColumns=w+"px 6px 1fr"; }
  apply();
  var drag=false;
  sp.addEventListener("mousedown",function(ev){ drag=true; ev.preventDefault(); document.body.style.userSelect="none"; });
  window.addEventListener("mousemove",function(ev){ if(!drag) return; w=Math.min(760,Math.max(300,ev.clientX)); apply(); });
  window.addEventListener("mouseup",function(){
    if(!drag) return; drag=false; document.body.style.userSelect="";
    try{ localStorage.setItem("runoff_railw",String(w)); }catch(e){}
    map.invalidateSize();
  });
})();

/* ---------------- payload check ---------------- */
var HAVE_CAT=(typeof EPCAT!=="undefined")&&EPCAT&&EPCAT.rows&&EPCAT.rows.length>0;
var HAVE_PTS=(typeof EPPTS!=="undefined")&&!!EPPTS;
var HAVE_HUC=(typeof HUC8GJ!=="undefined")&&!!HUC8GJ;
if(!HAVE_CAT){
  el("nobuild").hidden=false;
}else{
  el("filters").hidden=false;
  boot();
}

function boot(){

/* ---------------- unpack catalog ---------------- */
var THR=(EPCAT.mrms&&EPCAT.mrms.thresholds)||[1,2,5,10,20,25,50,100,200];
var EPS=EPCAT.rows.map(function(r){
  var o={};
  EPCAT.fields.forEach(function(f,i){o[f]=r[i];});
  o.year=parseInt(String(o.t0).slice(0,4),10);
  o.c=[(o.bbox[0]+o.bbox[2])/2,(o.bbox[1]+o.bbox[3])/2];
  o.has=!!(o.a1&&o.m1);
  o.hours=hoursBetween(o.t0,o.t1);
  return o;
});
var hasLSR=EPS.some(function(e){return e.nlsr>=0;});
var N_MRMS=EPS.filter(function(e){return e.has;}).length;
var hasMRMS=N_MRMS>0;
if(!hasLSR){ el("lab-lsr").style.display="none"; el("f-lsr").style.display="none"; }
if(hasMRMS){
  el("mrmsfilters").hidden=false; el("colorbox").hidden=false;
  el("v-done").textContent=fmtN(N_MRMS)+" of "+fmtN(EPS.length)+" computed";
}
function hoursBetween(a,b){
  var d=(Date.parse(String(b).replace(" ","T")+":00:00Z")-Date.parse(String(a).replace(" ","T")+":00:00Z"))/36e5;
  return isFinite(d)?d:null;
}

/* per episode accessors that follow the chosen duration and threshold */
var UI={dur:1, thr:1};       /* thr = index into THR */
function mArr(e){ return e["m"+UI.dur]; }
function aArr(e){ return e["a"+UI.dur]; }
function mMax(e){ var a=mArr(e); return a&&isNum(a[0])?a[0]:null; }
function rMean(e){ return e.rain&&isNum(e.rain[1])?e.rain[1]:null; }
function aMax(e){ var a=aArr(e); return a&&isNum(a[0])?a[0]:null; }
function covAt(e,ti){ var a=aArr(e); return a&&isNum(a[1+ti])?a[1+ti]/1000:null; }

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

/* threshold select */
var thrSel=el("sel-thr");
THR.forEach(function(t,i){
  var o=document.createElement("option"); o.value=i; o.textContent=t+" year"+(t>1?"s":"")+(t>=200?" (cap)":"");
  thrSel.appendChild(o);
});
thrSel.value=1; UI.thr=1;
thrSel.addEventListener("change",function(){ UI.thr=parseInt(thrSel.value,10); refresh(); redrawSelection(); });

/* duration segmented control */
var MMAX_RANGE={1:200,3:300,6:400};
document.querySelectorAll("#seg-dur input").forEach(function(r){
  r.addEventListener("change",function(){
    UI.dur=parseInt(this.value,10);
    document.querySelectorAll("#seg-dur label").forEach(function(l){ l.classList.toggle("on", l.querySelector("input").checked); });
    ["lab-dur1","lab-dur2","lab-dur3"].forEach(function(id){ el(id).textContent=UI.dur+" h"; });
    var s=el("f-mmax"); s.max=MMAX_RANGE[UI.dur]; if(parseInt(s.value,10)>s.max) s.value=s.max;
    el("v-mmax").textContent=s.value+" mm";
    refresh(); redrawSelection();
  });
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
var sMmax=slider("f-mmax","v-mmax",function(v){return v+" mm";});
var sRmean=slider("f-rmean","v-rmean",function(v){return v+" mm";});
var sCov=slider("f-cov","v-cov",function(v){return v+" %";});
var sAmax=slider("f-amax","v-amax",function(v){return v<=0?"any":(">= "+v+" yr");});
el("f-only").addEventListener("change",refresh);
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
    /* a rainfall threshold above zero excludes episodes whose MRMS statistics
       have not been computed yet (unknown is not the same as low) */
    var vm=parseInt(sMmax.value,10), vr=parseInt(sRmean.value,10),
        vc=parseInt(sCov.value,10), va=parseInt(sAmax.value,10);
    if(el("f-only").checked && !e.has) return false;
    if(vm>0 && !(mMax(e)>=vm)) return false;
    if(vr>0 && !(rMean(e)>=vr)) return false;
    if(vc>0 && !(covAt(e,UI.thr)*100>=vc)) return false;
    if(va>0 && !(aMax(e)>=va)) return false;
  }
  return true;
}
function sortKey(e,k){
  if(k==="date") return e.t0;
  if(k==="mmax") return mMax(e);
  if(k==="rmean") return rMean(e);
  if(k==="cov") return covAt(e,UI.thr);
  if(k==="amax") return aMax(e);
  var v=e[k];
  return (v===null||v===undefined)?-1:v;
}
var matched=[], dotsSig="";
function refresh(){
  var k=el("sel-sort").value;
  matched=EPS.filter(matches);
  matched.sort(function(a,b){
    var av=sortKey(a,k), bv=sortKey(b,k);
    if(av===null||av===undefined) av=-1; if(bv===null||bv===undefined) bv=-1;
    return av<bv?1:(av>bv?-1:0);
  });
  var t={ev:0,d:0,dmg:0,has:0};
  matched.forEach(function(e){t.ev+=e.nev;t.d+=e.deaths;t.dmg+=e.dmg;if(e.has)t.has++;});
  el("rescount").innerHTML="<b>"+fmtN(matched.length)+"</b> of "+fmtN(EPS.length)+
    " episodes match: "+fmtN(t.ev)+" events, "+fmtN(t.d)+" fatalities, "+fmt$(t.dmg)+" damage."+
    (hasMRMS?(" "+fmtN(t.has)+" of them have MRMS statistics."):" MRMS statistics are not built in this copy of the site.");
  renderList();
  renderDots();
  renderLegend();
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
    if(e.has){
      var cv=covAt(e,UI.thr);
      extra="<br><em>"+UI.dur+" h peak "+mm(mMax(e))+"</em>, "+yr(aMax(e))+" peak return period, "+
        pct(cv)+" at or above "+THR[UI.thr]+" yr";
    }else if(hasMRMS){ extra="<br>MRMS statistics not computed yet"; }
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
var rend=L.canvas({padding:0.4});   /* the one shared renderer, see the note above */
var dotsG=L.layerGroup().addTo(map);
var hucG=L.layerGroup().addTo(map);
var ctyG=L.layerGroup().addTo(map);
var evG=L.layerGroup().addTo(map);
var lsrG=L.layerGroup().addTo(map);
function raisePoints(){   /* report points above everything else on the shared canvas */
  [evG,lsrG].forEach(function(g){ g.eachLayer(function(l){ if(l.bringToFront) l.bringToFront(); }); });
}
function raiseSelection(){  /* watersheds above the dots, then the points above the watersheds */
  hucG.eachLayer(function(l){ if(l.bringToFront) l.bringToFront(); });
  raisePoints();
}
function metricOfEp(e,k){   /* footprint level value of the map metric from EPCAT */
  if(!e.has) return null;
  if(k==="cov") return covAt(e,UI.thr);
  if(k==="mmax") return mMax(e);
  if(k==="mmean"){ var a=mArr(e); return a&&isNum(a[1])?a[1]:null; }
  if(k==="amax") return aMax(e);
  if(k==="rmean") return rMean(e);
  return null;
}
function renderDots(){
  /* skip the rebuild when the matched set and the colouring are unchanged (slider drags fire fast) */
  var k=hasMRMS?el("sel-metric").value:"";
  var sig=matched.length+":"+(matched.length?matched[0].id+"-"+matched[matched.length-1].id:"")+":"+k+":"+UI.dur+":"+UI.thr;
  if(hasMRMS){ var note=el("colornote"); if(note) note.textContent="Dots: every matching episode with statistics, footprint value. Polygons: the watersheds of the selected episode. Duration and threshold follow the filters: "+UI.dur+" h, "+THR[UI.thr]+" yr."; }
  if(sig===dotsSig) return;
  dotsSig=sig;
  dotsG.clearLayers();
  matched.forEach(function(e){
    var v=hasMRMS?metricOfEp(e,k):null;
    var m=L.circleMarker(e.c,{renderer:rend,radius:Math.min(9,2+Math.sqrt(e.nev)),
      weight:isNum(v)?0.6:0,color:"#0b0e13",fillOpacity:isNum(v)?.85:.45,fillColor:isNum(v)?colorOf(k,v):"#8b95a5"});
    m.bindTooltip("Episode "+e.id+" | "+epTitle(e)+" | "+e.nev+" events"+
      (isNum(v)?(" | "+SCALES[k].fmt(k==="cov"?Math.round(v*100)/100:Math.round(v))):""),{sticky:true});
    m.on("click",function(){ select(e,null); });
    dotsG.addLayer(m);
  });
  raiseSelection();   /* redrawn dots would otherwise sit on top of the selection */
}

/* geometry indexes */
var FIPSIDX={};
COUNTYGJ.features.forEach(function(f){ FIPSIDX[f.properties.f]=f; });
var HUCIDX={};
if(HAVE_HUC) HUC8GJ.features.forEach(function(f){ HUCIDX[f.properties.h]=f; });

/* ---------------- colour scales ---------------- */
var RAMP=["#2c3a56","#3f5f96","#5b8dd9","#c9a13d","#dd7a33","#c94040"];
var SCALES={
  cov:  {breaks:[0.02,0.10,0.25,0.50,0.75], fmt:function(v){return Math.round(v*100)+"%";},
         label:function(){return "share of the watershed at or above "+THR[UI.thr]+" yr, "+UI.dur+" h";}},
  mmax: {breaks:{1:[10,25,50,75,100],3:[15,40,75,110,150],6:[20,50,90,140,200]},
         fmt:function(v){return v+" mm";}, label:function(){return "peak "+UI.dur+" h accumulation, any cell";}},
  mmean:{breaks:{1:[5,10,20,35,50],3:[8,15,30,50,80],6:[10,20,40,70,100]},
         fmt:function(v){return v+" mm";}, label:function(){return "mean of the cell peaks, "+UI.dur+" h";}},
  amax: {breaks:[2,5,10,25,100], fmt:function(v){return v+" yr";},
         label:function(){return "peak "+UI.dur+" h return period, any cell";}},
  rmean:{breaks:[10,25,50,75,100], fmt:function(v){return v+" mm";},
         label:function(){return "episode rain, watershed mean";}}
};
function breaksOf(k){ var b=SCALES[k].breaks; return Array.isArray(b)?b:b[UI.dur]; }
function colorOf(k,v){
  if(!isNum(v)) return "#3a4250";
  var b=breaksOf(k);
  for(var i=0;i<b.length;i++) if(v<b[i]) return RAMP[i];
  return RAMP[5];
}
function metricOf(h,k){   /* h = huc8 entry of the episode JSON */
  if(!h) return null;
  if(k==="cov"){ var a=h["ari"+UI.dur]; return a&&a.cov?a.cov[UI.thr]:null; }
  if(k==="mmax"){ var m=h["max"+UI.dur]; return m?m.max:null; }
  if(k==="mmean"){ var m2=h["max"+UI.dur]; return m2?m2.mean:null; }
  if(k==="amax"){ var a2=h["ari"+UI.dur]; return a2?a2.max:null; }
  if(k==="rmean"){ return h.rain&&h.rain.ep?h.rain.ep.mean:null; }
  return null;
}
el("sel-metric").addEventListener("change",function(){ renderDots(); renderLegend(); redrawSelection(); });

function renderLegend(){
  var lg=el("maplegend"), html="";
  if(hasMRMS){
    var k=el("sel-metric").value, b=breaksOf(k), sc=SCALES[k];
    html+="<div class='t'>"+sc.label()+"</div>";
    html+="<div class='k'><span class='sw' style='background:"+RAMP[0]+"'></span>under "+sc.fmt(b[0])+"</div>";
    for(var i=1;i<b.length;i++)
      html+="<div class='k'><span class='sw' style='background:"+RAMP[i]+"'></span>"+sc.fmt(b[i-1])+" to "+sc.fmt(b[i])+"</div>";
    html+="<div class='k'><span class='sw' style='background:"+RAMP[5]+"'></span>"+sc.fmt(b[b.length-1])+" and over</div>";
    html+="<div class='k'><span class='dt' style='background:#8b95a5'></span>episode without statistics yet</div>";
  }else{
    html+="<div class='k'><span class='dt' style='background:#8b95a5'></span>episode (matches filters)</div>";
    if(SEL) html+="<div class='k'><span class='sw' style='background:rgba(242,183,5,.15);border:1px solid #f2b705'></span>watershed of the episode</div>";
  }
  html+="<div class='k'><span class='sw' style='border:1.5px dashed #f2b705;background:transparent'></span>reporting county</div>";
  html+="<div class='k'><span class='dt' style='background:#f2b705'></span>Storm Events flash flood</div>";
  html+="<div class='k'><span class='dt' style='background:#5b8dd9'></span>Local Storm Report</div>";
  lg.innerHTML=html;
}

/* ---------------- selection ---------------- */
var SEL=null, SELJ=null, SELPTS=null, SELREP=null, HUCLAYERS={}, JCACHE={}, RCACHE={};
function clearSelection(){
  SEL=null; SELJ=null; SELPTS=null; SELREP=null; HUCLAYERS={};
  hucG.clearLayers(); ctyG.clearLayers(); evG.clearLayers(); lsrG.clearLayers();
  el("detail").hidden=true;
  document.querySelectorAll(".eprow.on").forEach(function(r){r.classList.remove("on");});
  renderLegend();
}
function select(e,row){
  clearSelection();
  SEL=e;
  if(row) row.classList.add("on");
  /* county footprint outline */
  var feats=(e.fips||[]).map(function(f){return FIPSIDX[f];}).filter(Boolean);
  if(feats.length){
    ctyG.addLayer(L.geoJSON({type:"FeatureCollection",features:feats},{pane:"ctypane",interactive:false,
      style:{color:"#f2b705",weight:1.4,dashArray:"4 3",fill:false,opacity:.9}}));
  }
  /* points: from the map payload now, redrawn with narratives once rep/<id>.json is in */
  var pts=HAVE_PTS?(EPPTS[String(e.id)]||EPPTS[e.id]||null):null;
  SELPTS=pts;
  /* watersheds first (without statistics, then with them once the JSON is in), points on top */
  drawHucs(e,null);
  drawPoints(pts);
  loadReports(e);
  zoomTo(e,9);
  renderDetail(e,null);
  if(e.has || hasMRMS) loadJson(e);
}
/* pts rows: ev [lat,lon,ts,deaths,dmg(,narrative,county,state)]
             lsr [lat,lon,ts,type,source(,remark,city,county)] */
function drawPoints(pts){
  evG.clearLayers(); lsrG.clearLayers();
  if(!pts) return;
  (pts.ev||[]).forEach(function(p){
    var m=L.circleMarker([p[0],p[1]],{renderer:rend,radius:4,weight:1,
      color:"#161308",fillOpacity:.95,fillColor:"#f2b705"});
    m.bindPopup("<b>Storm Events flash flood</b><br>"+esc(p[2])+" UTC"+
      (p[6]?("<br>"+esc(p[6])+(p[7]?(", "+esc(p[7])):"")):"")+
      (p[3]?("<br>Fatalities: "+p[3]):"")+(p[4]?("<br>Damage: "+fmt$(p[4])):"")+
      (p[5]?("<br><span class='desc'>Description: "+esc(p[5])+"</span>"):""),{maxWidth:360});
    evG.addLayer(m);
  });
  (pts.lsr||[]).forEach(function(p){
    var m=L.circleMarker([p[0],p[1]],{renderer:rend,radius:3.4,weight:1,
      color:"#0a1220",fillOpacity:.95,fillColor:"#5b8dd9"});
    m.bindPopup("<b>Local Storm Report: "+esc(p[3]||"FLASH FLOOD")+"</b><br>"+esc(p[2])+" UTC"+
      (p[6]||p[7]?("<br>"+esc(p[6]||"")+(p[6]&&p[7]?", ":"")+esc(p[7]||"")):"")+
      (p[4]?("<br>Source: "+esc(p[4])):"")+
      (p[5]?("<br><span class='desc'>Description: "+esc(p[5])+"</span>"):""),{maxWidth:360});
    lsrG.addLayer(m);
  });
}
function loadReports(e){
  if(RCACHE[e.id]!==undefined){ if(RCACHE[e.id]) applyReports(e,RCACHE[e.id]); return; }
  fetch("assets/data/rep/"+e.id+".json",{cache:"force-cache"}).then(function(r){
    if(!r.ok) throw new Error("HTTP "+r.status); return r.json();
  }).then(function(rep){
    RCACHE[e.id]=rep; if(SEL===e) applyReports(e,rep);
  }).catch(function(){ RCACHE[e.id]=null; });
}
function applyReports(e,rep){
  SELREP=rep;
  drawPoints({ev:rep.ev||[],lsr:rep.lsr||[]});
  renderDetail(e,SELJ);
}
function zoomTo(e,maxZoom){
  var b=null;
  if(HAVE_HUC) (e.hucs||[]).forEach(function(h){ var f=HUCIDX[h]; if(!f) return;
    var lb=L.geoJSON(f).getBounds(); b=b?b.extend(lb):lb; });
  if(!b) b=L.latLngBounds([[e.bbox[0],e.bbox[1]],[e.bbox[2],e.bbox[3]]]);
  map.fitBounds(b,{padding:[36,36],maxZoom:maxZoom||9});
}
function loadJson(e){
  if(JCACHE[e.id]!==undefined){ if(JCACHE[e.id]) applyJson(e,JCACHE[e.id]); return; }
  fetch("assets/data/ep/"+e.id+".json",{cache:"force-cache"}).then(function(r){
    if(!r.ok) throw new Error("HTTP "+r.status); return r.json();
  }).then(function(j){
    JCACHE[e.id]=j; if(SEL===e) applyJson(e,j);
  }).catch(function(){
    JCACHE[e.id]=null;
    if(SEL===e){ var n=el("mrms-note"); if(n) n.textContent=e.has?
      "The per episode file could not be loaded (open the site through a web server, not from a file path).":
      "MRMS statistics have not been computed for this episode yet."; }
  });
}
function applyJson(e,j){
  SELJ=j;
  drawHucs(e,j);
  renderDetail(e,j);
}
function redrawSelection(){
  if(!SEL) return;
  drawHucs(SEL,SELJ);
  renderDetail(SEL,SELJ);
}
function drawHucs(e,j){
  hucG.clearLayers(); HUCLAYERS={};
  if(!HAVE_HUC) return;
  var k=el("sel-metric").value;
  var byH={};
  if(j) (j.huc8||[]).forEach(function(h){ byH[h.h]=h; });
  (e.hucs||[]).forEach(function(code){
    var f=HUCIDX[code]; if(!f) return;
    var h=byH[code]||null, v=h?metricOf(h,k):null;
    var lyr=L.geoJSON(f,{renderer:rend,style:{
      color:"#f2b705",weight:1,opacity:.85,
      fillColor:h?colorOf(k,v):"#f2b705", fillOpacity:h?.62:.12}});
    lyr.on("click",function(){ hucPopup(lyr,code,f.properties.n,h); highlightRow(code); });
    lyr.on("mouseover",function(){ lyr.setStyle({weight:2.4}); lyr.bringToFront(); raisePoints(); });
    lyr.on("mouseout",function(){ lyr.setStyle({weight:1}); });
    HUCLAYERS[code]=lyr;
    hucG.addLayer(lyr);
  });
  raisePoints();   /* the watersheds were just (re)drawn on top of the report points */
  renderLegend();
}
function hucPopup(lyr,code,name,h){
  var html="<b>"+esc(name||"")+"</b> (HUC8 "+code+")";
  if(h){
    html+="<table>";
    html+="<tr><td>Watershed area</td><td>"+fmtN(Math.round(h.huc_km2||h.area_km2||0))+" km2</td></tr>";
    html+="<tr><td>Share inside the reporting counties</td><td>"+pct(h.frac)+"</td></tr>";
    html+="<tr><td>Flash flood events / LSRs</td><td>"+h.nev+" / "+h.nlsr+"</td></tr>";
    html+="<tr><td>Episode rain, mean / max</td><td>"+mm(h.rain&&h.rain.ep?h.rain.ep.mean:null)+" / "+mm(h.rain&&h.rain.ep?h.rain.ep.max:null)+"</td></tr>";
    [1,3,6].forEach(function(d){
      var m=h["max"+d]||{}, a=h["ari"+d]||{};
      html+="<tr><td>Peak "+d+" h accumulation</td><td>"+mm(m.max)+"</td></tr>";
      html+="<tr><td>Peak "+d+" h return period</td><td>"+yr(a.max)+"</td></tr>";
    });
    var a=h["ari"+UI.dur]||{};
    if(a.cov){
      html+="<tr><td colspan=2 style='padding-top:5px'><b>Share of the watershed at or above, "+UI.dur+" h</b></td></tr>";
      var cells="";
      THR.forEach(function(t,i){ cells+="<tr><td>"+t+" yr</td><td>"+pct(a.cov[i])+"</td></tr>"; });
      html+=cells;
    }
    html+="</table>";
  }else{
    html+="<br>No MRMS statistics yet.";
  }
  lyr.bindPopup(html,{maxWidth:320}).openPopup();
}
function highlightRow(code){
  document.querySelectorAll(".detail tr.hrow").forEach(function(r){ r.classList.toggle("on", r.dataset.h===code); });
}

/* ---------------- detail card ---------------- */
function sparkline(series){
  if(!series||!series.length) return "";
  var W=360,H=78,padL=28,padB=14,padT=8;
  var vmax=0; series.forEach(function(s){ if(isNum(s[2])&&s[2]>vmax) vmax=s[2]; });
  if(vmax<=0) return "";
  var n=series.length, iw=(W-padL-6)/n, ih=H-padB-padT;
  var y=function(v){ return padT+ih-(v/vmax)*ih; };
  var s="<svg class='spark' viewBox='0 0 "+W+" "+H+"' preserveAspectRatio='none'>";
  s+="<line x1='"+padL+"' y1='"+(padT+ih)+"' x2='"+(W-6)+"' y2='"+(padT+ih)+"' stroke='#2f3949'/>";
  s+="<text x='0' y='"+(padT+8)+"' fill='#6b7686' font-size='9'>"+Math.round(vmax)+" mm</text>";
  s+="<text x='0' y='"+(padT+ih)+"' fill='#6b7686' font-size='9'>0 mm</text>";
  var pts=[];
  series.forEach(function(r,i){
    var x=padL+i*iw;
    if(isNum(r[1])) s+="<rect x='"+(x+1).toFixed(1)+"' y='"+y(r[1]).toFixed(1)+"' width='"+Math.max(1,iw-2).toFixed(1)+"' height='"+(padT+ih-y(r[1])).toFixed(1)+"' fill='#5b8dd9' opacity='.85'/>";
    if(isNum(r[2])) pts.push((x+iw/2).toFixed(1)+","+y(r[2]).toFixed(1));
  });
  if(pts.length>1) s+="<polyline points='"+pts.join(" ")+"' fill='none' stroke='#f2b705' stroke-width='1.6'/>";
  var step=Math.max(1,Math.ceil(n/6));
  series.forEach(function(r,i){ if(i%step===0) s+="<text x='"+(padL+i*iw+1).toFixed(1)+"' y='"+(H-3)+"' fill='#6b7686' font-size='8.5'>"+r[0].slice(5,13)+"</text>"; });
  s+="</svg>";
  return s;
}
function renderDetail(e,j){
  var d=el("detail");
  var dur=UI.dur, ti=UI.thr, T=THR[ti];
  var html="<h3>Episode "+e.id+"</h3><div class='per'>"+e.t0+" to "+e.t1+" UTC"+
    (isNum(e.hours)?(" | "+e.hours+" h"):"")+" | "+(e.states||"-")+
    "<br>"+(e.fips?e.fips.length:0)+" reporting count"+((e.fips||[]).length===1?"y":"ies")+", "+
    (e.hucs?e.hucs.length:0)+" HUC8 watershed"+((e.hucs||[]).length===1?"":"s")+
    (isNum(e.km2)?(", footprint "+fmtN(Math.round(e.km2))+" km2"):"")+"</div>";
  html+="<div class='kv'>"+
    "<div><b>"+e.nev+"</b><span>flash flood events</span></div>"+
    "<div><b>"+(e.nlsr>=0?e.nlsr:"-")+"</b><span>Local Storm Reports</span></div>"+
    "<div><b>"+e.deaths+" / "+e.inj+"</b><span>fatalities / injuries</span></div>"+
    "<div><b>"+fmt$(e.dmg)+"</b><span>reported damage</span></div></div>";

  if(SELREP&&SELREP.narr){
    html+="<div class='sec'>Episode narrative <small>NWS, Storm Events Database</small></div>"+
      "<div class='narr'>"+esc(SELREP.narr)+"</div>"+
      "<div class='dq'>Each report on the map carries its own description: click a gold Storm Events point or a blue Local Storm Report.</div>";
  }

  if(j&&j.fp&&j.fp.rain){
    var fp=j.fp, rn=fp.rain;
    html+="<div class='sec'>Rainfall over the footprint <small>MRMS QPE, Pass 2</small></div>";
    html+="<div class='kv'>"+
      "<div><b>"+mm(rn.ep.mean)+"</b><span>episode rain, footprint mean</span></div>"+
      "<div><b>"+mm(rn.ep.max)+"</b><span>episode rain, wettest cell</span></div></div>";
    html+="<table><tr><th>window</th><th class='n'>peak, any cell</th><th class='n'>at</th><th class='n'>mean of cell peaks</th></tr>";
    [1,3,6].forEach(function(dd){ var m=fp["max"+dd]||{};
      html+="<tr"+(dd===dur?" style='color:var(--text)'":"")+"><td>"+dd+" h</td><td class='n'>"+mm(m.max)+"</td><td class='n' style='font-weight:400;color:var(--dim)'>"+
        (m.t?esc(m.t.slice(5)):"-")+"</td><td class='n'>"+mm(m.mean)+"</td></tr>"; });
    html+="</table>";
    if(rn.pre&&isNum(rn.pre.mean)) html+="<div class='dq'>In the 5 hours before the episode start: "+mm(rn.pre.mean)+" footprint mean, "+mm(rn.pre.max)+" wettest cell.</div>";

    html+="<div class='sec'>Return periods <small>FLASH QPE ARI, peak over the episode</small></div>";
    html+="<table><tr><th>window</th><th class='n'>peak, any cell</th><th class='n'>&ge; 2 yr</th><th class='n'>&ge; 10 yr</th><th class='n'>&ge; 100 yr</th></tr>";
    var i2=THR.indexOf(2), i10=THR.indexOf(10), i100=THR.indexOf(100);
    [1,3,6].forEach(function(dd){ var a=fp["ari"+dd]||{}, c=a.cov||[];
      html+="<tr"+(dd===dur?" style='color:var(--text)'":"")+"><td>"+dd+" h</td><td class='n'>"+yr(a.max)+"</td><td class='n'>"+pct(c[i2])+"</td><td class='n'>"+pct(c[i10])+"</td><td class='n'>"+pct(c[i100])+"</td></tr>"; });
    html+="</table>";
    var ad=fp["ari"+dur]||{};
    if(ad.cov){
      html+="<div class='dq' style='margin-top:8px'>Share of the footprint area at or above each return period, "+dur+" h window"+
        (isNum(ad.valid)&&ad.valid<0.98?(" ("+pct(1-ad.valid)+" of the area has no ARI value)"):"")+":</div>";
      html+="<div class='covbar'>";
      THR.forEach(function(t,i){ var c=ad.cov[i]||0;
        html+="<div"+(i===ti?" style='color:var(--gold)'":"")+"><i><b style='height:"+Math.round(Math.max(c,0.002)*100)+"%'></b></i><em>"+pct(c)+"</em>"+t+" yr</div>"; });
      html+="</div>";
    }
    if(j.series&&j.series.length>1){
      html+="<div class='sec'>Hourly footprint rain <small>bars mean, line wettest cell</small></div>"+sparkline(j.series);
      var mx=j.series.map(function(r){return r[2];}).filter(isNum), ax=j.series.map(function(r){return r[3];}).filter(isNum);
      if(mx.length) html+="<div class='dq'>Hour by hour, the wettest cell of the footprint received between "+
        Math.round(Math.min.apply(null,mx))+" and "+Math.round(Math.max.apply(null,mx))+" mm"+
        (ax.length?("; the highest 1 h return period in the footprint ranged from "+yr(Math.min.apply(null,ax))+" to "+yr(Math.max.apply(null,ax))):"")+".</div>";
    }
    /* watersheds */
    var hs=(j.huc8||[]).slice();
    var k=el("sel-metric").value;
    hs.sort(function(a,b){ var av=metricOf(a,k), bv=metricOf(b,k); av=isNum(av)?av:-1; bv=isNum(bv)?bv:-1; return bv-av; });
    html+="<div class='sec'>Watersheds <small>"+hs.length+" HUC8, in the order of the map colour</small></div>";
    html+="<table><tr><th>watershed</th><th class='n'>ev/LSR</th><th class='n'>peak "+dur+" h</th><th class='n'>ARI "+dur+" h</th><th class='n'>&ge; "+T+" yr</th></tr>";
    hs.forEach(function(h){ var m=h["max"+dur]||{}, a=h["ari"+dur]||{};
      html+="<tr class='hrow' data-h='"+h.h+"'><td>"+esc(h.n||h.h)+"<br><span style='color:var(--dim);font-size:10.5px'>"+h.h+", "+fmtN(Math.round(h.huc_km2||0))+" km2</span></td>"+
        "<td class='n'>"+h.nev+" / "+h.nlsr+"</td><td class='n'>"+mm(m.max)+"</td><td class='n'>"+yr(a.max)+"</td><td class='n'>"+pct(a.cov?a.cov[ti]:null)+"</td></tr>"; });
    html+="</table>";
    /* data notes */
    var PN={pass2:"Pass 2",pass1:"Pass 1",radar:"radar only"};
    var prods=Object.keys(j.qpe.products||{}).map(function(p){ var b=p.split("@");
      return (PN[b[0]]||b[0])+(b[1]?(" from "+b[1].toUpperCase()):"")+" ("+j.qpe.products[p]+" h)"; }).join(", ");
    var miss=(j.qpe.missing||[]).length, amiss=0; [1,3,6].forEach(function(dd){ amiss+=((j.ari.missing||{})[String(dd)]||[]).length; });
    html+="<div class='dq'>Windows end from "+e.t0+" to one hour after "+e.t1+" UTC ("+j.hours+" hourly fields; the 5 h before the start are read for the rolling windows). QPE files: "+esc(prods)+
      (miss?("; "+miss+" hour"+(miss>1?"s":"")+" missing from both archives"):"")+". FLASH ARI"+
      (amiss?(": "+amiss+" duration hour"+(amiss>1?"s":"")+" missing"):" complete")+
      (j.ari.dry_hours_not_sampled?("; "+j.ari.dry_hours_not_sampled+" dry hour"+(j.ari.dry_hours_not_sampled>1?"s":"")+" not sampled"):"")+
      ". Coverage counts cells without an ARI value as not exceeding. Areas are cos latitude weighted, cells of about 1 km.</div>";
    if(j.qpe.artifact_cells>0)
      html+="<div class='dq' style='color:#e8b4a8'>Caution: "+fmtN(j.qpe.artifact_cells)+" grid cells ("+fmtN(j.qpe.artifact_cells_in_footprint||0)+
        " inside the footprint) carried hourly values above 100 mm in four or more hours, or above 150 mm in three, which is a radar artifact rather than rain. "+
        "They were removed from every statistic, but neighbouring cells may still be inflated; treat the peaks of this episode with care.</div>";
  }else{
    html+="<div class='sec'>MRMS rainfall and return periods</div><div id='mrms-note' style='color:var(--dim);font-size:12px'>"+
      (e.has?"Loading the precomputed statistics...":(hasMRMS?"Not computed yet for this episode.":"Not built in this copy of the site."))+"</div>";
  }
  html+="<div class='btnrow'>"+
    "<button class='btn2 gold' id='dl-ep'>Download episode package</button>"+
    "<button class='btn2' id='zoom-ep'>Zoom to footprint</button></div>";
  d.innerHTML=html; d.hidden=false;
  el("zoom-ep").addEventListener("click",function(){ zoomTo(e,10); });
  el("dl-ep").addEventListener("click",function(){ downloadPackage(e,SELPTS,j); });
  d.querySelectorAll("tr.hrow").forEach(function(r){
    r.addEventListener("click",function(){
      var code=r.dataset.h, lyr=HUCLAYERS[code];
      highlightRow(code);
      if(lyr){ map.fitBounds(lyr.getBounds(),{padding:[30,30],maxZoom:10});
        var h=null; (j.huc8||[]).forEach(function(x){ if(x.h===code) h=x; });
        hucPopup(lyr,code,h?h.n:code,h); }
    });
  });
}

/* ---------------- package download ---------------- */
function csvEsc(v){
  v=(v===null||v===undefined)?"":String(v);
  return (/[",\n]/.test(v))?('"'+v.replace(/"/g,'""')+'"'):v;
}
function toCsv(header,rows){
  var out=header.join(",")+"\n";
  rows.forEach(function(r){ out+=r.map(csvEsc).join(",")+"\n"; });
  return out;
}
function statRow(name,code,s){
  var r=[name,code,s.area_km2];
  var rn=s.rain||{ep:{},pre:{}};
  r.push(rn.ep?rn.ep.max:null, rn.ep?rn.ep.mean:null, rn.pre?rn.pre.max:null, rn.pre?rn.pre.mean:null);
  [1,3,6].forEach(function(d){ var m=s["max"+d]||{}; r.push(m.max,m.mean,m.t||""); });
  [1,3,6].forEach(function(d){ var a=s["ari"+d]||{}; r.push(a.max,a.valid); (a.cov||THR.map(function(){return null;})).forEach(function(c){ r.push(c); }); });
  return r;
}
function statHeader(){
  var h=["unit","code","area_km2","rain_ep_max_mm","rain_ep_mean_mm","rain_pre5h_max_mm","rain_pre5h_mean_mm"];
  [1,3,6].forEach(function(d){ h.push("max"+d+"h_peak_mm","max"+d+"h_mean_of_peaks_mm","max"+d+"h_peak_time_utc"); });
  [1,3,6].forEach(function(d){ h.push("ari"+d+"h_peak_yr","ari"+d+"h_valid_frac"); THR.forEach(function(t){ h.push("ari"+d+"h_cov_ge"+t+"yr"); }); });
  return h;
}
function downloadPackage(e,pts,j){
  var zip=new JSZip(), dir="episode_"+e.id+"/";
  var rep=SELREP||null;
  var summary={episode_id:e.id, window_utc:[e.t0,e.t1], duration_h:e.hours, states:e.states,
    n_events:e.nev, n_lsr:(e.nlsr>=0?e.nlsr:null), fatalities:e.deaths,
    injuries:e.inj, damage_usd:e.dmg, county_fips:e.fips, huc8:e.hucs, report_bbox:e.bbox,
    footprint_km2:e.km2,
    episode_narrative:(rep&&rep.narr)?rep.narr:null,
    mrms:(j&&j.fp)?{qpe:EPCAT.mrms.qpe, ari:EPCAT.mrms.ari, thresholds:THR, footprint:j.fp, counties:j.cty,
      qpe_files:j.qpe, ari_files:j.ari}:null,
    source:"NOAA NCEI Storm Events Database; NWS Local Storm Reports via IEM; MRMS archive via IEM (AWS fallback); FLASH QPE ARI (NSSL); USGS WBD HUC8",
    note:"Event coordinates are NWS report locations, not storm centers. The watershed footprint and the MRMS fields describe where and how hard it rained."};
  zip.file(dir+"episode_summary.json",JSON.stringify(summary,null,2));
  /* with narratives when rep/<id>.json was loaded, plain rows otherwise */
  var evRows=(rep&&rep.ev&&rep.ev.length)?rep.ev:((pts&&pts.ev)||[]);
  var lsrRows=(rep&&rep.lsr&&rep.lsr.length)?rep.lsr:((pts&&pts.lsr)||[]);
  if(evRows.length)
    zip.file(dir+"events.csv",toCsv(["lat","lon","begin_utc","deaths","damage_usd","narrative","county","state"],evRows));
  if(lsrRows.length)
    zip.file(dir+"lsrs.csv",toCsv(["lat","lon","valid_utc","typetext","source","narrative","city","county"],lsrRows));
  if(j&&j.fp){
    zip.file(dir+"footprint_stats.csv",toCsv(statHeader(),[statRow("footprint_huc8_union","",j.fp),statRow("reporting_counties","",j.cty)]));
    var hrows=(j.huc8||[]).map(function(h){ var r=statRow(h.n,h.h,h); r.splice(3,0,h.huc_km2,h.inter_km2,h.frac,h.nev,h.nlsr); return r; });
    var hh=statHeader(); hh.splice(3,0,"huc8_km2","inside_counties_km2","inside_counties_frac","n_events","n_lsr");
    zip.file(dir+"huc8_stats.csv",toCsv(hh,hrows));
    zip.file(dir+"hourly_footprint.csv",toCsv(["hour_utc","rain_mean_mm","rain_max_mm","ari1h_max_yr"],j.series||[]));
    zip.file(dir+"episode_mrms.json",JSON.stringify(j));
  }
  zip.file(dir+"README.md",
    "# RUNOFF episode "+e.id+"\n\n"+
    "Weather-caused flash flood episode, "+e.t0+" to "+e.t1+" UTC ("+(e.states||"-")+").\n\n"+
    "Files:\n"+
    "- episode_summary.json: totals, reporting counties, HUC8 watersheds touched, the NWS episode narrative, MRMS footprint statistics\n"+
    "- events.csv: NOAA Storm Events flash flood reports in this episode, with the NWS event narrative\n"+
    "- lsrs.csv: NWS Local Storm Reports matched to this episode (time window + county footprint), with the report remark as narrative\n"+
    "- footprint_stats.csv: MRMS statistics for the watershed union and for the reporting counties\n"+
    "- huc8_stats.csv: the same statistics per HUC8 watershed, with event and LSR counts\n"+
    "- hourly_footprint.csv: hourly footprint mean and maximum rain and maximum 1 h ARI\n"+
    "- episode_mrms.json: the complete precomputed record as published on the site\n\n"+
    "Definitions:\n"+
    "- rain: MRMS MultiSensor QPE 1 h Pass 2 (gauge corrected); Pass 1 or radar only where Pass 2 is absent (see qpe_files)\n"+
    "- max1h/max3h/max6h: rolling accumulations with window ends from the episode start hour to one hour after its end;\n"+
    "  peak = highest cell, mean_of_peaks = area weighted mean of the cell peaks\n"+
    "- rain_ep: rain in the same windows (start hour to end + 1 h); rain_pre5h: the 5 hours before the start\n"+
    "- ari: FLASH QPE_ARI 1H/3H/6H (years, capped at 200), top of the hour, cell maximum over the episode;\n"+
    "  cov_geT = share of the unit area at or above T years (cos latitude weighted; cells without ARI count as not exceeding)\n"+
    "- watershed footprint: every HUC8 (USGS WBD) intersecting the reporting counties by at least 1 percent of its area\n"+
    "  or 10 km2, or holding a report\n\n"+
    "Event coordinates are report locations, not storm centers; use the footprint\n"+
    "and the MRMS fields to characterize the storm itself.\n");
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
el("ly-huc").addEventListener("change",function(e){
  e.target.checked?hucG.addTo(map):map.removeLayer(hucG); });
el("ly-cty").addEventListener("change",function(e){
  e.target.checked?ctyG.addTo(map):map.removeLayer(ctyG); });
el("ly-ev").addEventListener("change",function(e){
  e.target.checked?evG.addTo(map):map.removeLayer(evG); });
el("ly-lsr").addEventListener("change",function(e){
  e.target.checked?lsrG.addTo(map):map.removeLayer(lsrG); });

el("reset-all").addEventListener("click",function(){
  sy0.value=Y0; sy1.value=Y1;
  for(var i=0;i<stSel.options.length;i++) stSel.options[i].selected=false;
  [["f-ev","v-ev",function(v){return String(v);}],["f-lsr","v-lsr",function(v){return String(v);}],
   ["f-death","v-death",function(v){return String(v);}],["f-dmg","v-dmg",function(v){return fmt$(dmgFromSlider(v));}],
   ["f-mmax","v-mmax",function(v){return v+" mm";}],["f-rmean","v-rmean",function(v){return v+" mm";}],
   ["f-cov","v-cov",function(v){return v+" %";}],["f-amax","v-amax",function(v){return v<=0?"any":(">= "+v+" yr");}]
  ].forEach(function(t){ el(t[0]).value=0; el(t[1]).textContent=t[2](0); });
  el("f-only").checked=false;
  refresh();
});

/* ---------------- boot ---------------- */
renderLegend();
refresh();
/* small debug hook for the QA harness */
window.RUNOFF_EP={groups:{dots:dotsG,huc:hucG,cty:ctyG,ev:evG,lsr:lsrG},
  state:function(){ return {sel:SEL?SEL.id:null, json:!!SELJ, dur:UI.dur, thr:THR[UI.thr], matched:matched.length}; }};
/* deep link: episodes.html#ep=204934 */
var mh=/ep=(\d+)/.exec(location.hash||"");
if(mh){ var target=null; EPS.forEach(function(x){ if(String(x.id)===mh[1]) target=x; });
  if(target) select(target,null); }

}
