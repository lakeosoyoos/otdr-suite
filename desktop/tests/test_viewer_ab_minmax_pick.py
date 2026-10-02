"""The A+B tables' Minimum / Maximum cells find the fiber that holds them.

Robert 2026-10-01: "now we can't click cells in event panel and have it take
us to that row".  The jump (#274) lived only in the one-direction table, and
#314 moved every paired load to the FR / Suite A+B tables without it.  Both
A+B tables now carry it, and the reflectance Minimum / Maximum (added the same
day) name their row too: a loss goes to the fiber's Average row, a
reflectance to the direction row it was read on.  Plain JS, no runtime here:
pins the source.
"""
from __future__ import annotations

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")
FR = SRC.split("function paintFrBidiGrid(", 1)[1].split("\nfunction ", 1)[0]
SUITE = SRC.split("function paintSuiteBidiGrid(", 1)[1].split("\nfunction ", 1)[0]
ONE = SRC.split("function renderFastReporterGrid(", 1)[1].split(
    "\n// ─── FastReporter mode", 1)[0]
JUMP = SRC.split("function wireAggJump(g, have) {", 1)[1].split("\nfunction ", 1)[0]
GO = SRC.split("function gridGoCell(g, fi, which, col, opts = {}) {", 1)[1].split("\nfunction ", 1)[0]


def _agg(grid):
    return grid.split("const aggRow = (label, fn, gated) => {", 1)[1].split("\n  };", 1)[0]


def test_both_ab_tables_name_the_owner_of_min_and_max():
    for grid in (FR, SUITE):
        agg = _agg(grid)
        # the Average (gated) row names nobody
        assert "const lossFi = (gated || v == null" in agg
        assert "aggOwnAttrs(have, lossFi, 'avg', i, false)" in agg
        assert "aggOwnAttrs(have, reflFi, reflW, i, true)" in agg
        # matched on the printed figure, so the row shows the same number
        assert "fmtR(leg.refl) !== fmtR(r)" in agg
        assert "wireAggJump({ scroller, table, tb, descs, paintRows }, have);" in grid


def test_the_click_picks_centres_and_flashes():
    assert "const td = ev.target.closest('td.fr-own');" in JUMP
    assert "if (samePick(keys)) { gPickKey = null;" in JUMP          # again lets go
    assert "gridGoCell(g, fi, which, +td.dataset.ownCol, { centre: true, refl: !!td.dataset.ownRefl });" in JUMP
    assert "draw();" in JUMP
    assert 'Show only flagged rows' in JUMP
    assert "if (opts.centre || rowTop < winTop" in GO
    assert "opts.refl && descs[k][1] === which && loss && loss.nextElementSibling" in GO


def test_the_one_direction_reflectance_min_max_names_its_trace():
    agg = _agg(ONE)
    assert 'data-own-refl="1"' in agg
    assert "td.dataset.ownRefl && loss && loss.nextElementSibling" in ONE
