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
  • SOR: run on the WHOLE folder as one set (no direction split), require ≥2
    files, stage flat (dedup basenames), one report
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


# Fiber-number extraction.
#
# This is the VIEWER's extract_fiber_num, copied verbatim.  It must stay
# byte-for-byte in step with viewer/trace_server.extract_fiber_num (and with
# splicereport/splicereportmatchexfo._extract_fiber_num, which is already
# locked to it) so the number this runner emits for a pair resolves to the
# SAME file when the Viewer loads that fiber by number.
#
# It did NOT, until 2026-08-29.  The six-line version that stood here handled
# two patterns; the viewer's handles the whole catalogue surveyed across ~38k
# real files.  Measured over the 45,035 distinct OTDR filenames on disk, the
# two disagreed on 3,709:
#   * 3,679 where this runner returned None and the viewer resolved the fiber
#     (1,152 ELMNEW, 999 NEWELM, 576 PTL#PTL#####, 318 ...withstartstop, ...).
#     Those pairs came back viewable:false and the tech could not click through.
#   * 30 where BOTH returned a number and they DIFFERED — the worse case, since
#     the pair reports viewable:true and the deep link opens the WRONG trace.
#     All multi-wavelength TRC exports: TEST0001_155016251310.trc reads as
#     fiber 1 in the Viewer and 1310 here, so all twelve TEST fibers collapsed
#     onto "1310" and all eighteen VERSLK fibers onto "1625" — the runner was
#     reading the WAVELENGTH as the fiber number.
#
# desktop/tests/test_viewer_fiber_identity.py locks all three copies together.

def _extract_fiber_num(fn):
    """Extract fiber number from a SOR/JSON/TRC filename.

    Handles every naming pattern that has shown up on the user's disk
    after a survey of ~38k real files across 11 cable codes (see
    Project Memory note from 2026-06-13):

      ``LAGDUR0001.sor``                  -> 1     (run after a prefix)
      ``Norsea001_1550.sor``              -> 1     (strip _<wavelength>)
      ``Seattle to Spokane d.0431.sor``   -> 431   (rightmost digit run)
      ``20260520_LAGDUR0001.sor``         -> 1     (date prefix ignored)
      ``DURSAN001_1550 .json``            -> 1     (EXFO trailing-space)
      ``VERSLK001_131015501625 .json``    -> 1     (multi-λ suffix)
      ``TEST0001_155016251310.trc``       -> 1     (multi-λ TRC)
      ``CHC-HCH-LS-089.trc``              -> 89    (dashed long-shot)
      ``._STRROM0001_1550.sor``           -> None  (macOS AppleDouble)

    Rule:
      1. Strip the extension AND any leading "._" (AppleDouble metadata
         that lands next to real files in zips extracted on Mac).
      2. Right-strip whitespace (EXFO FastReporter exports JSON with a
         trailing space between the wavelength code and ``.json``).
      3. Strip one OR MORE concatenated trailing wavelength codes,
         e.g. ``_131015501625`` is three wavelengths jammed together.
      4. Take the RIGHTMOST run of digits — fiber numbers are
         conventionally last after any cable / span / date prefix.
      5. Tie-panel filenames butt a 1-digit ILA/panel suffix straight
         against a 4-digit zero-padded port with NO delimiter, e.g.
         ``PTL1PTL60145`` (ILA1→ILA6, port 0145) or ``DNW1DNW50148``
         (port 0148).  The rightmost run then reads as one number
         (60145) instead of the real port (145) and every fiber lands
         far past any real cable → the stray-fiber guard drops them all
         and aborts.  When the run ends in a 4-char zero-padded field
         (``0NNN``) with extra digits jammed in front, trust the padded
         port.  A genuine 4-digit fiber (1050, 1152) has no leading
         zero, so it is left untouched.

    Returns None when no fiber number can be extracted (which causes
    ``_load_dir`` to skip the file rather than silently overwrite a
    valid fiber).
    """
    # AppleDouble sidecars created by macOS zip-extractors mirror the
    # real filename with a "._" prefix; they're not OTDR data.  Skip
    # them BEFORE the digit walk so they never collide with the real
    # fiber that follows.
    if os.path.basename(fn).startswith("._"):
        return None
    stem, _ = os.path.splitext(fn)
    stem = stem.rstrip()
    # Strip ONE OR MORE concatenated trailing wavelength codes.  The
    # multi-λ EXFO exports we see in the field write all three
    # wavelengths jammed together: ``_131015501625``.  Without the +
    # quantifier the whole concatenation reads as one giant fiber
    # number (~131_500_000_000) and every fiber collides.
    stem = re.sub(
        r'[\s_\-.](?:850|1300|1310|1383|1490|1550|1577|1625|1650)+$', '', stem)
    matches = re.findall(r'\d+', stem)
    if not matches:
        return None
    run = matches[-1]
    # Tie-panel filenames jam a 1-digit ILA/panel suffix onto the 4-digit
    # zero-padded port (``PTL1PTL60145`` → run ``60145``).  If the run ends
    # in a zero-padded 4-char field with digits in front of it, the padded
    # field is the real port; the prefix is the ILA suffix.  (A real 4-digit
    # fiber like 1050 has no leading zero, so ``0\d{3}$`` won't match it.)
    m = re.search(r'0\d{3}$', run)
    if m and len(m.group()) < len(run):
        return int(m.group())
    return int(run)


