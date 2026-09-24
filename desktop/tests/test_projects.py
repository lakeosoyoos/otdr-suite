"""Projects: save a span's setup to a file, open it after a restart.

Robert, 2026-09-23: "can we use OTDR Suite to create saveable projects that
will survive the app opening and closing?"  A project file holds span 1's
folders (plus any added spans), the site names, the customer profile with
its tables, the cable type, the analysis mode, the report folder and the
Viewer span markers.  Paths are stored relative to the project file AND
absolute, so a project saved beside the traces opens on another machine.
"""
from __future__ import annotations

import json
import os
import shutil

import pytest

from conftest import (REPO_ROOT, FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR,
                      run_streamlit)

SRC = (REPO_ROOT / "app.py").read_text(encoding="utf-8")


@pytest.fixture
def hub():
    import app
    return app


@pytest.fixture
def settings_dir(tmp_path, monkeypatch):
    d = tmp_path / "settings"
    d.mkdir()
    monkeypatch.setenv("OTDR_SETTINGS_DIR", str(d))
    return d


@pytest.fixture
def span_dir(tmp_path):
    """A span folder the way techs keep them: <span>/A and <span>/B."""
    root = tmp_path / "WSC-SUI"
    shutil.copytree(FIXTURE_SPLICE_A_DIR, root / "A")
    shutil.copytree(FIXTURE_SPLICE_B_DIR, root / "B")
    return root


def _button(at, label):
    for b in at.button:
        if b.label == label:
            return b
    raise AssertionError(f"{label!r} not rendered; saw {[b.label for b in at.button]}")


def _sr_page(**state):
    at = run_streamlit().run()
    for k, v in state.items():
        at.session_state[k] = v
    at.sidebar.radio[0].set_value("Splice Report").run()
    assert not at.exception, list(at.exception)
    return at


# ── the file format ──────────────────────────────────────────────────────
def test_paths_are_stored_relative_and_absolute_and_resolve_after_a_move(hub, span_dir, tmp_path):
    snap = {"analysis_mode": "fr", "profile": "Lumen", "otdr_settings": {"x": 1},
            "conn_settings": None, "cable_type": None, "report_dest": "",
            "spans": [{"mode": "two", "dir_a": str(span_dir / "A"),
                       "dir_b": str(span_dir / "B"), "folder": "",
                       "site_a": "WSC", "site_b": "SUI"}]}
    proj = span_dir / "WSC_to_SUI.otdrproj"
    data = hub.project_to_file_data(snap, str(proj))
    s1 = data["spans"][0]
    assert s1["dir_a"] == {"rel": "A", "abs": str(span_dir / "A")}
    assert s1["dir_b"]["rel"] == "B"
    hub.project_write(str(proj), data)

    # The whole span folder moves (another machine, another OneDrive path):
    # the absolute path is gone, the relative one still finds the folders.
    moved = tmp_path / "elsewhere" / "WSC-SUI"
    shutil.move(str(span_dir), str(moved))
    back, _ = hub.project_read(str(moved / "WSC_to_SUI.otdrproj"))
    assert back["spans"][0]["dir_a"] == str(moved / "A")
    assert back["spans"][0]["dir_b"] == str(moved / "B")
    assert (back["analysis_mode"], back["profile"]) == ("fr", "Lumen")
    assert back["spans"][0]["site_a"] == "WSC"


def test_a_project_saved_away_from_the_traces_falls_back_to_the_absolute_path(hub, span_dir, tmp_path):
    snap = {"spans": [{"mode": "two", "dir_a": str(span_dir / "A"),
                       "dir_b": str(span_dir / "B"), "folder": "",
                       "site_a": "", "site_b": ""}]}
    proj = tmp_path / "Downloads" / "p.otdrproj"
    data = hub.project_to_file_data(snap, str(proj))
    # A relative ref pointing at a folder that is not there must not win.
    data["spans"][0]["dir_a"]["rel"] = "nowhere/A"
    hub.project_write(str(proj), data)
    back, _ = hub.project_read(str(proj))
    assert back["spans"][0]["dir_a"] == str(span_dir / "A")


