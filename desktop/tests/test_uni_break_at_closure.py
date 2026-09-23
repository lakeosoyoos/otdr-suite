"""A break at a closure is still a break, and it rides that closure's column.

uni_find_breaks excused ANY fiber whose end-of-fiber landed on a validated
closure.  A fiber can be cut, or spliced dead, inside a closure like anywhere
else; if the cable carries on past that point the fiber is broken however
neatly it coincides with a splice.  On DFW 433-864 uni reported three broken
fibers while the BIDIRECTIONAL engine reported four -- fiber 646 dies at
30.1 km, which is Splice 9, so uni said nothing about it.

And one place in the cable gets one column: a break ON a closure is carried on
that closure's column as `broke_members` and printed as "broke" in exactly
those cells, rather than twinning the header with a Break column at the same
km (HOWLAN main: Break @85.79 beside Splice @85.86).

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


def test_break_at_a_mid_cable_closure_is_still_a_break():
    """A fiber that dies at a closure with cable still ahead of it is broken.

    uni_find_breaks used to excuse ANY fiber whose EOF landed on a validated
    closure.  DFW->ILA F302/F303 die at 40.57 km on a 48.53 km span with three
    closures still ahead; once the small-job floor let 40.57 be discovered the
    blanket exemption swallowed both breaks.  The same guard was hiding real
    breaks on full cables — DFW 433-864 fiber 646 dies at 30.1 km, which the
    BIDIRECTIONAL engine reports as "646 broke@30.1k" while uni said nothing,
    because 30.1 is Splice 9."""
    _run("""
        CLOSURES = [10.0, 20.0, 30.0, 40.0]
        splices = [{'position_km_refined': c} for c in CLOSURES]
        def fiber(eof):
            return {'events': [{'dist_km': 0.0, 'type': '1F9999LS',
                                'is_end': False, 'splice_loss': 0.0},
                               {'dist_km': eof, 'type': '0E9999LS',
                                'is_end': True, 'splice_loss': 0.0}]}
        # Dies at closure 3 of 4 — cable carries on, so it is broken.
        out = E.uni_find_breaks({7: fiber(30.0)}, splices, 48.0)
        assert [b['fiber'] for b in out] == [7], out
        # Dies at the LAST closure with nothing beyond it — end of route.
        out = E.uni_find_breaks({8: fiber(40.0)}, splices, 48.0)
        assert out == [], out
        # Nowhere near a closure — broken, as always.
        out = E.uni_find_breaks({9: fiber(25.0)}, splices, 48.0)
        assert [b['fiber'] for b in out] == [9], out
        # Reaches the far end — not broken.
        assert E.uni_find_breaks({10: fiber(47.9)}, splices, 48.0) == []
        print('OK')
    """)


def test_break_at_a_closure_rides_that_closures_column():
    """One place in the cable, one column.  A break ON a closure does not get
    a column of its own: the splice column carries those fibers as
    `broke_members`, the grid prints "broke" in exactly their cells, and every
    other fiber still shows its splice loss.  HOWLAN main twinned a Break
    column at 85.79 with a Splice column at 85.86 — the same km, read twice."""
    _run("""
        valid = [{'position_km_refined': 40.57, 'position_km_display': 40.57,
                  'count': 12}]
        brk = [{'kind': 'break', 'position_km_refined': 40.57,
                'position_km_display': 40.57,
                'members': [{'fiber': 302, 'position_km': 40.57},
                            {'fiber': 303, 'position_km': 40.57}]}]
        cols = E.uni_build_columns(valid, [], brk)
        assert [c['kind'] for c in cols] == ['splice'], cols
        assert cols[0]['broke_members'] == {302, 303}, cols[0]
        # ...and a break AWAY from any closure keeps its own column.
        brk2 = [{'kind': 'break', 'position_km_refined': 22.0,
                 'position_km_display': 22.0,
                 'members': [{'fiber': 9, 'position_km': 22.0}]}]
        cols2 = E.uni_build_columns(valid, [], brk2)
        assert sorted(c['kind'] for c in cols2) == ['break', 'splice'], cols2
        print('OK')
    """)


def test_a_broke_cell_is_not_a_reburn():
    """Nobody re-burns a splice on a fiber that is cut.  A splice cell holding
    only broke entries must not count toward the reburn percentage."""
    _run("""
        cols = [{'kind': 'splice', 'position_km_refined': 40.57,
                 'position_km_display': 40.57, 'fiber_count': 12,
                 'broke_members': {302, 303}}]
        only_broke = {(0, 0): [(302, None), (303, None)]}
        s = E.uni_build_reburn_summary(only_broke, cols, 1)
        assert s['reburn_cells'] == 0, s
        mixed = {(0, 0): [(302, None), (307, 0.317)]}
        s = E.uni_build_reburn_summary(mixed, cols, 1)
        assert s['reburn_cells'] == 1, s
        print('OK')
    """)
