"""FastReporter mode lays events out as columns the way FastReporter does.

Read off FR's own WSC<->SUI export (fibers 1-432, 20 Event columns, 4,381
rows) and replayed row by row: the first fiber founds the columns at its own
positions; a later fiber's event joins a column under FR's event-matching
rule (the bidirectional pairing rule: within pulse + 20 m, or inside the
founder's inner window, or with the founder inside the event's window); a
fiber gives at most one event per column, and when two of its events could
take the same column the NEARER one takes it and the other founds its own.
16 of the 20 export columns match FR member for member (the rest: one row
fr_bidi_table does not produce, and one event 36.964 m from two columns that
FR gave a third).  The old rule -- join the nearest column within the
tolerance, events taken in position order -- merged fiber 20's 26.372 km
event into the closure column and pushed its 26.423 km event, the one FR
keeps there, into a column of its own.

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


def test_window_joins_beyond_the_tolerance_and_nearer_event_wins():
    _run("""
        tol = 48.0
        # founder at 10,000 m with a 76 m inner window; an event 51 m past it
        # joins (inside the window), one 51 m past a 20 m window does not
        items = [(10000.0, 1, False, 'f1', 76.0),
                 (10051.0, 2, False, 'f2', 20.0),
                 (20000.0, 1, False, 'g1', 20.0),
                 (20051.0, 2, False, 'g2', 20.0),
                 # founder inside the event's own window: 51 m BEFORE, 64 m wide
                 (30000.0, 1, False, 'h1', 20.0),
                 (29949.0, 2, False, 'h2', 64.0),
                 # fiber 3 has two events near founder 40,000: the nearer takes it
                 (40000.0, 1, False, 'k1', 43.0),
                 (39964.0, 3, False, 'k3a', 43.0),
                 (40003.0, 3, False, 'k3b', 43.0),
                 # reflective rows never share a splice column
                 (40001.0, 4, True, 'r4', 43.0)]
        cols = E._fr_columns(items, tol)
        got = [(c['pos_m'], c['refl'], sorted(c['members'])) for c in cols]
        assert got == [(10000.0, False, [1, 2]),
                       (20000.0, False, [1]), (20051.0, False, [2]),
                       (30000.0, False, [1, 2]),
                       (39964.0, False, [3]), (40000.0, False, [1, 3]),
                       (40001.0, True, [4])], got
        k = [c for c in cols if c['pos_m'] == 40000.0][0]
        assert k['members'][3] == (40003.0, 'k3b'), k['members']
        # items without a window still lay out on distance alone
        plain = E._fr_columns([(0.0, 1, False, None), (30.0, 2, False, None), (100.0, 3, False, None)], tol)
        assert [sorted(c['members']) for c in plain] == [[1, 2], [3]]
        print('OK')
    """)