# Report-output / junk directories that must NEVER be inventoried as input.
# The hub writes its pairs cache to <folder>/SecretSauce_reports/pairs_cache.json
# INSIDE the analyzed folder; without this prune, the recursive walk counted that
# .json as an acquisition, so a 2nd run on a pure-SOR folder aborted with the
# bogus "Mixed file types — keep one type per run."
_SKIP_DIRS = {'SecretSauce_reports', '__MACOSX'}


def _inventory(folder):
    sor, trc, jsn = [], [], []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]   # prune in place
        for f in files:
            # ALL dot-prefixed files: AppleDouble ('._*') plus the hub's own
            # report caches written INTO the analyzed folder
            # (.uni_result_cache.json, .sr_grid_cache.json — the back-from-
            # Viewer restore files).  No real acquisition is ever a dotfile;
            # counting one as JSON aborted a pure-SOR run with the bogus
            # "Mixed file types" (caught by the click-through audit on the
            # LAMBEY uni folder).
            if f.startswith('.'):
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


def _inventory_with_zips(folder, notes):
    """`_inventory`, plus any OTDR files that live INSIDE .zip archives.

    A span is often delivered as per-direction zips with nothing loose beside
    them (CHE to PLA 1152f = two zips holding 1152 traces each; Deming Tie
    Panel = two holding 288 each).  Without this the walk finds nothing and the
    run dead-ends on "No .sor, .trc, or .json files found" while the tech is
    looking at a folder that plainly contains the span.

    RE-DELIVERY GATE.  A folder can hold the loose files AND a zip of the same
    files — WInterhaven to Niland Final Traces carries 576 loose .sor and an
    Archive.zip of those same 576.  Taking both would compare every fiber
    against its own copy and print 576 confirmed duplicates that do not exist.
    A file extracted from a zip is therefore skipped when a file with the SAME
    basename AND the same sha256 is already in hand.  Name and content, not
    content alone: a byte-identical copy under a DIFFERENT name is a real
    duplicate the engine is meant to catch.  Every skip is reported, both on
    stderr and in the manifest, so a silently-halved input is impossible.

    Returns (sor, trc, jsn, extract_dir).  The caller must remove extract_dir.
    """
    sor, trc, jsn = _inventory(folder)
    try:
        import folder_intake as fi          # stdlib-only, bundled beside error_report
        zips = fi.zip_paths(folder)
    except Exception:                       # intake unavailable -> behave as before
        return sor, trc, jsn, None
    if not zips:
        return sor, trc, jsn, None

    seen = set()
    for p_ in sor + trc + jsn:
        try:
            seen.add(fi.content_key(p_))
        except OSError:
            pass
    extract_dir = tempfile.mkdtemp(prefix='ss_zip_')
    buckets = {'.sor': sor, '.trc': trc, '.json': jsn}
    for i, zp in enumerate(zips):
        name = os.path.basename(zp)
        dest = os.path.join(extract_dir, '_zip%d' % i)
        try:
            got = fi.extract_zip(zp, dest, fi.OTDR_EXTS_WITH_TRC)
        except Exception as exc:
            notes.append(f'{name}: unreadable, skipped ({type(exc).__name__})')
            continue
        if not got:
            continue
        added = 0
        for f in got:
            try:
                key = fi.content_key(f)
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            buckets[os.path.splitext(f)[1].lower()].append(f)
            added += 1
        skipped = len(got) - added
        if added and skipped:
            notes.append(f'{name}: added {added} file(s); skipped {skipped} '
                         f'already present outside the zip')
        elif added:
            notes.append(f'{name}: added {added} file(s)')
        else:
            notes.append(f'{name}: skipped, all {len(got)} file(s) already '
                         f'present outside the zip')
    for k in buckets:
        buckets[k].sort()
    for note in notes:
        print(f'Zip intake: {note}')
    return sor, trc, jsn, extract_dir


