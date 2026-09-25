"""Clicking a Minimum / Maximum cell finds the fiber that holds it.

Asked for 2026-09-22: "when we click on the min or max cell at the bottom of
an event column we need to highlight that row in Viewer Event Panel, we need
to highlight that trace in Viewer above, and we need to move so that row is
centered in the event panel and easy to see."

So one click answers in three places: the grid row is marked and centred, the
cell it came from is flashed, and the trace above is drawn bold with the rest
dimmed back.  The Average is nobody's measurement, so it stays inert.  Plain
JS, no runtime here: pins the source.
"""
from __future__ import annotations

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")
GRID = SRC.split("function renderFastReporterGrid(", 1)[1].split(
    "\n// ─── FastReporter mode", 1)[0]
CLICK = GRID.split("const footer = table.tFoot;", 1)[1].split("\n  });", 1)[0]


def test_only_min_and_max_cells_name_a_trace():
    agg = GRID.split("const aggRow = (label, fn, gated) => {", 1)[1].split("\n  };", 1)[0]
    # `gated` is the Average row, and it gets no owner
    assert "const owner = (!gated && v != null)" in agg
    assert "c.ev.findIndex(e => { const L = evLoss(e); return L != null && fmt(L) === fmt(v); })" in agg
    assert 'class="fr-own" data-ti="${owner}" data-own-col="${i}"' in agg
    assert "click to find it" in agg


def test_the_click_marks_centres_and_flashes():
    assert "const ti = +td.dataset.ti, ci = +td.dataset.ownCol;" in CLICK
    # centred in the window the pinned header, stats strip and footer leave
    assert "const topPin = headH + namesH;" in CLICK
    assert "scroller.scrollTop = Math.max(0, rowTop - topPin - (view - FR_ROW_H) / 2);" in CLICK
    assert "paintRows(k);" in CLICK                     # the body is virtual
    assert "cell.classList.add('fr-hit');" in CLICK     # the source cell flashes
    assert "draw();" in CLICK                           # ... and the chart follows


def test_the_virtual_painter_can_be_made_to_hold_a_named_row():
    """A panel dragged down to a couple of rows made the slice arithmetic stop
    one short of the row just scrolled to, and the lookup found nothing."""
    paint = GRID.split("function paintRows(mustK) {", 1)[1].split("\n  }", 1)[0]
    assert "first = Math.min(first, Math.max(0, mustK - 2));" in paint
    assert "last = Math.max(last, Math.min(shown.length, mustK + 3));" in paint


def test_a_row_hidden_by_the_row_filter_says_so_instead():
    assert "const k = shown.indexOf(ti);" in CLICK
    assert 'Its row is hidden by' in CLICK and 'Show only flagged rows' in CLICK


def test_clicking_the_same_cell_again_lets_go():
    assert "if (gPickKey === t.key) { gPickKey = null; renderEventTable(); draw(); return; }" in CLICK
    assert "gPickKey = null;                   // nothing left to point at" in SRC   # clearAll


def test_the_picked_row_survives_a_repaint():
    assert "const pick = (t.key === gPickKey) ? ' class=\"fr-pick\"' : '';" in GRID
    assert "table.fr-table tr.fr-pick td { background:" in SRC


def test_the_chart_draws_the_picked_trace_bold_and_dims_the_rest():
    draw = SRC.split("function draw() {", 1)[1].split("\nfunction ", 1)[0]
    # picked LAST, so it lies over the others
    assert "sort((a, b) => (a.key === gPickKey) - (b.key === gPickKey))" in draw
    assert "ctx.globalAlpha = (gPickKey && t.key !== gPickKey) ? 0.22 : 1;" in draw
    tr = SRC.split("function drawTrace(t, r) {", 1)[1].split("\nfunction ", 1)[0]
    assert "const lw = (gPickKey && t.key === gPickKey) ? 2.6 : 1.2;" in tr
    assert "ctx.lineWidth = lw + 1.8;" in tr            # the light-colour edge scales too
    # the markers set their own alpha, so they fold the dimming in themselves
    mk = SRC.split("function drawEventMarkers(t, r) {", 1)[1].split("\nfunction ", 1)[0]
    assert "const dimA = (gPickKey && t.key !== gPickKey) ? 0.22 : 1;" in mk
    assert "ctx.globalAlpha = (n == null ? 0.35 : 0.55) * dimA;" in mk
    assert "ctx.lineWidth = 1.2;" in mk                 # stated, not inherited from a bold trace
