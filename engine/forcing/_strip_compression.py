r"""Rewrite an event-indexed forcing NetCDF without zlib compression.

Stripping compression trades disk space for a ~30-60x reduction in per-run time.
"""

import argparse
import logging
import time
from pathlib import Path

import xarray as xr

log = logging.getLogger('StripCompression')


def main() -> None:
    """Rewrite an event-indexed forcing NetCDF without zlib compression."""
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()

    log.info('Reading %s (decompressing, one-time cost) ...', args.input)
    t0 = time.time()
    ds = xr.open_dataset(args.input).load()
    log.info('done (%.1fs)', time.time() - t0)

    # Build a minimal, netCDF4-backend-valid encoding per variable: no
    # compression, but keep dtype/_FillValue/chunksizes where present (the
    # rest of what open_dataset reports, e.g. szip/zstd/preferred_chunks,
    # isn't accepted as a *write* encoding by this backend).
    keep_keys = {'dtype', '_FillValue', 'chunksizes'}
    encoding = {}
    for name, var in ds.variables.items():
        enc = {k: v for k, v in var.encoding.items() if k in keep_keys}
        enc['zlib'] = False
        encoding[name] = enc

    log.info('Writing %s (uncompressed) ...', args.output)
    t1 = time.time()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    ds.to_netcdf(args.output, encoding=encoding)
    log.info('done (%.1fs)', time.time() - t1)

    in_size = args.input.stat().st_size / 1e9
    out_size = args.output.stat().st_size / 1e9
    log.info(
        '%s: %.2f GB -> %s: %.2f GB',
        args.input.name,
        in_size,
        args.output.name,
        out_size,
    )


if __name__ == '__main__':
    main()
