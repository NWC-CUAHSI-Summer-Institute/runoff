/* Flash Flood Impact Explorer - map logic.
   Data: CSTATS/HSTATS (per unit, per year [ep,ev,deaths,inj,propDmg,cropDmg]),
   COUNTYGJ/HUC8GJ/STATESGJ geometry, GLOBALS per-year CONUS totals. */
"use strict";

var state = { scale:"county", display:"ep", color:"freq", y0:2015, y1:2025 };

/* ---------- formatting ---------- */
function fmtN(n){ return n.toLocaleString("en-US"); }
function fmtUSD(v){
  var s;
  if (v>=1e9) s="$"+(v/1e9).toFixed(v>=1e10?0:1)+"B";
  else if (v>=1e6) s="$"+(v/1e6).toFixed(v>=1e7?0:1)+"M";
  else if (v>=1e3) s="$"+Math.round(v/1e3)+"K";
  else s="$"+Math.round(v);
  return s.replace(".0B","B").replace(".0M","M");
}
function fmtVal(v){ return state.color==="dmg" ? fmtUSD(v) : fmtN(Math.round(v)); }

/* ---------- data access ---------- */
function stats(){ return state.scale==="county" ? CSTATS : HSTATS; }
function sums(u){                       /* totals for unit over selected years */
  var s=[0,0,0,0,0,0];
  for (var yr=state.y0; yr<=state.y1; yr++){
    var r=u.y[yr]; if(!r) continue;
    for (var k=0;k<6;k++) s[k]+=r[k];
  }
  return s;
}
function metric(s){
  if (state.color==="freq") return state.display==="ep" ? s[0] : s[1];
  if (state.color==="d")   return s[2];
  if (state.color==="i")   return s[3];
  return s[4]+s[5];                                    /* dmg */
}
function metricLabel(){
  if (state.color==="freq") return state.display==="ep" ? "Episodes with flash flooding" : "Flash flood events";
  if (state.color==="d")   return "Fatalities";
  if (state.color==="i")   return "Injuries";
  return "Damage (property + crop)";
}

/* ---------- choropleth classing ---------- */
var RAMP=["#2c3a56","#3f5f96","#5b8dd9","#c9a13d","#dd7a33","#c94040"];
var ZEROFILL="#151b26", breaks=[];
var LADDER=[1,2,3,5,10,15,20,25,30,40,50,75,100,150,200,250,300,400,500,750,1000,1500,2000,3000,5000,7500,10000];
function niceCount(v){
  for (var i=0;i<LADDER.length;i++) if (LADDER[i]>=v) return LADDER[i];
  return Math.ceil(v/1000)*1000;
}
function nextCount(v){
  for (var i=0;i<LADDER.length;i++) if (LADDER[i]>v) return LADDER[i];
  return v+1000;
}
function niceUSD(v){
  if (v<=0) return 0;
  var k=Math.pow(10,Math.floor(Math.log(v)/Math.LN10));
  var m=v/k;
  return (m<=1?1:(m<=2.5?2.5:(m<=5?5:10)))*k;
}
function computeBreaks(){
  /* quantiles of the current data, snapped up to round numbers so the legend
     reads 5, 10, 25, 50 instead of 7, 13, 233 */
  var vals=[], st=stats();
  for (var id in st){ var v=metric(sums(st[id])); if (v>0) vals.push(v); }
  vals.sort(function(a,b){return a-b;});
  breaks=[];
  var qs=[0.30,0.55,0.75,0.88,0.96], dollars=(state.color==="dmg");
  for (var i=0;i<qs.length;i++){
    var raw=vals.length? vals[Math.min(vals.length-1, Math.floor(qs[i]*vals.length))] : (i+1);
    var b=dollars? niceUSD(raw) : niceCount(raw);
    while (breaks.length && b<=breaks[breaks.length-1])
      b=dollars? niceUSD(breaks[breaks.length-1]*1.5) : nextCount(breaks[breaks.length-1]);
    breaks.push(b);
  }
}
function colorOf(v){
  if (v<=0) return ZEROFILL;
  for (var i=0;i<breaks.length;i++) if (v<=breaks[i]) return RAMP[i];
  return RAMP[5];
}

/* ---------- map ---------- */
var map=L.map("map",{zoomSnap:.5, attributionControl:true, preferCanvas:true})
         .setView([38.6,-95.8],4.5);
var BASE=BASEMAPS.attach(map,"dark");   /* CARTO first, Esri/OSM failover */

