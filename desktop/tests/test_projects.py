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
import sys
import shutil

import pytest

from conftest import (REPO_ROOT, FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR,
                      goto, run_streamlit)

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
    monkeypatch.setenv("OTDR_TEST_HOME", "1")


def _labels(at):
    return [b.label for b in at.button]


def _start_project(folder):
    at = run_streamlit().run()
    _button(at, "📂 Open Recent Project").click().run()
    at.text_input(key="home_folder").set_value(str(folder)).run()
    _button(at, "Open this folder").click().run()
    assert not at.exception, list(at.exception)
    return at


def test_home_screen_offers_run_traces_and_start_project(home_on, settings_dir):
    at = run_streamlit().run()
    assert not at.exception, list(at.exception)
    assert {"🔬 Quick Analysis", "📁 Start New Project", "📂 Open Recent Project"} <= set(_labels(at))
    assert not [r for r in at.sidebar.radio if r.label == "Tool"]


def test_run_traces_is_the_suite_as_it_was(home_on, settings_dir):
    at = run_streamlit().run()
    _button(at, "🔬 Quick Analysis").click().run()
    assert not at.exception, list(at.exception)
    # Quick Analysis opens straight on the Suite screen (2026-09-30): the
    # tool list and the Trace Folders in the left panel, no load screen and
    # no project.
    assert "qa_stage" not in at.session_state
    tool = next(r for r in at.sidebar.radio if r.label == "Tool")
    assert tool.options == ["Viewer", "Splice Report", "Splice Report FEC", "Viewer FEC",
                            "Unidirectional", "Secret Sauce"]
    assert "##### Trace Folders" in [m.value for m in at.sidebar.markdown]
    assert not any(b.key == "qa_load" for b in at.button)
    assert "project_path" not in at.session_state
    # Its Home button goes back.
    at.button(key="go_home").click().run()
    assert "📁 Start New Project" in _labels(at)


def test_start_project_makes_the_work_folder_the_project(home_on, settings_dir, tmp_path):
    work = tmp_path / "SITEA-SITEB"
    work.mkdir()
    at = _start_project(work)
    proj = work / "SITEA-SITEB.otdrproj"
    assert proj.is_file()
    # No tool list or Analysis switch in a project (Robert, 2026-09-27): the
    # project screen's tabs open the tools.
    assert not [r for r in at.sidebar.radio if r.label == "Tool"]
    assert at.session_state["nav_radio"] == "Project Status"
    # No Load span box in a project: traces come in through section 4.
    assert not [e for e in at.sidebar.expander if "Load span" in e.label]
    # The tools point at the work folder.
    assert at.session_state["view_dir_a_input"] == str(work / "Traces" / "A")
    assert at.session_state["sr_report_dest"] == str(work / "Reports")
    data = json.loads(proj.read_text(encoding="utf-8"))
    assert data["spans"][0]["dir_a"]["rel"] == "Traces/A"


def test_a_project_saves_itself_and_comes_back_after_a_restart(home_on, settings_dir, span_dir):
    at = _start_project(span_dir)
    goto(at, "Splice Report")
    at.session_state["view_dir_a_input"] = str(span_dir / "A")
    at.session_state["view_dir_b_input"] = str(span_dir / "B")
    at.run()
    at.session_state["sr_site_a"] = "WSC"
    at.run()
    data = json.loads((span_dir / "WSC-SUI.otdrproj").read_text(encoding="utf-8"))
    assert data["spans"][0]["site_a"] == "WSC"          # no Save button needed

    at2 = run_streamlit().run()                          # "restart"
    _button(at2, "📂 Open Recent Project").click().run()
    assert any("📁 WSC-SUI" in m.value for m in at2.markdown)   # Recent
    at2.button(key="home_recent_0").click().run()
    assert not at2.exception, list(at2.exception)
    assert at2.session_state["nav_radio"] == "Project Status"
    assert at2.session_state["sr_site_a"] == "WSC"
    assert at2.session_state["view_dir_a_input"] == str(span_dir / "A")


