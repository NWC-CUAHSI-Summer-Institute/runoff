/* RUNOFF shared basemaps.
   One registry for every map page (access, records, episodes) with automatic
   failover: if the primary tile provider errors repeatedly (network filter,
   rate limit, outage), the map swaps to the next provider in the chain
   instead of sitting on a gray background.

   Chains: Esri canvas (base plus reference labels) -> OpenStreetMap.

   The public CARTO basemap CDN (basemaps.cartocdn.com) started answering with
   "API KEY REQUIRED" tiles in 2026. Those tiles are valid images, so a tile
   error failover never fires; CARTO was therefore removed from the chains.
   Nothing on this site needs a key. This repository and site are public, so
   never write a private key into any file here. */
"use strict";
var BASEMAPS=(function(){
  var ESRI="https://server.arcgisonline.com/ArcGIS/rest/services/";
  var ESRI_ATTR="Tiles &copy; Esri, HERE, Garmin, OpenStreetMap contributors";
  var OSM={urls:["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
    opt:{maxZoom:13,attribution:"&copy; OpenStreetMap contributors"}};

  var CHAINS={
    dark:[{urls:[ESRI+"Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
                 ESRI+"Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}"],
           opt:{maxZoom:13,attribution:ESRI_ATTR}},
          OSM],
    light:[{urls:[ESRI+"Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
                  ESRI+"Canvas/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}"],
            opt:{maxZoom:13,attribution:ESRI_ATTR}},
           OSM],
    sat:[{urls:[ESRI+"World_Imagery/MapServer/tile/{z}/{y}/{x}"],
          opt:{maxZoom:13,attribution:"Imagery &copy; Esri"}},
         OSM]
  };

  /* attach(map, kind) -> handle with .set(kind). Mounts the first provider of
     the chain (all of its tile layers, base first); 6 tile errors on the base
     layer with no successful load in between advances to the next provider. */
  function attach(map, kind){
    var h={kind:kind||"dark", idx:0, layer:null};
    function mount(){
      if(h.layer){ map.removeLayer(h.layer); h.layer=null; }
      var chain=CHAINS[h.kind]||CHAINS.dark;
      var cfg=chain[Math.min(h.idx,chain.length-1)];
      var errs=0;
      var layers=[];
      for(var i=0;i<cfg.urls.length;i++){
        var o={}; for(var k in cfg.opt) o[k]=cfg.opt[k];
        if(i>0) o.attribution="";
        var lyr=L.tileLayer(cfg.urls[i],o);
        if(i===0){
          lyr.on("tileload",function(){ errs=0; });
          lyr.on("tileerror",function(){
            errs++;
            if(errs>=6 && h.idx<chain.length-1){ h.idx++; mount(); }
          });
        }
        layers.push(lyr);
      }
      h.layer=L.layerGroup(layers);
      h.layer.addTo(map);
    }
    h.set=function(kind){ h.kind=kind; h.idx=0; mount(); };
    mount();
    return h;
  }

  return {attach:attach, CHAINS:CHAINS};
})();
