# engine/gages - USGS gages of the RUNOFF set

RUNOFF carries the GAGES-II stream gages draining less than 1000 km2 in CONUS
(5,897 sites, assets/data/data.js). This module adds what the episode page needs
to show them: the official station name from the NWIS site service and the
upstream drainage basin polygon from the USGS NLDI
(api.water.usgs.gov/nldi/linked-data/nwissite/USGS-<id>/basin).

    python engine/gages/fetch_basins.py            # names + basins, resumable
    python engine/gages/fetch_basins.py --workers 4
    python engine/episodes/build_mrms_payload.py   # then: gage counts per episode

Outputs

    data/gages/gages.csv            id, name, lat, lon, area_km2, huc8, n_events, basin
    assets/data/gages.js            var GAGES2 = {id: [lat, lon, area_km2, name, huc8, basin]}
    assets/data/basin/<id>.json     GeoJSON Feature per gage, simplified to about 100 m

On the episode page a gage belongs to an episode when its HUC8 is part of the
episode footprint. The gages are drawn as green triangles for the selected
episode; clicking one shows the station and draws its basin. The number of
gages is a filter and a sort key, and the download package includes gages.csv
and basins.geojson for the episode. Sites the NLDI does not know (discontinued
or off the NHDPlus network) keep basin = 0 and show without a polygon.
