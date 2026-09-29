"""Unidirectional: no settings, no report (Robert 2026-09-28: "block uni too").

The Splice Report stopped running when any part of its Settings box fails to
draw (test_sr_settings_down_blocks_report).  The Unidirectional page reads the
same box (three of its rows reach the Uni engine) plus its own Unidirectional
settings panel, and it used to warn "running with default thresholds" and run.
Now a failure of ANY of the three (the threshold table, the Connector &
Launch knobs, Uni's own panel) turns "Run unidirectional report" off, says why
right above the button, and says what to do.  A click made while the
settings were up, landing on the failed run, starts nothing.  The Viewer
runs no report and is not blocked: only that is pinned here, because what
the Viewer shows when the box is down is its own rule (Robert: no flags,
events and values only), handled on its own.
"""
from __future__ import annotations

import sys
import types

import pytest

from conftest import FIXTURE_SPLICE_A_DIR, run_streamlit

RUN = "Run unidirectional report"
BLOCK_TEXT = "The settings did not load completely, so Run is turned off"
POLICY_BLOCK = ("DLL load failed while importing indexers: "
                "An Application Control policy has blocked this file.")
HALF_LOADED = ("partially initialized module 'pandas' has no attribute "
               "'DataFrame' (most likely due to a circular import)")


def _part(k):
    """Which part of the page a component call draws."""
    if k.get("mode") != "knobs":
        return "table"
    return "uni" if k.get("key") == "uni_settings_component" else "knobs"


def _broken_component(message, exc_type, parts):
    """Stand-in for components.otdr_settings whose call raises for the
    `parts` named, the way the real one does when Windows will not load
    pandas or pyarrow, and returns None (nothing edited) for the rest."""
    mod = types.ModuleType("components.otdr_settings")

    def otdr_settings(*a, **k):
        if _part(k) in parts:
            raise exc_type(message)
        return None

    mod.otdr_settings = otdr_settings
    return mod


def _page(dest):
    """The Unidirectional page on the splice fixture's A folder.  Reports go
    to `dest` (a tmp dir), so a build that wrongly runs never writes to
    Downloads."""
    at = run_streamlit().run()
    at.session_state["uni_folder_input"] = str(FIXTURE_SPLICE_A_DIR)
    at.session_state["uni_report_dest"] = str(dest)
    at.sidebar.radio[0].set_value("Unidirectional").run()
    return at


def _run(at):
    for b in at.button:
        if b.label == RUN:
            return b
    raise AssertionError(f"{RUN!r} not rendered; saw {[b.label for b in at.button]}")


def _run_started(at):
    return any(k in at.session_state for k in ("uni_pending_cmd", "uni_job"))


NOTICE = "The settings did not load completely"


def test_healthy_settings_leave_run_on(tmp_path):
    at = _page(tmp_path)
    assert not at.exception, list(at.exception)
    assert _run(at).disabled is False
    assert not any(BLOCK_TEXT in e.value for e in at.error)


@pytest.mark.parametrize("parts", [
    {"table", "knobs", "uni"},      # the component will not load at all
    {"knobs"},                      # only the Connector & Launch knobs
    {"uni"},                        # only Uni's own settings panel
], ids=["everything", "knobs-only", "uni-panel-only"])
@pytest.mark.parametrize("message,exc_type,advice", [
    (POLICY_BLOCK, ImportError, "Windows blocked a file"),
    (HALF_LOADED, AttributeError, "Close OTDR Suite completely"),
], ids=["policy-block", "half-loaded"])
def test_settings_that_cannot_draw_turn_run_off(monkeypatch, tmp_path, parts,
                                                message, exc_type, advice):
    monkeypatch.setitem(sys.modules, "components.otdr_settings",
                        _broken_component(message, exc_type, parts))
    at = _page(tmp_path)
    assert not at.exception, list(at.exception)
    assert _run(at).disabled is True
    shown = [e.value for e in at.error] + [w.value for w in at.warning]
    assert not any("running with default" in s for s in shown), shown
    assert any(BLOCK_TEXT in e.value for e in at.error), shown
    assert any(advice in c.value for c in at.caption), \
        [c.value for c in at.caption]


def test_a_click_that_lands_on_the_failed_run_starts_nothing(monkeypatch,
                                                              tmp_path):
    at = _page(tmp_path)
    run = _run(at)
    assert run.disabled is False
    # The settings break between drawing the button and the click's rerun.
    monkeypatch.setitem(sys.modules, "components.otdr_settings",
                        _broken_component(HALF_LOADED, AttributeError,
                                          {"table", "knobs", "uni"}))
    run.click().run()
    assert not at.exception, list(at.exception)
    assert not _run_started(at), "Run started with no settings"
    assert not list(tmp_path.iterdir()), "a report was written"
    assert _run(at).disabled is True


def test_the_viewer_is_not_blocked(monkeypatch, tmp_path):
    """The block is for the two report pages.  The Viewer draws the same
    box, but it runs no report, so it never shows the run-off notice."""
    monkeypatch.setitem(sys.modules, "components.otdr_settings",
                        _broken_component(HALF_LOADED, AttributeError,
                                          {"table", "knobs", "uni"}))
    at = run_streamlit().run()
    at.sidebar.radio[0].set_value("Viewer").run()
    assert not at.exception, list(at.exception)
    assert not any(NOTICE in e.value for e in at.error), \
        [e.value for e in at.error]
