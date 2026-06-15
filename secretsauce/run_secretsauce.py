#!/usr/bin/env python3
"""Secret Sauce runner — invoked as a SUBPROCESS by the OTDR Suite hub.

Why a subprocess: Secret Sauce ships its own (divergent) copy of
sor_reader324802a.py.  The hub process already loads the *viewer's* copy of
that module for the trace server, and two same-named modules can't coexist in
one interpreter.  Running here in a fresh interpreter — with only this folder
on sys.path — gives Secret Sauce its own clean namespace.  This is also how it
bundles for the .exe, so the boundary is identical in dev and prod.

Mirrors the logic of SecretSauce-Desktop/desktop_app.py:
  • recursive inventory of .sor / .trc / .json
  • reject mixed file types
  • SOR: group by file-internal GenParams direction key, keep groups ≥2,
    stage flat (dedup basenames), one report per group
  • TRC / JSON: stage flat, one report

Contract: prints exactly ONE line of JSON to stdout (the manifest).  All
engine chatter is redirected to stderr so it can't corrupt the manifest.

Usage:
  python run_secretsauce.py --folder <input> --out-dir <output> --format xlsx
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# Repo root (parent) on path so the stdlib-only error_report module imports in
# dev; in a frozen build the launcher adds the bundle root before dispatch.
sys.path.insert(0, os.path.dirname(HERE))
try:
    from error_report import report_error
except Exception:                                  # reporting is best-effort
    def report_error(*a, **k):
        pass


# Fiber-number extraction — MUST stay byte-for-byte in step with the viewer's
# trace_server.extract_fiber_num so the number we emit for a pair resolves to
# the SAME .sor file when the Viewer loads it by number from the same folder.
_FIBER_NUM_RE = re.compile(r'(\d{3,4})_\d{3,4}\b')


def _extract_fiber_num(fn):
    """STRROM0064_1550.sor -> 64,  ELMMIL1152_1550.sor -> 1152."""
    m = _FIBER_NUM_RE.search(fn)
    if m:
        return int(m.group(1))
    base = os.path.splitext(fn)[0]
    tail = re.search(r'(\d{3,4})$', base)
    return int(tail.group(1)) if tail else None


def _inventory(folder):
    sor, trc, jsn = [], [], []
    for root, _dirs, files in os.walk(folder):
        for f in files:
            if f.startswith('._'):                 # AppleDouble files
                continue
            low = f.lower()
            full = os.path.join(root, f)
            if low.endswith('.sor'):
                sor.append(full)
            elif low.endswith('.trc'):
                trc.append(full)
            elif low.endswith('.json'):
                jsn.append(full)
    return sor, trc, jsn


def _stage_flat(paths):
    """Copy files into a fresh flat temp dir, de-duplicating basenames."""
    td = tempfile.mkdtemp(prefix='ss_stage_')
    used = set()
    for p in paths:
        base = os.path.basename(p)
        dest = base
        i = 1
        while dest.lower() in used:
            stem, ext = os.path.splitext(base)
            dest = f'{stem}__{i}{ext}'
            i += 1
        used.add(dest.lower())
        shutil.copy(p, os.path.join(td, dest))
    return td


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--folder', required=True)
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--format', default='xlsx', choices=['xlsx', 'pdf', 'pairs'])
    args = ap.parse_args()

    # Redirect engine stdout -> stderr; keep a clean fd for the manifest.
    real_stdout = sys.stdout
    sys.stdout = sys.stderr

    def emit(payload):
        real_stdout.write(json.dumps(payload) + '\n')
        real_stdout.flush()

    folder = args.folder.strip().strip('"')
    if not folder or not os.path.isdir(folder):
        emit({'ok': False, 'error': f'not a folder: {folder}'})
        return

    sor, trc, jsn = _inventory(folder)
    counts = {'sor': len(sor), 'trc': len(trc), 'json': len(jsn)}
    n_kinds = sum(bool(x) for x in (sor, trc, jsn))
    if n_kinds == 0:
        emit({'ok': False, 'error': 'No .sor, .trc, or .json files found.', 'counts': counts})
        return
    if n_kinds > 1:
        emit({'ok': False, 'error': 'Mixed file types — keep one type per run.', 'counts': counts})
        return

    # ── "Stay in app" pairs mode ────────────────────────────────────────
    # Emit the per-pair metrics as JSON (no file written) so the hub can
    # render the duplicate report in-page and deep-link each pair into the
    # Viewer.  SOR-only: the in-app overlay loads .sor fibers by number from
    # the picked folder, which the TRC/JSON engines don't map onto.
    if args.format == 'pairs':
        if not sor:
            emit({'ok': False,
                  'error': 'In-app pairs view supports .sor files only '
                           '(use Excel/PDF for .trc / .json).',
                  'counts': counts})
            return
        _emit_pairs(sor, folder, counts, emit)
        return

    os.makedirs(args.out_dir, exist_ok=True)
    want_xlsx = (args.format == 'xlsx')
    ext = 'xlsx' if want_xlsx else 'pdf'
    written = []

    try:
        if sor:
            from report_sor import run_sor_xlsx_bytes, run_sor_bytes
            from sor_reader324802a import direction_key_from_genparams

            groups = defaultdict(list)
            for p in sor:
                key = direction_key_from_genparams(p) or os.path.basename(p)[:8]
                groups[key].append(p)
            groups = {k: v for k, v in groups.items() if len(v) >= 2}
            if not groups:
                emit({'ok': False, 'error': 'Could not form a direction group with >=2 SOR files.',
                      'counts': counts})
                return

            for key, paths in groups.items():
                stage = _stage_flat(paths)
                title = f'Secret Sauce — {key}'
                try:
                    if want_xlsx:
                        data, nf, npairs = run_sor_xlsx_bytes(stage, title)
                    else:
                        data, nf, npairs = run_sor_bytes(stage, title)
                finally:
                    shutil.rmtree(stage, ignore_errors=True)
                fname = (f'{key}_secret_sauce.{ext}' if len(groups) > 1 else f'report.{ext}')
                fname = _safe_name(fname)
                outp = os.path.join(args.out_dir, fname)
                with open(outp, 'wb') as fh:
                    fh.write(data)
                written.append({'path': outp, 'n_files': nf, 'n_pairs': npairs, 'key': key})

        elif trc:
            from report import run_trc_xlsx_bytes, run_trc_bytes
            stage = _stage_flat(trc)
            try:
                if want_xlsx:
                    data, nf, npairs = run_trc_xlsx_bytes(stage, 'Secret Sauce')
                else:
                    data, nf, npairs = run_trc_bytes(stage, 'Secret Sauce')
            finally:
                shutil.rmtree(stage, ignore_errors=True)
            outp = os.path.join(args.out_dir, f'report.{ext}')
            with open(outp, 'wb') as fh:
                fh.write(data)
            written.append({'path': outp, 'n_files': nf, 'n_pairs': npairs, 'key': 'TRC'})

        else:  # json
            from report import run_json_xlsx_bytes, run_json_bytes
            stage = _stage_flat(jsn)
            try:
                if want_xlsx:
                    data, nf, npairs = run_json_xlsx_bytes(stage, 'Secret Sauce')
                else:
                    data, nf, npairs = run_json_bytes(stage, 'Secret Sauce')
            finally:
                shutil.rmtree(stage, ignore_errors=True)
            outp = os.path.join(args.out_dir, f'report.{ext}')
            with open(outp, 'wb') as fh:
                fh.write(data)
            written.append({'path': outp, 'n_files': nf, 'n_pairs': npairs, 'key': 'JSON'})

    except Exception as exc:
        import traceback
        traceback.print_exc()                       # goes to stderr
        report_error("secret sauce engine", exc,
                     {"counts": counts, "format": args.format})
        emit({'ok': False, 'error': f'{type(exc).__name__}: {exc}', 'counts': counts})
        return

    emit({'ok': True, 'counts': counts, 'written': written})


def _verdict(p_dup):
    """Plain-English verdict matching the report's likelihood tiers."""
    if p_dup > 0.99:
        return 'CONFIRMED duplicate'
    if p_dup > 0.5:
        return 'Likely duplicate'
    if p_dup > 0.1:
        return 'Possible duplicate'
    return 'Unique'