def test_opening_is_not_a_change_and_does_not_rewrite_the_file(home_on, settings_dir, tmp_path):
    work = tmp_path / "W"
    work.mkdir()
    _start_project(work)
    proj = work / "W.otdrproj"
    before = proj.read_bytes()
    at = run_streamlit().run()
    _button(at, "📂 Open Recent Project").click().run()
    at.button(key="home_recent_0").click().run()
    at.run()
    assert proj.read_bytes() == before


def test_a_missing_work_folder_is_an_error_on_home(home_on, settings_dir, tmp_path):
    at = run_streamlit().run()
    _button(at, "📂 Open Recent Project").click().run()
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
    _button(at, "🔬 Quick Analysis").click().run()
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
    goto(at, "Splice Report")
    box = at.text_input(key="view_dir_a_input")
    assert box.proto.set_value and box.proto.value == str(span_dir / "Traces" / "A")
    # In a project the report goes to the job, no choice (2026-09-27).
    assert not [t for t in at.text_input if t.key == "sr_report_dest"]
    assert at.session_state["sr_report_dest"] == str(span_dir / "Reports")
    assert any("Report saved to Job File" in m.value for m in at.markdown)


# ── new project from traces / from a production sheet ─────────────────
def _setup(kind_label):
    at = run_streamlit().run()
    _button(at, kind_label).click().run()
    assert not at.exception, list(at.exception)
    return at


def test_new_project_from_traces_fills_in_and_lands_on_status(home_on, settings_dir, span_dir, tmp_path):
    at = _setup("📁 Start New Project")
    assert any("## New Project" in m.value for m in at.markdown)
    assert _button(at, "Create project").disabled        # nothing loaded yet
    at.text_input(key="setup_tr_a").set_value(str(span_dir / "A")).run()
    at.text_input(key="setup_tr_b").set_value(str(span_dir / "B")).run()
    assert any("A 24 fibers, B 24 fibers" in s.value for s in at.success)
    # The name is the two site names read from the traces, as the Read line
    # shows them (no site name is written in this test).
    name = at.text_input(key="setup_name").value
    site_a, site_b = name.split(" to ")
    assert site_a not in ("", "A") and site_b not in ("", "B")
    assert any(f"**{site_a} → {site_b}**" in s.value for s in at.success)
    at.text_input(key="setup_parent").set_value(str(tmp_path / "Projects")).run()
    assert _button(at, "Create project").disabled          # no customer yet
    at.selectbox(key="setup_customer").set_value("Lumen").run()
    _button(at, "Create project").click().run()
    assert not at.exception, list(at.exception)
    work = tmp_path / "Projects" / name
    # The first shoot, in its own dated folder (the .sor files say 2026-05-06).
    assert len(list((work / "Traces" / "2026-05-06" / "A").iterdir())) == 24
    assert at.session_state["project_final_shoot"] == "2026-05-06"
    assert at.session_state["app_mode"] == "project"
    assert at.session_state["nav_radio"] == "Project Status"
    assert at.session_state["sr_site_a"] == site_a
    job = at.session_state["fqa_job"]
    assert job["fiber_count"] == 24 and job["site_a"]["alias"] == site_a
    # Where new projects go is remembered for next time.
    data = json.loads((settings_dir / "settings.json").read_text(encoding="utf-8"))
    assert data["projects_root"] == str(tmp_path / "Projects")