def test_not_a_project_and_newer_version_are_refused(hub, tmp_path):
    p = tmp_path / "x.otdrproj"
    p.write_text(json.dumps({"hello": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="not an OTDR Suite project"):
        hub.project_read(str(p))
    p.write_text(json.dumps({"format": hub.PROJECT_FORMAT, "version": 99}),
                 encoding="utf-8")
    with pytest.raises(ValueError, match="newer OTDR Suite"):
        hub.project_read(str(p))


def test_recent_list_is_newest_first_deduped_and_capped(hub, settings_dir, tmp_path):
    paths = []
    for i in range(hub.PROJECT_RECENT_MAX + 2):
        p = tmp_path / f"p{i}.otdrproj"
        p.write_text("{}", encoding="utf-8")
        paths.append(str(p))
        hub._remember_project(str(p))
    hub._remember_project(paths[0])            # reopening moves it to the top
    rec = hub.recent_projects()
    assert rec[0] == paths[0] and len(rec) == hub.PROJECT_RECENT_MAX
    assert len(set(rec)) == len(rec)
    data = json.loads((settings_dir / "settings.json").read_text(encoding="utf-8"))
    assert data["last_project"] == paths[0]
    # The analysis mode shares the file and survives the project writes.
    hub.save_analysis_mode("fr")
    hub._remember_project(paths[1])
    assert hub.load_analysis_mode() == "fr"


def test_the_page_and_the_project_read_the_same_keys():
    # One key map for both, so a renamed widget key cannot silently drop out
    # of saved projects.
    inputs = SRC.split("\ndef _sr_span_inputs(", 1)[1].split("\ndef ", 1)[0]
    sites = SRC.split("\ndef _sr_site_inputs(", 1)[1].split("\ndef ", 1)[0]
    assert "_sr_span_keys(span)" in inputs and "_sr_span_keys(span)" in sites
    assert "SR_MAX_SPANS_CAP = 8" in SRC and "\nSR_MAX_SPANS = 8" in SRC


# ── the app: home screen, work folder, restart ──────────────────────────
@pytest.fixture
def home_on(monkeypatch):
    monkeypatch.setenv("OTDR_HOME_SCREEN", "1")


def _labels(at):
    return [b.label for b in at.button]


def _start_project(folder):
    at = run_streamlit().run()
    at.text_input(key="home_folder").set_value(str(folder)).run()
    _button(at, "Open this folder").click().run()
    assert not at.exception, list(at.exception)
    return at


def test_home_screen_offers_run_traces_and_start_project(home_on, settings_dir):
    at = run_streamlit().run()
    assert not at.exception, list(at.exception)
    assert {"🔬 Run Traces", "📈 Start New Project from Traces",
            "📄 Start New Project from Production Sheet"} <= set(_labels(at))
    assert not [r for r in at.sidebar.radio if r.label == "Tool"]


def test_run_traces_is_the_suite_as_it_was(home_on, settings_dir):
    at = run_streamlit().run()
    _button(at, "🔬 Run Traces").click().run()
    assert not at.exception, list(at.exception)
    tool = next(r for r in at.sidebar.radio if r.label == "Tool")
    assert tool.options == ['Viewer', 'Splice Report', 'Unidirectional',
                            'Secret Sauce', 'FQA Builder', 'Field Capture']
    assert tool.value == "Viewer"
    assert "project_path" not in at.session_state
    # Home is one button at the foot of the sidebar, and it goes back.
    _button(at, "🏠 Home").click().run()
    assert "📈 Start New Project from Traces" in _labels(at)


def test_start_project_makes_the_work_folder_the_project(home_on, settings_dir, tmp_path):
    work = tmp_path / "ELMDALE-MILLER"
    work.mkdir()
    at = _start_project(work)
    proj = work / "ELMDALE-MILLER.otdrproj"
    assert proj.is_file()
    tool = next(r for r in at.sidebar.radio if r.label == "Tool")
    assert tool.options[0] == "Project status" and tool.value == "Project status"
    # No Load span box in a project: traces come in through section 4.
    assert not [e for e in at.sidebar.expander if "Load span" in e.label]
    # The tools point at the work folder.
    assert at.session_state["view_dir_a_input"] == str(work / "Traces" / "A")
    assert at.session_state["sr_report_dest"] == str(work / "Reports")
    data = json.loads(proj.read_text(encoding="utf-8"))
    assert data["spans"][0]["dir_a"]["rel"] == "Traces/A"


def test_a_project_saves_itself_and_comes_back_after_a_restart(home_on, settings_dir, span_dir):
    at = _start_project(span_dir)
    tool = next(r for r in at.sidebar.radio if r.label == "Tool")
    tool.set_value("Splice Report").run()
    at.session_state["view_dir_a_input"] = str(span_dir / "A")
    at.session_state["view_dir_b_input"] = str(span_dir / "B")
    at.run()
    at.session_state["sr_site_a"] = "WSC"
    at.run()
    data = json.loads((span_dir / "WSC-SUI.otdrproj").read_text(encoding="utf-8"))
    assert data["spans"][0]["site_a"] == "WSC"          # no Save button needed

    at2 = run_streamlit().run()                          # "restart"
    assert "📁 WSC-SUI" in _labels(at2)                  # Recent
    _button(at2, "📁 WSC-SUI").click().run()
    assert not at2.exception, list(at2.exception)
    assert at2.session_state["nav_radio"] == "Project status"
    assert at2.session_state["sr_site_a"] == "WSC"
    assert at2.session_state["view_dir_a_input"] == str(span_dir / "A")


def test_opening_is_not_a_change_and_does_not_rewrite_the_file(home_on, settings_dir, tmp_path):
    work = tmp_path / "W"
    work.mkdir()
    _start_project(work)
    proj = work / "W.otdrproj"
    before = proj.read_bytes()
    at = run_streamlit().run()
    _button(at, "📁 W").click().run()
    at.run()
    assert proj.read_bytes() == before


def test_a_missing_work_folder_is_an_error_on_home(home_on, settings_dir, tmp_path):
    at = run_streamlit().run()
    at.text_input(key="home_folder").set_value(str(tmp_path / "nope")).run()
    _button(at, "Open this folder").click().run()
    assert any("Could not open that work folder" in e.value for e in at.error)


def test_viewer_click_through_stays_in_the_project(home_on, settings_dir, span_dir):
    # The cell click into the Viewer is a URL nav that wipes session_state;
    # the project (and its site names) come back, not the home screen.
    (span_dir / "Traces" / "A").mkdir(parents=True)     # a report ran on them
    (span_dir / "Traces" / "B").mkdir()
    at = _start_project(span_dir)
    ta = str(span_dir / "Traces" / "A")
    at.session_state["sr_site_a"] = "WSC"
    at.run()
    at2 = run_streamlit()
    at2.query_params["nav"] = "sr"
    at2.query_params["sra"] = ta
    at2.query_params["srb"] = str(span_dir / "Traces" / "B")
    at2.run()
    assert not at2.exception, list(at2.exception)
    assert at2.session_state["app_mode"] == "project"
    assert at2.session_state["sr_site_a"] == "WSC"


def test_run_traces_click_through_does_not_open_a_project(home_on, settings_dir, span_dir):
    _start_project(span_dir)
    at = run_streamlit().run()
    _button(at, "🔬 Run Traces").click().run()
    at2 = run_streamlit()
    at2.query_params["nav"] = "sr"
    at2.query_params["sra"] = str(span_dir / "Traces" / "A")
    at2.run()
    assert at2.session_state["app_mode"] == "traces"


def test_viewer_span_markers_travel_in_the_file(hub, settings_dir, span_dir, monkeypatch, tmp_path):
    import trace_server
    store = tmp_path / "spans.json"
    monkeypatch.setattr(trace_server, "SPAN_STORE", str(store))
    a, b = str(span_dir / "A"), str(span_dir / "B")
    trace_server.span_decl_set("a", "start", 0.123, a, b)
    snap = {"spans": [{"mode": "two", "dir_a": a, "dir_b": b, "folder": "",
                       "site_a": "", "site_b": ""}]}
    proj = span_dir / "p.otdrproj"
    hub.project_write(str(proj), hub.project_to_file_data(
        snap, str(proj), hub._span_markers_for(snap)))
    store.unlink()                                   # another machine
    back, markers = hub.project_read(str(proj))
    hub._restore_span_markers(back, markers)
    assert trace_server.span_decl(a, b)["a"] == {"start_km": 0.123}
    # A machine that already has its own markers keeps them.
    trace_server.span_decl_set("a", "start", 0.5, a, b)
    hub._restore_span_markers(back, markers)
    assert trace_server.span_decl(a, b)["a"] == {"start_km": 0.5}


def test_the_browser_is_sent_the_projects_folders_not_blank_boxes(home_on, settings_dir, span_dir):
    """The project fills the tools' keys while Project status is on screen;
    the tool draws its boxes a run later.  Streamlit only sends a code-set
    value to the browser when it was assigned in the run that draws the box,
    so the server held Traces/A while the browser showed an empty box (found
    in the browser, 2026-09-23).  AppTest's .value reads the server, so this
    reads what goes to the browser: the widget message."""
    shutil.copytree(span_dir / "A", span_dir / "Traces" / "A")
    shutil.copytree(span_dir / "B", span_dir / "Traces" / "B")
    at = _start_project(span_dir)
    at.run()                                   # the keys now sit in old state
    tool = next(r for r in at.sidebar.radio if r.label == "Tool")
    tool.set_value("Splice Report").run()
    box = at.text_input(key="view_dir_a_input")
    assert box.proto.set_value and box.proto.value == str(span_dir / "Traces" / "A")
    dest = at.text_input(key="sr_report_dest")
    assert dest.proto.set_value and dest.proto.value == str(span_dir / "Reports")


# ── new project from traces / from a production sheet ─────────────────
def _setup(kind_label):
    at = run_streamlit().run()
    _button(at, kind_label).click().run()
    assert not at.exception, list(at.exception)
    return at


def test_new_project_from_traces_fills_in_and_lands_on_status(home_on, settings_dir, span_dir, tmp_path):
    at = _setup("📈 Start New Project from Traces")
    assert any("New project from traces" in m.value for m in at.markdown)
    at.text_input(key="setup_tr_a").set_value(str(span_dir / "A")).run()
    at.text_input(key="setup_tr_b").set_value(str(span_dir / "B")).run()
    assert any("A 24 fibers, B 24 fibers" in s.value for s in at.success)
    assert at.text_input(key="setup_name").value == "ELMDALE to MILLER"
    at.text_input(key="setup_parent").set_value(str(tmp_path / "Projects")).run()
    _button(at, "Create project").click().run()
    assert not at.exception, list(at.exception)
    work = tmp_path / "Projects" / "ELMDALE to MILLER"
    assert len(list((work / "Traces" / "A").iterdir())) == 24
    assert at.session_state["app_mode"] == "project"
    assert at.session_state["nav_radio"] == "Project status"
    assert at.session_state["sr_site_a"] == "ELMDALE"
    job = at.session_state["fqa_job"]
    assert job["fiber_count"] == 24 and job["site_a"]["alias"] == "ELMDALE"
    # Where new projects go is remembered for next time.
    data = json.loads((settings_dir / "settings.json").read_text(encoding="utf-8"))
    assert data["projects_root"] == str(tmp_path / "Projects")


def test_new_project_from_a_production_sheet_fills_the_job_form(home_on, settings_dir, tmp_path):
    from test_project_status import production_sheet
    sheet = production_sheet(tmp_path / "Span 4 Production Sheet.xlsx")
    at = _setup("📄 Start New Project from Production Sheet")
    at.text_input(key="setup_prod_path").set_value(sheet).run()
    assert any("14 locations, 12 splices" in s.value for s in at.success)
    name = at.text_input(key="setup_name").value
    assert " to " in name
    at.text_input(key="setup_parent").set_value(str(tmp_path / "P")).run()
    _button(at, "Create project").click().run()
    assert not at.exception, list(at.exception)
    work = tmp_path / "P" / name
    assert (work / "Production" / "Span 4 Production Sheet.xlsx").is_file()
    assert at.session_state["nav_radio"] == "Project status"
    job = at.session_state["fqa_job"]
    assert job["site_a"]["aisle"] == "100" and job["site_z"]["bay"] == "008"
    text = " ".join(m.value for m in at.markdown)
    assert "4 · Data Files" in text


def test_the_setup_name_follows_the_input_until_typed_over(home_on, settings_dir, span_dir, tmp_path):
    at = _setup("📈 Start New Project from Traces")
    at.text_input(key="setup_tr_a").set_value(str(span_dir / "A")).run()
    at.text_input(key="setup_tr_b").set_value(str(span_dir / "B")).run()
    at.text_input(key="setup_name").set_value("My span").run()
    at.run()
    assert at.text_input(key="setup_name").value == "My span"


def test_an_existing_project_folder_is_not_overwritten(home_on, settings_dir, span_dir, tmp_path):
    work = tmp_path / "P" / "ELMDALE to MILLER"
    work.mkdir(parents=True)
    (work / "ELMDALE to MILLER.otdrproj").write_text("{}", encoding="utf-8")
    at = _setup("📈 Start New Project from Traces")
    at.text_input(key="setup_tr_a").set_value(str(span_dir / "A")).run()
    at.text_input(key="setup_tr_b").set_value(str(span_dir / "B")).run()
    at.text_input(key="setup_parent").set_value(str(tmp_path / "P")).run()
    _button(at, "Create project").click().run()
    assert any("already a project" in e.value for e in at.error)
    assert (work / "ELMDALE to MILLER.otdrproj").read_text(encoding="utf-8") == "{}"


def test_back_returns_home(home_on, settings_dir):
    at = _setup("📄 Start New Project from Production Sheet")
    _button(at, "← Back").click().run()
    assert "🔬 Run Traces" in _labels(at)


def test_a_production_sheet_added_later_keeps_the_traces_answers(hub, tmp_path, monkeypatch):
    """Traces first, production sheet second: the job form gains the sheet's
    answers without losing the site names and fiber count from the traces."""
    from test_project_status import production_sheet
    from fqa.job_facts import JobFacts, derive
    job = {"site_a": {"alias": "ELMDALE"}, "site_z": {"alias": "MILLER"}, "fiber_count": 24}
    prod = hub._read_prod(production_sheet(tmp_path / "p.xlsx"))
    merged = json.loads(derive(prod, JobFacts.from_dict(job)).to_json())
    assert merged["site_a"]["alias"] == "ELMDALE" and merged["fiber_count"] == 24
    assert merged["site_a"]["aisle"] == "100"
