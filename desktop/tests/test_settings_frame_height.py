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


def test_resize_observer_watches_body_so_the_panel_can_shrink():
    """<html> stretches to fill the frame, so observing it means grow-only.

    Verified live: a 140-px probe took the frame 685 -> 825 px, but removing
    it left 144 px of dead space because documentElement stayed viewport-sized
    and never reported a shrink.  <body> is margin/padding-free here, so its
    box is the content and it shrinks with it.
    """
    s = _html()
    assert "ResizeObserver" in s
    assert "observe(document.body)" in s
    assert "observe(document.documentElement)" not in s, \
        "observing <html> is grow-only — the frame can never shrink back"


def test_frame_resize_retriggers_measurement():
    """A frame resize rewraps content and is the self-heal branch's trigger."""
    s = _html()
    assert "addEventListener('resize', reportHeight)" in s


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


# ── Second instance on one page (task #110) ──────────────────────────────
# Splice Report now renders the component TWICE: the EXFO threshold table and
# the Connector & launch knobs.  The second instance came up in a 0-px iframe
# with its rows laid out correctly inside it — invisible, the same class of
# bug as the 4-px sliver above, reached a different way.
#
# Why the existing defences all missed it:
#   * body.scrollHeight is the CONTENT height, identical whether the frame is
#     358 px or 0 px, so the ResizeObserver (which watches the border box)
#     never fired;
#   * the height posted at load arrived before Streamlit had mounted that
#     instance and was dropped;
#   * every later pass hit the "nothing changed" guard.
# Verified live: iframe 1 sat at 0 px with body 354 px, and one reportHeight()
# call took it 0 -> 358.

def test_height_self_heals_without_a_new_layout_change():
    """There must be a trigger that re-posts when the frame we were GIVEN
    disagrees with the height we asked for — the self-heal branch in
    reportHeight() had no caller of its own."""
    s = _html()
    assert "function healFrameHeight(" in s, "no self-heal loop"
    assert "requestAnimationFrame(healFrameHeight)" in s, \
        "self-heal must be driven, not defined and forgotten"


def test_self_heal_uses_raf_not_a_timer():
    """setInterval is throttled to about once a minute in a hidden tab — the
    exact state a background tab is in when the tech switches back to it and
    expects the panel to be there.  rAF pauses while hidden and resumes on the
    first visible frame."""
    s = _html()
    assert "setInterval(reportHeight" not in s, \
        "a throttled timer cannot heal a hidden tab on return"
    # …and it must be throttled, or it forces a layout read every frame.
    assert "lastHealCheck" in s, "unthrottled rAF would read layout 60x/s"


def test_the_two_instances_have_distinct_component_keys():
    """Same component, one page: distinct keys or Streamlit reuses a single
    instance and one panel's edits land in the other."""
    src = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    assert "key='conn_settings_component'" in src
    assert 'key=f"otdr_component::{st.session_state.otdr_profile}"' in src