map.createPane("statepane");  map.getPane("statepane").style.zIndex=430;
map.getPane("statepane").style.pointerEvents="none";   /* lines on top, clicks pass through */
map.createPane("unitpane");   map.getPane("unitpane").style.zIndex=420;

L.geoJSON(STATESGJ,{pane:"statepane",interactive:false,
  style:{color:"#9aa5b3",weight:1,opacity:.45,fill:false}}).addTo(map);

function unitStyle(f){
  var id=f.properties.f||f.properties.h, u=stats()[id];
  var v=u? metric(sums(u)) : 0;
  return {pane:"unitpane", fillColor:colorOf(v), fillOpacity:v>0?.78:.35,
          color:"#212b3b", weight:.6, opacity:1};
}
var hoverbox=document.getElementById("hoverbox");
function onEach(f,l){
  l.on("mouseover",function(){
    l.setStyle({weight:1.8,color:"#f2b705"}); l.bringToFront();
    var id=f.properties.f||f.properties.h, u=stats()[id];
    var v=u? metric(sums(u)) : 0;
    hoverbox.innerHTML="<b>"+f.properties.n+"</b> &nbsp;"+metricLabel().toLowerCase()+": <b>"+fmtVal(v)+"</b>";
    hoverbox.style.display="block";
  });
  l.on("mouseout",function(){ l.setStyle(unitStyle(f)); hoverbox.style.display="none"; });
  l.on("click",function(e){ if (e.originalEvent) L.DomEvent.stopPropagation(e.originalEvent); openPopup(f,e.latlng); });
}
var layers={ county:null, huc8:null };
function buildLayer(scale){
  if (layers[scale]) return layers[scale];
  var gj = scale==="county" ? COUNTYGJ : HUC8GJ;
  layers[scale]=L.geoJSON(gj,{style:unitStyle,onEachFeature:onEach});
  return layers[scale];
}

/* ---------- popup ---------- */
var CAUSE_LABEL={"Heavy Rain":"heavy rain","Heavy Rain / Tropical System":"tropical systems",
  "Heavy Rain / Burn Area":"rain on burn scars","Heavy Rain / Snow Melt":"rain with snowmelt",
  "Ice Jam":"ice jams","Not specified":"not specified"};
function openPopup(f,latlng){
  var id=f.properties.f||f.properties.h, u=stats()[id];
  var html;
  if (!u){
    html="<div class='pp'><h3>"+f.properties.n+"</h3><div class='per'>"+state.y0+" to "+state.y1+
         "</div><div class='freq'>No weather-caused flash floods on record here for 2015 to 2025.</div></div>";
  } else {
    var s=sums(u);
    var drivers=Object.keys(u.c).map(function(k){return [k,u.c[k]];})
        .sort(function(a,b){return b[1]-a[1];}).slice(0,3)
        .map(function(p){return (CAUSE_LABEL[p[0]]||p[0].toLowerCase())+" ("+fmtN(p[1])+")";}).join(", ");
    var mx=1, yr;
    for (yr=2015; yr<=2025; yr++){ var r=u.y[yr]; if (r&&r[1]>mx) mx=r[1]; }
    var bars="";
    for (yr=2015; yr<=2025; yr++){
      var ev=u.y[yr]? u.y[yr][1]:0, h=Math.max(2,Math.round(ev/mx*34));
      var hot=(yr>=state.y0&&yr<=state.y1)?" class='hot'":"";
      bars+="<i"+hot+" style='height:"+h+"px' title='"+yr+": "+ev+" events'></i>";
    }
    var sub=state.scale==="huc8" ? "HUC "+id+" &nbsp;|&nbsp; " : "";
    html="<div class='pp'><h3>"+u.n+"</h3><div class='per'>"+sub+"Selected period: "+state.y0+" to "+state.y1+"</div>"+
      "<div class='freq'><b>"+fmtN(s[0])+"</b> storm episodes produced <b>"+fmtN(s[1])+"</b> flash flood events</div>"+
      "<table>"+
      "<tr><td>Fatalities</td><td>"+fmtN(s[2])+"</td></tr>"+
      "<tr><td>Injuries</td><td>"+fmtN(s[3])+"</td></tr>"+
      "<tr><td>Property damage</td><td>"+fmtUSD(s[4])+"</td></tr>"+
      "<tr><td>Crop damage</td><td>"+fmtUSD(s[5])+"</td></tr>"+
      "</table>"+
      "<div class='drv'><b>Common drivers (2015 to 2025):</b> "+drivers+"</div>"+
      "<div class='spark'>"+bars+"</div>"+
      "<div class='sparklab'><span>2015</span><span>flash flood events per year</span><span>2025</span></div>"+
      "</div>";
  }
  L.popup({maxWidth:320}).setLatLng(latlng).setContent(html).openOn(map);
}

