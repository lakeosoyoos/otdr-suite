"""The chart's event lines and loss labels are an option in the gear, off by
default (the boss, 2026-10-01: they should not show all the time)."""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[2] / "viewer" / "viewer.html").read_text(encoding="utf-8")


def _menu():
    i = SRC.index('<span id="evt-view-menu"')
    return SRC[i:SRC.index("</span>", SRC.index("set-pin-agg", i))]


def test_the_switch_lives_in_the_gear_not_the_toolbar():
    assert 'id="cb-events"' not in SRC
    assert re.search(r'<input id="set-event-labels" type="checkbox">\s*Show Event Labels</label>', _menu())


def test_off_by_default_and_remembered():
    assert "let gShowEvents = false;" in SRC
    assert "gShowEvents = localStorage.getItem('otdr_viewer_event_labels') === '1';" in SRC
    i = SRC.index("getElementById('set-event-labels').onchange")
    handler = SRC[i:i + 300]
    assert "localStorage.setItem('otdr_viewer_event_labels', gShowEvents ? '1' : '0')" in handler
    assert "getElementById('set-event-labels').checked = gShowEvents;" in SRC


def test_every_mark_and_label_link_follows_the_switch():
    draw = SRC[SRC.index("function draw() {"):]
    draw = draw[:draw.index("\n}\n")]
    ev = draw[draw.index("if (gShowEvents) {"):]
    # the chart marks each file's own events only (no report columns)
    assert "drawEventMarkers(" in ev and "drawPairing(" not in draw
    # label tip, label click, hover and right-click all stand down with it
    assert "if (!gShowEvents || gMarkerMode || gDragging) return;" in SRC
    assert "if (!gMarkerMode && gShowEvents) {" in SRC
    assert "const overLabel = gShowEvents && !gMarkerMode && labelHit(" in SRC


def test_the_gear_count_ignores_it():
    fn = SRC[SRC.index("function syncViewBtn()"):]
    fn = fn[:fn.index("\n}\n")]
    assert "gShowEvents" not in fn
