"""The EVENTS grid's "Show only failing events" view.

Asked for 2026-09-22: "in the menu that drops down in the events in the
viewer panel we need the option to show only failing events. this would
leave every cell empty unless it was a flagged cell."  So it is NOT the
existing "flagged only", which drops whole fibers: every row and column
stays where it is and every cell the report does not flag prints blank, so
the failures are all that is left on screen.  Plain JS, no runtime here:
pins the source.
"""
from __future__ import annotations

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def test_the_toggle_is_its_own_state_not_the_row_filter():
    assert "let gFailOnly = false;" in SRC
    assert "let gFlaggedOnly = false;" in SRC          # still the row filter
    assert "const cellText = (txt) => gFailOnly ? '' : txt;" in SRC


def test_both_menus_the_dots_and_a_right_click_open_carry_it():
    item = SRC.split("function failOnlyItem() {", 1)[1].split("\n}", 1)[0]
    assert 'data-view="failonly"' in item
    assert "Show only failing events" in item
    assert "${gFailOnly ? '✓ ' : ''}" in item          # the menu shows its state
    for fn in ("function showSpanMenu(", "function showDirChooser("):
        body = SRC.split(fn, 1)[1].split("\nasync function ", 1)[0].split("\nfunction ", 1)[0]
        assert "failOnlyItem()" in body, fn
        assert "b.dataset.view === 'failonly'" in body, fn
    tog = SRC.split("function toggleFailOnly() {", 1)[1].split("\n}", 1)[0]
    assert "gFailOnly = !gFailOnly;" in tog and "renderEventTable();" in tog


def test_a_flagged_loss_cell_still_prints_and_the_rest_go_blank():
    """Single-direction grid: only the report's own verdict survives — over
    the gate (fr-hi) or a break (fr-brk).  A gainer is coloured but is not a
    failure, so it empties with everything else."""
    lc = SRC.split("const lossCell = (v, isBreak, attrs = '') => {", 1)[1].split("\n  };", 1)[0]
    assert "const keep = cls === ' class=\"fr-hi\"' || cls === ' class=\"fr-brk\"';" in lc
    assert "${(gFailOnly && !keep) ? '' : fmt(v)}" in lc
    # the <td> and its data attributes survive, so the span menu still works
    assert "`<td${cls}${attrs}>" in lc


def test_the_other_data_cells_go_through_cellText():
    grid = SRC.split("function renderFastReporterGrid(", 1)[1].split("\n// ─── FastReporter mode", 1)[0]
    for cell in (
        "<td>${cellText(fmtR(e ? e.reflection : null))}</td>",          # reflectance
        '<td class="fr-sec">${cellText(s ? fmtK(s.len) : \'—\')}</td>',  # section
        '<td class="fr-stat">${cellText(f(mn))}</td>',                  # statistics
    ):
        assert cell in grid, cell


def test_the_fr_bidirectional_grid_blanks_the_same_way():
    bidi = SRC.split("function paintFrBidiGrid(", 1)[1].split("\n// ─── Declaring the span", 1)[0]
    lc = bidi.split("const lossCell = (v, synthetic, attrs = '') => {", 1)[1].split("\n  };", 1)[0]
    # a synthesised leg is greyed, not flagged: only fr-hi keeps its number
    assert "const keep = cls.includes('fr-hi');" in lc
    assert "${(gFailOnly && !keep) ? '' : fmt(v)}" in lc
    assert "${cellText(fmtR(leg.refl))}" in bidi
    assert "<td class=\"fr-sec\">${cellText(v ? fmt(v.loss) : '---')}</td>" in bidi


def test_the_panel_hint_says_the_view_is_on():
    assert SRC.count("' · only failing events printed'") == 2
