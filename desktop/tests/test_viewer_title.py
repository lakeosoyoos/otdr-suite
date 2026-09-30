"""The Viewer's toolbar carries no "OTDR Viewer" heading (Robert); the tab
title still says OTDR Viewer, never "BIDI VIEWER"."""
from __future__ import annotations

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def test_no_toolbar_heading_tab_title_says_otdr_viewer():
    assert "<h1>OTDR Viewer</h1>" not in SRC
    assert "<title>OTDR Viewer</title>" in SRC
    assert "BIDI VIEWER" not in SRC


def test_hover_writes_no_numbers_to_the_readout():
    # Robert 2026-09-29/30: hovering shows the dashed line only.
    fn = SRC.split("function drawCrosshair(r) {", 1)[1].split("\n}\n", 1)[0]
    assert "setReadout" not in fn
    assert "dB`" not in fn


def test_readout_keeps_warnings_and_takes_no_room_when_empty():
    # Load notes, failures, the FR frame warning and the B-mirrored note
    # still go through setReadout; an empty line is not drawn.
    assert "#readout { display: none; }" not in SRC
    assert "#readout:empty { display: none; }" in SRC
    assert '<div id="readout"></div>' in SRC
    fn = SRC.split("function setReadout(s) {", 1)[1].split("\n}\n", 1)[0]
    assert "gFrameWarn" in fn and "gMirrorNote" in fn


def test_failures_bold_red_warnings_bold_orange_no_plain_notes():
    # Robert 2026-09-30: "failures in red text bold and warnings in orange
    # text bold", then "no plain notes".  Colours are tokens; each part gets
    # its own span; a note like "48 traces loaded" is not shown at all.
    assert "--readout-fail: #c0392b;" in SRC
    assert "--readout-warn: #c25e00;" in SRC
    assert "#readout .ro-fail { color: var(--readout-fail); font-weight: 700; }" in SRC
    assert "#readout .ro-warn { color: var(--readout-warn); font-weight: 700; }" in SRC
    assert "const READOUT_FAIL_START = /^(could not |viewer failed|drop failed|nothing written)/;" in SRC
    fn = SRC.split("function setReadout(s) {", 1)[1].split("\n}\n", 1)[0]
    assert "parts.push([s, 'ro-fail'])" in fn
    assert "parts.push([s.slice(cut + 2), 'ro-fail'])" in fn
    assert "parts.push([gFrameWarn.trim(), 'ro-warn'], [gMirrorNote.trim(), 'ro-warn']);" in fn
    # nothing else is ever pushed: no plain part
    assert fn.count("parts.push(") == 3
    # the tail it keeps is the one loadFailNote() writes
    assert "return `, could not load ${gLoadFailures" in SRC