def test_new_project_from_a_production_sheet_fills_the_job_form(home_on, settings_dir, tmp_path):
    from test_project_status import production_sheet
    sheet = production_sheet(tmp_path / "Span 4 Production Sheet.xlsx")
    at = _setup("📁 Start New Project")
    at.text_input(key="setup_prod_path").set_value(sheet).run()
    assert any("14 locations, 12 splices" in s.value for s in at.success)
    name = at.text_input(key="setup_name").value
    assert " to " in name
    at.text_input(key="setup_parent").set_value(str(tmp_path / "P")).run()
    at.selectbox(key="setup_customer").set_value("Zayo").run()
    _button(at, "Create project").click().run()
    assert not at.exception, list(at.exception)
    assert at.session_state["otdr_profile"] == "Zayo"          # the customer's profile
    assert any("No FQA form set up for Zayo" in i.value for i in at.info)
    work = tmp_path / "P" / name
    assert (work / "Production" / "Span 4 Production Sheet.xlsx").is_file()
    assert at.session_state["nav_radio"] == "Project Status"
    job = at.session_state["fqa_job"]
    assert job["site_a"]["aisle"] == "100" and job["site_z"]["bay"] == "008"
    text = " ".join(m.value for m in at.markdown)
    assert "4 · Data Files" in text


def test_new_project_says_so_when_only_one_direction_is_given(home_on, settings_dir, span_dir):
    """Found clicking through a real span: an A folder alone was dropped
    without a word and the project was created with no traces."""
    at = _setup("📁 Start New Project")
    at.text_input(key="setup_tr_a").set_value(str(span_dir / "A")).run()
    assert any("Only the A-direction folder is filled in" in w.value for w in at.warning)
    at.text_input(key="setup_tr_b").set_value(str(span_dir / "B")).run()
    assert not any("Only the" in w.value for w in at.warning)


def test_the_setup_name_follows_the_input_until_typed_over(home_on, settings_dir, span_dir, tmp_path):
    at = _setup("📁 Start New Project")
    at.text_input(key="setup_tr_a").set_value(str(span_dir / "A")).run()
    at.text_input(key="setup_tr_b").set_value(str(span_dir / "B")).run()
    at.text_input(key="setup_name").set_value("My span").run()
    at.run()
    assert at.text_input(key="setup_name").value == "My span"


def test_an_existing_project_folder_is_not_overwritten(home_on, settings_dir, span_dir, tmp_path):
    at = _setup("📁 Start New Project")
    at.text_input(key="setup_tr_a").set_value(str(span_dir / "A")).run()
    at.text_input(key="setup_tr_b").set_value(str(span_dir / "B")).run()
    # A project already sits where this one would go (its name is the one
    # read from the traces).
    name = at.text_input(key="setup_name").value
    work = tmp_path / "P" / name
    work.mkdir(parents=True)
    (work / f"{name}.otdrproj").write_text("{}", encoding="utf-8")
    at.text_input(key="setup_parent").set_value(str(tmp_path / "P")).run()
    at.selectbox(key="setup_customer").set_value("Lumen").run()
    _button(at, "Create project").click().run()
    assert any("already a project" in e.value for e in at.error)
    assert (work / f"{name}.otdrproj").read_text(encoding="utf-8") == "{}"


def test_back_returns_home(home_on, settings_dir):
    at = _setup("📁 Start New Project")
    _button(at, "← Back").click().run()
    assert "🔬 Quick Analysis" in _labels(at)


def test_a_production_sheet_added_later_keeps_the_traces_answers(hub, tmp_path, monkeypatch):
    """Traces first, production sheet second: the job form gains the sheet's
    answers without losing the site names and fiber count from the traces."""
    from test_project_status import production_sheet
    from fqa.job_facts import JobFacts, derive
    job = {"site_a": {"alias": "SITEA"}, "site_z": {"alias": "SITEB"}, "fiber_count": 24}
    prod = hub._read_prod(production_sheet(tmp_path / "p.xlsx"))
    merged = json.loads(derive(prod, JobFacts.from_dict(job)).to_json())
    assert merged["site_a"]["alias"] == "SITEA" and merged["fiber_count"] == 24
    assert merged["site_a"]["aisle"] == "100"


