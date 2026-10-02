"""The chart's event marks are an option in the gear, off by default (the
boss, 2026-10-01: they should not show all the time).  Off, the chart still
shows each event's line and number (Robert 2026-10-01: "unless Display
Events is selected we don't show anything in our chart except the vertical
lines and the event numbers ... as we did before").  A second switch, Show
Failed Event Labels, labels only the failed events ("Show Event Labels is
all and Failed is only failed"); the two are one or the other."""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[2] / "viewer" / "viewer.html").read_text(encoding="utf-8")


def _menu():
    i = SRC.index('<span id="evt-view-menu"')
    return SRC[i:SRC.index("</span>", SRC.index("set-pin-agg", i))]


def test_the_switch_lives_in_the_gear_not_the_toolbar():
    assert 'id="cb-events"' not in SRC
    assert re.search(r'<input id="set-event-labels" type="checkbox">\s*Show Event Labels</label>', _menu())
    assert re.search(r'<input id="set-failed-labels" type="checkbox">\s*Show Failed Event Labels</label>', _menu())


def test_off_by_default_and_remembered():
    assert "let gShowEvents = false;" in SRC
    assert "let gShowFailedLabels = false;" in SRC
    assert "gShowEvents = localStorage.getItem('otdr_viewer_event_labels') === '1';" in SRC
    assert "gShowFailedLabels = !gShowEvents && localStorage.getItem('otdr_viewer_failed_labels') === '1';" in SRC
    fn = SRC[SRC.index("function setEventLabels("):]
    fn = fn[:fn.index("\n}\n")]
    assert "localStorage.setItem('otdr_viewer_event_labels', gShowEvents ? '1' : '0')" in fn
    assert "localStorage.setItem('otdr_viewer_failed_labels', gShowFailedLabels ? '1' : '0')" in fn
    assert "getElementById('set-event-labels').checked = gShowEvents;" in SRC
    assert "getElementById('set-failed-labels').checked = gShowFailedLabels;" in SRC


def test_one_switch_or_the_other():
    fn = SRC[SRC.index("function setEventLabels("):]
    fn = fn[:fn.index("\n}\n")]
    assert "gShowFailedLabels = failed && !all;" in fn
    assert "getElementById('set-event-labels').onchange = (e) => setEventLabels(e.target.checked, false);" in SRC
    assert "getElementById('set-failed-labels').onchange = (e) => setEventLabels(false, e.target.checked);" in SRC


def test_off_the_chart_keeps_lines_and_numbers():
    draw = SRC[SRC.index("function draw() {"):]
    draw = draw[:draw.index("\n}\n")]
    # only the drawer's marks follow the switch; the lines and numbers do not
    assert "if (gShowEvents && gDrawerMarks.length) covered = drawPairing(gDrawerMarks, r);" in draw
    # Failed: the drawer's failed marks on top, nothing covered, so every
    # trace keeps its numbers
    assert "else if (gShowFailedLabels && gDrawerMarks.length) drawPairing(gDrawerMarks, r, true);" in draw
    assert "if (gShowEvents)" not in draw
    assert "drawEventMarkers(t, r);" in draw
    # a number is still a link, has its tip and its span menu, switch or not
    assert "if (gMarkerMode || gDragging) return;" in SRC
    assert "if (!gMarkerMode) {\n    const lh = labelHit(" in SRC
    assert "const overLabel = !gMarkerMode && labelHit(" in SRC
    menu = SRC[SRC.index("canvas.addEventListener('contextmenu'"):]
    assert "gShowEvents" not in menu[:menu.index("\n});")]


def test_the_gear_count_ignores_it():
    fn = SRC[SRC.index("function syncViewBtn()"):]
    fn = fn[:fn.index("\n}\n")]
    assert "gShowEvents" not in fn and "gShowFailedLabels" not in fn
