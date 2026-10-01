"""Viewer: a click on a fibre's name in an A+B or one-direction table picks it.

Found 2026-10-01.  Both A+B tables (OTDR Suite's and FastReporter's) had TWO
click listeners on their rows:

  * gridPickFibers, on the fibre-name cell only, set the pick to the fibre's
    A trace (B when A was hidden);
  * the row listener added with the FILES panel tools (#444), on any cell,
    calls pickRow(), which picks the row's trace and lets go when that trace
    is already picked.

A click on the name ran both.  The first picked the A trace, the second saw
it already picked and let go, so clicking "F103" on its A->B row did
nothing; the rest of the row worked.  On the OTDR Suite table of a
one-direction load the rows carry only A or only B ({ta: null} or
{tb: null}), and gridPickFibers threw reading the missing leg's key on every
click ("Cannot read properties of null (reading 'key')", reported as a field
error) before the row listener picked the fibre.

The row listener does everything the name listener did, so gridPickFibers is
gone: a click on a row's name now picks exactly what a click on the rest of
the row picks (a direction row its trace, an Average row both of its fibre's
traces), and a second click lets go.
"""
from __future__ import annotations

import re

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def _fn(name):
    i = SRC.index("function " + name + "(")
    return SRC[i:SRC.index("\n}\n", i) + 3]


def test_no_second_name_listener():
    assert "gridPickFibers" not in SRC


def test_one_row_listener_per_table_picks_any_cell():
    for name in ("paintSuiteBidiGrid", "paintFrBidiGrid"):
        fn = _fn(name)
        # exactly one click listener on the rows, and it is the pickRow one
        assert len(re.findall(r"tb\.addEventListener\('click'", fn)) == 1, name
        assert "const tr = ev.target.closest('tr[data-fiber]');" in fn, name
        assert "pickRow(tr.dataset.avg ? [p.ta.key, p.tb.key]" in fn, name


def test_one_direction_table_has_no_average_row():
    # the row listener reads both legs only on an Average row, which a
    # one-direction table never prints, so a missing leg is never read
    assert "const LEGS = oneDir ? [oneDir] : ['a', 'b', 'avg'];" in _fn("paintSuiteBidiGrid")


def test_pick_row_toggles():
    fn = _fn("pickRow")
    assert "keys = keys.filter(Boolean);" in fn
    assert "gPickKey = samePick(keys) ? null" in fn
