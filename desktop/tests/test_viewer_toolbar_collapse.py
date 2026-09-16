"""The Viewer's toolbar folds away behind one small chevron at its left edge.

The boss: "a minimalistic button on the left side of the top panel that will
minimize so we don't see all the settings and toggles; drop it down or have it
hidden."  Folded, only the title, the Back button and the status line stay and
the chart takes the height; the state is remembered per browser.

Plain JS, no runtime here: pins the source.
"""
from __future__ import annotations

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def test_the_chevron_is_the_first_thing_in_the_toolbar():
    tb = SRC.split('<div id="toolbar">', 1)[1]
    assert tb.lstrip().startswith('<button id="btn-toolbar-toggle"')


def test_folding_hides_only_the_setting_groups():
    assert "#toolbar.collapsed .group { display: none; }" in SRC
    assert "#toolbar.collapsed h1" not in SRC
    assert "#toolbar.collapsed #readout" not in SRC


def test_the_state_is_remembered_and_the_chart_resizes():
    fn = SRC.split("function setToolbarCollapsed(on, persist = true)", 1)[1].split("\nfunction ", 1)[0]
    assert "localStorage.setItem(TOOLBAR_KEY, on ? '0' : '1')" in fn
    assert "resizeCanvas();" in fn
    assert "if (localStorage.getItem(TOOLBAR_KEY) === '0') setToolbarCollapsed(true, false);" in SRC
