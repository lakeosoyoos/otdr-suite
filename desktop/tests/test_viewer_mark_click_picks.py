"""A click on a mark or event number on a trace picks that trace.

Robert 2026-10-01: "if we click on a trace, it takes us to the row in the
event panel and nearest column to our click. but it doesn't select it or make
it larger in the chart."  A click near an event lands on the drawer's mark (or
the file's event number) sitting on the line, not the line itself, and that
path only went to the cell.  It now picks the trace as a line click does, and
on the picked trace lets go (#503: a picked fiber's marks cover its line).
Plain JS, no runtime here: pins the source.
"""
from __future__ import annotations

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")
UP = SRC.split("window.addEventListener('mouseup', (ev) => {", 1)[1].split(
    "if (gTraceClick) {", 1)[0]


def test_a_mark_click_picks_its_trace_then_lets_go():
    assert "const pt = c.hit.pick || c.hit.t;" in UP
    assert "if (pt) gPickKey = isPicked(pt.key) ? null : pt.key;" in UP
    # picked BEFORE the jump, so the repainted row carries the mark
    assert UP.index("gPickKey = isPicked") < UP.index("c.hit.go()")
    assert UP.index("gPickKey = isPicked") < UP.index("gGridGoTo(c.hit.t, c.hit.e)")
    assert "draw();" in UP                         # bold above, the rest faint


def test_a_drawer_mark_names_its_trace_even_when_filled_in():
    # `t` stays null on a filled-in (hollow) leg for the span menu; `pick`
    # carries the trace the mark is drawn on.
    assert "go: () => D.goCell(fi, q.w, ci), pick: q.t," in SRC
