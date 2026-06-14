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
import shutil
import sys
import tempfile
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


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
    ap.add_argument('--format', default='xlsx', choices=['xlsx', 'pdf'])
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
        emit({'ok': False, 'error': f'{type(exc).__name__}: {exc}', 'counts': counts})
        return

    emit({'ok': True, 'counts': counts, 'written': written})


def _safe_name(name):
    """Sanitize a filename for Windows-illegal characters."""
    bad = '/\\:*?<>|'
    return ''.join('_' if c in bad else c for c in name)


if __name__ == '__main__':
    main()
