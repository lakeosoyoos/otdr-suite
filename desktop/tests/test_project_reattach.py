"""A report cell click in a project, then "← Back" (2026-10-03).

The cell link is an href (?nav=viewer&...), so the page loads into a new
session and _project_reattach puts the last project back.  It brought back
the spans only: FQA Progress fell from "10 of 35 in hand" to "4 of 34" (the
job details 1.03, 1.04, 1.09-1.11 and 4.01 were gone), the Viewer and Splice
Report showed A/B folder boxes instead of the shoot chosen with Run In…, and
Home did nothing (the mode gate put the project straight back).
"""
from __future__ import annotations

import re
import shutil

import pytest

from conftest import FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, run_streamlit


@pytest.fixture
def home_on(monkeypatch):
    monkeypatch.setenv("OTDR_TEST_HOME", "1")


@pytest.fixture
def settings_dir(tmp_path, monkeypatch):
    d = tmp_path / "settings"
    d.mkdir()
    monkeypatch.setenv("OTDR_SETTINGS_DIR", str(d))
    return d


@pytest.fixture
def span_dir(tmp_path):
    root = tmp_path / "SITEA-SITEB"
    shutil.copytree(FIXTURE_SPLICE_A_DIR, root / "A")
    shutil.copytree(FIXTURE_SPLICE_B_DIR, root / "B")
    return root


def _button(at, label):
    for b in at.button:
        if b.label == label:
            return b
    raise AssertionError(f"{label!r} not rendered; saw {[b.label for b in at.button]}")


def _in_hand(at):
    for m in at.markdown:
        hit = re.search(r"\*\*(\d+) of (\d+) in hand\*\*", m.value)
        if hit:
            return int(hit.group(1)), int(hit.group(2))
    raise AssertionError("no 'in hand' count on the project screen")


def _new_project(span_dir, tmp_path):
    at = run_streamlit().run()
    _button(at, "📁 Start New Project").click().run()
    at.text_input(key="setup_tr_a").set_value(str(span_dir / "A")).run()
    at.text_input(key="setup_tr_b").set_value(str(span_dir / "B")).run()
    at.text_input(key="setup_parent").set_value(str(tmp_path / "Projects")).run()
    box = at.selectbox(key="setup_customer")
    box.set_value(box.options[0]).run()          # any customer
    _button(at, "Create Project").click().run()
    assert not at.exception, list(at.exception)
    assert at.session_state["app_mode"] == "project"
    # AppTest's tree still holds the setup screen's widgets after Create (a
    # further run fails on them), so the project is opened again, as Open
    # Recent Project does.
    at = run_streamlit().run()
    _button(at, "📂 Open Recent Project").click().run()
    at.button(key="home_recent_0").click().run()
    assert not at.exception, list(at.exception)
    assert at.session_state["app_mode"] == "project"
    return at


def _run_in_splice_report(at):
    """The shoot's Run In… > Splice Report on the Traces tab."""
    import app
    sh = app.list_shoots(app.work_dir(at.session_state["project_path"]))[0]
    at.button(key=app.run_in_key(sh, "Splice Report")).click().run()
    assert not at.exception, list(at.exception)
    assert at.session_state["nav_radio"] == "Splice Report"
    assert at.session_state["project_run_shoot"] == sh["id"]
    return sh


def _cell_click(sh):
    """What the flagged cell's "This Tab (Viewer Page)" link starts."""
    at = run_streamlit()
    at.query_params["nav"] = "viewer"
    at.query_params["fiber"] = "20"
    at.query_params["km"] = "1.0"
    at.query_params["src"] = "sr"
    at.query_params["sra"] = sh["a"]
    at.query_params["srb"] = sh["b"]
    at.run()
    assert not at.exception, list(at.exception)
    return at


def test_job_details_and_the_run_in_shoot_come_back_after_a_cell_click(
        home_on, settings_dir, span_dir, tmp_path):
    at = _new_project(span_dir, tmp_path)
    before = _in_hand(at)
    job = dict(at.session_state["fqa_job"])
    sh = _run_in_splice_report(at)

    at2 = _cell_click(sh)
    assert at2.session_state["app_mode"] == "project"
    assert at2.session_state["nav_radio"] == "Viewer"
    # The job form and the shoot chosen with Run In… are back.
    assert at2.session_state["fqa_job"] == job
    assert at2.session_state["project_run_shoot"] == sh["id"]
    assert at2.session_state["project_final_shoot"] == sh["id"]

    # "← Back to Splice Report": the shoot's line, not A/B folder boxes.
    at2.button(key="view_back_sr").click().run()
    assert not at2.exception, list(at2.exception)
    assert at2.session_state["nav_radio"] == "Splice Report"
    assert any("chosen on the project's Traces tab" in c.value for c in at2.caption)

    # Back to Project: FQA Progress reads as it did before the click.
    at2.button(key="go_project").click().run()
    assert not at2.exception, list(at2.exception)
    assert at2.session_state["nav_radio"] == "Project Status"
    assert _in_hand(at2) == before


def test_home_leaves_a_project_a_cell_click_brought_back(
        home_on, settings_dir, span_dir, tmp_path):
    at = _new_project(span_dir, tmp_path)
    sh = _run_in_splice_report(at)
    at2 = _cell_click(sh)
    assert at2.session_state["app_mode"] == "project"
    at2.button(key="go_project").click().run()
    at2.button(key="go_home").click().run()
    assert not at2.exception, list(at2.exception)
    assert "app_mode" not in at2.session_state
    assert "📁 Start New Project" in [b.label for b in at2.button]
    at2.run()                                   # and it stays on Home
    assert "📁 Start New Project" in [b.label for b in at2.button]


def test_home_leaves_quick_analysis_a_cell_click_brought_back(home_on, settings_dir, span_dir):
    at = run_streamlit()
    at.query_params["nav"] = "viewer"
    at.query_params["fiber"] = "20"
    at.query_params["src"] = "sr"
    at.query_params["sra"] = str(span_dir / "A")
    at.query_params["srb"] = str(span_dir / "B")
    at.run()
    assert at.session_state["app_mode"] == "traces"
    at.button(key="go_home").click().run()
    assert not at.exception, list(at.exception)
    assert "📁 Start New Project" in [b.label for b in at.button]
