/* RUNOFF shared basemaps.
   One registry for every map page (access, records, episodes) with automatic
   failover: if the primary tile provider errors repeatedly (network filter,
   rate limit, outage), the map swaps to the next provider in the chain
   instead of sitting on a gray background.

   Chains: CARTO (primary look of the site) -> Esri canvas -> OpenStreetMap.

   CARTO note: the public CARTO basemap CDN (basemaps.cartocdn.com) does not
   take an API key on the tile URL. If the project later moves to CARTO's
   platform tiles with a keyed endpoint, set CARTO_KEY below to the PUBLIC
   referrer-restricted key for that endpoint. Never commit a private key:
   this repository and site are public, so anything written here is public. */
"use strict";
var BASEMAPS=(function(){
  var CARTO_KEY="";   /* optional, see note above; leave "" for the public CDN */

  function carto(style){
    var u="https://{s}.basemaps.cartocdn.com/"+style+"/{z}/{x}/{y}{r}.png";
    if(CARTO_KEY) u+="?api_key="+CARTO_KEY;
    return {url:u,opt:{maxZoom:13,subdomains:"abcd",
      attribution:"&copy; OpenStreetMap contributors &copy; CARTO"}};
  }
  var ESRI_ATTR="Tiles &copy; Esri";
  var OSM={url:"https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    opt:{maxZoom:13,attribution:"&copy; OpenStreetMap contributors"}};

  var CHAINS={
    dark:[carto("dark_all"),
      {url:"https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
       opt:{maxZoom:13,attribution:ESRI_ATTR}},
      OSM],
    light:[carto("light_all"),
      {url:"https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
       opt:{maxZoom:13,attribution:ESRI_ATTR}},
      OSM],
    sat:[{url:"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
       opt:{maxZoom:13,attribution:"Imagery &copy; Esri"}},
      OSM]
  };

  /* attach(map, kind) -> handle with .set(kind). Mounts the first provider of
     the chain; 6 tile errors with no successful load in between advances to
     the next provider. */
  function attach(map, kind){
    var h={kind:kind||"dark", idx:0, layer:null};
    function mount(){
      if(h.layer){ map.removeLayer(h.layer); h.layer=null; }
      var chain=CHAINS[h.kind]||CHAINS.dark;
      var cfg=chain[Math.min(h.idx,chain.length-1)];
      var errs=0;
      var lyr=L.tileLayer(cfg.url,cfg.opt);
      lyr.on("tileload",function(){ errs=0; });
      lyr.on("tileerror",function(){
        errs++;
        if(errs>=6 && h.idx<chain.length-1){ h.idx++; mount(); }
      });
      h.layer=lyr;
      lyr.addTo(map);
    }
    h.set=function(kind){ h.kind=kind; h.idx=0; mount(); };
    mount();
    return h;
  }

  return {attach:attach, CHAINS:CHAINS};
})();
