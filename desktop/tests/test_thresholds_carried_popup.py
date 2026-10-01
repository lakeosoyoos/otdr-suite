"""Thresholds Carried Over from Previous Tool (Robert 2026-09-29).

  "when we change tools I want a pop up that says Thresholds Carried Over
   from Previous Tool and they have to click Edit Settings or OK. Ok should
   be dark color and can be clicked with return or enter. Edit Settings
   should open the OTDR Settings drop down and move to that place in the
   screen"
  "if we click a flagged report cell and it takes us to viewer we need
   viewer to carry over with those same settings and thresholds"

    pop-up       landing on the Viewer, Splice Report or Unidirectional from
                 another tool, by the Select Tool list or a "← Back" button;
                 never on Secret Sauce, never on the first page, never on a
                 report-cell or pair click (Robert: no pop-up on cell jumps)
    answer       OK or Edit Settings only: no ✕, Esc or click outside
    OK           dark, takes Return / Enter
    Edit         opens the Settings box and scrolls the page to it
    carry-over   a cell or pair click starts a new session; it opens on the
                 profile and thresholds of the session it came from, not on
                 the Default profile
    run hold     a report running on the page the tech lands on holds the
                 pop-up until the run finishes or is cancelled (Robert,
                 "do A"); a queue of spans holds it until the last span
"""
from __future__ import annotations

import ast
import copy
import sys

import pytest

from conftest import (REPO_ROOT, run_streamlit, import_trace_server,
                      finish_engine_run, FIXTURE_SPLICE_A_DIR,
                      FIXTURE_SPLICE_B_DIR)

TS = import_trace_server()
SRC = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
VIEWER_HTML = (REPO_ROOT / "viewer" / "viewer.html").read_text(encoding="utf-8")
TITLE = "Thresholds Carried Over from Previous Tool"
DEFAULT = "Default (engine baseline)"


