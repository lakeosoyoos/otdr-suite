"""The Viewer's header says "OTDR Viewer" (the boss), not "BIDI VIEWER"."""
from __future__ import annotations

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def test_the_header_and_tab_title_say_otdr_viewer():
    assert "<h1>OTDR Viewer</h1>" in SRC
    assert "<title>OTDR Viewer</title>" in SRC
    assert "BIDI VIEWER" not in SRC
