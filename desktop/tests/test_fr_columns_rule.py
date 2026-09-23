"""FastReporter mode lays events out as columns the way FastReporter does.

Read off FR's own WSC<->SUI export (fibers 1-432, 20 Event columns, 4,381
rows) replayed row by row, then pinned by driving FastReporter on chosen
fiber sets: the first fiber founds the columns at its own positions; a later
fiber's event looks only at its nearest column by merged position and joins
it under FR's event-matching rule on the A->B legs (within pulse + 20 m, or
one A leg inside the other's inner window); a fiber gives at most one event
per column, and when two of its events have the same nearest column the
NEARER one takes it and the other founds its own.  19 of the 20 export
columns match FR member for member; the last is a row fr_bidi_table does not
produce.  Fibers 16 and 17 (47.16 m by mean, A legs 50.99 m) stay apart in
FR; fibers 10 and 17 (45.89 m) merge; fiber 419 opens its own column between
two others because its A leg is 48.44 m from the nearer founder's, and joins
that founder when the other column is absent -- all driven and reproduced.

Engine tests run in a clean subprocess (three engines, three sor_reader
copies, never one process).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
BDR_DIR = REPO_ROOT / "desktop" / "tests" / "fixtures" / "bdr"


def _run(body):
    header = ("import sys, math\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import sor_reader324802a as sr\n"
              "import splicereportmatchexfo as E\n"
              f"BDR = {str(BDR_DIR)!r}\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_second_event_at_a_closure_founds_its_own_column_on_real_keys():
    # WSC<->SUI fibers 3 and 20 (FR's own .bdr).  Fiber 20 carries two events
    # at Splice 5: 26.372 km and 26.423 km, 35.7 m and 2.5 m from fiber 3's
    # 26.4205 km.  FR's export puts 26.423 in the closure column and prints
    # 26.372 as its own column (fibers 20, 327, 207) -- the nearer event
    # takes the column.  Tolerance here is 48 m, so distance alone would
    # have let 26.372 in first.
    _run("""
        fa, fb = {}, {}
        for f in (3, 20):
            fp = BDR + '/WSC_SUI_%04d_1550.bdr' % f
            fa[f] = sr.parse_bdr_side(fp, 'a'); fb[f] = sr.parse_bdr_side(fp, 'b')
        splices, results = E.fr_report_grid(fa, fb, 0.001)
        cols = [(round(s['position_km'], 4), sorted(f for (f, si) in results if si == i))
                for i, s in enumerate(splices)]
        near = [c for c in cols if 26.3 < c[0] < 26.5]
        assert near == [(26.372, [20]), (26.4205, [3, 20])], near
        d = {(f, si): round(r['bidir_dist'], 4) for (f, si), r in results.items()}
        i5 = [i for i, s in enumerate(splices) if round(s['position_km'], 4) == 26.4205][0]
        assert d[(20, i5)] == 26.423, d[(20, i5)]
        print('OK')
    """)


def test_a_leg_tolerance_windows_nearest_only_and_nearer_event_wins():
    _run("""
        tol = 48.0
        # (pos_m, fiber, reflective, payload, a_leg_pos_m, a_leg_cursor_b_m)
        items = [
            # the A legs are what FR measures, not the merged mean: an event
            # 47 m from the founder by mean with its A leg 51 m off stays out
            # and founds its own column; the next fiber's event at the same
            # spot has that new column as its nearest and joins it
            (10000.0, 1, False, 'f1', 10000.0, 10000.0),
            (10047.0, 2, False, 'f2', 10051.0, 10051.0),
            (10047.0, 3, False, 'f3', 10045.0, 10045.0),
            # beyond the tolerance an A leg inside the founder's window joins,
            # and a founder inside the event's window joins
            (20000.0, 1, False, 'g1', 20000.0, 20076.0),
            (20051.0, 2, False, 'g2', 20051.0, 20051.0),
            (30000.0, 1, False, 'h1', 30000.0, 30000.0),
            (29949.0, 2, False, 'h2', 29949.0, 30013.0),
            # ... but a window that stops short does not
            (35000.0, 1, False, 'j1', 35000.0, 35020.0),
            (35051.0, 2, False, 'j2', 35051.0, 35051.0),
            # only the NEAREST column is tried: fiber 3's event is nearer the
            # 40074 column, whose founder's A leg is 49 m from its own, so it
            # founds a column of its own even though the 40000 column's A leg
            # is 25 m away (WSC fiber 419)
            (40000.0, 1, False, 'k1', 40000.0, 40043.0),
            (40074.0, 2, False, 'k2', 40074.0, 40074.0),
            (40038.0, 3, False, 'k3', 40025.0, 40065.0),
            # two events of one fiber with the same nearest column: the
            # nearer takes it, the other founds its own
            (50000.0, 1, False, 'm1', 50000.0, 50043.0),
            (49964.0, 4, False, 'm4a', 49964.0, 50007.0),
            (50003.0, 4, False, 'm4b', 50003.0, 50046.0),
            # reflective rows never share a splice column
            (50001.0, 5, True, 'r5', 50001.0, 50044.0)]
        cols = E._fr_columns(items, tol)
        got = [(c['pos_m'], c['refl'], sorted(c['members'])) for c in cols]
        assert got == [(10000.0, False, [1]), (10047.0, False, [2, 3]),
                       (20000.0, False, [1, 2]),
                       (30000.0, False, [1, 2]),
                       (35000.0, False, [1]), (35051.0, False, [2]),
                       (40000.0, False, [1]), (40038.0, False, [3]), (40074.0, False, [2]),
                       (49964.0, False, [4]), (50000.0, False, [1, 4]),
                       (50001.0, True, [5])], got
        k = [c for c in cols if c['pos_m'] == 50000.0][0]
        assert k['members'][4] == (50003.0, 'm4b'), k['members']
        # items without leg fields lay out on their position alone
        plain = E._fr_columns([(0.0, 1, False, None), (30.0, 2, False, None), (100.0, 3, False, None)], tol)
        assert [sorted(c['members']) for c in plain] == [[1, 2], [3]]
        print('OK')
    """)


def _cols_for(fibers, near_km):
    """Columns the FR grid lays out for these WSC fibers (vendored FR keys),
    as [(km, [fibers])], restricted to +-0.2 km of near_km."""
    return f"""
        fa, fb = {{}}, {{}}
        for f in {list(fibers)!r}:
            fp = BDR + '/WSC_SUI_%04d_1550.bdr' % f
            fa[f] = sr.parse_bdr_side(fp, 'a'); fb[f] = sr.parse_bdr_side(fp, 'b')
        splices, results = E.fr_report_grid(fa, fb, -9.0)   # every row a cell
        cols = [(round(s['position_km'], 4), sorted(f for (f, si) in results if si == i))
                for i, s in enumerate(splices) if abs(s['position_km'] - {near_km!r}) < 0.2]
    """


def test_driven_on_fastreporter_the_a_legs_decide_not_the_mean():
    # FastReporter's own Files-panel Event Table on these exact keys
    # (2026-09-22): fibers 16 and 17 at Splice 11 are 47.16 m apart by merged
    # position, under the 48.08 m tolerance, and FR keeps them apart -- their
    # A->B legs are 50.99 m apart.  Fibers 10 and 17, 45.89 m apart by both,
    # merge.
    _run(_cols_for((16, 17), 59.47) + """
        assert cols == [(59.4525, [16]), (59.4996, [17])], cols
        print('OK')
    """)
    _run(_cols_for((10, 17), 59.47) + """
        assert cols == [(59.4537, [10, 17])], cols
        print('OK')
    """)


def test_driven_on_fastreporter_fiber_419_founds_its_own_column_only_beside_342():
    # Seven fibers at the 53.7 km closure, FR's aligned table: 53.7587 km
    # {1, 198}, 53.7957 km {419, 420}, 53.8326 km {342, 364, 379}.  Fiber 419
    # is 36.964 m from both the fiber-1 and the fiber-342 columns; the
    # nearer (by 1.5e-11 m, FR's float64 metres) is 342's, whose founder A
    # leg is 48.44 m from 419's, so 419 founds its own and 420 joins it.
    # Remove 342, 364 and 379 and FR puts 419 and 420 in fiber 1's column.
    _run(_cols_for((1, 198, 342, 364, 379, 419, 420), 53.79) + """
        assert cols == [(53.7587, [1, 198]), (53.7957, [419, 420]), (53.8326, [342, 364, 379])], cols
        print('OK')
    """)
    _run(_cols_for((1, 198, 419, 420), 53.79) + """
        assert cols == [(53.7587, [1, 198, 419, 420])], cols
        print('OK')
    """)
