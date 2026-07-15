#!/usr/bin/env python3
"""Splice Report runner — invoked as a SUBPROCESS by the OTDR Suite hub.

Like run_secretsauce.py: the splice engine ships its OWN sor_reader324802a.py
(different from the viewer's), so it runs here in a clean interpreter with only
this folder on sys.path.  Same isolation in dev and in the frozen build.

Replicates the SOR path of splicereportmatchexfo.main() (the SOR-only branch —
no trace-enhancement, since SOR files carry 'trace' not 'full_trace'), then:
  • writes the Excel report (the deliverable), and
  • emits a JSON grid of flagged cells {fiber, splice, km, loss, category, label}
    so the hub's Splice Report page can render a clickable grid that drives the
    viewer (click a cell -> load that fiber + zoom to that km).

Contract: prints exactly ONE JSON manifest line to stdout; all engine chatter
goes to stderr.

Usage:
  python run_splicereport.py --dir-a <A> --dir-b <B> --out <xlsx> [--site-a X --site-b Y]
                             [--overrides '{"REBURN_THRESHOLD": 0.12, ...}']

--overrides carries the OTDR settings panel's threshold edits.  The panel
lives in the hub process; the engine lives here, so the values cross the
process boundary as JSON.  Each key is an engine module-level constant
(REBURN_THRESHOLD / SINGLE_DIR_THRESHOLD / BIDIR_CONNECTOR_LOSS /
LAUNCH_BAD_REFL_DB / ...) and is applied with setattr BEFORE the pipeline
runs — the engine reads those globals at runtime, so mutating them changes
what gets flagged.  Absent / empty → today's baseline behavior.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))            # repo root → error_report
try:
    from error_report import report_error
except Exception:
    def report_error(*a, **k):
        pass


def _category(res):
    # A reflective event recategorized as a dirty/bad connector by the engine
    # (reflective + real loss step) — surface the refined category in the
    # manifest so the report distinguishes it from a clean reflective event.
    if res.get('event_source') == 'dirty_connector':
                                  return 'dirty_connector'
    if res.get('is_break'):       return 'break'
    if res.get('is_broke'):       return 'broke'
    if res.get('is_dead_zone'):   return 'deadzone'
    if res.get('is_bend'):        return 'bend'
    if res.get('is_ref'):         return 'ref'
    if res.get('is_gainer'):      return 'gainer'
    if res.get('is_bfill'):       return 'bfill'
    if res.get('is_a_only'):      return 'a_only'
    if res.get('is_b_only'):      return 'b_only'
    loss = res.get('bidir_loss')
    if loss is not None and loss >= 0.160:
        return 'reburn'
    return 'event'


# ── Dataset-provenance tolerances (FIX 3 — Elmdale-Miller guard) ──────────
# A bidirectional report only makes sense when the A-set and B-set are the
# SAME cable shot from both ends.  If the two folders came from different
# acquisitions (wrong folder picked, mixed spans, our report vs a reference
# from a different test), the per-fiber A↔B mirror silently pairs unrelated
# events and the whole grid is bogus with no sign of why.  These tolerances
# decide when the two directions have diverged enough to warn the tech.
PROV_EOL_TOL_KM   = 2.0   # median end-of-line span may differ by this much
PROV_CLOSURE_TOL  = 2     # discovered closure counts may differ by this many


def _nominal_wavelength_nm(wl):
    """Snap an exact per-trace wavelength (e.g. 1548.0 / 1539.8 nm — EXFO
    records the laser's measured λ, which jitters a few nm) to the nearest
    standard OTDR band so a provenance check compares 1310-vs-1550, not the
    instrument's per-shot jitter."""
    if wl is None:
        return None
    bands = (1310.0, 1383.0, 1490.0, 1550.0, 1577.0, 1625.0, 1650.0)
    return min(bands, key=lambda b: abs(b - float(wl)))


def _median_eol_km(fibers):
    """Median end-of-line distance across a direction's fibers (the cable
    span as that direction measured it)."""
    import numpy as np
    eols = []
    for r in fibers.values():
        ends = [e['dist_km'] for e in r.get('events', []) if e.get('is_end')]
        if ends:
            eols.append(min(ends))
    return float(np.median(eols)) if eols else None


def _modal_nominal_wl(fibers):
    """Most-common nominal wavelength band across a direction's fibers."""
    from collections import Counter
    c = Counter()
    for r in fibers.values():
        nm = _nominal_wavelength_nm(r.get('wavelength'))
        if nm is not None:
            c[nm] += 1
    return c.most_common(1)[0][0] if c else None


def _provenance_warnings(E, fa, fb):
    """Pre-flight: confirm the A-set and B-set look like the SAME cable shot
    from both ends.  Returns a list of human-readable WARNING strings (empty
    when the pair is consistent).  Purely additive / defensive — any internal
    error is swallowed so the report still runs."""
    warns = []
    try:
        eol_a, eol_b = _median_eol_km(fa), _median_eol_km(fb)
        if eol_a is not None and eol_b is not None and abs(eol_a - eol_b) > PROV_EOL_TOL_KM:
            warns.append(
                "DATASET MISMATCH: A-set median EOL %.2f km vs B-set %.2f km "
                "(differ by %.2f km > %.1f km tolerance) — A and B may be from "
                "DIFFERENT acquisitions; the bidirectional pairing will be "
                "unreliable."
                % (eol_a, eol_b, abs(eol_a - eol_b), PROV_EOL_TOL_KM))

        wl_a, wl_b = _modal_nominal_wl(fa), _modal_nominal_wl(fb)
        if wl_a is not None and wl_b is not None and wl_a != wl_b:
            warns.append(
                "DATASET MISMATCH: A-set wavelength ~%d nm vs B-set ~%d nm — "
                "the two directions were shot at different wavelengths; they "
                "are not the same bidirectional acquisition."
                % (int(wl_a), int(wl_b)))

        # Closure-count comparison: run the same discovery on each direction.
        # Counts should match (same cable, same closures) regardless of the
        # mirrored frame; a material difference means the two sets disagree on
        # how many closures exist.
        try:
            n_clo_a = len(E.discover_splices(fa))
            n_clo_b = len(E.discover_splices(fb))
            if abs(n_clo_a - n_clo_b) > PROV_CLOSURE_TOL:
                warns.append(
                    "DATASET MISMATCH: A-set has %d discovered closures vs "
                    "B-set %d (differ by %d > %d tolerance) — the directions "
                    "disagree on the cable's closure layout; check that A and "
                    "B are the same span."
                    % (n_clo_a, n_clo_b, abs(n_clo_a - n_clo_b), PROV_CLOSURE_TOL))
        except Exception as _exc:
            print("splicereport: provenance closure-count check skipped (%s)"
                  % _exc, file=sys.stderr)
    except Exception as _exc:
        # Never let a defensive pre-flight crash the report.
        print("splicereport: provenance guard skipped (%s)" % _exc, file=sys.stderr)
    return warns


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir-a', required=True)
    ap.add_argument('--dir-b', required=True)
    ap.add_argument('--out', required=True, help='output .xlsx path')
    ap.add_argument('--site-a', default='A')
    ap.add_argument('--site-b', default='B')
    ap.add_argument('--threshold', type=float, default=None)
    ap.add_argument('--ribbon-size', type=int, default=None)
    ap.add_argument('--overrides', default=None,
                    help='JSON dict of engine-global threshold overrides '
                         'from the OTDR settings panel.')
    args = ap.parse_args()

    real_stdout = sys.stdout
    sys.stdout = sys.stderr                            # engine prints → stderr

    def emit(payload):
        real_stdout.write(json.dumps(payload) + '\n')
        real_stdout.flush()

    a = args.dir_a.strip().strip('"')
    b = args.dir_b.strip().strip('"')
    if not os.path.isdir(a) or not os.path.isdir(b):
        emit({'ok': False, 'error': 'Both A and B folders are required and must exist.'})
        return

    try:
        import numpy as np
        import splicereportmatchexfo as E

        # ── Apply OTDR-panel threshold overrides to the engine module ──
        # The hub serialized the panel's settings to JSON; each key is an
        # engine module-level constant the engine reads at runtime
        # (REBURN_THRESHOLD, SINGLE_DIR_THRESHOLD, BIDIR_CONNECTOR_LOSS,
        # LAUNCH_BAD_REFL_DB, ...).  Mutate them BEFORE deriving the
        # `threshold` local below so a changed bidir splice loss actually
        # lowers the bidir flag threshold.  Only override globals that
        # already exist on the engine (ignore unknown / visual-only rows).
        if args.overrides:
            try:
                _ov = json.loads(args.overrides)
            except (json.JSONDecodeError, TypeError):
                _ov = {}
            # A bad --overrides must degrade to baseline, never abort the report.
            # Valid JSON that isn't a dict (e.g. '5', '[1,2]', 'true') has no
            # .items(); ignore it wholesale.
            if not isinstance(_ov, dict):
                _ov = {}
            import math
            # Integer COUNT globals must stay positive (RIBBON_SIZE feeds a
            # grid divisor — 0/negative → ZeroDivisionError / corrupt grid).
            _positive_int_globals = {'RIBBON_SIZE'}
            # Loss-magnitude thresholds must stay finite AND positive: json.loads
            # accepts the NaN/Infinity tokens and float('nan') coerces cleanly, so
            # an unvalidated REBURN_THRESHOLD=NaN made `abs(loss) >= nan` ALWAYS
            # False → zero reburns flagged, silently defeating the 0.160 invariant
            # (and a negative threshold flags everything).  NaN/inf is never a
            # valid value for ANY numeric global, so reject it universally.
            # BEND_SPLICE_FOLD_KM is a DISTANCE: 0/negative would pull events
            # sitting AT splices into phantom columns — keep it positive too.
            _positive_float_globals = {'REBURN_THRESHOLD', 'BEND_SPLICE_FOLD_KM'}
            for _k, _v in _ov.items():
                if not hasattr(E, _k):
                    continue
                _cur = getattr(E, _k)
                try:
                    if isinstance(_cur, int) and not isinstance(_cur, bool):
                        _new = int(_v)
                        if _k in _positive_int_globals and _new <= 0:
                            print("splicereport: skip override %s=%r (must be > 0)"
                                  % (_k, _v), file=sys.stderr)
                            continue
                        setattr(E, _k, _new)
                    else:
                        _new = float(_v)
                        if not math.isfinite(_new):
                            print("splicereport: skip override %s=%r (not finite)"
                                  % (_k, _v), file=sys.stderr)
                            continue
                        if _k in _positive_float_globals and _new <= 0:
                            print("splicereport: skip override %s=%r (must be > 0)"
                                  % (_k, _v), file=sys.stderr)
                            continue
                        setattr(E, _k, _new)
                except (TypeError, ValueError, OverflowError):
                    # Non-numeric / wrong-type / non-representable (int(inf)) value:
                    # skip this key, keep the engine's baseline for it.
                    print("splicereport: skip bad override %s=%r" % (_k, _v),
                          file=sys.stderr)
                    continue

        threshold = args.threshold if args.threshold is not None else E.REBURN_THRESHOLD
        ribbon_size = args.ribbon_size if args.ribbon_size is not None else E.RIBBON_SIZE

        print("Loading A/B trace files…", file=sys.stderr, flush=True)
        fa, fb = E.load_all(a, b)
        if not fa or not fb:
            emit({'ok': False, 'error': f'Loaded A={len(fa)} B={len(fb)} fibers — both directions required.'})
            return
        n_fibers = max(fa.keys())
        # A mislabeled / stray file whose parsed fiber number is absurd (an
        # unhandled wavelength suffix, or a concatenated multi-λ tail → billions)
        # balloons the ribbon × splice grid into a multi-minute write_xlsx hang /
        # OOM.  No real cable exceeds ~1728 fibers, so DROP any fiber numbered far
        # past the file count and well past any real cable — loudly — instead of
        # silently building an enormous grid (warning alone never capped it).
        _sane_max = max(2 * len(fa) + 2 * ribbon_size, 5000)
        _stray = sorted(k for k in fa if k > _sane_max)
        if _stray and len(_stray) == len(fa):
            # EVERY A-side file parsed past the ceiling.  A whole folder of real
            # traces is not all mislabeled — that is the parser failing to read
            # this naming pattern (tie-panel names like ``PTL1PTL60145`` jam a
            # 1-digit ILA suffix onto the port and used to read as 60145).  Fail
            # closed with an honest message instead of dropping every file and
            # blaming the filenames; building the grid up to a spurious max would
            # hang / OOM.
            emit({'ok': False, 'error':
                  'Could not read a usable fiber/port number from any A-side '
                  'filename (parsed e.g. #%s) — the filename pattern was not '
                  'recognized.' % ', '.join(map(str, _stray[:5]))})
            return
        if _stray:
            # A few outliers among otherwise good files: drop them loudly and
            # keep going on the rest (the grid stays sane).
            print("splicereport: dropping %d stray-numbered file(s) (fiber #%s%s) — "
                  "a mislabeled / unhandled-wavelength filename was inflating the "
                  "grid; fix the filename(s) to include those fibers."
                  % (len(_stray), ', '.join(map(str, _stray[:5])),
                     '…' if len(_stray) > 5 else ''), file=sys.stderr)
            for _k in _stray:
                fa.pop(_k, None)
                fb.pop(_k, None)
            n_fibers = max(fa.keys())
        # A moderate skew that survives the drop (a mislabeled file whose number
        # is high but not absurd) keeps the grid sane, but still warn so the tech
        # can spot it.
        if n_fibers > 2 * len(fa):
            print("splicereport: warning: max fiber number %d but only %d A-side "
                  "files loaded — a mislabeled / stray file may be inflating the "
                  "grid (check filenames)." % (n_fibers, len(fa)), file=sys.stderr)

        # ── Dataset-provenance pre-flight (FIX 3) ──
        # Before pairing A↔B, confirm the two folders are the SAME cable shot
        # from both ends (matched EOL span, wavelength, closure count).  A
        # mismatch means A and B came from different acquisitions and the
        # bidirectional grid would be silently bogus — surface a clear WARNING
        # in the manifest (additive; the report still runs).
        provenance_warnings = _provenance_warnings(E, fa, fb)
        for _w in provenance_warnings:
            print("splicereport: " + _w, file=sys.stderr)

        # Pass 0 — normalize events for splice discovery (SOR path keeps these).
        for r in list(fa.values()) + list(fb.values()):
            r['_raw_events'] = r['events']
            # Offset between normalized event coords and the raw trace samples,
            # so the silent-side windower indexes the (unshifted) trace right.
            r['_trace_offset_km'] = E._untrimmed_launch_offset_km(r['events'])
            r['events'] = E._normalize_untrimmed_events(r['events'])

        cand = E.discover_splices(fa)
        # fibers_b lets the B direction veto the end-region phantom drop: a
        # real splice in the last 3 km of the cable (HOWLAN Splice 1) sits
        # near B's launch and is unmistakable from there — without this it
        # was silently deleted whenever the span was loaded in the "wrong"
        # direction.
        real, phantom = E.refine_closure_centers(fa, cand, return_phantoms=True,
                                                 fibers_b=fb)
        splices = sorted(list(real) + list(phantom),
                         key=lambda sp: sp.get('position_km_refined', sp['position_km']))
        num = 0
        for sp in splices:
            if sp.get('column_kind') == 'splice':
                num += 1
                sp['splice_display_num'] = num

        first_splice_km = splices[0]['position_km'] if splices else None
        launch_issues = E.detect_launch_issues(fa, fb, first_splice_km)

        ends = sorted([e['dist_km'] for r in fa.values() for e in r['events'] if e['is_end']])
        span_km = round(float(np.median(ends[int(len(ends) * 0.75):])), 2) if ends else 0.0

        # SOR-only path: no trace enhancement; free the raw-event stash.
        for r in list(fa.values()) + list(fb.values()):
            r.pop('_raw_events', None)

        print(f"Analyzing {len(fa)} fibers across {len(splices)} closures "
              "(bidirectional)…", file=sys.stderr, flush=True)
        results = E.analyze_all(fa, fb, splices, threshold)
        a_st = E.scan_a_standalone_events(fa, splices, results, span_km, fibers_b=fb)
        ghost = E.scan_bidir_ghost_reflections(fa, fb, splices, {**results, **a_st}, span_km)
        merged = E.scan_merged_reflective_events(fa, fb, splices, {**results, **a_st, **ghost}, span_km)
        bpb = E.scan_b_past_breaks(fa, fb, splices, threshold, results, span_km)
        pre = {**results, **a_st, **ghost, **merged, **bpb}
        bside = E.scan_b_side_breaks(fa, fb, splices, pre, span_km)
        all_results = {**results, **a_st, **ghost, **merged, **bpb, **bside}

        E.apply_field_gainer_rule(all_results, span_km)
        E.apply_connector_loss_rule(all_results, E.BIDIR_CONNECTOR_LOSS)
        # Additive review-bend sweep: surface off-grid consensus bends the
        # length-model/LSA test silently drops (display-only; never demotes).
        all_results.update(
            E.flag_consensus_bends(all_results, fa, fb, splices, span_km))
        # Account-then-flag: split_offsplice now keeps a fiber's helix-drifted
        # OWN splice attributed to its closure column (one column per closure,
        # like the tech grid) and only spins off GENUINELY additional events.
        all_results, splices = E.split_offsplice_events_into_own_columns(
            all_results, splices, total_span_km=span_km, fibers_a=fa)

        cells, lca, lcb = E.build_ribbon_data(
            all_results, n_fibers, ribbon_size, len(splices), launch_issues=launch_issues)

        # ── Distributed section-loss pass (ADDITIVE, fully separate) ──
        # Surfaces degrading fiber STRETCHES (elevated dB/km, no discrete event)
        # that the event-based grid above is blind to.  A-direction only.  This
        # never touches all_results / splices / cells, so n_flagged is untouched;
        # it gets its OWN category + count (n_distributed_loss).
        #
        # The raw per-fiber sections are then AGGREGATED into cable-wide
        # FINDINGS: a real degradation shows up as the same km region on many
        # fibers, so emitting hundreds of per-fiber rows is noise.  The findings
        # list (one row per real region) is the primary output; the raw
        # per-fiber section count is kept as a reference field.
        try:
            distributed_loss_sections = E.scan_distributed_loss(fa)
            distributed_loss = E.aggregate_distributed_loss(distributed_loss_sections)
        except Exception as _exc:
            print("splicereport: distributed-loss pass skipped (%s)" % _exc,
                  file=sys.stderr)
            distributed_loss_sections = []
            distributed_loss = []

        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        print("Writing the Excel report…", file=sys.stderr, flush=True)
        E.write_xlsx(cells, splices, n_fibers, ribbon_size, args.out,
                     args.site_a, args.site_b, span_km,
                     launch_cells_a=lca, launch_cells_b=lcb,
                     fibers_a=fa, fibers_b=fb, all_results=all_results,
                     distributed_loss=distributed_loss)

        # ── Grid JSON for the clickable Splice Report page ──
        def sp_km(si):
            sp = splices[si]
            return round(float(sp.get('position_km_refined', sp['position_km'])), 4)

        col = []
        for si, sp in enumerate(splices):
            col.append({'index': si, 'km': sp_km(si),
                        'kind': sp.get('column_kind', 'splice'),
                        'num': sp.get('splice_display_num')})
        grid_cells = []
        for (fnum, si), res in all_results.items():
            if si is None or si < 0 or si >= len(splices):
                continue
            grid_cells.append({
                'fiber': int(fnum),
                'splice': int(si),
                'km': sp_km(si),
                'loss': (None if res.get('bidir_loss') is None
                         else round(float(res['bidir_loss']), 3)),
                'category': _category(res),
                # Additive borderline / review marker (display-only — does not
                # affect category or whether the cell is flagged / counted).
                'borderline': bool(res.get('is_borderline', False)),
                # Real flag status (reburn/break/ref/bend/single-dir).  A
                # borderline-only cell is emitted for review with is_flagged=False
                # so it is NOT counted in n_flagged.
                'is_flagged': bool(res.get('is_flagged', True)),
                'label': str(res.get('label', '')),
            })
        grid_cells.sort(key=lambda c: (c['fiber'], c['km']))

        emit({
            'ok': True,
            'xlsx': args.out,
            'site_a': args.site_a, 'site_b': args.site_b,
            'span_km': span_km, 'n_fibers': n_fibers, 'ribbon_size': ribbon_size,
            'n_splices': sum(1 for c in col if c['kind'] == 'splice'),
            'n_columns': len(col),
            'n_flagged': sum(1 for c in grid_cells if c['is_flagged']),
            'n_borderline': sum(1 for c in grid_cells if c['borderline']),
            # Distributed section-loss is its OWN category with its OWN count —
            # deliberately NOT folded into n_flagged and NOT emitted as a grid
            # cell, so the event columns / flag count are unaffected.
            # `distributed_loss` is now the AGGREGATED cable-wide findings list
            # (one entry per real region); `n_distributed_loss` is the number of
            # findings.  The raw per-fiber section count is kept for reference.
            'n_distributed_loss': len(distributed_loss),
            'distributed_loss': distributed_loss,
            'n_distributed_loss_sections': len(distributed_loss_sections),
            'columns': col,
            'cells': grid_cells,
            # Additive provenance pre-flight result (FIX 3): empty when A and B
            # are a consistent bidirectional pair; otherwise carries clear
            # mismatch WARNINGs for the manifest / hub to surface.
            'warnings': provenance_warnings,
        })
    except Exception as exc:
        import traceback
        traceback.print_exc()                 # → stderr (hub captures via log=proc.stderr)
        # Emit the graceful manifest FIRST so a reporting hiccup can't downgrade
        # this into a "no manifest" crash for the hub.
        emit({'ok': False, 'error': f'{type(exc).__name__}: {exc}'})
        try:
            report_error("splice report engine", exc, {"dir_a": a, "dir_b": b})
        except Exception:
            pass


def _emit_fatal(exc):
    """Last-resort manifest when main() escaped its own guard (a failure before
    the try-block, or emit() itself failing).  The hub parses the LAST JSON line
    of stdout, so writing one here means it never sees a bare "no manifest".
    main() may have repointed sys.stdout at stderr, so write to the ORIGINAL
    stdout (sys.__stdout__); dump the traceback to stderr for the report."""
    import traceback as _tb
    _tb.print_exc()
    try:
        out = sys.__stdout__
        if out is not None:
            out.write(json.dumps(
                {'ok': False,
                 'error': '%s: %s' % (type(exc).__name__, exc)}) + '\n')
            out.flush()
    except Exception:
        pass


if __name__ == '__main__':
    try:
        main()
    except Exception as _exc:
        _emit_fatal(_exc)
        sys.exit(1)