def test_open_a_recent_project_is_a_third_choice_with_its_own_screen(home_on, settings_dir, tmp_path):
    work = tmp_path / "Span 7"
    work.mkdir()
    _start_project(work)
    at = run_streamlit().run()
    assert "📂 Open Recent Project" in _labels(at)
    assert not [b for b in at.button if b.key == "home_recent_0"]   # not on Home any more
    _button(at, "📂 Open Recent Project").click().run()
    assert any("📁 Span 7" in m.value and str(work) in m.value for m in at.markdown)
    at.button(key="home_recent_0").click().run()
    assert at.session_state["app_mode"] == "project"
    assert at.session_state["project_path"] == str(work / "Span 7.otdrproj")


# ── Audit Project ────────────────────────────────────────────────────────
def _audit(home_on_unused, span_dir):
    at = _start_project(span_dir)
    at.button(key="ps_audit_start").click().run()
    assert not at.exception, list(at.exception)
    return at


def _heading(at):
    return next(m.value for m in at.markdown if m.value.startswith("### "))


def test_audit_walks_open_items_in_order_and_fills_one_in(home_on, settings_dir, span_dir):
    at = _audit(home_on, span_dir)
    assert any("Audit Project" in m.value for m in at.markdown)
    assert _heading(at) == "### 1.01  Bldg 1 (A end) site address"
    at.text_input(key="aud_site_a.address").set_value("123 Main St").run()
    _button(at, "Save and continue").click().run()
    assert not at.exception, list(at.exception)
    assert at.session_state["fqa_job"]["site_a"]["address"] == "123 Main St"
    # Filled in, so the audit moved on to the next open item.
    assert _heading(at) == "### 1.02  Bldg 2 (Z end) site address"


def test_audit_skip_moves_on_and_the_summary_lists_what_was_skipped(home_on, settings_dir, span_dir):
    at = _audit(home_on, span_dir)
    first = _heading(at)
    _button(at, "Skip for now ⏭").click().run()
    assert _heading(at) != first
    # Skip everything that is left; the summary names what was skipped.
    for _ in range(80):
        if not any(b.label == "Skip for now ⏭" for b in at.button):
            break
        _button(at, "Skip for now ⏭").click().run()
    assert any("skipped" in i.value for i in at.info)
    _button(at, "Go through the skipped ones again").click().run()
    assert _heading(at) == first
    _button(at, "Exit audit").click().run()
    assert any(b.key == "ps_audit_start" for b in at.button)


def test_audit_marks_a_hand_check_done(home_on, settings_dir, span_dir):
    at = _audit(home_on, span_dir)
    for _ in range(80):
        if _heading(at).startswith("### 4.03"):
            break
        _button(at, "Skip for now ⏭").click().run()
    _button(at, "The file names follow the convention").click().run()
    assert at.session_state["project_manual"]["4.03"] is True
    assert not _heading(at).startswith("### 4.03")


# ── project packages (.zdb) ─────────────────────────────────────
def test_export_then_open_a_package_on_another_machine(hub, home_on, settings_dir, span_dir, tmp_path):
    import zipfile
    at = _start_project(span_dir)
    at.session_state["sr_site_a"] = "WSC"
    at.run()
    work = str(span_dir)
    hub.add_shoot(str(span_dir / "A"), str(span_dir / "B"), work)
    small = hub.export_project(work, "none", str(tmp_path / "out"))
    full = hub.export_project(work, "all", str(tmp_path / "out"))
    assert small.endswith(".zdb") and "no traces" in small
    names = zipfile.ZipFile(small).namelist()
    assert "manifest.json" in names and not any("/Traces/" in n for n in names)
    assert any(n.endswith("/Traces/2026-05-06/A/ELMMIL0001_1550.sor") for n in zipfile.ZipFile(full).namelist())
    assert os.path.getsize(small) < hub.EMAIL_LIMIT_BYTES
    # "Another machine": unpack somewhere else, and it opens with its settings.
    root = tmp_path / "other pc" / "OTDR Projects"
    root.mkdir(parents=True)
    new = hub.import_project(full, str(root))
    assert os.path.basename(new) == "WSC-SUI"
    data = json.loads(open(hub.project_file_for_folder(new)[0], encoding="utf-8").read())
    assert data["spans"][0]["site_a"] == "WSC"
    # A second import of the same package does not overwrite the first.
    assert os.path.basename(hub.import_project(full, str(root))) == "WSC-SUI (2)"


