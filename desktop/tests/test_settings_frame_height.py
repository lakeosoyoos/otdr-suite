"""Regression: the OTDR settings panel rendered as a 4-px sliver (2026-07-29).

Boss's report was "there is no settings tab in Splice Report" — he saw the
Customer profile dropdown, a thin grey bar, then "Active overrides", with no
threshold table between them.  The table was there; the iframe holding it was
4 px tall.

Root cause: the panel reported its height exactly once, synchronously, from
inside the `streamlit:render` handler:

    renderRows();
    setFrameHeight(document.body.scrollHeight + 4);   // <- reads 0 pre-layout

`document.body.scrollHeight` is read before the browser has laid the freshly
built table out, so on a cold load it comes back 0 and Streamlit pins the
iframe at 4 px.  Nothing ever re-measured, so the panel stayed invisible for
the whole session.  It is a race, which is why it hit the boss and not every
machine.  Confirmed live: iframe 4 px / content 681 px, and re-posting the
height from inside the iframe took it 4 -> 685 px.

Fix: measure again after layout (rAF + delayed passes) and keep watching with
a ResizeObserver, guarded so a pre-layout 0 is never reported and identical
heights aren't re-posted.

Browser/bridge code — no Streamlit runtime in pytest, so lock it at the source.
"""
from conftest import REPO_ROOT

PANEL = REPO_ROOT / "components" / "otdr_settings" / "index.html"


def _html():
    return PANEL.read_text(encoding="utf-8")


def test_height_is_remeasured_after_layout():
    """A single synchronous measurement is what broke; several passes fix it."""
    s = _html()
    assert "function reportHeight()" in s
    assert "function scheduleHeightReports()" in s
    assert "requestAnimationFrame(reportHeight)" in s, \
        "no post-layout re-measure — a pre-layout 0 would stick"
    # At least two delayed passes for late font/CSS metrics.
    assert s.count("setTimeout(reportHeight,") >= 2


def test_render_handler_schedules_remeasure():
    """The render message is when new rows appear, so it must re-measure."""
    s = _html()
    handler = s.split('data.type !== "streamlit:render"', 1)[1]
    assert "renderRows();" in handler
    assert "scheduleHeightReports();" in handler, \
        "render handler still reports height only once, pre-layout"


def test_resize_observer_keeps_frame_in_sync():
    s = _html()
    assert "ResizeObserver" in s
    assert "observe(document.documentElement)" in s


def test_zero_height_is_never_reported():
    """The 4-px sliver is literally setFrameHeight(0 + 4)."""
    s = _html()
    body = s.split("function reportHeight()", 1)[1].split("\n}", 1)[0]
    assert "if (h <= 0) return;" in body, "a pre-layout 0 can still be reported"


def test_identical_heights_are_not_reposted():
    """Several passes must not mean several Streamlit state updates."""
    s = _html()
    assert "lastReportedHeight" in s
    # ...but a frame that doesn't match what we asked for must self-heal.
    assert "window.innerHeight" in s
