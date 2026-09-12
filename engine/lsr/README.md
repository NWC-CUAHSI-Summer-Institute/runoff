# engine/lsr - NWS Local Storm Reports

Local Storm Reports (LSRs) are the point reports NWS offices relay in real
time: spotters, emergency managers, law enforcement, the public. Each has a
UTC time, a location, a type (FLASH FLOOD, FLOOD, DEBRIS FLOW, HEAVY RAIN,
...), a source, and often a remark. They complement the Storm Events
Database: Storm Events is the curated post-event record organized into
episodes; LSRs are the raw ground truth, and many flash flood impacts appear
only here. For Flash Flood Guidance style modeling, LSRs are the flooding
observations and no stream gage is involved.

Source: Iowa Environmental Mesonet archive API
(mesonet.agron.iastate.edu/cgi-bin/request/gis/lsr.py), CSV, UTC in and out.

    python engine/lsr/fetch_lsrs.py --wy 2021 2025              # CONUS archive
    python engine/lsr/fetch_lsrs.py --start 2025-07-01 --end 2025-07-10 --states TX

Pulls are chunked per state per water year, cached one CSV per chunk under
data/lsr/, and resumable: rerun the same command and only missing chunks are
fetched. All report types are kept on disk; filtering to flash flood types
happens in engine/episodes/build_catalog.py, so widening the type set later
does not require re-pulling the archive.

Parsing notes learned the hard way: the CSV has unescaped commas inside
REMARK on some rows, so it is read with QUOTE_NONE and bad lines skipped;
rows without coordinates or a timestamp are dropped; duplicates are removed
on (VALID, LAT, LON, TYPETEXT, WFO).