def test_a_package_cannot_write_outside_its_folder(hub, tmp_path):
    import zipfile
    bad = tmp_path / "bad.otdrproject"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("otdrproject.json", json.dumps({"format": hub.LEGACY_PACKAGE_FORMAT, "name": "x"}))
        z.writestr("x/../../escape.txt", "no")
    with pytest.raises(ValueError, match="unsafe path"):
        hub.import_project(str(bad), str(tmp_path / "root"))
    assert not (tmp_path / "escape.txt").exists()
    other = tmp_path / "other.zip"
    with zipfile.ZipFile(other, "w") as z:
        z.writestr("hello.txt", "hi")
    import folder_intake as fi
    with pytest.raises(fi.ShareFileError, match="not an OTDR Suite file"):
        hub.import_project(str(other), str(tmp_path / "root"))


def test_a_refused_package_leaves_nothing_in_the_projects_folder(hub, tmp_path):
    # 2026-09-27: a package with no project file used to leave its
    # half-unpacked folder behind ("y", "y (2)" ... in the real Documents).
    import zipfile
    import folder_intake as fi
    root = tmp_path / "root"
    root.mkdir()
    (root / "keep").mkdir()                        # someone's real project
    legacy = tmp_path / "y.otdrproject"
    with zipfile.ZipFile(legacy, "w") as z:
        z.writestr("otdrproject.json", json.dumps({"format": hub.LEGACY_PACKAGE_FORMAT, "name": "keep"}))
        z.writestr("keep/readme.txt", "hi")
    readme = tmp_path / "readme.txt"
    readme.write_text("hi", encoding="utf-8")
    zdb = fi.share_write(tmp_path / "y.zdb", "project", {"keep/readme.txt": str(readme)}, {"name": "keep"})
    halfway = tmp_path / "half.otdrproject"
    with zipfile.ZipFile(halfway, "w") as z:
        z.writestr("otdrproject.json", json.dumps({"format": hub.LEGACY_PACKAGE_FORMAT, "name": "keep"}))
        z.writestr("keep/readme.txt", "written before the refusal")
        z.writestr("keep/../../escape.txt", "no")
    for pkg, why in ((legacy, "no project file"), (zdb, "no project file"), (halfway, "unsafe path")):
        with pytest.raises(ValueError, match=why):
            hub.import_project(str(pkg), str(root))
        assert sorted(p.name for p in root.iterdir()) == ["keep"], pkg
    assert not (root / "keep" / "readme.txt").exists()


def test_final_traces_export_holds_only_the_final_shoot(hub, tmp_path, span_dir):
    import zipfile
    work = str(tmp_path / "w")
    os.makedirs(work)
    hub.add_shoot(str(span_dir / "A"), str(span_dir / "B"), work)
    hub.add_shoot(str(span_dir / "A"), str(span_dir / "B"), work, label="reshoot")
    hub.st.session_state["project_final_shoot"] = "2026-05-06"
    names = zipfile.ZipFile(hub.export_project(work, "final", str(tmp_path / "o"))).namelist()
    hub.st.session_state.pop("project_final_shoot", None)
    assert any("/Traces/2026-05-06/A/" in n for n in names)
    assert not any("reshoot" in n for n in names)


