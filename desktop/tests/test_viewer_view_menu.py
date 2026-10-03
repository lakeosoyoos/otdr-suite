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
    assert "const cellText = (txt) => cellFilterOn() ? '' : txt;" in SRC
    assert "const cellFilterOn = () => gFailCellsOnly || gWarnCellsOnly;" in SRC


def test_the_row_filter_is_no_longer_a_checkbox_in_the_header_strip():
    """It was in the EVENTS strip since #58 and the boss never found it."""
    assert "set-flagged-only" not in SRC
    strip = SRC.split('<span id="evt-settings">', 1)[1].split("</span>\n    </div>", 1)[0]
    assert "flagged only" not in strip
    assert strip.count('type="checkbox"') == 0      # the switches moved under the title
    assert "set-loss" in strip                      # the rest stays
    title = SRC.split('<div id="evt-title">', 1)[1].split("</div>", 1)[0]
    assert 'id="set-failcells"' in title and 'id="set-sections-off"' in title
    assert "if (cb) cb.checked = gFailCellsOnly;" in SRC     # the menu keeps the box in step


def test_both_menus_carry_both_items_with_their_state():
    item = SRC.split("function viewItems() {", 1)[1].split("\n}", 1)[0]
    assert "${gFlaggedOnly ? '✓ ' : ''}Show Only Flagged Rows" in item
    assert "${gFailCellsOnly ? '✓ ' : ''}Show Only Failing Cells" in item
    assert 'data-view="rows"' in item and 'data-view="cells"' in item
    for fn in ("function showSpanMenu(", "function showDirChooser("):
        body = SRC.split(fn, 1)[1].split("\nasync function ", 1)[0].split("\nfunction ", 1)[0]
        assert "viewItems()" in body, fn
        assert "toggleView(b.dataset.view)" in body, fn
    tog = SRC.split("function toggleView(which) {", 1)[1].split("\n}", 1)[0]
    assert "if (which === 'rows') gFlaggedOnly = !gFlaggedOnly;" in tog
    assert "else gFailCellsOnly = !gFailCellsOnly;" in tog
    assert "renderEventTable();" in tog


def test_the_row_filter_still_filters_rows_in_every_grid():
    # the single-direction grid, FastReporter's A+B table and the OTDR Suite
    # A+B table (2026-09-28): a fibre with nothing failing leaves all three
    assert SRC.count("(!gFlaggedOnly || rowFails[i])") == 3
    for painter in ("renderFastReporterGrid", "paintFrBidiGrid", "paintSuiteBidiGrid"):
        body = SRC.split("function " + painter + "(", 1)[1].split("\n}\n", 1)[0]
        assert "(!gFlaggedOnly || rowFails[i])" in body, painter


def test_a_flagged_loss_cell_still_prints_and_the_rest_go_blank():
    """Single-direction grid: only the report's own verdict survives — over
    the gate (fr-hi) or a break (fr-brk).  A gainer is coloured but is not a
    failure, so it empties with everything else."""
    lc = SRC.split("const lossCell = (v, isBreak, attrs = '', e = null, ti = -1) => {", 1)[1].split("\n  };", 1)[0]
    assert "const keep = (gFailCellsOnly && (cls === ' class=\"fr-hi\"' || cls === ' class=\"fr-brk\"'))" in lc
    # blank AND uncoloured: a shaded empty cell reads as a missing value
    assert "if (cellFilterOn() && !keep) return `<td${attrs}></td>`;" in lc
    # the <td> and its data attributes survive, so the span menu still works
    assert "`<td${cls}${attrs}>" in lc


def test_the_other_data_cells_go_through_cellText():
    grid = SRC.split("function renderFastReporterGrid(", 1)[1].split("\n// ─── FastReporter mode", 1)[0]
    for cell in (
        "<td>${cellText(fmtR(e ? e.reflection : null))}</td>",          # reflectance
        # no section here: blank, as FastReporter prints it
        '<td class="fr-sec">${cellText(s ? fmtK(s.len) : \'\')}</td>',  # section
        '<td class="fr-stat">${cellText(none(mn) ? \'\' : f(mn))}</td>',  # statistics
    ):
        assert cell in grid, cell


def test_the_fr_bidirectional_grid_blanks_the_same_way():
    bidi = SRC.split("function paintFrBidiGrid(", 1)[1].split("\n// ─── Declaring the span", 1)[0]
    lc = bidi.split("const lossCell = (v, synthetic, attrs = '', gate = null, warn = null) => {", 1)[1].split("\n  };", 1)[0]
    # a synthesised leg is greyed, not flagged: only fr-hi keeps its number
    assert "const keep = (gFailCellsOnly && cls.includes('fr-hi'))" in lc
    # blank AND uncoloured: a shaded empty cell reads as a missing value
    assert "if (cellFilterOn() && !keep) return `<td${attrs}></td>`;" in lc
    assert "if (cellFilterOn() && !(gFailCellsOnly && bad)) return '<td></td>';" in bidi   # reflectance cell
    # no section from this row: blank; one this direction did not measure: ---
    assert "<td class=\"fr-sec\">${cellText(v ? fmt(v.loss) : no)}</td>" in bidi
    assert "const no = s ? '---' : '';" in bidi


