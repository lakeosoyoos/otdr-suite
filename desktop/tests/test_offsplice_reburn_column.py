"""Off-splice relocation is about DISTANCE, not category — reburns included.

DFW<->ILA1 433-864 (tech report 2026-09-22): the damage at 42.01 km is not one
of the 14 discovered closures, so a fiber whose only event there measured as a
plain splice loss -- F840 .202, F847 .675, F854 .176, F858 1.403, F861 1.883 --
was anchored to Splice 13 at 43.489 km and printed 1.48 km from where both
directions put it.  Same at the near end: F862's event at 4.546 km printed
under Splice 1 @ 3.235 km.

split_offsplice_events_into_own_columns only ever *considered* cells already
labelled bend / break / broke / reflective / single-direction / gainer, so a
plain bidirectional reburn's distance from its column was never checked.
FastReporter never moves an event that far -- its columns ARE the event
positions (see _fr_columns).  A reburn 1.5 km from a closure is not a reburn at
that closure.

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"


def _run(body):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_far_reburn_leaves_its_closure_column():
    """Five reburn cells measured at 42.01 km, assigned to the closure at
    43.489 km, get their own column at 42.01 — they do not print as Splice 13."""
    _run("""
        splices = [{'position_km': 40.322, 'position_km_refined': 40.322,
                    'column_kind': 'splice'},
                   {'position_km': 43.489, 'position_km_refined': 43.489,
                    'column_kind': 'splice'}]
        def reburn(f, km, loss):
            return {'fiber': f, 'splice_idx': 1, 'bidir_dist': km,
                    'bidir_loss': loss, 'is_flagged': True,
                    'is_bend': False, 'is_break': False, 'is_broke': False,
                    'is_ref': False, 'is_a_only': False, 'is_b_only': False,
                    'is_gainer': False}
        cells = {(840, 1): reburn(840, 42.007, 0.202),
                 (847, 1): reburn(847, 42.016, 0.675),
                 (854, 1): reburn(854, 42.014, 0.176),
                 (858, 1): reburn(858, 42.018, 1.403),
                 (861, 1): reburn(861, 42.016, 1.883)}
        out, sp2 = E.split_offsplice_events_into_own_columns(
            dict(cells), [dict(s) for s in splices], total_span_km=48.29)
        kms = [round(s.get('position_km_refined', s['position_km']), 2) for s in sp2]
        assert 42.01 in kms or 42.02 in kms, f"no 42.01 column: {kms}"
        new_idx = next(i for i, s in enumerate(sp2)
                       if abs(s.get('position_km_refined', s['position_km']) - 42.014) < 0.05)
        for f in (840, 847, 854, 858, 861):
            si = next(k[1] for k in out if k[0] == f)
            assert si == new_idx, f"fiber {f} still at column {si}, want {new_idx}"
        # Losses are untouched — only the column was ever wrong.
        assert abs(out[(840, new_idx)]['bidir_loss'] - 0.202) < 1e-9
        assert abs(out[(861, new_idx)]['bidir_loss'] - 1.883) < 1e-9
        print('OK')
    """)


def test_reburn_at_its_closure_stays_put():
    """A reburn measured AT its closure is not disturbed — no phantom column,
    no relocation.  This is the common case and must not move."""
    _run("""
        splices = [{'position_km': 43.489, 'position_km_refined': 43.489,
                    'column_kind': 'splice'}]
        def reburn(f, km, loss):
            return {'fiber': f, 'splice_idx': 0, 'bidir_dist': km,
                    'bidir_loss': loss, 'is_flagged': True,
                    'is_bend': False, 'is_break': False, 'is_broke': False,
                    'is_ref': False, 'is_a_only': False, 'is_b_only': False,
                    'is_gainer': False}
        cells = {(f, 0): reburn(f, 43.489 + (f - 500) * 0.004, 0.21)
                 for f in (500, 501, 502)}
        out, sp2 = E.split_offsplice_events_into_own_columns(
            dict(cells), [dict(s) for s in splices], total_span_km=48.29)
        assert len(sp2) == 1, f"spawned a phantom column: {sp2}"
        for f in (500, 501, 502):
            assert (f, 0) in out, f"fiber {f} moved off its closure"
        print('OK')
    """)


def test_reburn_with_no_loss_is_not_a_candidate():
    """A reburn cell carrying no bidirectional loss has no measurement to
    place, so it never spawns a column of its own."""
    _run("""
        splices = [{'position_km': 43.489, 'position_km_refined': 43.489,
                    'column_kind': 'splice'}]
        cells = {(700, 0): {'fiber': 700, 'splice_idx': 0, 'bidir_dist': 42.01,
                            'bidir_loss': None, 'is_flagged': False,
                            'is_bend': False, 'is_break': False,
                            'is_broke': False, 'is_ref': False,
                            'is_a_only': False, 'is_b_only': False,
                            'is_gainer': False}}
        out, sp2 = E.split_offsplice_events_into_own_columns(
            dict(cells), [dict(s) for s in splices], total_span_km=48.29)
        assert len(sp2) == 1, f"spawned a phantom column: {sp2}"
        print('OK')
    """)
