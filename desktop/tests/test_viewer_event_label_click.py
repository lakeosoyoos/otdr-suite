"""Event numbers on the trace are links into the event grid, and the grid
fills the panel at whatever height it is dragged to.

The boss: "hover our cursor over an event number on the trace, have it turn
to an arrow, click on the number, and have it take us there in the event
panel below.  Also, when we drag the event panel up it doesn't give us more
rows."

The second one was a CSS cap: the grid's scroller set its own max-height, so
the panel grew and the rows did not.  Plain JS, no runtime here: pins the
source the way the other viewer tests do.
"""
from __future__ import annotations

import re

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def _css(selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", SRC)
    assert m, selector
    return m.group(1)


def test_the_grid_fills_the_panel_instead_of_capping_its_own_height():
    assert "max-height" not in _css(".fr-scroll")
    assert "max-height" not in _css(".evt-scroll")
    assert "flex: 1" in _css(".fr-scroll")
    panel = _css("#event-panel")
    assert "flex-direction: column" in panel and "overflow: hidden" in panel
    wrap = _css("#event-tbody-wrap")
    assert "flex: 1" in wrap and "min-height: 0" in wrap


def test_every_drawn_event_number_records_a_hit_box():
    fn = SRC.split("function drawEventMarkers(", 1)[1].split("\nfunction ", 1)[0]
    assert "gLabelHits.push({" in fn
    assert "ctx.measureText(String(n)).width" in fn
    # cleared once per frame, before anything is drawn
    draw = SRC.split("function draw() {", 1)[1].split("\nfunction ", 1)[0]
    assert draw.index("gLabelHits = [];") < draw.index("drawGrid(r);")


def test_hovering_a_number_shows_the_arrow_and_clicking_it_goes_to_the_cell():
    move = SRC.split("canvas.addEventListener('mousemove'", 1)[1].split("window.addEventListener('mouseup'", 1)[0]
    assert "labelHit(ev.offsetX, ev.offsetY)) ? 'default' : 'crosshair'" in move
    down = SRC.split("canvas.addEventListener('mousedown'", 1)[1].split("canvas.addEventListener('mousemove'", 1)[0]
    # pressing on a number must not start a pan
    assert "gLabelClick = { hit: lh" in down
    assert down.index("gLabelClick = { hit: lh") < down.index("kind: 'pan'")
    up = SRC.split("window.addEventListener('mouseup'", 1)[1].split("canvas.addEventListener('dblclick'", 1)[0]
    assert "gGridGoTo(c.hit.t, c.hit.e)" in up


def test_the_grid_scrolls_the_row_into_the_window_paints_and_marks_the_cell():
    goto = SRC.split("gGridGoTo = (t, e) => {", 1)[1].split("\n  };", 1)[0]
    assert "shown.indexOf(ti)" in goto                       # hidden by flagged-only → no-op
    assert "paintRows();" in goto                            # the body is virtual
    assert 'td[data-km="${e.dist_km}"]' in goto              # the event's own raw km
    assert "td.classList.add('fr-hit')" in goto
    assert "scroller.scrollLeft +=" in goto                  # sideways too
    # the scroll event after scrollTop changes must not repaint over the mark
    paint = SRC.split("function paintRows() {", 1)[1].split("\n  }", 1)[0]
    assert "lastFirst = first;" in paint
    # and a re-render drops the stale hook
    assert "gGridGoTo = null;" in SRC.split("function renderEventTable() {", 1)[1].split("\n}", 1)[0]


def test_the_event_panel_can_be_dragged_all_the_way_to_the_top():
    fn = SRC.split("// ─── Event-panel resizer", 1)[1].split("// ─── ", 1)[0]
    assert "MIN_CHART = 0;" in fn
    draw = SRC.split("function draw() {", 1)[1].split("\nfunction ", 1)[0]
    assert "if (r.w <= 0 || r.h <= 0) return;" in draw


def test_right_clicking_a_number_opens_the_span_menu_for_that_event():
    fn = SRC.split("canvas.addEventListener('contextmenu'", 1)[1].split("});", 1)[0]
    assert "labelHit(ev.offsetX, ev.offsetY)" in fn
    assert "ev.preventDefault();" in fn
    assert "showSpanMenu(ev.clientX, ev.clientY, lh.t.dir, lh.e.dist_km, lh.t.fiber, lh.t.src || lh.t.dir)" in fn


def test_right_clicking_a_file_removes_it_from_the_list_and_the_viewer():
    fn = SRC.split("function showFileDirMenu(", 1)[1].split("\nasync function ", 1)[0]
    # The label carries the count: one row alone, or the whole marked set.
    assert '<button data-remove="1">Remove ${marked > 1 ? marked + \' marked files\' : \'file\'}' in fn
    assert "removeFile(k, fiber)" in fn
    rm = SRC.split("function removeFile(", 1)[1].split("\n}", 1)[0]
    assert "gRemovedFiles.add(key);" in rm and "removeTrace(key);" in rm
    # the list is the selection: a removed file has no row and cannot be loaded again
    panel = SRC.split("function renderFilesPanel() {", 1)[1].split("\n}", 1)[0]
    assert "if (gRemovedFiles.has(`${dir}-${f}`)) return;" in panel
    assert SRC.count("gRemovedFiles.has(key)") >= 3      # addFibers, loadOne, bulk overview


def test_an_event_outside_the_span_keeps_a_tick_that_can_be_right_clicked():
    fn = SRC.split("function drawEventMarkers(", 1)[1].split("\nfunction ", 1)[0]
    out = fn.split("if (n == null) {", 1)[1].split("continue;", 1)[0]
    assert "gLabelHits.push({ x0: px - 4, x1: px + 4, y0: py - 10, y1: py + 10, t, e });" in out
