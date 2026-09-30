"""Changing a setting must not reload the Viewer (Robert 2026-09-29): "Keep
traces highlighted if changing setting as long as you don't leave viewer".

Streamlit places the Viewer frame by its position on the page.  With a
Splice Report behind the Viewer, changing a threshold brings up the caption
"Pass/fail in the Viewer follows the Splice Report on screen ..." above the
frame.  That moved the frame down a place, Streamlit rebuilt it, and the
Viewer reloaded empty: every trace the tech had loaded and highlighted was
gone.  The settings area now sits in one slot, so what shows inside it
never moves the frame.
"""
from __future__ import annotations

import pytest

from conftest import run_streamlit, import_trace_server

TS = import_trace_server()
CAPTION = "Pass/fail in the Viewer follows the Splice Report on screen"


@pytest.fixture(autouse=True)
def _clean_viewer():
    """trace_server is process-global: never leak a gate into another test."""
    TS.set_thresholds(None)
    TS.set_settings(None)
    yield
    TS.set_thresholds(None)
    TS.set_settings(None)


def _viewer_frame_path(at):
    """Where the Viewer frame sits on the page: the one iframe whose src is
    the trace server (the pop-out button and the watchdog are srcdoc)."""
    def walk(node, path=()):
        for i, ch in getattr(node, "children", {}).items():
            yield path + (i,), ch
            yield from walk(ch, path + (i,))
    hits = [p for p, ch in walk(at.main)
            if getattr(ch, "type", None) == "iframe"
            and "127.0.0.1" in (getattr(ch.proto, "src", "") or "")]
    assert len(hits) == 1, hits
    return hits[0]


def _captions(at):
    return [c.value for c in at.caption]


def test_the_report_caption_coming_up_does_not_move_the_viewer_frame():
    at = run_streamlit()
    at.run()
    assert not at.exception
    assert not any(CAPTION in c for c in _captions(at))
    before = _viewer_frame_path(at)

    # A Splice Report ran at a gate the settings on screen no longer match:
    # the same state a threshold change after the run leaves behind.
    TS.set_thresholds({"REBURN_THRESHOLD": 0.9}, source="sr")
    at.run()
    assert not at.exception
    assert any(CAPTION in c for c in _captions(at))
    assert _viewer_frame_path(at) == before

    # ...and going away again (the settings put back) does not move it either.
    TS.set_thresholds(None)
    at.run()
    assert not any(CAPTION in c for c in _captions(at))
    assert _viewer_frame_path(at) == before