def test_the_panel_hint_names_whichever_view_is_on():
    # once per event table: single-direction, FastReporter A+B, OTDR Suite A+B.
    # Every caption is only the filter words (Suite 2026-09-29, the other two
    # 2026-09-30), joined with ' · ', so no leading dot.
    assert "' · flagged rows only'" not in SRC
    for painter in ("paintSuiteBidiGrid", "renderFastReporterGrid", "paintFrBidiGrid"):
        body = SRC.split("function " + painter + "(", 1)[1].split("\n}\n", 1)[0]
        assert "'flagged rows only'" in body and "'failing cells only'" in body, painter
        assert "].filter(Boolean).join(' · ');" in body, painter


def test_the_view_switches_sit_behind_a_gear():
    """Robert 2026-09-29: the check boxes behind a settings icon with a drop
    down, plus Show averages only.  The gear is lit while any is on."""
    title = SRC.split('<div id="evt-title">', 1)[1].split("</div>", 1)[0]
    assert 'id="evt-view-btn"' in title and 'id="evt-view-menu"' in title
    menu = title.split('id="evt-view-menu"', 1)[1]
    for box in ('set-failcells', 'set-warncells', 'set-sections-off', 'set-avg-only'):
        assert f'id="{box}"' in menu, box
    assert 'Show Averages Only' in menu
    assert "btn.classList.toggle('on', on.length > 0);" in SRC
    # the menu is fixed and the sticky header lifted, or the table covers it
    assert "position: fixed; z-index: 50;" in SRC
    head = SRC.split('#event-panel-header {\n    padding', 1)[1].split('}', 1)[0]
    assert 'z-index: 20;' in head



def test_the_button_says_settings_not_a_gear():
    """Robert 2026-10-02: the gear was too small to see; the button reads
    Settings and opens the same menu."""
    btn = SRC.split('<button id="evt-view-btn"', 1)[1].split("</button>", 1)[0]
    assert ">Settings<span id=\"evt-view-on\"></span>" in btn
    assert "&#9881;" not in btn
    assert 'aria-haspopup="true"' in btn
    assert ("document.getElementById('evt-view-btn').addEventListener('click'" in SRC)
    assert "setViewMenu(document.getElementById('evt-view-menu').hidden);" in SRC

def test_averages_only_keeps_each_fibres_average_row():
    """Both two-direction tables (FastReporter and OTDR Suite): with no cell
    filter on, each fibre keeps its Average row alone.  With one on, the
    filter's rule decides the direction rows, so a connector's failing
    direction still shows (#370) and averages-only changes nothing."""
    # (a file listed with no partner has no Average: its one row stays)
    rule = ("    : ['a', 'b', 'avg'].filter(w => (!collapse || w === 'avg' || legKept(fi, w))\n"
            "                                 && (collapse || !gAvgOnly || w === 'avg')).map(w => [fi, w]));")
    assert SRC.count(rule) == 1
    # the Suite table's twin; a one-direction table has no Average row, so
    # averages-only leaves its one row per fibre alone
    assert ("    .filter(w => oneDir || ((!collapse || w === 'avg' || legKept(fi, w))\n"
            "              && (collapse || !gAvgOnly || w === 'avg'))).map(w => [fi, w]));") in SRC
    assert "localStorage.setItem('otdr_viewer_avg_only'" in SRC
    item = SRC.split("function viewItems() {", 1)[1].split("\n}", 1)[0]
    assert "${gAvgOnly ? '✓ ' : ''}Show Averages Only" in item
    suite = SRC.split("function paintSuiteBidiGrid(", 1)[1].split("\n}\n", 1)[0]
    assert "if (gAvgOnly && !cellFilterOn() && !oneDir)" in suite and "'averages only'" in suite


def test_the_gate_note_stays_on_screen_when_it_is_news():
    """Robert 2026-09-29: "(following OTDR Settings 0.160 dB)" is gone, on
    hover only.  A down Settings box, an override, loss grading off and a
    one-direction load on a bidirectional report still show beside the box."""
    fn = SRC[SRC.index('function syncGateUI'):][:1600]
    assert ("const unusual = flagsOff() || gGateOverride != null || gateIsOff()\n"
            "    || (gSourceReport !== 'uni' && oneDirOnly());") in fn
    assert "lbl.textContent = unusual ? `(${gateLabel()})` : '';" in fn