def test_export_destinations_are_the_usual_folders_and_remembered_ones(hub, settings_dir, tmp_path, monkeypatch):
    home = tmp_path / "home"
    for d in ("Downloads", "Desktop"):
        (home / d).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    work = tmp_path / "work"
    work.mkdir()
    job_folder = tmp_path / "SharePoint job folder"
    job_folder.mkdir()
    hub._remember_export_dest(str(job_folder))
    labels = [l for l, _p in hub.export_destinations(str(work))]
    assert labels == ["Downloads", "Desktop", "This project's folder", str(job_folder)]
    hub._remember_export_dest(str(tmp_path / "gone"))
    assert str(tmp_path / "gone") not in [p for _l, p in hub.export_destinations(str(work))]


def test_export_is_a_tab_of_the_project_screen(home_on, settings_dir, span_dir):
    """Export Project was a pinned button with a pop-up; it is a tab now
    (Robert, 2026-09-27)."""
    at = _start_project(span_dir)
    assert "Export Project" in [t.label for t in at.tabs]
    assert not any(b.key in ("fx_export", "bar_audit") for b in at.button)
    assert at.radio(key="ps_export_mode").options[0].startswith("Without traces")
    assert "Choose another folder…" in at.selectbox(key="ps_export_where").options


def _fake_winreg(monkeypatch, entries):
    """A stand-in winreg holding the sync app's library list."""
    import types
    w = types.ModuleType("winreg")
    w.HKEY_CURRENT_USER = "HKCU"
    keys = {f"k{i}": e for i, e in enumerate(entries)}

    class Key:
        def __init__(self, name):
            self.name = name

    def OpenKey(parent, sub):
        if parent == "HKCU":
            return Key("root")
        return Key(sub)

    def EnumKey(k, i):
        names = list(keys)
        if i >= len(names):
            raise OSError
        return names[i]

    def QueryValueEx(k, name):
        v = keys[k.name].get(name)
        if v is None:
            raise OSError
        return (v, 1)
    w.OpenKey, w.EnumKey, w.QueryValueEx = OpenKey, EnumKey, QueryValueEx
    monkeypatch.setitem(sys.modules, "winreg", w)


def test_onedrive_synced_libraries_are_not_offered(hub, home_on, settings_dir, span_dir, tmp_path, monkeypatch):
    # Robert, 2026-09-30: "I don't want to use OneDrive": SharePoint is the
    # sign-in only, so a library this PC syncs is offered nowhere.
    jobs = tmp_path / "Acme Fiber" / "Field Ops - Jobs"
    jobs.mkdir(parents=True)
    _fake_winreg(monkeypatch, [{"MountPoint": str(jobs),
                                "UrlNamespace": "https://acme.sharepoint.com/sites/FieldOps/Jobs/"}])
    at = _setup("📁 Start New Project")
    assert not at.exception, list(at.exception)
    assert "setup_parent_sp" not in {s.key for s in at.selectbox}
    work = tmp_path / "w"
    work.mkdir()
    assert str(jobs) not in [p for _l, p in hub.export_destinations(str(work))]


def test_a_tool_opened_from_the_project_has_a_way_back(home_on, settings_dir, span_dir):
    at = _start_project(span_dir)
    at.session_state["nav_radio"] = "Viewer"
    at.run()
    assert not at.exception, list(at.exception)
    assert not [r for r in at.sidebar.radio if r.label == "Tool"]
    at.button(key="go_project").click().run()
    assert at.session_state["nav_radio"] == "Project Status"
    assert [t.label for t in at.tabs][0] == "Events"
    # The audit starts from the Audit FQA tab.
    at.button(key="ps_audit_start").click().run()
    assert not at.exception, list(at.exception)
    assert _heading(at).startswith("### 1.01")


def test_the_customer_list_is_splice_reports_customers(hub):
    names = hub.project_customers()
    assert "Lumen" in names and "Zayo" in names and "AWS / IIG MT.1085" in names
    assert "Default (engine baseline)" not in names and "Custom (edit table below)" not in names
    assert names == [n for n in hub.CUSTOMER_PROFILES if n in names]     # same order