def _func_src(name):
    for node in ast.walk(ast.parse(SRC)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(SRC, node)
    raise AssertionError(f"{name} not found")


def _profile():
    """A profile other than Default, found by what it sets rather than by
    name: the repo is public, so no customer names here."""
    import app as hub
    for name in hub.CUSTOMER_PROFILES:
        if "Custom" not in name and name != DEFAULT and (
                hub._otdr_settings_from_profile(name)
                != hub._otdr_settings_from_profile(DEFAULT)):
            return name
    raise AssertionError("no profile differs from Default")


@pytest.fixture(autouse=True)
def _clean_viewer():
    """trace_server is process-global: never leak a gate into another test."""
    TS.set_thresholds(None)
    TS.set_settings(None)
    yield
    TS.set_thresholds(None)
    TS.set_settings(None)


def _hub():
    at = run_streamlit(default_timeout=180).run()
    assert not at.exception, at.exception
    return at


def _open(at, page):
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, at.exception
    return at


def _popup(at):
    return [d for d in at.get("dialog") if d.proto.dialog.title == TITLE]


def _press(at, key):
    next(b for b in at.button if b.key == key).click().run()
    assert not at.exception, at.exception
    return at


# ── when it shows ────────────────────────────────────────────────────────

def test_no_popup_when_the_suite_opens():
    assert not _popup(_hub())


@pytest.mark.parametrize("start,to", [
    ("Viewer", "Splice Report"), ("Viewer", "Unidirectional"),
    ("Splice Report", "Viewer"), ("Unidirectional", "Splice Report"),
])
def test_a_tool_change_onto_a_settings_tool_asks(start, to):
    at = _hub()
    if start != "Viewer":
        _press(_open(at, start), "carry_ok")
    _open(at, to)
    (d,) = _popup(at)
    said = " ".join(m.value for m in d.markdown)
    assert f"**{to}** is using the same thresholds" in said
    assert f"as **{start}**" in said
    assert "Customer Profile:" in said
    assert [b.label for b in d.button] == ["Edit Settings", "OK"]


def _profile_named_with(ch):
    """A profile whose name holds `ch`, or None (no names written here)."""
    import app as hub
    return next((n for n in hub.CUSTOMER_PROFILES
                 if ch in n and "Custom" not in n), None)


@pytest.mark.parametrize("which", ["default", "customer", "ampersand"])
def test_the_pop_up_names_the_selected_profile(which):
    """Robert: the pop-up says which customer profile is selected, on the line under
    the sentence ("i like where it was before"), in a green box ("to catch
    our eye"; option C of the mock-ups)."""
    import html
    picked = {"default": DEFAULT, "customer": _profile(),
              "ampersand": _profile_named_with("&")}[which]
    if picked is None:
        pytest.skip("no profile name holds an '&'")
    at = _hub()
    at.selectbox(key="otdr_profile_select").set_value(picked).run()
    assert not at.exception, at.exception
    _open(at, "Splice Report")
    (d,) = _popup(at)
    lines = [m.value for m in d.markdown]
    assert "is using the same thresholds" in lines[0]
    box = lines[1]
    assert box.startswith('<div style="background:#e7f5ea;border:1px solid #9fd3aa;')
    assert f"Customer Profile: <span" in box
    assert box.endswith(f">{html.escape(picked)}</span></div>")


def test_secret_sauce_gets_no_popup_and_the_way_back_asks():
    at = _open(_hub(), "Secret Sauce")
    assert not _popup(at)
    _open(at, "Splice Report")
    (d,) = _popup(at)
    # Secret Sauce has no thresholds: they came from the Viewer before it.
    assert "as **Viewer**" in " ".join(m.value for m in d.markdown)


def test_the_back_button_from_the_viewer_asks():
    at = _hub()
    at.session_state["came_from_splicereport"] = True
    at.run()
    next(b for b in at.button if b.label == "← Back to Splice Report").click().run()
    assert not at.exception, at.exception
    assert at.session_state["nav_radio"] == "Splice Report"
    assert len(_popup(at)) == 1


def test_a_cell_click_into_the_viewer_gets_no_popup():
    at = run_streamlit(default_timeout=180)
    at.query_params["nav"] = "viewer"
    at.query_params["fiber"] = "1"
    at.query_params["km"] = "1.0"
    at.query_params["src"] = "sr"
    at.run()
    assert not at.exception, at.exception
    assert at.session_state["nav_radio"] == "Viewer"
    assert not _popup(at)


# ── the answer ───────────────────────────────────────────────────────────

def test_it_stays_until_answered():
    at = _open(_hub(), "Splice Report")
    at.run()
    at.run()
    assert len(_popup(at)) == 1
    # no ✕, no Esc, no click outside
    assert _popup(at)[0].proto.dialog.dismissible is False


def test_ok_closes_it_for_good():
    at = _press(_open(_hub(), "Splice Report"), "carry_ok")
    assert not _popup(at)
    at.run()
    assert not _popup(at)


def test_edit_settings_closes_it_and_opens_the_box_once():
    at = _press(_open(_hub(), "Splice Report"), "carry_edit")
    assert not _popup(at)
    opener = [f for f in at.get("iframe") if ".st-key-otdr_settings_box" in f.proto.srcdoc]
    assert len(opener) == 1
    js = opener[0].proto.srcdoc
    assert "summary" in js and "scrollIntoView" in js
    at.run()
    assert not [f for f in at.get("iframe") if ".st-key-otdr_settings_box" in f.proto.srcdoc]


def test_another_popup_on_the_same_run_goes_first():
    """Streamlit opens one pop-up per run: Clear Traces waits for nobody,
    and this one comes back on the run after."""
    at = _open(_hub(), "Splice Report")
    next(b for b in at.sidebar.button if b.label == "Clear Traces").click().run()
    assert not at.exception, at.exception
    assert [d.proto.dialog.title for d in at.get("dialog")] == ["Clear Traces"]
    next(b for b in at.button if b.key == "clear_traces_cancel").click().run()
    assert len(_popup(at)) == 1


def test_ok_is_dark_and_takes_return():
    import app as hub
    assert hub.CARRY_OK_KEY == "carry_ok"
    css = hub._CARRY_OK_CSS
    # The button takes its colour from the Light / Dark palette; in Light
    # (the default) that is still the dark navy it always was.
    assert ".st-key-carry_ok button{background-color:var(--otdr-accent-2)" in css
    assert hub.THEME_VARS["light"]["accent-2"] == "#16324f"
    js = hub._CARRY_ENTER_JS
    assert ".st-key-carry_ok button" in js
    assert "ev.key !== 'Enter'" in js and "ok.click()" in js
    assert "ok.focus(" in js
    # A listener lives only as long as the pop-up's frame: each pop-up puts
    # in its own and takes out the one before (a one-time install left the
    # second pop-up of a page load with a dead listener).
    assert "removeEventListener('keydown', w.__otdrCarryKeys, true)" in js
    assert "d.addEventListener('keydown', w.__otdrCarryKeys, true)" in js
    assert "__otdrCarryEnter" not in js
    # the dialog draws the script and the style, with OK as the primary button
    body = _func_src("_thresholds_carried_dialog")
    assert "st_components_html(_CARRY_ENTER_JS, height=0)" in body
    assert "_CARRY_OK_CSS" in body and "type='primary'" in body


def test_the_settings_box_is_the_one_edit_settings_finds():
    body = _func_src("_render_settings_box")
    assert "st.container(key=SETTINGS_BOX_KEY)" in body
    import app as hub
    assert f".st-key-{hub.SETTINGS_BOX_KEY}" in hub._OPEN_SETTINGS_JS


# ── the settings ride a cell click ───────────────────────────────────────

def _tuned_session():
    """A session on the Splice Report with a non-default profile and one
    threshold edited by hand, as a tech leaves it before clicking a cell."""
    picked = _profile()
    at = _press(_open(_hub(), "Splice Report"), "carry_ok")
    at.selectbox(key="otdr_profile_select").set_value(picked).run()
    assert not at.exception, at.exception
    tuned = copy.deepcopy(at.session_state["otdr_settings"])
    tuned["bidir_splice_loss"].update(apply=True, fail=0.123)
    at.session_state["otdr_settings"] = tuned
    at.run()
    assert not at.exception, at.exception
    return at, picked, tuned


def _cell_click(cid):
    at = run_streamlit(default_timeout=180)
    at.query_params["nav"] = "viewer"
    at.query_params["fiber"] = "1"
    at.query_params["km"] = "1.0"
    at.query_params["dir"] = "both"
    at.query_params["src"] = "sr"
    if cid is not None:
        at.query_params["cs"] = cid
    at.run()
    assert not at.exception, at.exception
    return at


def test_a_cell_click_opens_the_viewer_on_the_same_settings():
    at, picked, tuned = _tuned_session()
    cid = at.session_state["_carry_id"]
    view = _cell_click(cid)
    assert view.session_state["nav_radio"] == "Viewer"
    assert view.session_state["otdr_profile"] == picked
    assert view.selectbox(key="otdr_profile_select").value == picked
    assert view.session_state["otdr_settings"]["bidir_splice_loss"]["fail"] == 0.123
    assert view.session_state["otdr_settings"] == tuned
    # ...and the Viewer judges by them
    assert TS.CONFIG["settings"]["REBURN_THRESHOLD"] == 0.123
    # the next click from the Viewer carries the same id on
    assert view.session_state["_carry_id"] == cid


def test_back_from_that_viewer_keeps_the_settings():
    at, picked, tuned = _tuned_session()
    view = _cell_click(at.session_state["_carry_id"])
    next(b for b in view.button if b.label == "← Back to Splice Report").click().run()
    assert not view.exception, view.exception
    assert view.session_state["otdr_profile"] == picked
    assert view.session_state["otdr_settings"] == tuned
    assert len(_popup(view)) == 1


@pytest.mark.parametrize("cid", [None, "0123456789ab", "not-an-id"])
def test_a_link_without_a_known_id_opens_on_default(cid):
    _tuned_session()
    view = _cell_click(cid)
    assert view.session_state["otdr_profile"] == DEFAULT
    assert view.session_state["_carry_id"] != cid


def test_the_pop_out_back_carries_the_id():
    """The pop-out Viewer's "← Back" opens a new hub tab when the old one is
    gone: /api/list hands it the hub's carry id and the link sends it."""
    at, picked, _ = _tuned_session()
    assert TS.CONFIG["hub_carry"] == at.session_state["_carry_id"]
    assert "'hub_carry': CONFIG.get('hub_carry') or ''" in (
        REPO_ROOT / "viewer" / "trace_server.py").read_text(encoding="utf-8")
    assert "if (gInfo.hub_carry) q.set('cs', gInfo.hub_carry);" in VIEWER_HTML
    back = run_streamlit(default_timeout=180)
    back.query_params["nav"] = "sr"
    back.query_params["cs"] = TS.CONFIG["hub_carry"]
    back.run()
    assert not back.exception, back.exception
    assert back.session_state["nav_radio"] == "Splice Report"
    assert back.session_state["otdr_profile"] == picked


def test_every_link_into_the_viewer_carries_the_id():
    """Report cells, Unidirectional cells and Secret Sauce pairs all build
    their link tail with _panel_qs, which carries the id."""
    body = _func_src("_panel_qs")
    assert "&cs={st.session_state.get('_carry_id', '')}" in body


# ── a report running on the page holds the pop-up ───────────────────────
# Robert, 2026-09-29 ("do A"): coming back to a page with a report running,
# the tech sees the counter and Cancel with nothing over them; the pop-up
# opens when the run ends or is cancelled, and still has to be answered.

def _gated_cmd(tmp_path, name):
    """An engine stand-in that runs until the test creates its gate file."""
    gate = tmp_path / f"{name}.go"
    return [sys.executable, "-c",
            "import os, sys, time\n"
            "while not os.path.exists(sys.argv[1]): time.sleep(0.05)",
            str(gate)], gate


def _loaded_hub():
    """The hub with the splice fixture in the left panel, so the Splice
    Report and Unidirectional pages reach their run block."""
    at = run_streamlit(default_timeout=180)
    at.session_state["view_dir_a_input"] = str(FIXTURE_SPLICE_A_DIR)
    at.session_state["view_dir_b_input"] = str(FIXTURE_SPLICE_B_DIR)
    at.run()
    assert not at.exception, at.exception
    return at


def _start_run(at, prefix, cmd, tmp_path):
    at.session_state[f"{prefix}_pending_cmd"] = cmd
    if prefix == "uni":
        at.session_state["uni_out_xlsx"] = str(tmp_path / "uni.xlsx")
    at.run()
    assert not at.exception, at.exception
    job = at.session_state[f"{prefix}_job"]
    assert job["proc"].poll() is None
    return job


def _cancel_button(at, prefix):
    return [b for b in at.button if b.key == f"{prefix}_cancel_btn"]


def _away_and_back(at, page):
    """To the Viewer (its pop-up answered) and back to `page`."""
    _press(_open(at, "Viewer"), "carry_ok")
    return _open(at, page)


def _run_under_way(tmp_path, page, prefix):
    at = _press(_open(_loaded_hub(), page), "carry_ok")
    cmd, gate = _gated_cmd(tmp_path, "run")
    job = _start_run(at, prefix, cmd, tmp_path)
    _away_and_back(at, page)
    assert at.session_state["_carry_popup"]["to"] == page
    return at, job, gate


@pytest.mark.parametrize("page,prefix", [("Splice Report", "sr"),
                                         ("Unidirectional", "uni")])
def test_a_run_on_the_page_holds_the_popup_until_it_finishes(tmp_path, page, prefix):
    at, job, gate = _run_under_way(tmp_path, page, prefix)
    # the counter and Cancel, nothing over them
    assert not _popup(at)
    assert _cancel_button(at, prefix)
    at.run()
    assert not _popup(at)
    gate.touch()
    finish_engine_run(at, prefix, timeout=60)
    assert f"{prefix}_job" not in at.session_state
    assert len(_popup(at)) == 1
    assert _popup(at)[0].proto.dialog.dismissible is False
    _press(at, "carry_ok")
    assert not _popup(at)


def test_a_cancelled_run_opens_the_popup(tmp_path):
    at, job, gate = _run_under_way(tmp_path, "Splice Report", "sr")
    assert not _popup(at)
    _cancel_button(at, "sr")[0].click().run()
    assert not at.exception, at.exception
    assert "sr_job" not in at.session_state
    assert any("Run cancelled" in i.value for i in at.info)
    assert len(_popup(at)) == 1
    gate.touch()


def test_a_queue_of_spans_holds_it_until_the_last_span(tmp_path):
    """Span 1 ends, span 2 starts on the same click-free rerun: no pop-up in
    between; it opens when span 2 is done."""
    at = _press(_open(_loaded_hub(), "Splice Report"), "carry_ok")
    cmd1, gate1 = _gated_cmd(tmp_path, "span1")
    cmd2, gate2 = _gated_cmd(tmp_path, "span2")
    dirs = (str(FIXTURE_SPLICE_A_DIR), str(FIXTURE_SPLICE_B_DIR))
    at.session_state["sr_queue"] = [{"span": 2, "dirs": dirs, "cmd": cmd2}]
    at.session_state["sr_running"] = {"span": 1, "dirs": dirs, "cmd": cmd1}
    job1 = _start_run(at, "sr", cmd1, tmp_path)
    _away_and_back(at, "Splice Report")
    assert not _popup(at)
    gate1.touch()
    job1["proc"].wait(timeout=60)
    at.run()
    assert not at.exception, at.exception
    job2 = at.session_state["sr_job"]
    assert job2["proc"] is not job1["proc"] and job2["proc"].poll() is None
    assert not _popup(at)
    gate2.touch()
    finish_engine_run(at, "sr", timeout=60)
    assert len(_popup(at)) == 1


def test_leaving_again_during_the_run_shows_it_where_there_is_no_run(tmp_path):
    at, job, gate = _run_under_way(tmp_path, "Splice Report", "sr")
    assert not _popup(at)
    _open(at, "Viewer")                      # no run of its own
    assert len(_popup(at)) == 1
    _press(at, "carry_ok")
    _open(at, "Splice Report")               # still running: held again
    assert not _popup(at)
    gate.touch()
    finish_engine_run(at, "sr", timeout=60)
    assert len(_popup(at)) == 1


def test_a_run_nothing_collects_does_not_hold_it_for_good(tmp_path):
    """Clear Traces during a run empties the folders: the page returns before
    its run block and the job stays behind.  The pop-up waits only while
    that engine is still running."""
    at, job, gate = _run_under_way(tmp_path, "Splice Report", "sr")
    at.session_state["view_dir_a_input"] = ""
    at.session_state["view_dir_b_input"] = ""
    at.run()
    assert not _popup(at)                    # the engine is still going
    gate.touch()
    job["proc"].wait(timeout=60)
    at.run()
    assert "sr_job" in at.session_state      # nothing collected it
    assert len(_popup(at)) == 1


def test_the_hold_is_in_after_page():
    body = _func_src("_after_page")
    assert "and not _page_run_going(page)" in body
    import app as hub
    assert hub._PAGE_RUN_PREFIX == {"Splice Report": "sr", "Unidirectional": "uni"}
