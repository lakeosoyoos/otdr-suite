"""Uni: one event, one column (PLACHE 1152 double-count).

The unidirectional report double-reported EVERY closure on the boss's
PLACHE 1152 run at 2500 ns: 21 of 21 splice columns had a "Bend/Damage"
twin 80-130 m away carrying the same fibers at the same losses, 3,027 of
8,618 flagged rows were one event counted twice, and 24 bend/damage
columns existed where 1 was real.

Two radii caused it, both below the pulse smear (255 m at 2500 ns) while
per-fiber scatter runs ~100 m:

  * uni_find_off_splice_events excluded an event from the off-splice list
    only within UNI_CLOSURE_MATCH_KM (75 m) of a closure, so events in the
    75-255 m band escaped and seeded their own cluster;
  * the grid then filled splice columns from a 75 m window and off-splice
    columns from UNI_OFF_SPLICE_CLUSTER_M (100 m) — overlapping windows
    around centres ~100 m apart, so one event filled both cells.

Both now floor at the run's pulse smear via _uni_at_splice_km(), matching
the bidirectional path's _fold_km().  BOTH halves are required: floor only
the exclusion and an event 75-255 m from a closure would seed no column
AND fill no cell — it would vanish from the report entirely, which is
worse than double-reporting it.

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
    out = p.stdout.strip().splitlines()
    assert out and out[-1] == "OK", p.stdout


def test_at_splice_radius_is_pulse_floored():
    """_uni_at_splice_km(): legacy 75 m with no readable pulse; the smear
    when there is one; never narrower than the smear."""
    _run("""
        E._RUN_PULSE_SMEAR_KM = 0.0
        assert abs(E._uni_at_splice_km() - E.UNI_CLOSURE_MATCH_KM) < 1e-9
        E._RUN_PULSE_SMEAR_KM = 0.2553           # 2500 ns
        assert abs(E._uni_at_splice_km() - 0.2553) < 1e-9
        E._RUN_PULSE_SMEAR_KM = 0.0204           # 200 ns — smear under 75 m
        assert abs(E._uni_at_splice_km() - E.UNI_CLOSURE_MATCH_KM) < 1e-9
        print('OK')
    """)


def test_event_inside_the_smear_seeds_no_off_splice_column():
    """The PLACHE shape: a closure at 30.0 km with per-fiber events scattered
    ~100 m.  At 2500 ns none of them may seed an off-splice column."""
    _run("""
        def fib(pos):
            return {'events': [
                {'dist_km': pos, 'splice_loss': 0.18, 'is_end': False,
                 'is_reflective': False, 'type': '0F9999LS'},
                {'dist_km': 60.0, 'splice_loss': 0.0, 'is_end': True,
                 'is_reflective': True, 'type': '1E9999LS'}]}
        fibers = {i: fib(30.0 + (i % 5 - 2) * 0.05) for i in range(1, 25)}
        splices = [{'position_km': 30.0, 'position_km_refined': 30.0}]
        E._RUN_PULSE_SMEAR_KM = 0.2553
        assert E.uni_find_off_splice_events(fibers, splices, span_km=60.0) == []
        # ...and with no pulse info the legacy 75 m radius lets them through
        E._RUN_PULSE_SMEAR_KM = 0.0
        assert len(E.uni_find_off_splice_events(fibers, splices, span_km=60.0)) > 0
        print('OK')
    """)


def test_genuinely_off_splice_event_still_reported():
    """The floor must not silence real mid-span damage: an event well beyond
    the smear from any closure still becomes an off-splice event."""
    _run("""
        def fib(pos):
            return {'events': [
                {'dist_km': pos, 'splice_loss': 0.22, 'is_end': False,
                 'is_reflective': False, 'type': '0F9999LS'},
                {'dist_km': 60.0, 'splice_loss': 0.0, 'is_end': True,
                 'is_reflective': True, 'type': '1E9999LS'}]}
        fibers = {i: fib(41.0) for i in range(1, 25)}          # 11 km away
        splices = [{'position_km': 30.0, 'position_km_refined': 30.0}]
        E._RUN_PULSE_SMEAR_KM = 0.2553
        out = E.uni_find_off_splice_events(fibers, splices, span_km=60.0)
        assert len(out) == 24, out[:3]
        print('OK')
    """)


def test_splice_fill_window_carries_the_same_floor():
    """The other half: an event 150 m from the closure is excluded from the
    off-splice list, so the splice column's fill window MUST reach it or the
    loss disappears from the report altogether."""
    _run("""
        import inspect
        src = inspect.getsource(E.uni_fill_grid) if hasattr(E, 'uni_fill_grid') else ''
        if not src:
            for name, obj in vars(E).items():
                if callable(obj) and 'window = (' in (inspect.getsource(obj)
                        if getattr(obj, '__module__', '') == E.__name__ else ''):
                    src = inspect.getsource(obj); break
        assert '_uni_at_splice_km()' in src, (
            'splice fill window is not pulse-floored; events 75-255 m from a '
            'closure would seed no column and fill no cell')
        print('OK')
    """)