def _emit_pairs(sor, folder, counts, emit):
    """Run the SOR analysis (grouped by direction like the report) and emit
    every pair with its fiber numbers, score, likelihood, and verdict — sorted
    worst-first (most-likely-duplicate first) — so the hub can render an
    in-page report whose rows deep-link both fibers into the Viewer.

    Each pair carries `viewable` + (when False) `reason`.  A pair is viewable
    when both files map to DISTINCT fiber numbers that are UNIQUE within the
    picked folder, because the Viewer resolves a fiber by number from that one
    folder (its extract_fiber_num is mirrored here).  Cross-direction-group
    files can collide on a number in a flat folder; we flag those rather than
    silently overlay the wrong trace.
    """
    from report_sor import _analyze_sor
    from sor_reader324802a import direction_key_from_genparams

    # Fiber numbers as the Viewer will see them in this (flat) folder: a
    # number is ambiguous if more than one .sor in the folder yields it.
    num_counts = defaultdict(int)
    name_to_num = {}
    for p in sor:
        base = os.path.basename(p)
        num = _extract_fiber_num(base)
        name_to_num[os.path.splitext(base)[0]] = num
        if num is not None:
            num_counts[num] += 1

    groups = defaultdict(list)
    for p in sor:
        key = direction_key_from_genparams(p) or os.path.basename(p)[:8]
        groups[key].append(p)
    groups = {k: v for k, v in groups.items() if len(v) >= 2}
    if not groups:
        emit({'ok': False,
              'error': 'Could not form a direction group with >=2 SOR files.',
              'counts': counts})
        return

    out_pairs = []
    n_files = 0
    for key, paths in groups.items():
        stage = _stage_flat(paths)
        try:
            analysis = _analyze_sor(stage)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        n_files += len(analysis['files'])
        for pr in analysis['pairs']:
            na, nb = pr['a'], pr['b']           # filename stems
            fa = name_to_num.get(na)
            fb = name_to_num.get(nb)
            viewable, reason = True, None
            if fa is None or fb is None:
                viewable, reason = False, 'no fiber number in filename'
            elif fa == fb:
                viewable, reason = False, 'both files share fiber number'
            elif num_counts.get(fa, 0) > 1 or num_counts.get(fb, 0) > 1:
                viewable, reason = False, 'fiber number not unique in folder'
            out_pairs.append({
                'group': key,
                'fileA': na, 'fileB': nb,
                'fiberA': fa, 'fiberB': fb,
                'score': round(float(pr['score']), 4),
                'shape_r': (None if pr.get('shape_r') is None
                            else round(float(pr['shape_r']), 4)),
                'p_dup': round(float(pr['p_dup']), 4),
                'verdict': _verdict(float(pr['p_dup'])),
                'viewable': viewable,
                'reason': reason,
            })

    # Worst-first: highest likelihood, then lowest σ (most similar) as tiebreak.
    out_pairs.sort(key=lambda d: (-d['p_dup'], d['score']))
    n_flagged = sum(1 for d in out_pairs if d['p_dup'] > 0.5)

    emit({
        'ok': True,
        'mode': 'pairs',
        'folder': folder,
        'counts': counts,
        'n_files': n_files,
        'n_pairs': len(out_pairs),
        'n_flagged': n_flagged,
        'pairs': out_pairs,
    })


def _safe_name(name):
    """Sanitize a filename for Windows-illegal characters."""
    bad = '/\\:*?<>|'
    return ''.join('_' if c in bad else c for c in name)


if __name__ == '__main__':
    main()
