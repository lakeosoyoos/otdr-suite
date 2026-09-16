"""The bar between the chart and the FILES panel drags to resize the panel.

The boss: "slide the bar between the Files and viewer so that files panel gets
larger or smaller."  Same idiom as the event-panel resizer: pointer drag,
clamped so neither side vanishes, remembered per browser, double-click resets.
Plain JS, no runtime here: pins the source.
"""
from __future__ import annotations

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def test_the_bar_sits_between_the_chart_and_the_files_panel():
    i_main_end = SRC.index('<div id="files-resizer"')
    assert SRC.index('<canvas id="chart">') < i_main_end < SRC.index('<div id="files-panel">')
    assert "cursor: ew-resize" in SRC


def test_drag_resizes_clamped_remembered_and_double_click_resets():
    fn = SRC.split("// ─── Files-panel resizer", 1)[1].split("// ─── Event-panel resizer", 1)[0]
    assert "const KEY   = 'viewer.filesPanelWidth';" in fn
    assert "apply(startW + (startX - ev.clientX));" in fn
    assert "Math.max(MIN_PANEL, Math.min(w, Math.max(MIN_PANEL, max)))" in fn
    assert "localStorage.setItem(KEY, String(panel.offsetWidth));" in fn
    assert "bar.addEventListener('dblclick'" in fn
    assert "resizeCanvas();" in fn