/* ---------- legend + rail totals ---------- */
function refreshLegend(){
  var el=document.getElementById("legend"), rows="";
  rows+="<div class='ttl'>"+metricLabel()+"</div>";
  rows+="<div class='sub'>per "+(state.scale==="county"?"county":"HUC8 watershed")+", "+
        state.y0+" to "+state.y1+"</div>";
  rows+="<div class='k'><span class='sw' style='background:"+ZEROFILL+"'></span>none recorded in these years</div>";
  var lo=1;
  for (var i=0;i<breaks.length;i++){
    rows+="<div class='k'><span class='sw' style='background:"+RAMP[i]+"'></span>"+rangeText(lo,breaks[i])+"</div>";
    lo=breaks[i]+1;
  }
  rows+="<div class='k'><span class='sw' style='background:"+RAMP[5]+"'></span>over "+fmtVal(breaks[4])+"</div>";
  rows+="<div class='lnote'>Blue shades mean lower numbers, gold and orange mean high, and red marks the highest. Shapes that stay dark have nothing on record for the selected years. The ranges update with every choice you make above.</div>";
  el.innerHTML=rows;
}
function rangeText(lo,hi){
  if (state.color==="dmg") return lo<=1? "up to "+fmtUSD(hi) : fmtUSD(lo-1)+" to "+fmtUSD(hi);
  return lo>=hi? fmtN(hi) : fmtN(lo)+" to "+fmtN(hi);
}
function refreshTotals(){
  var t=[0,0,0,0,0,0];
  for (var yr=state.y0; yr<=state.y1; yr++){
    var g=GLOBALS[yr]; if(!g) continue;
    for (var k=0;k<6;k++) t[k]+=g[k];
  }
  document.getElementById("t-ep").textContent=fmtN(t[0]);
  document.getElementById("t-ev").textContent=fmtN(t[1]);
  document.getElementById("t-d").textContent=fmtN(t[2]);
  document.getElementById("t-i").textContent=fmtN(t[3]);
  document.getElementById("t-dmg").textContent=fmtUSD(t[4]+t[5]);
}

/* ---------- redraw pipeline ---------- */
var current=null;
function redraw(rebuildLayer){
  computeBreaks();
  if (rebuildLayer){
    if (current) map.removeLayer(current);
    current=buildLayer(state.scale).addTo(map);
  }
  current.setStyle(unitStyle);
  refreshLegend(); refreshTotals();
  map.closePopup();
}

/* ---------- controls ---------- */
function seg(id,handler){
  var box=document.getElementById(id);
  box.addEventListener("click",function(e){
    var b=e.target.closest("button"); if(!b) return;
    box.querySelectorAll("button").forEach(function(x){x.classList.remove("on");});
    b.classList.add("on");
    handler(b.dataset.v);
  });
}
seg("seg-scale",function(v){ state.scale=v; redraw(true); });
seg("seg-display",function(v){
  state.display=v;
  if (state.color!=="freq"){ state.color="freq"; document.getElementById("sel-color").value="freq"; }
  redraw(false);
});
document.getElementById("sel-color").addEventListener("change",function(){
  state.color=this.value; redraw(false);
});
function yearSel(id,handler){
  var s=document.getElementById(id);
  for (var y=2015;y<=2025;y++){ var o=document.createElement("option"); o.value=y; o.textContent=y; s.appendChild(o); }
  s.addEventListener("change",function(){ handler(parseInt(this.value,10)); });
  return s;
}
var s0=yearSel("y-from",function(v){ state.y0=v; if(state.y1<v){state.y1=v;s1.value=v;} redraw(false); });
var s1=yearSel("y-to",  function(v){ state.y1=v; if(state.y0>v){state.y0=v;s0.value=v;} redraw(false); });
s0.value=2015; s1.value=2025;
document.getElementById("y-all").addEventListener("click",function(){
  state.y0=2015; state.y1=2025; s0.value=2015; s1.value=2025; redraw(false);
});
document.querySelectorAll("#basebox input").forEach(function(r){
  r.addEventListener("change",function(){ BASE.set(this.value); });
});

/* ---------- boot ---------- */
redraw(true);