def test_one_new_project_screen_takes_a_sheet_traces_or_both(home_on, settings_dir, span_dir, tmp_path):
    """Robert, 2026-09-24: one Start button; 1 project, 2 production sheet,
    3 traces, 4 customer; Create once there is either a sheet or traces."""
    from test_project_status import production_sheet
    sheet = production_sheet(tmp_path / "Span 4 Production Sheet.xlsx")
    at = _setup("📁 Start New Project")
    text = " ".join(m.value for m in at.markdown)
    order = [text.index(t) for t in ("1 · The Project", "2 · The Production Sheet",
                                     "3 · The Traces", "4 · Customer")]
    assert order == sorted(order)
    at.selectbox(key="setup_customer").set_value("Lumen").run()
    assert _button(at, "Create project").disabled         # customer, but no sheet or traces
    at.text_input(key="setup_prod_path").set_value(sheet).run()
    at.text_input(key="setup_tr_a").set_value(str(span_dir / "A")).run()
    at.text_input(key="setup_tr_b").set_value(str(span_dir / "B")).run()
    at.text_input(key="setup_parent").set_value(str(tmp_path / "P")).run()
    name = at.text_input(key="setup_name").value
    assert name == "Flagler to Bethune"                    # the sheet names it first
    _button(at, "Create project").click().run()
    assert not at.exception, list(at.exception)
    work = tmp_path / "P" / name
    assert (work / "Production" / "Span 4 Production Sheet.xlsx").is_file()
    assert len(list((work / "Traces" / "2026-05-06" / "A").iterdir())) == 24
    job = at.session_state["fqa_job"]
    # The sheet's answers, with the traces filling what it left.
    assert job["site_a"]["alias"] == "Flagler" and job["site_a"]["aisle"] == "100"
    assert job["fiber_count"]


# ── .zdb / .zfc openers (share_dispatch) ────────────────────────────────
class _StubSt:
    def __init__(self, state=None):
        self.session_state = dict(state or {})


def _packaged_project(hub, work_parent, name="WSC-SUI"):
    """A minimal real project folder (project file + one Field file)."""
    work = work_parent / name
    work.mkdir(parents=True)
    hub._write_new_project(str(work), "WSC", "SUI", {})
    (work / "Field").mkdir(exist_ok=True)
    (work / "Field" / "note.txt").write_text("hello", encoding="utf-8")
    return work


def test_zdb_export_import_round_trip(hub, settings_dir, tmp_path):
    import folder_intake as fi
    work = _packaged_project(hub, tmp_path / "src")
    zdb = hub.export_project(str(work), "none", str(tmp_path / "out"))
    sf = fi.share_open(zdb, expect="project")
    assert sf.kind == "project" and sf.manifest["meta"]["name"] == "WSC-SUI"
    new = hub.import_project(zdb, str(tmp_path / "root"))
    assert os.path.basename(new) == "WSC-SUI"
    assert open(os.path.join(new, "Field", "note.txt"), encoding="utf-8").read() == "hello"
    assert hub.project_file_for_folder(new)[1]


def test_old_otdrproject_still_imports(hub, settings_dir, tmp_path, monkeypatch):
    import zipfile
    work = _packaged_project(hub, tmp_path / "src")
    old = tmp_path / "old.otdrproject"
    with zipfile.ZipFile(old, "w") as z:
        z.writestr("otdrproject.json", json.dumps(
            {"format": hub.LEGACY_PACKAGE_FORMAT, "version": 1, "name": "WSC-SUI"}))
        for root, _d, files in os.walk(work):
            for f in files:
                p = os.path.join(root, f)
                z.write(p, "WSC-SUI/" + os.path.relpath(p, work).replace(os.sep, "/"))
    new = hub.import_project(str(old), str(tmp_path / "root"))
    assert hub.project_file_for_folder(new)[1]
    # And through the hub's one opener.
    stub = _StubSt()
    monkeypatch.setattr(hub, "st", stub)
    monkeypatch.setattr(hub, "_default_projects_root", lambda: str(tmp_path / "root2"))
    level, msg = hub.open_share_file(str(old))
    assert level == "success", msg
    assert os.path.basename(stub.session_state["_setup_open"]) == "WSC-SUI"


