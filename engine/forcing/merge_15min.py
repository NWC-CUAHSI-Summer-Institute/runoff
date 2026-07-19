r"""Merge AORC and MRMS 15-min NetCDFs into a single combined forcing file.

MRMS's `storm_id` is the same ID as AORC's `event_id` (aggregate_events.py
names its files storm_<event_id>_15min.nc). This script:
  1. Joins AORC and MRMS on event_id (MRMS storm_id cast to str).
  2. Aligns catchments by ID *per event* (inner join on divide_id / catchment
     strings, independently for each matched event).
  3. Writes a combined NetCDF using ragged (CSR) per-event catchment storage
     -- see flash_preprocess.mrms.merge_parts for why: AORC and MRMS parts
     can each span several disjoint-catchment VPUs/basins, and unioning
     every event's catchments onto one shared dense axis scales with the
     *sum* of every basin's catchments rather than any single event's own
     (small) upstream set.

Output
------
  Coordinates:
    event_id        (event,)     str     shared AORC/MRMS event ID
    n_steps         (event,)     i32     valid 15-min steps (minimum of both sources)
    ts_start        (event,)     f64     start of the window (minutes since 1970-01-01)
    ts_end          (event,)     f64     end of the window (minutes since 1970-01-01)
    event_gage_id   (event,)     str     zero-padded 8-digit USGS gauge downstream of event
    event_divide_id (event,)     str     NextGen catchment ID downstream of event
    cat_ptr         (event+1,)   i64     CSR offsets: event i's catchments are
                                          entry[cat_ptr[i]:cat_ptr[i+1]]
    divide_id       (entry,)     str     NextGen divide ID (per-event inner join)
    latitude        (entry,)     f32
    longitude       (entry,)     f32

  Variables (all (entry, time_step) f32):
    P     from MRMS
    T     from AORC
    PET   from AORC

Edit the CONFIG block at the top of this file to set all options, or
override per-invocation via CLI flags (see below).

Usage
-----
    python engine/forcing/merge_15min.py \\
        --aorc     /path/to/aorc_15min.nc \\
        --aorc-hr  /path/to/aorc_hr.nc \\
        --mrms     /path/to/mrms_15min.nc \\
        --output   /path/to/forcing_15min.nc
"""

import argparse
import logging
import sys
from pathlib import Path

import netCDF4
import numpy as np
from tqdm.auto import tqdm

from flash_preprocess.paths import CACHE_DIR as _CACHE_DIR
from flash_preprocess.paths import EVENTS_CSV as _EVENTS_CSV

log = logging.getLogger('MergeForcing')


# CONFIG -------------------------- #
# AORC 15-min forcing NetCDF (output of aorc/extract.py).
AORC_NC = _CACHE_DIR / 'aorc_15min.nc'

# AORC hourly antecedent NetCDF (output of aorc/extract.py); used only to
# check that its warmup window ends exactly where the MRMS event window begins.
AORC_HR_NC = _CACHE_DIR / 'aorc_hr.nc'

# MRMS 15-min forcing NetCDF (output of mrms/extract.py).
MRMS_NC = _CACHE_DIR / 'mrms_15min.nc'

# Output combined forcing NetCDF.
OUTPUT_NC = _EVENTS_CSV.parent / 'forcing_15min.nc'

# zlib compression level 1-9.
COMPLEVEL = 4
# -------------------------- #


def _load_str_var(ds: netCDF4.Dataset, name: str, idx=None) -> np.ndarray:
    """Read a netCDF4 string variable (optionally a slice) as a plain numpy object array."""
    raw = ds.variables[name][:] if idx is None else ds.variables[name][idx]
    if isinstance(raw, np.ma.MaskedArray):
        raw = raw.filled()
    out = np.empty(len(raw), dtype=object)
    for i, v in enumerate(raw):
        out[i] = str(v) if not isinstance(v, str) else v
    return out


def parse_args():
    """Parse command-line overrides for the CONFIG block above."""
    parser = argparse.ArgumentParser(
        description="Merge AORC and MRMS 15-min forcing NetCDFs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        '--aorc',
        type=Path,
        default=AORC_NC,
        help="AORC 15-min NC, output of aorc/extract.py (default: %(default)s)",
    )
    parser.add_argument(
        '--aorc-hr',
        type=Path,
        default=AORC_HR_NC,
        help="AORC hourly antecedent NC, used only to check that its warmup "
        "window ends exactly where the MRMS event window begins "
        "(default: %(default)s)",
    )
    parser.add_argument(
        '--mrms',
        type=Path,
        default=MRMS_NC,
        help="MRMS 15-min NC, output of mrms/extract.py (default: %(default)s)",
    )
    parser.add_argument(
        '--output',
        type=Path,
        default=OUTPUT_NC,
        help="Output NetCDF path (default: %(default)s)",
    )
    parser.add_argument(
        '--complevel',
        type=int,
        default=COMPLEVEL,
        help="zlib compression level 1-9 (default: %(default)s)",
    )
    return parser.parse_args()


