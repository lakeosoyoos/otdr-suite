"""The Viewer's toolbar folds away: drag the bar under it up.

The boss: "minimize so we don't see all the settings and toggles" -- and then,
"how about we just drag the bar all the way up?"  So the handle is the bar
under the toolbar: drag it up to fold, down to unfold, double-click to toggle.
Folded, only the title, the Back button and the status line stay and the chart
takes the height; the state is remembered per browser.

Plain JS, no runtime here: pins the source.
"""
from __future__ import annotations

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def test_the_handle_is_the_bar_right_under_the_toolbar():
    after = SRC.split('<div id="readout">ready</div>\n</div>', 1)[1]
    assert after.lstrip().startswith('<div id="toolbar-resizer"')
    assert 'btn-toolbar-toggle' not in SRC


def test_folding_hides_only_the_setting_groups():
    assert "#toolbar.collapsed .group { display: none; }" in SRC
    assert "#toolbar.collapsed h1" not in SRC
    assert "#toolbar.collapsed #readout" not in SRC


def test_the_state_is_remembered_and_the_chart_resizes():
    fn = SRC.split("function setToolbarCollapsed(on, persist = true)", 1)[1].split("\nfunction ", 1)[0]
    assert "localStorage.setItem(TOOLBAR_KEY, on ? '0' : '1')" in fn
    assert "resizeCanvas();" in fn
    assert "if (localStorage.getItem(TOOLBAR_KEY) === '0') setToolbarCollapsed(true, false);" in SRC


def test_drag_up_folds_drag_down_unfolds_double_click_toggles():
    blk = SRC.split("const bar = document.getElementById('toolbar-resizer');", 1)[1].split("})();", 1)[0]
    assert "if (dy < -TOOLBAR_DRAG_PX && !toolbarCollapsed()) { setToolbarCollapsed(true)" in blk
    assert "else if (dy > TOOLBAR_DRAG_PX && toolbarCollapsed()) { setToolbarCollapsed(false)" in blk
    assert "bar.addEventListener('dblclick', () => setToolbarCollapsed(!toolbarCollapsed()));" in blk