def test_open_share_file_opens_a_zdb(hub, settings_dir, tmp_path, monkeypatch):
    work = _packaged_project(hub, tmp_path / "src")
    zdb = hub.export_project(str(work), "none", str(tmp_path / "out"))
    stub = _StubSt()
    monkeypatch.setattr(hub, "st", stub)
    monkeypatch.setattr(hub, "_default_projects_root", lambda: str(tmp_path / "root"))
    level, msg = hub.open_share_file(zdb)
    assert level == "success" and "WSC-SUI" in msg
    assert hub.project_file_for_folder(stub.session_state["_setup_open"])[1]


def test_zfc_goes_to_the_open_projects_field_folder(hub, settings_dir, tmp_path, monkeypatch):
    import folder_intake as fi
    work = _packaged_project(hub, tmp_path / "src")
    zfc = fi.share_write(tmp_path / "capture", "field-capture", {"a.txt": b"x"})
    stub = _StubSt({"project_path": hub.project_file_for_folder(str(work))[0]})
    monkeypatch.setattr(hub, "st", stub)
    level, msg = hub.open_share_file(str(zfc))
    assert level == "success", msg
    assert (work / "Field" / "capture.zfc").read_bytes() == zfc.read_bytes()
    # Opening it again does not make a second copy.
    hub.open_share_file(str(zfc))
    assert sorted(os.listdir(work / "Field")) == ["capture.zfc", "note.txt"]


def test_zfc_with_no_project_open_says_open_the_project_first(hub, tmp_path, monkeypatch):
    import folder_intake as fi
    zfc = fi.share_write(tmp_path / "capture", "field-capture", {"a.txt": b"x"})
    monkeypatch.setattr(hub, "st", _StubSt())
    level, msg = hub.open_share_file(str(zfc))
    assert level == "error" and "Open its project first" in msg


def test_wrong_kind_and_newer_version_show_the_share_message(hub, tmp_path, monkeypatch):
    import zipfile
    import folder_intake as fi
    zfc = fi.share_write(tmp_path / "capture", "field-capture", {"a.txt": b"x"})
    wrong = tmp_path / "renamed.zdb"
    shutil.copy(zfc, wrong)
    with pytest.raises(fi.ShareFileError, match="is a Field Capture file, not a Project file"):
        hub.import_project(str(wrong), str(tmp_path / "root"))
    newer = tmp_path / "newer.zdb"
    with zipfile.ZipFile(newer, "w") as z:
        z.writestr("manifest.json", json.dumps(
            {"format": fi.SHARE_FORMAT, "kind": "project", "version": 99}))
    monkeypatch.setattr(hub, "st", _StubSt())
    level, msg = hub.open_share_file(str(newer))
    assert level == "error" and "newer OTDR Suite" in msg
    level, msg = hub.open_share_file(str(tmp_path / "missing.zdb"))
    assert level == "error" and "was not found" in msg


def test_open_screen_lists_zdb_zfc_and_old_packages():
    assert "'**Open a .zdb or .zfc File**'" in SRC
    assert "OPEN_FILE_EXTS = ('.zdb', '.zfc', LEGACY_PACKAGE_EXT)" in SRC


def test_double_clicked_zfc_with_no_project_shows_the_message(tmp_path, monkeypatch):
    import time as _t
    import folder_intake as fi
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    zfc = fi.share_write(tmp_path / "capture", "field-capture", {"a.txt": b"x"})
    (tmp_path / ".otdrSuite").mkdir()
    (tmp_path / ".otdrSuite" / "open_request.json").write_text(
        json.dumps({"path": str(zfc), "ts": _t.time()}), encoding="utf-8")
    at = run_streamlit().run()
    assert not at.exception, list(at.exception)
    assert any("Open its project first" in e.value for e in at.error)
