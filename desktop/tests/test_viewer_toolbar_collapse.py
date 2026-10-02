"""The Viewer's toolbar folds away: drag the bar under it up.

The boss: "minimize so we don't see all the settings and toggles" -- and then,
"how about we just drag the bar all the way up?"  So the handle is the bar
under the toolbar: drag it up to fold, down to unfold, double-click to toggle.
Folded, only the Back button and the status line stay and the chart
takes the height; the state is remembered per browser.

Plain JS, no runtime here: pins the source.
"""
from __future__ import annotations

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def test_the_handle_is_the_bar_right_under_the_toolbar():
    # The toolbar's last group (Summary Report + gear) closes the bar, and the handle
    # follows it at once.  The status line no longer sits in the bar (#34).
    after = SRC.split('Fiber Colors</label>\n    </span>\n  </div>\n</div>', 1)[1]
    assert after.lstrip().startswith('<div id="toolbar-resizer"')
    assert 'btn-toolbar-toggle' not in SRC


def test_folding_hides_only_the_setting_groups_not_the_fibers_box():
    assert "#toolbar.collapsed .group:not(.keep) { display: none; }" in SRC
    assert '<div class="group keep">\n    <label>Fibers:</label>' in SRC
    assert "#toolbar.collapsed h1" not in SRC
    assert "#toolbar.collapsed #readout" not in SRC


def test_every_visit_opens_unfolded_and_the_chart_resizes():
    fn = SRC.split("function setToolbarCollapsed(on)", 1)[1].split("\nfunction ", 1)[0]
    assert "localStorage.setItem" not in fn
    assert "resizeCanvas();" in fn
    assert "localStorage.getItem(TOOLBAR_KEY)" not in SRC
    assert "localStorage.removeItem(TOOLBAR_KEY)" in SRC


def test_drag_up_folds_drag_down_unfolds_double_click_toggles():
    blk = SRC.split("const bar = document.getElementById('toolbar-resizer');", 1)[1].split("})();", 1)[0]
    assert "if (dy < -TOOLBAR_DRAG_PX && !toolbarCollapsed()) { setToolbarCollapsed(true)" in blk
    assert "else if (dy > TOOLBAR_DRAG_PX && toolbarCollapsed()) { setToolbarCollapsed(false)" in blk
    assert "bar.addEventListener('dblclick', () => setToolbarCollapsed(!toolbarCollapsed()));" in blk


# ─── One row (Robert 2026-10-01) ──────────────────────────────────────

def test_the_bar_never_wraps_to_a_second_row():
    css = SRC.split("  #toolbar {", 1)[1].split("}", 1)[0]
    assert "flex-wrap: nowrap;" in css and "flex-wrap: wrap" not in css
    assert "#toolbar .group { flex-shrink: 0; }" in SRC


def test_what_does_not_fit_is_clipped_with_a_more_button():
    assert "overflow: hidden; }" in SRC.split("#tb-scroll {", 1)[1].split("}", 1)[0] + "}"
    assert '<button id="tb-more" class="tb-more"' in SRC
    assert '<button id="tb-more-l" class="tb-more"' in SRC
    assert "#tb-strip.more-r #tb-scroll" in SRC and "mask-image" in SRC
    blk = SRC.split("// ─── Toolbar: one row", 1)[1].split("})();", 1)[0]
    assert "more.hidden = !r;" in blk and "less.hidden = !l;" in blk
    assert "box.scrollLeft = box.scrollWidth;" in blk
    assert "addEventListener('wheel'" in blk


def test_summary_report_then_the_gear_at_the_right_end():
    right = SRC.split('<div class="group" id="tb-right">', 1)[1].split("</div>", 1)[0]
    assert right.index('id="btn-report"') < right.index('id="tb-gear"')
    menu = right.split('<span id="tb-gear-menu"', 1)[1]
    for box in ('id="cb-stack"', 'id="num-yspace"', 'id="cb-colors"'):
        assert box in menu
    assert "#tb-right { margin-left: auto; }" in SRC


def test_the_fibers_box_is_narrower():
    assert "#toolbar #fiber-input { width: 110px; }" in SRC
