"""The chart marks only the events the event table printed.

Robert, 2026-09-30, on a 432-fibre span in OTDR Mode: "we are in OTDR mode
but we seem to be showing a lot of events".  The chart drew every stored
event of every loaded file whatever the mode, while the OTDR Suite table
showed eight closures and the two ends.  Asked to build it: "closures only
in both modes".  FR Mode's table prints every FastReporter row, so there the
chart follows that table too (Robert chose to leave FR Mode as it is).

Plain JS, no runtime here: pins the source the way the other viewer tests do.
"""
from __future__ import annotations

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    return SRC.split(f"function {name}(", 1)[1].split("\nfunction ", 1)[0]


def test_an_in_span_event_the_table_did_not_print_is_not_drawn():
    draw = _fn("drawEventMarkers")
    skip = draw.index("if (n != null && !inTable(t, e)) continue;")
    # decided before the tick is stroked, and only for numbered (in-span)
    # events: the dim tick on the port / reel stays a right-click target
    assert skip < draw.index("ctx.stroke();")
    assert draw.index("const n = nums.get(e);") < skip


def test_a_trace_no_table_has_taken_still_draws_every_event():
    fn = _fn("inTable")
    assert "return !s || s.has(tableKmKey(e.dist_km));" in fn
    assert "let gTableKm = null;" in SRC


def test_the_key_matches_the_cells_data_km_rounding():
    # /api/trace rounds dist_km to 4 places and the grids print rawKm(...)
    # the same way, so a marker and its cell are one reading
    assert "function tableKmKey(km) { return Math.round(Number(km) * 1e4); }" in SRC


def test_every_grid_records_what_it_printed_and_redraws_the_chart():
    single = _fn("renderFastReporterGrid")
    assert "tableMarkReset(traces);" in single
    assert "tableMark(traces[ti], e.dist_km)" in single
    fr = _fn("paintFrBidiGrid")
    assert "tableMarkReset(have.flatMap(p => [p.ta, p.tb]));" in fr
    assert "!leg.synthetic" in fr                    # no event of its own in the file
    assert "tableMark(t, Number(leg.pos_m) / 1000)" in fr
    suite = _fn("paintSuiteBidiGrid")
    assert "tableMarkReset(have.flatMap(p => [p.ta, p.tb]));" in suite
    assert "tableMark(have[fi].ta, x.a.km)" in suite
    assert "tableMark(have[fi].tb, x.b.km)" in suite
    for body in (single, fr, suite):
        assert "draw();" in body[body.index("tableMarkReset("):]


def test_a_rebuild_keeps_the_last_marks_until_the_new_table_paints():
    # every setting change re-reads the table; a 432-pair FR read is slow
    assert "gTableKm" not in _fn("renderEventTable")


def test_no_fr_table_falls_back_to_every_event():
    fr = _fn("paintFrBidiGrid")
    assert fr.index("gTableKm = null; draw();") < fr.index("tableMarkReset(")
    ask = _fn("renderFrBidiGrid")
    assert "gTableKm = null; draw();" in ask
