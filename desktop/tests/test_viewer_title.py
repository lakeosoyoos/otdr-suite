"""The Viewer's toolbar carries no "OTDR Viewer" heading (Robert); the tab
title still says OTDR Viewer, never "BIDI VIEWER"."""
from __future__ import annotations

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def test_no_toolbar_heading_tab_title_says_otdr_viewer():
    assert "<h1>OTDR Viewer</h1>" not in SRC
    assert "<title>OTDR Viewer</title>" in SRC
    assert "BIDI VIEWER" not in SRC


def test_cursor_readout_line_is_hidden():
    assert "#readout { display: none; }" in SRC
