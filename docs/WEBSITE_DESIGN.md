# RUNOFF website: design notes

Static site, no build step, hosted on the Summer Institute GitHub (GitHub Pages).
Open index.html through any static server. For local preview:
`python -m http.server` in this folder, then http://localhost:8000

## Site map
| Page | Purpose |
|---|---|
| index.html | Landing: what RUNOFF is, experimental badge, About / Access Dataset |
| about.html | Data preparation story: 19,993 -> 7,990 -> 5,097 funnel, event separation, components |
| access.html | The explorer: scope, watershed filters, experiment selection, forcing, .py package |
| episodes.html | General modelling path: storm episodes with events, LSRs and precomputed MRMS rainfall and FLASH return periods per HUC8 (engine/episodes/README.md) |
| records.html | County and HUC8 flash flood records, 2015 to 2025 |
| prototypes/ | Earlier interface drafts, kept for reference |
| event_separation/ | Jessica's event extraction pipeline (the backend the generated script drives) |

## Access page flow (wizard)
Step bar sits on top of the map; the left rail shows only the current step.
Nothing requires scrolling the page.

0. Geography: CONUS or state based (state checklist).
1. Experiment scope: RUNOFF basins (event-linked) or all GAGES-II basins < 1000 km2.
2. Experiment criteria: sliders with data-driven ranges (events, fatalities,
   damage on a log scale, station count). Matching watersheds are the selected
   basins, drawn green live; all HUC8 stay gray. Layer toggles for HUC8 and gages.
3. USGS data: separated flood events (flash-tuned separation, Q-exceedance
   slider) or the entire 15-minute time series; water years.
4. Forcing (NextGen): radar-only MRMS PrecipRate at 2 / 10 / 15 / 30 / 60 min,
   or hourly gauge-corrected MultiSensor Pass 1 / Pass 2; all with AORC
   meteorology. MRMS download itself is a placeholder (adapter in development).
5. Package: "your data download package is ready" in the rail; one zip holds
   runoff_experiment.py (calls event_extraction_pipeline.run per HUC8, or the
   full-series retrieval via usgs_events.load_usgs_discharge), FORCING.json,
   and README instructions.

## Data files (assets/data/)
| File | Size | Built from |
|---|---|---|
| huc8.js (+ .geojson) | 2.7 MB | USGS WBD HUC8, simplified (tol 0.012, coords 3 dp), per-basin stats joined (events, deaths, damage, stations, event gages) |
| data.js | 0.4 MB | 5,897 GAGES-II gages < 1000 km2 with event counts, impacts, QPE-quality fields |
| episodes.js | 1.3 MB | episode catalogue (var EPCAT) with the precomputed MRMS summary per episode (engine/episodes/build_catalog.py, build_mrms_payload.py) |
| episode_points.js | 2.5 MB | Storm Events and LSR points per episode (var EPPTS) |
| ep/<id>.json | 2 to 30 KB each | per episode MRMS statistics for the footprint, the counties and every HUC8, fetched on click (engine/episodes/precompute_mrms.py) |
| counties_geo.js, states_geo.js | 3.6 + 0.8 MB | NWS county polygons simplified (tol 0.004), states dissolved from them |

Basemaps: Esri canvas (dark, light, imagery) with OpenStreetMap failover, no key
(assets/js/basemaps.js). The public CARTO CDN started requiring a key in 2026 and
was dropped.

The raw HUC8 shapefile (360 MB) and RUNOFF_interface.zip (258 MB) exceed GitHub
limits and are excluded via .gitignore. The pipeline README documents where to
get the shapefile (USGS WBD; also bundled in the HydroShare release).

## Style
Dark theme mirroring the AHWA Visual landing style: near-black background,
gold accent, red experimental badge, thin-line panels. Partner logos load from
assets/logos/ and fall back to text wordmarks when the PNGs are absent.
