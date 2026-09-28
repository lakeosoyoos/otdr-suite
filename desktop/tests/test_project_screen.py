"""The project screen: an overview, then tabs (Events, Traces, Reports,
Pictures, GPS, Audit FQA).

Robert, 2026-09-26: "Events tab will show every action that has happened
in the project ... Traces would be a running directory for any traces shot
for the project and their date ... Reports would be a directory where we
store and access all Uni, Splice Report, or Secret Sauce reports ...
Pictures ... GPS would be where we get GPS coordinates from field capture
or where we enter them manually".
"""
from __future__ import annotations

import json
import os
import shutil
import time
import zipfile
from types import SimpleNamespace

import pytest

from conftest import FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, run_streamlit


@pytest.fixture
def hub():
    import app
    return app


@pytest.fixture
def settings_dir(tmp_path, monkeypatch):
    d = tmp_path / "settings"
    d.mkdir()
    monkeypatch.setenv("OTDR_SETTINGS_DIR", str(d))
    monkeypatch.setenv("OTDR_TEST_HOME", "1")
    return d


@pytest.fixture
def span_dir(tmp_path):
    root = tmp_path / "ELMDALE-MILLER"
    shutil.copytree(FIXTURE_SPLICE_A_DIR, root / "A")
    shutil.copytree(FIXTURE_SPLICE_B_DIR, root / "B")
    return root


