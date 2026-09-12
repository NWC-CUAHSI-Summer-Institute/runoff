# engine/episodes - episode catalog for general flash flood modeling

This is the backend of the "General flash flood modeling" access path
(episodes.html): flash flood EPISODES from the NOAA Storm Events Database,
their events, their Local Storm Reports, and MRMS rainfall over each episode
footprint. No stream gage is required anywhere in this path.

## Workflow

    # 0. inputs: StormEvents details files for the calendar years you need
    #    https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/
    #    put StormEvents_details-ftp_v1.0_dYYYY_*.csv.gz under data/stormevents/

    # 1. Local Storm Reports archive (resumable, ~10-20 min for CONUS x 5 WYs)
    python engine/lsr/fetch_lsrs.py --wy 2021 2025

    # 2. the catalog + site payloads (~2-5 min)
    python engine/episodes/build_catalog.py --stormevents data/stormevents

    # 3. MRMS rainfall stats, at your pace (heavier; resumable per episode)
    python engine/episodes/mrms_stats.py --states TX --min-events 10
    python engine/episodes/mrms_stats.py --episode 191899 --timeseries

    # 4. rerun the catalog to merge MRMS numbers into the site payload
    python engine/episodes/build_catalog.py --stormevents data/stormevents

Steps 1 and 3 are optional: without step 1 the catalog is built with LSRs
marked "not fetched"; without step 3 the rainfall filters on episodes.html
stay hidden until the numbers exist. Nothing on the site pretends to have
data it does not have.

## What gets computed

Per episode (one NOAA EPISODE_ID, weather-caused flash floods only, county
reports, lower 48 + DC, dam and levee failures removed):

- UTC window from the events' local times and CZ_TIMEZONE offsets
- county footprint (FIPS list) and bounding box from the report coordinates
- events, fatalities, injuries, damage (property + crops, K/M/B parsed)
- LSRs matched by type (default FLASH FLOOD), time window [start - 3 h,
  end + 6 h], and county footprint, with a padded-bbox fallback when a
  report has no county UGC
- from mrms_stats.py: max rolling 6/12/24/72 h accumulation at any grid
  cell in the bbox (72 h lookback precedes the episode, so antecedent rain
  counts), and the max FLASH QPE ARI return period in years with the
  duration and hour it occurred

## Outputs

    data/episodes/episodes.csv           one row per episode (the master table)
    data/episodes/episode_events.csv     Storm Events rows with episode_id
    data/episodes/episode_lsrs.csv       matched LSRs with episode_id and remarks
    data/episodes/<id>/mrms_summary.json per-episode MRMS stats
    data/episodes/<id>/hourly_rain.csv   hourly footprint series (--timeseries)
    assets/data/episodes.js              site payload, var EPCAT
    assets/data/episode_points.js        site payload, var EPPTS

### Site payload format (consumed by assets/js/episodes.js)

EPCAT.fields order: id, t0, t1, states, nev, nlsr, deaths, inj, dmg, fips,
bbox, acc6, acc12, acc24, acc72, ari, aridur. Times are UTC "YYYY-MM-DD HH";
nlsr is -1 when LSRs were not fetched; acc*/ari are null until mrms_stats
ran for that episode; bbox is [min_lat, min_lon, max_lat, max_lon].

EPPTS maps episode id to {ev: [[lat, lon, "YYYY-MM-DD HH:MM", deaths,
damage_usd], ...], lsr: [[lat, lon, "YYYY-MM-DD HH:MM", TYPETEXT, SOURCE],
...]}.

## Notes

- Storm Events coordinates are NWS report locations, not storm centers. The
  footprint and the MRMS grids are what characterize the storm itself; say
  so when handing these tables to model developers.
- Rainfall stats are computed over the episode bounding box (padded 0.1
  deg), the same convention as the Iowa Flood Compendium pipeline this
  module descends from. A county-masked variant would change numbers by
  little at these scales and cost a rasterization dependency.
- FLASH ARI products exist on AWS from 2021 onward, which is one of the
  reasons the default window is WY 2021-2025. The QPE fetch falls back to
  the Iowa State mtarchive mirror; ARI is AWS only.
- Dependencies: pandas, numpy, requests (fetch_lsrs), xarray + cfgrib
  (mrms_stats). All are already used elsewhere in the RUNOFF engine.