def _write_report(outp, data):
    """Write report bytes to `outp`, (re)creating the parent dir if it vanished.

    out_dir lives INSIDE the analyzed folder (app.py builds it as
    <folder>/SecretSauce_reports) and is created once, up front — but the SOR
    analysis then runs for minutes, and cloud-sync / AV can remove or quarantine
    that dir mid-run.  Without this, a fully successful multi-minute analysis was
    thrown away by a FileNotFoundError at the final open() (prod issue #7).
    Ensure the parent immediately before writing, with a single retry to cover a
    delete that races the write; a genuinely un-writable dir still raises and is
    reported by the caller's except."""
    d = os.path.dirname(outp) or '.'
    for attempt in (1, 2):
        try:
            os.makedirs(d, exist_ok=True)
            with open(outp, 'wb') as fh:
                fh.write(data)
            return
        except OSError:
            if attempt == 2:
                raise                       # let the caller report + emit not-ok


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

    zip_notes = []
    _state = {'zip_dir': None}

    def emit(payload):
        # Additive contract: the key only appears when a zip was consulted, so
        # every unaffected manifest stays byte-stable.
        if zip_notes:
            payload['zip_notes'] = list(zip_notes)
        if _state['zip_dir']:
            shutil.rmtree(_state['zip_dir'], ignore_errors=True)
            _state['zip_dir'] = None
        real_stdout.write(json.dumps(payload) + '\n')
        real_stdout.flush()

    folder = args.folder.strip().strip('"')
    if not folder or not os.path.isdir(folder):
        emit({'ok': False, 'error': f'not a folder: {folder}'})
        return

    sor, trc, jsn, _state['zip_dir'] = _inventory_with_zips(folder, zip_notes)
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
    # Suspected broken / short fibers surfaced by the SOR analysis.  Emitted
    # as a top-level `short_traces` manifest key ONLY when non-empty, so
    # every unaffected manifest stays byte-stable (additive contract).
    # `window_warnings` follows the same contract (inconsistent-folder guard).
    short_traces_all = []
    window_warnings_all = []

    try:
        if sor:
            from report_sor import run_sor_xlsx_bytes, run_sor_bytes

            # Run on the WHOLE uploaded folder as ONE set — never split by
            # direction / location key.  The tech uploads a folder; Secret Sauce
            # reports on whatever is in it (one report, all files compared).
            groups = {'report': list(sor)} if len(sor) >= 2 else {}
            if not groups:
                emit({'ok': False, 'error': 'Need >=2 SOR files in the folder to compare.',
                      'counts': counts})
                return

            for key, paths in groups.items():
                stage = _stage_flat(paths)
                title = f'Secret Sauce — {key}'
                meta = {}
                try:
                    if want_xlsx:
                        data, nf, npairs = run_sor_xlsx_bytes(stage, title, meta=meta)
                    else:
                        data, nf, npairs = run_sor_bytes(stage, title, meta=meta)
                finally:
                    shutil.rmtree(stage, ignore_errors=True)
                short_traces_all.extend(meta.get('short_traces') or [])
                if meta.get('window_guard'):
                    window_warnings_all.append(meta['window_guard'])
                fname = (f'{key}_secret_sauce.{ext}' if len(groups) > 1 else f'report.{ext}')
                fname = _safe_name(fname)
                outp = os.path.join(args.out_dir, fname)
                _write_report(outp, data)
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
            _write_report(outp, data)
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
            _write_report(outp, data)
            written.append({'path': outp, 'n_files': nf, 'n_pairs': npairs, 'key': 'JSON'})

    except Exception as exc:
        import traceback
        traceback.print_exc()                       # goes to stderr
        report_error("secret sauce engine", exc,
                     {"counts": counts, "format": args.format})
        emit({'ok': False, 'error': f'{type(exc).__name__}: {exc}', 'counts': counts})
        return

    payload = {'ok': True, 'counts': counts, 'written': written}
    if short_traces_all:
        payload['short_traces'] = short_traces_all
    if window_warnings_all:
        payload['window_warnings'] = window_warnings_all
    emit(payload)


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
    """Run the SOR analysis on the WHOLE folder (one group, no direction split) and emit
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

    # One group = the whole folder (no direction / location split).
    groups = {'report': list(sor)} if len(sor) >= 2 else {}
    if not groups:
        emit({'ok': False,
              'error': 'Need >=2 SOR files in the folder to compare.',
              'counts': counts})
        return

    out_pairs = []
    n_files = 0
    short_traces_all = []
    window_warnings_all = []
    for key, paths in groups.items():
        stage = _stage_flat(paths)
        try:
            analysis = _analyze_sor(stage)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        n_files += len(analysis['files'])
        # Suspected broken / short fibers (EOF far below the folder median).
        # Included in the manifest ONLY when present, so unaffected
        # manifests stay byte-stable (additive contract).  Same for the
        # inconsistent-folder window warning.
        short_traces_all.extend(analysis.get('short_traces') or [])
        if analysis.get('window_guard'):
            window_warnings_all.append(analysis['window_guard'])
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
            rec = {
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
            }
            if pr.get('raw_identical'):
                # Raw-identity short-circuit (report_sor): the two files carry
                # the same acquisition data (literal copy / re-export).  Key is
                # only present when it fired, keeping every other manifest
                # byte-stable.
                rec['raw_identical'] = True
                rec['verdict'] = 'CONFIRMED duplicate (identical)'
            out_pairs.append(rec)

    # Worst-first: highest likelihood, then lowest σ (most similar) as tiebreak.
    out_pairs.sort(key=lambda d: (-d['p_dup'], d['score']))
    n_flagged = sum(1 for d in out_pairs if d['p_dup'] > 0.5)

    # Cap the EMITTED pair list.  out_pairs is sorted worst-first, so the likely
    # duplicates the tech cares about are at the top; the long tail is near-zero
    # non-duplicates nobody scrolls to.  On a combined bidirectional folder that
    # tail is enormous — 864 files → 372,816 pairs, 1152 → 662,976 — and
    # emitting them all builds an ~80-140 MB manifest + HTML table that freezes
    # the browser.  Keep the TRUE totals; ship only the top rows.
    MAX_EMIT_PAIRS = 500
    n_pairs_total = len(out_pairs)

    payload = {
        'ok': True,
        'mode': 'pairs',
        'folder': folder,
        'counts': counts,
        'n_files': n_files,
        'n_pairs': n_pairs_total,
        'n_flagged': n_flagged,
        'pairs': out_pairs[:MAX_EMIT_PAIRS],
        'pairs_truncated': n_pairs_total > MAX_EMIT_PAIRS,
        'pairs_shown': min(n_pairs_total, MAX_EMIT_PAIRS),
    }
    if short_traces_all:
        payload['short_traces'] = short_traces_all
    if window_warnings_all:
        payload['window_warnings'] = window_warnings_all
    emit(payload)


def _safe_name(name):
    """Sanitize a filename for Windows-illegal characters."""
    bad = '/\\:*?<>|'
    return ''.join('_' if c in bad else c for c in name)


if __name__ == '__main__':
    main()