def _touch(path, data=b"x", when=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if when:
        os.utime(path, (when, when))
    return path


def _package(path, job, sites=(), splices=(), photos=()):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("capture.json", json.dumps({"format": "otdr-capture", "v": 1, "job": job,
                                               "sites": list(sites), "splices": list(splices)}))
        for n in photos:
            z.writestr(n, b"\xff\xd8jpeg")
    return path


# ── Events ───────────────────────────────────────────────────────────────
def test_the_first_look_backfills_and_later_files_are_found(hub, tmp_path):
    work = tmp_path / "Job"
    _touch(work / "Reports" / "A_to_B_SpliceReport 2026-09-01 0900.xlsx", when=1_790_000_000)
    _touch(work / "Production" / "sheet.xlsx", when=1_789_000_000)
    assert hub.project_scan(str(work)) == 2
    ev = hub.events_read(str(work))["events"]
    assert [e["kind"] for e in ev] == ["Production Sheet", "Report"]     # oldest first
    assert all(e["how"] == "OTDR Suite" for e in ev)
    assert "Splice Report run" in ev[1]["text"]
    assert hub.project_scan(str(work)) == 0                              # nothing new
    # A photo dragged into the folder, and a .zfc saved from an email.
    _touch(work / "Pictures" / "Z end" / "box.jpg")
    (work / "Field").mkdir()
    _package(work / "Field" / "cap.zip", "job1",
             sites=[{"site": "A", "photos": ["photos/A-1-1.jpg"], "gps": {"lat": 39.4, "lon": -103.0}}],
             splices=[{"event": 1, "gps": {"lat": 39.4, "lon": -102.9}}],
             photos=["photos/A-1-1.jpg"])
    assert hub.project_scan(str(work)) == 2
    ev = hub.events_read(str(work))["events"][-2:]
    texts = {e["kind"]: e["text"] for e in ev}
    assert texts["Photo"] == "Photo added (Z end): box.jpg"
    assert texts["Field Capture"] == "Field Capture received: 1 photo, 2 GPS fixes (cap.zip)"
    assert all(e["how"] == "Found in folder" for e in ev)


def test_what_the_app_logs_is_not_logged_again_and_a_rerun_report_is(hub, tmp_path):
    work = tmp_path / "Job"
    work.mkdir()
    hub.project_scan(str(work))
    p = _touch(work / "FQA" / "pkg.xlsm")
    hub.project_log(str(work), "FQA", "FQA package built: pkg.xlsm", [str(p)])
    assert hub.project_scan(str(work)) == 0
    r = _touch(work / "Reports" / "unidirectional_events.xlsx", when=1_790_000_000)
    hub.project_scan(str(work))
    os.utime(r, (1_790_100_000, 1_790_100_000))
    assert hub.project_scan(str(work)) == 1
    assert hub.events_read(str(work))["events"][-1]["text"] == \
        "Unidirectional run again: unidirectional_events.xlsx"


def test_a_secret_sauce_run_is_one_event_not_one_per_file(hub, tmp_path):
    work = tmp_path / "Job"
    run = work / "Reports" / "Secret Sauce 2026-09-26 1400"
    for n in ("report.xlsx", "pairs.csv", "chart.png"):
        _touch(run / n)
    assert hub.project_scan(str(work)) == 1
    assert hub.events_read(str(work))["events"][0]["text"].startswith("Secret Sauce run")


def test_trace_shoots_are_one_event_each(hub, tmp_path, span_dir):
    work = tmp_path / "Job"
    shutil.copytree(span_dir / "A", work / "Traces" / "2026-05-06" / "A")
    shutil.copytree(span_dir / "B", work / "Traces" / "2026-05-06" / "B")
    assert hub.project_scan(str(work)) == 1
    e = hub.events_read(str(work))["events"][0]
    assert e["kind"] == "Traces" and "A 24 / B 24 fibers" in e["text"]


# ── GPS ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("lat,lon", [(39.468819, -102.968175), (32.7875, -114.7986), (0.5, -0.25)])
def test_dms_text_reads_back_as_the_same_fix(hub, lat, lon):
    kind, (la, lo) = hub.parse_event_location(hub.dms_text(lat, lon))
    assert kind == "gps" and abs(la - lat) < 1e-5 and abs(lo - lon) < 1e-5


def _prod():
    term = "termination"
    splice = "splice"
    locs = [SimpleNamespace(kind=term, name="West ILA", sheet="West", vault_id=None, address="1 Main"),
            SimpleNamespace(kind=splice, name="West Entry", sheet="Entry", vault_id="ENTRY", address=None),
            SimpleNamespace(kind=splice, name="Splice 1", sheet="Splice 1", vault_id=1,
                            address="39 28 7.75 N 102 58 5.43 W"),
            SimpleNamespace(kind=splice, name="Splice 2", sheet="Splice 2", vault_id=2, address=None),
            SimpleNamespace(kind=term, name="East ILA", sheet="East", vault_id=None, address="2 Main")]
    return SimpleNamespace(locations=locs, splices=[l for l in locs if l.kind == splice])


def test_typed_gps_beats_the_phone_which_beats_the_sheet(hub):
    pkgs = [("cap.zfc", {"splices": [{"event": 1, "gps": {"lat": 39.47, "lon": -103.0}},
                                     {"event": 2, "gps": {"lat": 39.46, "lon": -102.97}}],
                         "sites": [{"site": "A", "gps": {"lat": 39.48, "lon": -103.02}}]})]
    rows = hub.project_gps_rows(_prod(), pkgs, {"2": "39.45, -102.9"})
    by = {r["key"]: r for r in rows}
    assert [r["key"] for r in rows] == ["A", 1, 2, 3, "Z"]
    assert by["A"]["from"] == "phone"
    assert by[1]["from"] == "phone"
    assert by[2]["from"] == "typed" and by[2]["used"] == {"lat": 39.45, "lon": -102.9}
    assert by[3]["from"] == ""                     # nothing yet
    rows = hub.project_gps_rows(_prod(), [], {})
    assert {r["key"]: r["from"] for r in rows}[2] == "production sheet"
    # The FQA gets the phone's and the typed fixes, as the form's DMS text;
    # a splice the production sheet already answers is left to the builder.
    over = hub.fqa_location_overrides(_prod(), hub.project_gps_rows(_prod(), pkgs, {"2": "39.45, -102.9"}))
    assert set(over) == {"Entry", "Splice 1"}
    assert hub.parse_event_location(over["Splice 1"])[0] == "gps"


# ── the screen ───────────────────────────────────────────────────────────
def _new_project(at, span_dir, tmp_path):
    next(b for b in at.button if b.label == "📁 Start New Project").click().run()
    at.text_input(key="setup_tr_a").set_value(str(span_dir / "A")).run()
    at.text_input(key="setup_tr_b").set_value(str(span_dir / "B")).run()
    at.text_input(key="setup_parent").set_value(str(tmp_path / "Projects")).run()
    at.selectbox(key="setup_customer").set_value("Lumen").run()
    next(b for b in at.button if b.label == "Create project").click().run()
    assert not at.exception, list(at.exception)
    return tmp_path / "Projects" / "ELMDALE to MILLER"


def test_the_project_screen_has_the_overview_and_six_tabs(settings_dir, span_dir, tmp_path):
    at = run_streamlit().run()
    work = _new_project(at, span_dir, tmp_path)
    assert [t.label for t in at.tabs] == ["Events", "Traces", "Reports", "Pictures", "GPS",
                                          "Audit FQA", "Export Project"]
    text = " ".join(m.value for m in at.markdown)
    assert "**ELMDALE → MILLER**" in text and "**Shot 2026-05-06**" in text
    # The project's creation is the first event, and its traces are not
    # logged a second time by the folder scan.
    ev = json.loads((work / "Project Events.json").read_text(encoding="utf-8"))["events"]
    assert len(ev) == 1 and ev[0]["kind"] == "Project" and "customer Lumen" in ev[0]["text"]
    # A second shoot is an event with its date.  (A fresh session: AppTest
    # cannot drive widgets across the setup screen's key cleanup.)
    at = run_streamlit().run()
    next(b for b in at.button if b.label == "📂 Open Recent Project").click().run()
    at.text_input(key="home_folder").set_value(str(work)).run()
    next(b for b in at.button if b.label == "Open this folder").click().run()
    at.text_input(key="ps_tr_a").set_value(str(span_dir / "A")).run()
    at.text_input(key="ps_tr_b").set_value(str(span_dir / "B")).run()
    at.text_input(key="ps_tr_label").set_value("reshoot").run()
    next(b for b in at.button if b.key == "ps_tr_copy").click().run()
    assert not at.exception, list(at.exception)
    ev = json.loads((work / "Project Events.json").read_text(encoding="utf-8"))["events"]
    assert ev[-1]["kind"] == "Traces" and "shot 2026-05-06 · reshoot" in ev[-1]["text"]
    assert len(ev) == 2
    # The Audit FQA tab holds the checklist and the build; Start the Audit
    # opens the one-at-a-time audit.
    text = " ".join(m.value for m in at.markdown)
    for title in ("1 · Site Survey Data", "4 · Data Files", "Build the Lumen FQA Package"):
        assert title in text
    next(b for b in at.button if b.key == "ps_audit_start").click().run()
    assert at.session_state["audit_on"] is True


def test_a_report_run_in_a_project_keeps_its_own_file(hub, tmp_path, monkeypatch):
    work = tmp_path / "Job"
    proj = work / "Job.otdrproj"
    state = {"app_mode": "project", "project_path": str(proj)}
    monkeypatch.setattr(hub.st, "session_state", state)
    reports = str(work / "Reports")
    p = hub._project_run_path(reports, "A_to_B_SpliceReport.xlsx")
    assert os.path.dirname(p) == reports
    assert os.path.basename(p).startswith("A_to_B_SpliceReport 2") and p.endswith(".xlsx")
    # Anywhere else, and outside a project, the name is what it always was.
    assert hub._project_run_path(str(tmp_path / "Downloads"), "x.xlsx") == \
        os.path.join(str(tmp_path / "Downloads"), "x.xlsx")
    state["app_mode"] = "traces"
    assert hub._project_run_path(reports, "x.xlsx") == os.path.join(reports, "x.xlsx")


def test_projects_send_every_report_tool_to_reports(settings_dir, span_dir, tmp_path):
    at = run_streamlit().run()
    work = _new_project(at, span_dir, tmp_path)
    for key in ("sr_report_dest", "uni_report_dest", "ss_report_dest"):
        assert at.session_state[key] == str(work / "Reports")


# ── building the Lumen FQA package from the project ──────────────────────
def test_the_fqa_package_takes_the_projects_gps(hub, tmp_path):
    import openpyxl
    from test_project_status import production_sheet
    from fqa.production_sheet import read_production_sheet
    work = tmp_path / "Job"
    (work / "Production").mkdir(parents=True)
    prod_path = production_sheet(work / "Production" / "prod.xlsx")
    prod = read_production_sheet(prod_path)
    third = prod.splices[2]
    rows = hub.project_gps_rows(prod, [], {"3": "39.45, -102.9"})
    manifest = hub.build_project_fqa(str(work), prod_path, {}, rows, [],
                                     {"distances_m": None, "span_length_m": None})
    assert os.path.dirname(manifest["out"]) == str(work / "FQA")
    assert manifest["not_in_this_build"] in ([], ["photos"])
    ev = openpyxl.load_workbook(manifest["out"], keep_vba=True)["Event Log"]
    locs = {ev[f"N{r}"].value: ev[f"D{r}"].value for r in range(18, 60)}
    assert locs[third.vault_id] == hub.dms_text(39.45, -102.9)
    # The splices nobody typed keep the production sheet's text.
    other = prod.splices[3]
    assert locs[other.vault_id] == f"GPS for {other.sheet}"


def test_trace_distances_come_from_a_stored_read_not_a_new_engine_run(hub, tmp_path, span_dir,
                                                                      monkeypatch):
    """Drawing the page must never start a Splice Report run (minutes long)."""
    from test_project_status import production_sheet
    work = tmp_path / "Job"
    shutil.copytree(span_dir / "A", work / "Traces" / "2026-05-06" / "A")
    shutil.copytree(span_dir / "B", work / "Traces" / "2026-05-06" / "B")
    (work / "Production").mkdir()
    prod = hub._read_prod(production_sheet(work / "Production" / "p.xlsx"))
    state = {"project_path": str(work / "Job.otdrproj")}
    monkeypatch.setattr(hub.st, "session_state", state)
    runs = []
    monkeypatch.setattr(hub, "_fqa_sr_manifest",
                        lambda a, b: runs.append((a, b)) or {"ok": False, "error": "x"})
    t = hub.project_trace_distances(str(work), prod)
    assert runs == [] and t["read"] is False and t["distances_m"] is None
    t = hub.project_trace_distances(str(work), prod, run=True)
    assert len(runs) == 1 and t["read"] is True and t["distances_m"] is None


def test_the_fqa_package_gets_the_projects_photos(hub, tmp_path, monkeypatch):
    import io
    from PIL import Image
    from test_project_status import production_sheet

    def jpg(color):
        b = io.BytesIO()
        Image.new("RGB", (640, 480), color).save(b, "JPEG")
        return b.getvalue()

    work = tmp_path / "Job"
    (work / "Production").mkdir(parents=True)
    (work / "Field").mkdir()
    prod_path = production_sheet(work / "Production" / "p.xlsx")
    with zipfile.ZipFile(work / "Field" / "cap.zip", "w") as z:
        z.writestr("capture.json", json.dumps({"format": "otdr-capture", "v": 1, "job": "j1",
            "sites": [{"site": "A", "photos": ["photos/A-1-1.jpg", "photos/A-1-2.jpg"]},
                      {"site": "Z", "photos": ["photos/Z-1-1.jpg"]}]}))
        z.writestr("photos/A-1-1.jpg", jpg((200, 0, 0)))
        z.writestr("photos/A-1-2.jpg", jpg((0, 200, 0)))
        z.writestr("photos/Z-1-1.jpg", jpg((0, 0, 200)))
    _touch(work / "Pictures" / "Z end" / "broken.jpg", b"not a picture")
    photos = hub.project_photos(str(work), "j1")
    assert sorted(p["end"] for p in photos) == ["A", "A", "Z", "Z"]
    rows = hub.project_gps_rows(hub._read_prod(prod_path), [], {})
    m = hub.build_project_fqa(str(work), prod_path, {}, rows, photos,
                              {"distances_m": None, "span_length_m": None})
    assert m["photos"] == 3 and m["not_in_this_build"] == []
    assert "broken.jpg" in m["warnings"][0]
    wb = hub.read_fqa_workbook(m["out"])
    per = hub.fqa_photos_per_end(wb.get("pictures"))
    assert (per["A"], per["Z"]) == (2, 1)


# ── 2026-09-27: demo span, one ticked shoot, reports know their traces ───
def test_the_sample_span_opens_with_traces_sheet_photos_and_gps(settings_dir, tmp_path):
    (tmp_path / "Projects").mkdir()          # an absent root falls back to ~/Documents
    json.dump({"projects_root": str(tmp_path / "Projects")},
              open(settings_dir / "settings.json", "w", encoding="utf-8"))
    at = run_streamlit().run()
    at.button(key="home_demo").click().run()
    assert not at.exception, list(at.exception)
    at.run()
    work = tmp_path / "Projects" / "Sample Span"
    assert at.session_state["app_mode"] == "project"
    assert at.session_state["project_job_id"] == "demo0001"
    assert len(list((work / "Traces").glob("*/A/*.sor"))) == 24
    import app
    pkgs = app.collect_capture_packages(str(work / "Field"))
    assert pkgs and pkgs[0][1]["job"] == "demo0001"
    rows = app.project_gps_rows(app._read_prod(app.project_production_sheet(str(work))),
                                pkgs, {})
    phone = [r for r in rows if isinstance(r["key"], int)]
    assert sum(1 for r in phone if r["from"] == "phone") == len(phone) - 1   # one to type
    # A second click opens the same project, as it was left.
    at2 = run_streamlit().run()
    at2.button(key="home_demo").click().run()
    at2.run()
    assert at2.session_state["project_path"] == at.session_state["project_path"]


def test_each_shoot_row_runs_in_a_tool_and_the_final_does_not_move_it(settings_dir, span_dir,
                                                                       tmp_path):
    """A Run In… button on every shoot's row (Robert, 2026-09-27); the final
    traces are for the FQA side only."""
    at = run_streamlit().run()
    work = _new_project(at, span_dir, tmp_path)
    at = run_streamlit().run()
    next(b for b in at.button if b.label == "📂 Open Recent Project").click().run()
    at.text_input(key="home_folder").set_value(str(work)).run()
    next(b for b in at.button if b.label == "Open this folder").click().run()
    at.text_input(key="ps_tr_a").set_value(str(span_dir / "A")).run()
    at.text_input(key="ps_tr_b").set_value(str(span_dir / "B")).run()
    at.text_input(key="ps_tr_label").set_value("reshoot").run()
    next(b for b in at.button if b.key == "ps_tr_copy").click().run()
    keys = {b.key for b in at.button}
    assert {"run_viewer_2026-05-06", "run_unidirectional_2026-05-06 reshoot"} <= keys
    assert at.session_state["project_final_shoot"] == "2026-05-06"
    at.button(key="run_unidirectional_2026-05-06 reshoot").click().run()
    assert not at.exception, list(at.exception)
    assert at.session_state["nav_radio"] == "Unidirectional"
    assert at.session_state["uni_folder_input"] == str(work / "Traces" / "2026-05-06 reshoot" / "A")
    assert at.session_state["project_final_shoot"] == "2026-05-06"


def test_a_project_report_remembers_its_traces(hub, tmp_path, monkeypatch, span_dir):
    work = tmp_path / "Job"
    shoot = work / "Traces" / "2026-05-06"
    shutil.copytree(span_dir / "A", shoot / "A")
    shutil.copytree(span_dir / "B", shoot / "B")
    monkeypatch.setattr(hub.st, "session_state",
                        {"app_mode": "project", "project_path": str(work / "Job.otdrproj")})
    out = hub._project_run_path(str(work / "Reports"), "X_SpliceReport.xlsx",
                                traces=(str(shoot / "A"), str(shoot / "B")))
    used = hub.events_read(str(work))["report_traces"][hub._rel(str(work), out)]
    sh = hub._shoot_of(str(work), used)
    assert sh and sh["id"] == "2026-05-06"
    assert hub._traces_text(str(work), used, sh).startswith("Traces shot 2026-05-06")
    assert hub._traces_text(str(work), [], None).startswith("Traces not recorded")


def test_in_a_project_reports_are_saved_to_the_job_with_no_choice(settings_dir, span_dir,
                                                                   tmp_path):
    from conftest import goto
    at = run_streamlit().run()
    work = _new_project(at, span_dir, tmp_path)
    at = run_streamlit().run()
    next(b for b in at.button if b.label == "📂 Open Recent Project").click().run()
    at.text_input(key="home_folder").set_value(str(work)).run()
    next(b for b in at.button if b.label == "Open this folder").click().run()
    for page in ("Splice Report", "Unidirectional", "Secret Sauce"):
        goto(at, page)
        assert not at.exception, list(at.exception)
        keys = {t.key for t in at.text_input} | {b.key for b in at.button}
        assert not {"sr_report_dest", "uni_report_dest", "ss_report_dest",
                    "sr_report_dest_browse", "uni_report_dest_browse",
                    "ss_report_dest_browse"} & keys, page
        assert any("Report saved to Job File" in m.value for m in at.markdown), page
    assert at.session_state["ss_report_dest"] == str(work / "Reports")


def test_a_report_exports_as_a_copy_and_a_run_folder_as_a_zip(hub, tmp_path):
    rep = _touch(tmp_path / "Job" / "Reports" / "A_to_B_SpliceReport.xlsx", b"xlsx")
    out = hub.export_report(str(rep), str(tmp_path / "Downloads"))
    assert open(out, "rb").read() == b"xlsx" and os.path.isfile(rep)
    again = hub.export_report(str(rep), str(tmp_path / "Downloads"))
    assert again != out and again.endswith("(2).xlsx")
    run = tmp_path / "Job" / "Reports" / "Secret Sauce 2026-09-27 1000"
    _touch(run / "report.xlsx")
    z = hub.export_report(str(run), str(tmp_path / "Downloads"))
    assert z.endswith(".zip") and zipfile.ZipFile(z).namelist() == ["report.xlsx"]


# ── Quick Analysis: Load Traces, then the tools as tabs (2026-09-27) ─────
def _qa_loaded(span_dir):
    at = run_streamlit().run()
    at.button(key="home_traces").click().run()
    assert not at.exception, list(at.exception)
    assert at.session_state["qa_stage"] == "load"
    assert not [r for r in at.sidebar.radio if r.label == "Tool"]
    at.text_input(key="qa_tr_a").set_value(str(span_dir / "A")).run()
    at.text_input(key="qa_tr_b").set_value(str(span_dir / "B")).run()
    at.button(key="qa_load").click().run()
    assert not at.exception, list(at.exception)
    return at


def test_quick_analysis_loads_traces_once_then_every_tool_is_a_tab(settings_dir, span_dir):
    at = _qa_loaded(span_dir)
    assert at.session_state["qa_stage"] == "main"
    assert {"qa_tab_splice_report", "qa_tab_unidirectional", "qa_tab_secret_sauce",
            "qa_tab_viewer"} <= {b.key for b in at.button}
    assert any("ELMDALE" in m.value and "MILLER" in m.value for m in at.markdown)
    for key, heading in (("qa_tab_unidirectional", "Unidirectional"),
                         ("qa_tab_secret_sauce", "Secret Sauce"),
                         ("qa_tab_splice_report", "Bidirectional Splice Report")):
        at.button(key=key).click().run()
        assert not at.exception, list(at.exception)
        text = " ".join(m.value for m in at.markdown)
        assert heading in text and "Traces:** ✅" in text, key
        # The traces are chosen: no folder picking on any tool.
        labels = {b.label for b in at.button}
        assert not {"📁 Browse for folder", "📂 A-direction folder"} & labels, key
        assert not [t for t in at.text_input
                    if t.key in ("uni_folder_input", "ss_folder_input", "view_dir_a_input")]
    assert at.session_state["uni_folder_input"] == at.session_state["span_loaded"]["dir_a"]


def test_quick_analysis_home_and_back_offers_the_loaded_traces(settings_dir, span_dir):
    at = _qa_loaded(span_dir)
    at.button(key="go_home").click().run()
    at.button(key="home_traces").click().run()
    assert at.session_state["qa_stage"] == "load"
    at.button(key="qa_continue").click().run()
    assert at.session_state["qa_stage"] == "main"
    assert not at.exception, list(at.exception)


def test_replace_traces_goes_back_to_the_load_screen(settings_dir, span_dir):
    at = _qa_loaded(span_dir)
    at.button(key="qa_reload").click().run()
    assert at.session_state["qa_stage"] == "load"
    assert any(b.key == "qa_continue" for b in at.button)


# ── times, the project owner and their emails (2026-09-27) ───────────────
def test_times_show_am_pm_and_the_zone(hub):
    import time as _t
    when = _t.mktime((2026, 5, 6, 15, 5, 0, 0, 0, -1))
    txt = hub._when_text(when)
    assert txt.startswith("2026-05-06 03:05 PM") and len(txt.split()) == 4
    assert hub._when_text(None) == ""


def test_a_windows_zone_name_is_cut_to_its_letters(hub, monkeypatch):
    monkeypatch.setattr(hub.time, "strftime",
                        lambda f, tm=None: "Pacific Daylight Time" if f == "%Z" else "x")
    assert hub._tz_abbr(None) == "PDT"


def test_every_event_emails_the_owner_through_the_sender_mailbox(hub, tmp_path, monkeypatch):
    work = tmp_path / "Job"
    work.mkdir()
    hub.project_write(str(work / "Job.otdrproj"), {"format": hub.PROJECT_FORMAT, "version": 1,
                      "owner": {"name": "Pat Owner", "email": "pat@example.com"}})
    monkeypatch.setattr(hub.st, "session_state", {})
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent.append(("connect", host, port))
        def starttls(self, context=None):
            sent.append(("tls",))
        def login(self, u, p):
            sent.append(("login", u))
        def send_message(self, m):
            sent.append(("msg", m["To"], m["Subject"], m.get_content()))
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    ran = []
    monkeypatch.setattr(hub.threading if hasattr(hub, "threading") else __import__("threading"),
                        "Thread", lambda target, args, daemon: type(
                            "T", (), {"start": lambda self: ran.append(target(*args))})())
    # No sender configured: nothing is sent.
    monkeypatch.delenv("OTDR_MAIL_SENDER", raising=False)
    hub.project_log(str(work), "Report", "Splice Report run: x.xlsx")
    assert sent == []
    monkeypatch.setenv("OTDR_MAIL_SENDER", json.dumps(
        {"host": "smtp.example.com", "port": 587, "user": "otdr", "password": "pw",
         "from": "otdr-suite@example.com"}))
    hub.project_log(str(work), "Report", "Splice Report run: y.xlsx")
    msgs = [x for x in sent if x[0] == "msg"]
    assert ("connect", "smtp.example.com", 587) in sent and ("login", "otdr") in sent
    assert len(msgs) == 1 and msgs[0][1] == "pat@example.com"
    assert "Splice Report run: y.xlsx" in msgs[0][2] and "Done by:" in msgs[0][3]
    # A file found in the folder emails too; the first look's backfill does not.
    _touch(work / "Field" / "photo.jpg")
    hub.project_scan(str(work))
    assert len([x for x in sent if x[0] == "msg"]) == 2
