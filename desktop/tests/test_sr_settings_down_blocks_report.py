"""Splice Report: no Settings box, no report (Robert 2026-09-28).

The OTDR settings table is a custom component, and on some machines it
cannot draw (an App Control policy blocking a pandas or pyarrow file, a
pandas import that died partway).  The page is guarded so it survives.  The
old fallback said "running with default thresholds", dropped the
otdr_settings slot, and then built the run's overrides from None, which
_overrides_from_settings read as EVERY ROW UNTICKED: REBURN_THRESHOLD,
SINGLE_DIR_THRESHOLD, BIDIR_CONNECTOR_LOSS and the reflectance gates all went
out at the 1e9 off-sentinel.  The report flagged nothing and looked clean.

Robert's call: block the report, and block it if ANY part of the Settings
box fails (the threshold table or the Connector & Launch knobs under it).
Pinned here:
  1. no table (None) means no overrides, never the off-sentinels;
  2. a box that fails to draw, whole or only its knobs, turns Generate off,
     says why on screen (outside the collapsed box), and says what to do;
  3. a click made while the box was up, arriving on the run where it
     fails, starts nothing.
"""
from __future__ import annotations

import ast
import sys
import types

import pytest

from conftest import (REPO_ROOT, FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR,
                      run_streamlit)

APP = REPO_ROOT / "app.py"
GENERATE = "Generate Splice Report"
BLOCK_TEXT = ("The settings did not load completely, so Generate is "
              "turned off")
POLICY_BLOCK = ("DLL load failed while importing indexers: "
                "An Application Control policy has blocked this file.")
HALF_LOADED = ("partially initialized module 'pandas' has no attribute "
               "'DataFrame' (most likely due to a circular import)")


def _app_namespace():
    """_overrides_from_settings and the tables it reads, WITHOUT importing
    app.py (importing boots Streamlit)."""
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    wanted = {"_OTDR_KEY_TO_ENGINE_GLOBAL", "_OTDR_KEY_TO_WARN_GLOBAL",
              "_OTDR_KEY_TO_VIEWER_WARN", "_OTDR_DISABLE_SENTINEL",
              "_OTDR_KEY_DISABLE_VALUE", "_overrides_from_settings"}
    body = [n for n in tree.body
            if (isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") in wanted for t in n.targets))
            or (isinstance(n, ast.FunctionDef) and n.name in wanted)]
    ns = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(APP), "exec"), ns)
    return ns


NS = _app_namespace()


# ── 1. no table is not "every row unticked" ─────────────────────────────
def test_a_dropped_settings_slot_sends_no_off_sentinels():
    """st.session_state.get('otdr_settings') after the guard popped it."""
    out = NS["_overrides_from_settings"](None)
    assert out == {}, out
    assert NS["_OTDR_DISABLE_SENTINEL"] not in out.values()


def test_an_unticked_row_in_a_real_table_still_switches_its_gate_off():
    """Only the missing table changed: the Apply box is still an ON/OFF
    switch for the detection."""
    table = {k: {"apply": True, "fail": 0.5, "warning": 0.5}
             for k in NS["_OTDR_KEY_TO_ENGINE_GLOBAL"]}
    table["bidir_splice_loss"] = {"apply": False, "fail": 0.160,
                                  "warning": 0.160}
    out = NS["_overrides_from_settings"](table)
    assert out["REBURN_THRESHOLD"] == NS["_OTDR_DISABLE_SENTINEL"]


# ── 2 and 3. the page ───────────────────────────────────────────────────
def _broken_component(message, exc_type, only_knobs=False):
    """Stand-in for components.otdr_settings whose call raises, the way the
    real one does when Windows will not load pandas or pyarrow.  Both parts
    of the box draw through it; `only_knobs` breaks just the Connector &
    Launch knobs (mode='knobs') and lets the table draw, returning None as
    the real component does before the tech edits anything."""
    mod = types.ModuleType("components.otdr_settings")

    def otdr_settings(*a, **k):
        if only_knobs and k.get("mode") != "knobs":
            return None
        raise exc_type(message)

    mod.otdr_settings = otdr_settings
    return mod


def _page(dest):
    """The Splice Report page on the fixture span.  Reports go to `dest`
    (a tmp dir), so a build that wrongly runs never writes to Downloads."""
    at = run_streamlit().run()
    at.session_state["view_dir_a_input"] = str(FIXTURE_SPLICE_A_DIR)
    at.session_state["view_dir_b_input"] = str(FIXTURE_SPLICE_B_DIR)
    at.session_state["sr_report_dest"] = str(dest)
    return at


def _generate(at):
    for b in at.button:
        if b.label == GENERATE:
            return b
    raise AssertionError(f"{GENERATE!r} not rendered; "
                         f"saw {[b.label for b in at.button]}")


def _run_started(at):
    state = at.session_state
    return any(k in state for k in ("sr_queue", "sr_pending_cmd", "sr_job"))


def test_healthy_table_leaves_generate_on(tmp_path):
    at = _page(tmp_path)
    at.sidebar.radio[0].set_value("Splice Report").run()
    assert not at.exception, list(at.exception)
    assert _generate(at).disabled is False
    assert not any(BLOCK_TEXT in e.value for e in at.error)


@pytest.mark.parametrize("message,exc_type,advice,only_knobs", [
    (POLICY_BLOCK, ImportError, "Windows blocked a file", False),
    (HALF_LOADED, AttributeError, "Close OTDR Suite completely", False),
    # The table draws and only the knobs under it fail: still no report.
    (POLICY_BLOCK, ImportError, "Windows blocked a file", True),
    (HALF_LOADED, AttributeError, "Close OTDR Suite completely", True),
])
def test_settings_box_that_cannot_draw_turns_generate_off(
        monkeypatch, tmp_path, message, exc_type, advice, only_knobs):
    monkeypatch.setitem(sys.modules, "components.otdr_settings",
                        _broken_component(message, exc_type, only_knobs))
    at = _page(tmp_path)
    at.sidebar.radio[0].set_value("Splice Report").run()
    assert not at.exception, list(at.exception)
    assert ("otdr_settings" in at.session_state) is only_knobs
    assert "conn_settings" not in at.session_state
    assert _generate(at).disabled is True
    # The old texts promised a run they did not deliver.
    shown = [e.value for e in at.error] + [w.value for w in at.warning]
    assert not any("running with default" in s for s in shown), shown
    assert any(BLOCK_TEXT in e.value for e in at.error), shown
    assert any(advice in c.value for c in at.caption), \
        [c.value for c in at.caption]


def test_a_click_that_lands_on_the_failed_run_starts_nothing(monkeypatch,
                                                              tmp_path):
    at = _page(tmp_path)
    at.sidebar.radio[0].set_value("Splice Report").run()
    gen = _generate(at)
    assert gen.disabled is False
    # The table breaks between drawing the button and the click's rerun.
    monkeypatch.setitem(sys.modules, "components.otdr_settings",
                        _broken_component(HALF_LOADED, AttributeError))
    gen.click().run()
    assert not at.exception, list(at.exception)
    assert not _run_started(at), "Generate ran with no settings table"
    assert not list(tmp_path.iterdir()), "a report was written"
    assert _generate(at).disabled is True
