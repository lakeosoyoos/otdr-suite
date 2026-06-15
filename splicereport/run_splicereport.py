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
            for _k, _v in (_ov or {}).items():
                if hasattr(E, _k):
                    _cur = getattr(E, _k)
                    setattr(E, _k, int(_v) if isinstance(_cur, int)
                            and not isinstance(_cur, bool) else float(_v))

        threshold = args.threshold if args.threshold is not None else E.REBURN_THRESHOLD
        ribbon_size = args.ribbon_size if args.ribbon_size is not None else E.RIBBON_SIZE

        fa, fb = E.load_all(a, b)
        if not fa or not fb:
            emit({'ok': False, 'error': f'Loaded A={len(fa)} B={len(fb)} fibers — both directions required.'})
            return
        n_fibers = max(fa.keys())

        # Pass 0 — normalize events for splice discovery (SOR path keeps these).
        for r in list(fa.values()) + list(fb.values()):
            r['_raw_events'] = r['events']
            r['events'] = E._normalize_untrimmed_events(r['events'])

        cand = E.discover_splices(fa)
        real, phantom = E.refine_closure_centers(fa, cand, return_phantoms=True)
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
        all_results, splices = E.split_offsplice_events_into_own_columns(
            all_results, splices, total_span_km=span_km)

        cells, lca, lcb = E.build_ribbon_data(
            all_results, n_fibers, ribbon_size, len(splices), launch_issues=launch_issues)

        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        E.write_xlsx(cells, splices, n_fibers, ribbon_size, args.out,
                     args.site_a, args.site_b, span_km,
                     launch_cells_a=lca, launch_cells_b=lcb,
                     fibers_a=fa, fibers_b=fb, all_results=all_results)

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
            if si < 0 or si >= len(splices):
                continue
            grid_cells.append({
                'fiber': int(fnum),
                'splice': int(si),
                'km': sp_km(si),
                'loss': (None if res.get('bidir_loss') is None
                         else round(float(res['bidir_loss']), 3)),
                'category': _category(res),
                'label': str(res.get('label', '')),
            })
        grid_cells.sort(key=lambda c: (c['fiber'], c['km']))

        emit({
            'ok': True,
            'xlsx': args.out,
            'site_a': args.site_a, 'site_b': args.site_b,
            'span_km': span_km, 'n_fibers': n_fibers, 'ribbon_size': ribbon_size,
            'n_splices': sum(1 for c in col if c['kind'] == 'splice'),
            'n_columns': len(col), 'n_flagged': len(grid_cells),
            'columns': col,
            'cells': grid_cells,
        })
    except Exception as exc:
        import traceback
        traceback.print_exc()
        report_error("splice report engine", exc, {"dir_a": a, "dir_b": b})
        emit({'ok': False, 'error': f'{type(exc).__name__}: {exc}'})


if __name__ == '__main__':
    main()