def merge_forcing() -> None:
    """Run the AORC + MRMS merge pipeline."""
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    args = parse_args()

    # open source files
    nc_aorc = netCDF4.Dataset(args.aorc, 'r')
    nc_mrms = netCDF4.Dataset(args.mrms, 'r')

    aorc_event_ids = _load_str_var(nc_aorc, 'event_id')
    mrms_event_ids = np.array(
        [str(int(sid)) for sid in nc_mrms.variables['storm_id'][:]],
        dtype=object,
    )
    event_to_mrms_idx = {eid: i for i, eid in enumerate(mrms_event_ids)}

    # build aligned event list: iterate AORC order, keep events present in MRMS
    aorc_indices, mrms_indices, event_ids = [], [], []
    skipped = 0
    for i, eid in enumerate(aorc_event_ids):
        j = event_to_mrms_idx.get(eid)
        if j is None:
            skipped += 1
            continue
        aorc_indices.append(i)
        mrms_indices.append(j)
        event_ids.append(eid)

    n_events = len(aorc_indices)
    if n_events == 0:
        sys.exit("No events matched between AORC and MRMS files on event_id.")
    log.info(
        'Matched %d events (%d AORC events skipped, not present in MRMS file)',
        n_events,
        skipped,
    )

    # Both files' event windows come from the same build_manifest() logic,
    # but only if both were built from run_pipeline.py runs with matching
    # WINDOW_DAYS/CENTROID -- a stale file from an older config would merge
    # silently, splicing MRMS precip for one time window onto AORC
    # temperature/PET for a different one. Catch that here instead.
    aorc_ts_start_chk = np.array(nc_aorc.variables['ts_start'][:], dtype=np.float64)
    mrms_ts_start_chk = np.array(nc_mrms.variables['ts_start'][:], dtype=np.float64)
    offset_min = aorc_ts_start_chk[aorc_indices] - mrms_ts_start_chk[mrms_indices]
    bad = np.abs(offset_min) > 1.0  # allow <1 min for float rounding
    if bad.any():
        sys.exit(
            f"{bad.sum()}/{n_events} matched events have AORC/MRMS window start times that "
            f"disagree by more than a minute (median offset {np.median(offset_min[bad]):.0f} "
            f"min, e.g. event {event_ids[np.argmax(bad)]}). The two files were likely built "
            f"with different WINDOW_DAYS/CENTROID settings (or one is stale) -- regenerate "
            f"both from the same run_pipeline.py config before merging.",
        )

    # The AORC hourly antecedent file (30-day warmup) must end exactly where
    # the MRMS event window begins, with no gap or overlap -- its stored
    # ts_end is the timestamp of the *last hourly step itself* (an hourly
    # value at hour H covers [H, H+1)), so the true end of the warmup period
    # is ts_end + 60 min, which should equal MRMS's ts_start exactly.
    nc_aorc_hr = netCDF4.Dataset(args.aorc_hr, 'r')
    aorc_hr_event_ids = _load_str_var(nc_aorc_hr, 'event_id')
    aorc_hr_idx = {eid: i for i, eid in enumerate(aorc_hr_event_ids)}
    aorc_hr_ts_end = np.array(nc_aorc_hr.variables['ts_end'][:], dtype=np.float64)
    nc_aorc_hr.close()

    missing_hr = [eid for eid in event_ids if eid not in aorc_hr_idx]
    if missing_hr:
        sys.exit(
            f"{len(missing_hr)}/{n_events} matched events are missing from --aorc-hr "
            f"(e.g. event {missing_hr[0]}) -- aorc_hr.nc and the AORC/MRMS 15-min files "
            f"appear to come from different runs.",
        )
    hr_end_for_event = np.array([aorc_hr_ts_end[aorc_hr_idx[eid]] for eid in event_ids])
    gap_min = mrms_ts_start_chk[mrms_indices] - (hr_end_for_event + 60.0)
    bad_gap = np.abs(gap_min) > 1.0
    if bad_gap.any():
        sys.exit(
            f"{bad_gap.sum()}/{n_events} matched events have a gap/overlap between the AORC "
            f"hourly warmup and the MRMS event window (median {np.median(gap_min[bad_gap]):.0f} "
            f"min, e.g. event {event_ids[np.argmax(bad_gap)]}). aorc_hr.ts_end + 60min should "
            f"equal mrms.ts_start exactly -- the two pipelines likely used different "
            f"WINDOW_DAYS/ANTECEDENT_DAYS/CENTROID, or one file is stale.",
        )

    # figure out max_steps (both sources are nominally the same, take the max)
    aorc_max = nc_aorc.dimensions['time_step'].size
    mrms_max = nc_mrms.dimensions['time_step'].size
    max_steps = max(aorc_max, mrms_max)
    log.info(
        'time_step dim: AORC %d, MRMS %d -> output %d',
        aorc_max,
        mrms_max,
        max_steps,
    )

    aorc_n_steps = np.array(nc_aorc.variables['n_steps'][:], dtype=np.int32)
    mrms_n_steps = np.array(nc_mrms.variables['n_steps'][:], dtype=np.int32)
    aorc_ts_starts = aorc_ts_start_chk
    aorc_ts_ends = np.array(nc_aorc.variables['ts_end'][:], dtype=np.float64)
    aorc_gage_ids = _load_str_var(nc_aorc, 'event_gage_id')
    aorc_divide_ids = _load_str_var(nc_aorc, 'event_divide_id')

    out_n_steps = np.minimum(
        aorc_n_steps[aorc_indices],
        mrms_n_steps[mrms_indices],
    )

    # align catchments *per event*: inner join on divide_id, independently
    # for each matched event -- never union catchments across events, since
    # that's what makes this scale to many disjoint basins/VPUs.
    aorc_cat_ptr = nc_aorc.variables['cat_ptr'][:]
    mrms_cat_ptr = nc_mrms.variables['cat_ptr'][:]

    event_common_cats: list[list[str]] = []
    event_aorc_idx: list[np.ndarray] = []
    event_mrms_idx: list[np.ndarray] = []
    n_only_aorc, n_only_mrms = 0, 0

    for ai, mi in tqdm(
        zip(aorc_indices, mrms_indices),
        total=n_events,
        desc='align catchments',
    ):
        a_lo, a_hi = int(aorc_cat_ptr[ai]), int(aorc_cat_ptr[ai + 1])
        m_lo, m_hi = int(mrms_cat_ptr[mi]), int(mrms_cat_ptr[mi + 1])
        a_cats = _load_str_var(nc_aorc, 'divide_id', slice(a_lo, a_hi))
        m_cats = _load_str_var(nc_mrms, 'divide_id', slice(m_lo, m_hi))
        m_pos = {c: k for k, c in enumerate(m_cats)}

        common, a_idx, m_idx = [], [], []
        for k, c in enumerate(a_cats):
            j = m_pos.get(c)
            if j is not None:
                common.append(c)
                a_idx.append(k)
                m_idx.append(j)

        event_common_cats.append(common)
        event_aorc_idx.append(np.array(a_idx, dtype=np.int64))
        event_mrms_idx.append(np.array(m_idx, dtype=np.int64))
        n_only_aorc += len(a_cats) - len(common)
        n_only_mrms += len(m_cats) - len(common)

    total_entries = sum(len(c) for c in event_common_cats)
    if total_entries == 0:
        sys.exit("No common catchments between AORC and MRMS in any matched event.")
    log.info(
        'Catchments: %d event-catchment entries after per-event inner join '
        '(%d AORC-only entries dropped, %d MRMS-only entries dropped, across all '
        'matched events)',
        total_entries,
        n_only_aorc,
        n_only_mrms,
    )

    # create output
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    nc_out = netCDF4.Dataset(args.output, 'w', format='NETCDF4')
    nc_out.createDimension('event', n_events)
    nc_out.createDimension('ptr', n_events + 1)
    nc_out.createDimension('entry', total_entries)
    nc_out.createDimension('time_step', max_steps)

    v = nc_out.createVariable('event_id', str, ('event',))
    v.long_name = "shared AORC/MRMS event ID"
    v[:] = np.array(event_ids, dtype=object)

    v = nc_out.createVariable('n_steps', 'i4', ('event',))
    v.long_name = "valid 15-min timesteps (min of AORC and MRMS coverage)"
    v[:] = out_n_steps

    v = nc_out.createVariable('ts_start', 'f8', ('event',))
    v.units = "minutes since 1970-01-01 00:00:00 UTC"
    v.long_name = "start of the timeseries window (from AORC, time step 0)"
    v[:] = aorc_ts_starts[aorc_indices]

    v = nc_out.createVariable('ts_end', 'f8', ('event',))
    v.units = "minutes since 1970-01-01 00:00:00 UTC"
    v.long_name = "end of the timeseries window (from AORC, time step n_steps-1)"
    v[:] = aorc_ts_ends[aorc_indices]

    v = nc_out.createVariable('event_gage_id', str, ('event',))
    v.long_name = "zero-padded 8-digit USGS gauge ID downstream of this event"
    v[:] = aorc_gage_ids[aorc_indices]

    v = nc_out.createVariable('event_divide_id', str, ('event',))
    v.long_name = "NextGen catchment ID downstream of this event"
    v[:] = aorc_divide_ids[aorc_indices]

    cat_ptr_out = np.zeros(n_events + 1, dtype=np.int64)
    cat_ptr_out[1:] = np.cumsum([len(c) for c in event_common_cats])
    v_ptr = nc_out.createVariable('cat_ptr', 'i8', ('ptr',))
    v_ptr[:] = cat_ptr_out

    v_cat = nc_out.createVariable('divide_id', str, ('entry',))
    v_cat.long_name = "NextGen catchment ID"
    v_lat = nc_out.createVariable('latitude', 'f4', ('entry',))
    v_lat.units = 'degrees_north'
    v_lat.standard_name = 'latitude'
    v_lon = nc_out.createVariable('longitude', 'f4', ('entry',))
    v_lon.units = 'degrees_east'
    v_lon.standard_name = 'longitude'

    def _make_var(name: str, units: str, long_name: str) -> netCDF4.Variable:
        """Create a compressed (entry, time_step) float32 variable, pre-filled with NaN."""
        nv = nc_out.createVariable(
            name,
            'f4',
            ('entry', 'time_step'),
            fill_value=np.nan,
            zlib=True,
            complevel=args.complevel,
            chunksizes=(min(total_entries, 4096), max_steps),
        )
        nv.units = units
        nv.long_name = long_name
        nv.coordinates = "event_id n_steps ts_start ts_end event_gage_id event_divide_id cat_ptr divide_id latitude longitude"
        return nv

    v_p = _make_var('P', "mm [15 min]-1", "MRMS precipitation depth")
    v_t = _make_var('T', 'degC', "Air temperature at 2 m (interpolated)")
    v_pet = _make_var(
        'PET',
        "mm [15 min]-1",
        "Penman-Monteith ET0 (15-min, uniform split)",
    )

    log.info('Writing %d events ...', n_events)
    for out_i, (ai, mi) in tqdm(
        enumerate(zip(aorc_indices, mrms_indices)),
        total=n_events,
        desc="write events",
    ):
        lo, hi = int(cat_ptr_out[out_i]), int(cat_ptr_out[out_i + 1])
        if lo == hi:
            continue
        a_lo = int(aorc_cat_ptr[ai])
        m_lo = int(mrms_cat_ptr[mi])
        a_rows = a_lo + event_aorc_idx[out_i]
        m_rows = m_lo + event_mrms_idx[out_i]
        ns = int(out_n_steps[out_i])

        v_cat[lo:hi] = np.array(event_common_cats[out_i], dtype=object)
        v_lat[lo:hi] = nc_aorc.variables['latitude'][a_rows]
        v_lon[lo:hi] = nc_aorc.variables['longitude'][a_rows]

        p_pad = np.full((hi - lo, max_steps), np.nan, dtype=np.float32)
        p_pad[:, :ns] = nc_mrms.variables['P'][m_rows, :][:, :ns]
        v_p[lo:hi, :] = p_pad

        t_pad = np.full((hi - lo, max_steps), np.nan, dtype=np.float32)
        t_pad[:, :ns] = nc_aorc.variables['T'][a_rows, :][:, :ns]
        v_t[lo:hi, :] = t_pad

        pet_pad = np.full((hi - lo, max_steps), np.nan, dtype=np.float32)
        pet_pad[:, :ns] = nc_aorc.variables['PET'][a_rows, :][:, :ns]
        v_pet[lo:hi, :] = pet_pad

    nc_aorc.close()
    nc_mrms.close()
    nc_out.close()
    log.info('Done -> %s', args.output)
    log.info('%d events, %d event-catchment entries (ragged)', n_events, total_entries)


if __name__ == '__main__':
    merge_forcing()
