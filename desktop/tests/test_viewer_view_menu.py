"""The EVENTS menu's two View switches.

Asked for 2026-09-22: "in the menu that drops down in the events in the
viewer panel we need the option to show only failing events. this would
leave every cell empty unless it was a flagged cell", and then, on being
shown that a "flagged only" checkbox already existed in the EVENTS header
strip: "change the flagged only to Show only flagged rows. Keep the other
one Show only failing cells. Put them both in the same menu that we see by
clicking in viewer or at the headers."

So both live in the span menu (the column header's dots, and a right-click
on any event), and they are different axes that compose:

  Show only flagged rows    drops a fiber whose events all pass; the rows
                            that remain print every cell as normal.
  Show only failing cells   keeps every row and column and empties every
                            cell the report does not flag.

Plain JS, no runtime here: pins the source.
"""
from __future__ import annotations

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def test_two_states_one_per_axis():
    assert "let gFlaggedOnly = false;" in SRC       # rows
    assert "let gFailCellsOnly = false;" in SRC     # cells
    assert "const cellText = (txt) => gFailCellsOnly ? '' : txt;" in SRC


def test_the_row_filter_is_no_longer_a_checkbox_in_the_header_strip():
    """It was in the EVENTS strip since #58 and the boss never found it."""
    assert "set-flagged-only" not in SRC
    strip = SRC.split('<span id="evt-settings">', 1)[1].split("</span>\n    </div>", 1)[0]
    assert "flagged only" not in strip
    assert strip.count('type="checkbox"') == 1                  # sections, and only it
    assert "set-loss" in strip and "set-sections" in strip      # the rest stays


def test_both_menus_carry_both_items_with_their_state():
    item = SRC.split("function viewItems() {", 1)[1].split("\n}", 1)[0]
    assert "${gFlaggedOnly ? '✓ ' : ''}Show only flagged rows" in item
    assert "${gFailCellsOnly ? '✓ ' : ''}Show only failing cells" in item
    assert 'data-view="rows"' in item and 'data-view="cells"' in item
    for fn in ("function showSpanMenu(", "function showDirChooser("):
        body = SRC.split(fn, 1)[1].split("\nasync function ", 1)[0].split("\nfunction ", 1)[0]
        assert "viewItems()" in body, fn
        assert "toggleView(b.dataset.view)" in body, fn
    tog = SRC.split("function toggleView(which) {", 1)[1].split("\n}", 1)[0]
    assert "if (which === 'rows') gFlaggedOnly = !gFlaggedOnly;" in tog
    assert "else gFailCellsOnly = !gFailCellsOnly;" in tog
    assert "renderEventTable();" in tog


def test_the_row_filter_still_filters_rows_in_both_grids():
    assert SRC.count("!gFlaggedOnly || rowFails[i]") == 2


def test_a_flagged_loss_cell_still_prints_and_the_rest_go_blank():
    """Single-direction grid: only the report's own verdict survives — over
    the gate (fr-hi) or a break (fr-brk).  A gainer is coloured but is not a
    failure, so it empties with everything else."""
    lc = SRC.split("const lossCell = (v, isBreak, attrs = '') => {", 1)[1].split("\n  };", 1)[0]
    assert "const keep = cls === ' class=\"fr-hi\"' || cls === ' class=\"fr-brk\"';" in lc
    assert "${(gFailCellsOnly && !keep) ? '' : fmt(v)}" in lc
    # the <td> and its data attributes survive, so the span menu still works
    assert "`<td${cls}${attrs}>" in lc


def test_the_other_data_cells_go_through_cellText():
    grid = SRC.split("function renderFastReporterGrid(", 1)[1].split("\n// ─── FastReporter mode", 1)[0]
    for cell in (
        "<td>${cellText(fmtR(e ? e.reflection : null))}</td>",          # reflectance
        '<td class="fr-sec">${cellText(s ? fmtK(s.len) : \'-\')}</td>',  # section
        '<td class="fr-stat">${cellText(f(mn))}</td>',                  # statistics
    ):
        assert cell in grid, cell


def test_the_fr_bidirectional_grid_blanks_the_same_way():
    bidi = SRC.split("function paintFrBidiGrid(", 1)[1].split("\n// ─── Declaring the span", 1)[0]
    lc = bidi.split("const lossCell = (v, synthetic, attrs = '') => {", 1)[1].split("\n  };", 1)[0]
    # a synthesised leg is greyed, not flagged: only fr-hi keeps its number
    assert "const keep = cls.includes('fr-hi');" in lc
    assert "${(gFailCellsOnly && !keep) ? '' : fmt(v)}" in lc
    assert "${cellText(fmtR(leg.refl))}" in bidi
    assert "<td class=\"fr-sec\">${cellText(v ? fmt(v.loss) : '---')}</td>" in bidi


def test_the_panel_hint_names_whichever_view_is_on():
    assert SRC.count("' · flagged rows only'") == 2
    assert SRC.count("' · failing cells only'") == 2
