"""cli — folder/zip of .sor (+ anchor table) → helix-calibration .xlsx.

Usage:
    python -m helixcal.cli SOURCE ANCHORS [-o OUT.xlsx]
                           [--cable-type stranded_loose_tube|central_tube]
                           [--expected-ior 1.4682]

SOURCE  : a directory containing .sor files (searched recursively) OR a .zip
          of .sor files.
ANCHORS : the anchor table (.csv or .xlsx) per helixcal.anchors schema.

Cable type is a MANUAL setting — on this span GenParams cable_code is empty,
so cable-type auto-detect is impossible and the AEN142 sanity band must be
selected here.
"""

import argparse
import glob
import os
import sys
import tempfile
import zipfile

from . import sor_fields, anchors as anchors_mod, calibrate, report
from .calibrate import CABLE_TYPE_BANDS, DEFAULT_CABLE_TYPE


def _collect_sor_paths(source):
    """Return a list of .sor file paths from a directory (recursive) or a
    .zip.  For a zip, files are extracted to a temp dir whose path is returned
    alongside so the caller can clean it up; for a dir, tmpdir is None."""
    tmpdir = None
    if os.path.isdir(source):
        paths = sorted(glob.glob(os.path.join(source, "**", "*.sor"),
                                 recursive=True))
    elif zipfile.is_zipfile(source):
        tmpdir = tempfile.mkdtemp(prefix="helixcal_")
        with zipfile.ZipFile(source) as zf:
            members = [n for n in zf.namelist()
                       if n.lower().endswith(".sor")]
            for n in members:
                zf.extract(n, tmpdir)
        paths = sorted(glob.glob(os.path.join(tmpdir, "**", "*.sor"),
                                 recursive=True))
    elif os.path.isfile(source) and source.lower().endswith(".sor"):
        paths = [source]
    else:
        raise SystemExit(f"source {source!r} is not a directory, .zip, or .sor")
    return paths, tmpdir


def load_records(source):
    """Load all parseable trace records from a folder/zip/file source.
    Returns (records, n_failed)."""
    paths, tmpdir = _collect_sor_paths(source)
    records = []
    n_failed = 0
    try:
        for p in paths:
            try:
                rec = sor_fields.read_trace_record(p)
            except Exception:
                rec = None
            if rec:
                records.append(rec)
            else:
                n_failed += 1
    finally:
        # We keep extracted files only long enough to read them; records hold
        # no file handles, so the temp dir can be cleaned by the OS later.
        pass
    return records, n_failed


def run(source, anchors_path, output_path=None,
        cable_type=DEFAULT_CABLE_TYPE, expected_ior=None):
    """Programmatic entry: returns (CalibrationResult, output_path)."""
    if cable_type not in CABLE_TYPE_BANDS:
        print(f"WARN: cable_type {cable_type!r} has no AEN142 band; "
              f"band sanity check will be skipped", file=sys.stderr)
    records, n_failed = load_records(source)
    if n_failed:
        print(f"NOTE: {n_failed} file(s) did not parse and were skipped",
              file=sys.stderr)
    anchor_list = anchors_mod.load_anchors(anchors_path)
    result = calibrate.calibrate(
        records, anchor_list, cable_type=cable_type, expected_ior=expected_ior)

    if output_path is None:
        base = os.path.basename(os.path.normpath(source)) or "helixcal"
        output_path = os.path.join(os.getcwd(), f"helixcal_{base}.xlsx")
    report.write_report(result, output_path)
    return result, output_path


def _print_summary(result, output_path):
    print(f"\nHelix calibration — {result.n_traces} traces, "
          f"{result.n_anchors} anchor pairs")
    print(f"  m (factor)   : "
          f"{result.m:.5f}" if result.m is not None else "  m (factor)   : —")
    if result.efl_pct is not None:
        print(f"  EFL%         : {result.efl_pct:.4f} %")
    if result.b_m is not None:
        print(f"  offset b     : {result.b_m:.3f} m")
    if result.r2 is not None:
        print(f"  R²           : {result.r2:.6f}")
    print(f"  band         : {result.band_verdict}")
    print(f"  IOR          : {result.ior_label}")
    if result.fiber_m_std is not None:
        print(f"  per-fiber σ  : {result.fiber_m_std:.5f} "
              f"(outliers: {', '.join(result.outlier_fibers) or 'none'})")
    for w in result.warnings:
        print(f"  WARN: {w}")
    print(f"  report       : {output_path}")


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="helixcal",
        description="OTDR-fiber → cable-sheath helix/EFL calibration "
                    "(Corning AEN142 after-the-fact).")
    p.add_argument("source", help="folder OR .zip of .sor files (or one .sor)")
    p.add_argument("anchors", help="anchor table .csv or .xlsx")
    p.add_argument("-o", "--output", default=None, help="output .xlsx path")
    p.add_argument("--cable-type", default=DEFAULT_CABLE_TYPE,
                   choices=sorted(CABLE_TYPE_BANDS.keys()),
                   help="manual cable type → AEN142 sanity band")
    p.add_argument("--expected-ior", type=float, default=None,
                   help="fiber-spec IOR to verify stored IOR against")
    args = p.parse_args(argv)

    result, out = run(args.source, args.anchors, args.output,
                      cable_type=args.cable_type,
                      expected_ior=args.expected_ior)
    _print_summary(result, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
