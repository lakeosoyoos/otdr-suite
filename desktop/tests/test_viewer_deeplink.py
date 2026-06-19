"""Regression: the embedded Viewer must show the fiber you click / load, not
always F64.

Root cause (fixed): a Splice-Report cell click (or Duplicate-Check pair) arrives
as ?nav=viewer&fiber=… ; the hub stashes it in session_state['viewer_target']
and builds the iframe URL from it.  But page_viewer() used to CONSUME the target
with .pop on the first render, so the very next Streamlit rerun rebuilt the URL
WITHOUT the fiber — the iframe reloaded back to the viewer's hardcoded default
fiber (F64), and any fibers the tech had typed in were wiped.

The target is now read PERSISTENTLY, so the iframe src stays stable across reruns
and the viewer shows the clicked/loaded fiber.  These tests drive the real hub
(AppTest) and assert the target survives both the render and a follow-up rerun.
"""
from __future__ import annotations

from conftest import run_streamlit


def test_single_fiber_deeplink_survives_render_and_rerun():
    """Click a flagged cell (?nav=viewer&fiber=100&km=24.4): the hub switches to
    the Viewer AND keeps the target through the render and a follow-up rerun, so
    the iframe stays on F100 instead of snapping back to the default F64.

    (Under the old .pop, the target was already gone after the first render —
    this test fails on that code.)"""
    at = run_streamlit()
    at.query_params["nav"] = "viewer"
    at.query_params["fiber"] = "100"
    at.query_params["km"] = "24.4"
    at.run()
    assert not at.exception, f"nav raised: {list(at.exception)}"
    assert at.session_state["nav_radio"] == "Viewer"
    assert "viewer_target" in at.session_state, (
        "viewer_target missing after the render → it was consumed, so the iframe "
        "reverts to default F64"
    )
    assert at.session_state["viewer_target"]["fiber"] == "100"

    # A plain rerun (the nav query params were cleared by _handle_nav on the
    # first run, so this is what every later widget interaction looks like) must
    # still keep the target.
    at.run()
    assert not at.exception, f"rerun raised: {list(at.exception)}"
    assert "viewer_target" in at.session_state, (
        "viewer_target dropped on a plain rerun → iframe reverts to default F64"
    )
    assert at.session_state["viewer_target"]["fiber"] == "100"


def test_pair_deeplink_survives_render_and_rerun(tmp_path):
    """A Duplicate-Check pair (?nav=viewer&fibers=1,2) target must likewise
    persist so both fibers stay overlaid across reruns."""
    ssfolder = tmp_path / "ss"
    ssfolder.mkdir()
    at = run_streamlit()
    at.query_params["nav"] = "viewer"
    at.query_params["fibers"] = "1,2"
    at.query_params["dir"] = "a"
    at.query_params["ssfolder"] = str(ssfolder)
    at.run()
    assert not at.exception
    assert "viewer_target" in at.session_state
    assert at.session_state["viewer_target"]["fibers"] == "1,2"

    at.run()
    assert not at.exception
    assert "viewer_target" in at.session_state, (
        "pair target consumed on rerun → the overlay is lost"
    )
    assert at.session_state["viewer_target"]["fibers"] == "1,2"
