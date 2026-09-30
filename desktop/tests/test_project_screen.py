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
    root = tmp_path / "SITEA-SITEB"
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



# ── who did it (Robert, 2026-09-29: "a user column that shows who took what action")
def test_every_event_says_who_did_it(hub, tmp_path, monkeypatch):
    work = tmp_path / "Job"
    work.mkdir()
    hub.project_scan(str(work))
    monkeypatch.setattr(hub, "current_user", lambda: "jtech")
    monkeypatch.setattr(hub, "_file_owner", lambda p: "owner.of.file")
    p = _touch(work / "FQA" / "pkg.xlsm")
    hub.project_log(str(work), "FQA", "FQA package built: pkg.xlsm", [str(p)])
    # A file saved into the folder by hand is put down to the login that owns it.
    _touch(work / "Pictures" / "A end" / "box.jpg")
    hub.project_scan(str(work))
    ev = hub.events_read(str(work))["events"]
    assert [(e["kind"], e["user"]) for e in ev] == [("FQA", "jtech"), ("Photo", "owner.of.file")]
    assert hub.file_users(str(work)) == {"FQA/pkg.xlsm": "jtech",
                                         "Pictures/A end/box.jpg": "owner.of.file"}


def test_a_report_is_put_down_to_who_ran_it_not_who_found_it(hub, tmp_path, monkeypatch):
    """The folder scan that logs a report may run later on another PC (a
    shared job folder): the runner is recorded when the run starts."""
    work = tmp_path / "Job"
    work.mkdir()
    hub.project_scan(str(work))
    monkeypatch.setattr(hub.st, "session_state",
                        {"app_mode": "project", "project_path": str(work / "Job.otdrproj")})
    monkeypatch.setattr(hub, "current_user", lambda: "runner")
    out = hub._project_run_path(str(work / "Reports"), "X_SpliceReport.xlsx")
    _touch(__import__("pathlib").Path(out))
    monkeypatch.setattr(hub, "current_user", lambda: "someone.else")
    monkeypatch.setattr(hub, "_file_owner", lambda p: "someone.else")
    assert hub.project_scan(str(work)) == 1
    data = hub.events_read(str(work))
    assert data["events"][-1]["user"] == "runner"
    assert data["made_by"] == {}                 # used once


def test_an_old_event_log_without_users_still_reads(hub, tmp_path):
    work = tmp_path / "Job"
    work.mkdir()
    (work / "Project Events.json").write_text(json.dumps({"events": [
        {"when": 1, "kind": "Project", "text": "made", "how": "OTDR Suite", "files": ["x"]}],
        "known": {}}), encoding="utf-8")
    assert hub.file_users(str(work)) == {}


def test_the_owner_of_a_file_is_a_login_or_blank(hub, tmp_path):
    p = _touch(tmp_path / "f.txt")
    who = hub._file_owner(str(p))
    assert isinstance(who, str)
    if os.name != "nt":
        assert who == hub.current_user()
    assert hub._file_owner(str(tmp_path / "missing.txt")) == ""


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
    # "<A site> to <B site>", as read from the traces (no site name written here).
    name = at.text_input(key="setup_name").value
    next(b for b in at.button if b.label == "Create project").click().run()
    assert not at.exception, list(at.exception)
    return tmp_path / "Projects" / name


def test_the_project_screen_has_the_overview_and_six_tabs(settings_dir, span_dir, tmp_path):
    at = run_streamlit().run()
    work = _new_project(at, span_dir, tmp_path)
    assert [t.label for t in at.tabs] == ["Events", "Traces", "Reports", "Pictures", "GPS",
                                          "Audit FQA", "Export Project"]
    text = " ".join(m.value for m in at.markdown)
    site_a, site_b = work.name.split(" to ")
    assert f"**{site_a} → {site_b}**" in text and "**Shot 2026-05-06**" in text
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
    # Each says who did it, and the Events tab shows it in a User column.
    import app as _app
    assert {e["user"] for e in ev} == {_app.current_user()}
    table = at.dataframe[0].value
    assert list(table.columns) == ["When", "User", "Type", "What Happened", "How"]
    assert set(table["User"]) == {_app.current_user()}
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
    # Three shoots of the 24-fibre traces (first, reshoot, final).
    assert len(list((work / "Traces").glob("*/A/*.sor"))) == 72
    assert len(list((work / "Traces").glob("*/A"))) == 3
    import app
    pkgs = app.collect_capture_packages(str(work / "Field"))
    assert pkgs and pkgs[0][1]["job"] == "demo0001"
    rows = app.project_gps_rows(app._read_prod(app.project_production_sheet(str(work))),
                                pkgs, {})
    phone = [r for r in rows if isinstance(r["key"], int)]
    # Field Capture missed two points: the sample types one in by hand.
    assert sum(1 for r in phone if r["from"] == "phone") == len(phone) - 2
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


# ── Quick Analysis: straight to the Suite screen (2026-09-30) ────────────
# Robert: "remove the intermediate screen in Quick Analysis ... take us to
# the OTDR Suite home screen".  The left panel's Trace Folders hold the
# traces (the A and B boxes, Clear Traces); the tool list picks the tool.
def _side_box(at, key):
    return next(t for t in at.sidebar.text_input if t.key == key)


def _tool(at, name):
    next(r for r in at.sidebar.radio if r.label == "Tool").set_value(name).run()
    assert not at.exception, list(at.exception)
    return at


def _qa_loaded(span_dir):
    at = run_streamlit().run()
    at.button(key="home_traces").click().run()
    assert not at.exception, list(at.exception)
    assert "qa_stage" not in at.session_state
    _side_box(at, "view_dir_a_input").set_value(str(span_dir / "A")).run()
    _side_box(at, "view_dir_b_input").set_value(str(span_dir / "B")).run()
    return _tool(at, "Splice Report")


def test_quick_analysis_opens_on_the_suite_screen_with_nothing_loaded(settings_dir):
    at = run_streamlit().run()
    at.button(key="home_traces").click().run()
    assert not at.exception, list(at.exception)
    assert "qa_stage" not in at.session_state
    tool = next(r for r in at.sidebar.radio if r.label == "Tool")
    assert tool.options == ["Viewer", "Splice Report", "Unidirectional", "Secret Sauce"]
    assert "##### Trace Folders" in [m.value for m in at.sidebar.markdown]
    assert any(e.label == "☁️ From SharePoint" for e in at.sidebar.expander)
    assert not any(b.key in ("qa_load", "qa_reload", "qa_continue") for b in at.button)
    for name in ("Splice Report", "Unidirectional", "Secret Sauce"):
        _tool(at, name)                           # a tool page shows with no traces


def test_every_tool_runs_on_the_left_panels_traces(settings_dir, span_dir):
    at = _qa_loaded(span_dir)
    for name, heading in (("Unidirectional", "Unidirectional"),
                          ("Secret Sauce", "Secret Sauce"),
                          ("Splice Report", "Bidirectional Splice Report")):
        _tool(at, name)
        text = " ".join(m.value for m in at.markdown)
        assert heading in text, name
        assert any("loaded in the left panel" in c.value for c in at.main.caption), name
        # The panel holds the traces: no folder picking on the page.
        labels = {b.label for b in at.main.button}
        assert not {"📁 Browse for folder", "📂 A-direction folder"} & labels, name
        assert not [t for t in at.main.text_input
                    if t.key in ("uni_folder_input", "ss_folder_input", "view_dir_a_input")]


def test_a_direction_changed_in_the_left_panel_is_what_the_tools_run_on(
        settings_dir, span_dir, tmp_path):
    import shutil
    at = _qa_loaded(span_dir)
    b2 = tmp_path / "B again"
    shutil.copytree(span_dir / "B", b2)
    _side_box(at, "view_dir_b_input").set_value(str(b2)).run()
    assert not at.exception, list(at.exception)
    _tool(at, "Unidirectional")
    run_on = next(r for r in at.main.radio if r.label == "Run On")
    run_on.set_value("B folder").run()
    assert not at.exception, list(at.exception)
    assert at.session_state["view_dir_b_input"] == str(b2)
    assert at.session_state["uni_panel_side"] == "B folder"


def test_clear_traces_in_quick_analysis_empties_the_panel(settings_dir, span_dir):
    at = _qa_loaded(span_dir)
    next(b for b in at.sidebar.button if b.label == "Clear Traces").click().run()
    next(b for b in at.get("dialog")[0].button if b.label == "Allow").click().run()
    assert not at.exception, list(at.exception)
    assert at.session_state["app_mode"] == "traces"   # still the Suite screen
    assert _side_box(at, "view_dir_a_input").value == ""
    assert _side_box(at, "view_dir_b_input").value == ""


def test_quick_analysis_home_and_back_keeps_the_traces(settings_dir, span_dir):
    at = _qa_loaded(span_dir)
    at.button(key="go_home").click().run()
    at.button(key="home_traces").click().run()
    assert not at.exception, list(at.exception)
    assert _side_box(at, "view_dir_a_input").value == str(span_dir / "A")
    assert _side_box(at, "view_dir_b_input").value == str(span_dir / "B")



# ── Quick Analysis starts fresh after a project (2026-09-30) ─────────────
CARRY_TITLE = "Thresholds Carried Over from Previous Tool"
DEFAULT_PROFILE = "Default (engine baseline)"


def _carry_popup(at):
    return [d for d in at.get("dialog") if d.proto.dialog.title == CARRY_TITLE]


def _qa_after_the_sample_span(tmp_path, settings_dir):
    """Quick Analysis on a tool, Home, the Sample Span (a project with a
    customer profile), Home, then Quick Analysis again."""
    (tmp_path / "Projects").mkdir()
    json.dump({"projects_root": str(tmp_path / "Projects")},
              open(settings_dir / "settings.json", "w", encoding="utf-8"))
    at = run_streamlit().run()
    at.button(key="home_traces").click().run()
    _tool(at, "Unidirectional")
    at.button(key="go_home").click().run()
    at.button(key="home_demo").click().run()
    at.run()
    assert not at.exception, list(at.exception)
    assert at.session_state["app_mode"] == "project"
    assert at.session_state["otdr_profile"] != DEFAULT_PROFILE   # the sample's customer
    at.button(key="go_home").click().run()
    at.button(key="home_traces").click().run()
    assert not at.exception, list(at.exception)
    return at


def test_quick_analysis_after_a_project_has_no_carried_over_popup(settings_dir, tmp_path):
    at = _qa_after_the_sample_span(tmp_path, settings_dir)
    assert not _carry_popup(at)
    # A change of tool inside Quick Analysis still asks.
    _tool(at, "Splice Report")
    assert _carry_popup(at)


def test_quick_analysis_after_a_project_opens_on_the_default_profile(settings_dir, tmp_path):
    import app
    at = _qa_after_the_sample_span(tmp_path, settings_dir)
    _tool(at, "Splice Report")
    assert at.session_state["otdr_profile"] == DEFAULT_PROFILE
    assert at.session_state["otdr_profile_select"] == DEFAULT_PROFILE
    assert (dict(at.session_state["otdr_settings"])
            == app._otdr_settings_from_profile(DEFAULT_PROFILE))


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


# ── customer: green check until the settings are changed (2026-09-27) ────
def test_customer_check_turns_to_an_orange_x_when_the_settings_change(settings_dir, span_dir):
    at = _qa_loaded(span_dir)                     # Splice Report tab, loaded traces
    # Picking a customer reloads the page (st.rerun); AppTest does not
    # replay that, so each pick is followed by a run of our own.
    at.selectbox(key="otdr_profile_select").set_value("Lumen").run()
    at.run()
    assert not at.exception, list(at.exception)
    text = lambda: " ".join(m.value for m in at.markdown)
    assert ":green[**✅**]" in text() and "Settings changed" not in text()
    assert "color:#8a939e" in text()              # Settings name greyed
    # The tech changes a threshold: an orange X, and the name goes black.
    s = dict(at.session_state["otdr_settings"])
    key = next(iter(s))
    s[key] = dict(s[key], fail=float(s[key]["fail"]) + 0.5)
    at.session_state["otdr_settings"] = s
    at.run()
    assert "✖ Settings changed" in text() and ":green[**✅**]" not in text()
    assert "color:#000" in text()
    # Default is not a customer: no mark at all.
    at.selectbox(key="otdr_profile_select").set_value("Default (engine baseline)").run()
    at.run()
    assert ":green[**✅**]" not in text() and "Settings changed" not in text()


def test_the_sample_span_takes_real_photos_from_the_app_folder(hub, tmp_path, monkeypatch):
    """An installed build has no demo/private_photos: photos copied into the
    app folder's sample_photos stand in for the drawn ones."""
    import io
    from PIL import Image
    app_dir = tmp_path / "app"
    (app_dir / "sample_photos").mkdir(parents=True)
    for n in ("A-1.jpg", "A-2.jpg", "Z-1.jpg", "Z-2.jpg"):
        b = io.BytesIO()
        Image.new("RGB", (32, 24), (1, 2, 3)).save(b, "JPEG")
        (app_dir / "sample_photos" / n).write_bytes(b.getvalue())
    monkeypatch.setenv("OTDR_SUITE_APP_DIR", str(app_dir))
    demo = tmp_path / "demo"                  # a demo/ with no private_photos
    demo.mkdir()
    shutil.copy2(os.path.join(hub.DEMO_DIR, "Demo Field Capture.zfc"), demo)
    monkeypatch.setattr(hub, "DEMO_DIR", str(demo))
    dest = tmp_path / "cap.zfc"
    hub._demo_capture(str(dest))
    import folder_intake as fi
    sf = fi.share_open(str(dest), expect="field-capture")
    assert sf.read("photos/A-1-1.jpg") == (app_dir / "sample_photos" / "A-1.jpg").read_bytes()


def test_owner_recents_fill_the_boxes_and_saving_puts_an_owner_on_top(settings_dir, span_dir,
                                                                      tmp_path):
    json.dump({"owner_recents": [{"name": "Pat Example", "email": "pat@example.com"},
                                 {"name": "Lee Example", "email": "lee@example.com"}]},
              open(settings_dir / "settings.json", "w", encoding="utf-8"))
    at = run_streamlit().run()
    work = _new_project(at, span_dir, tmp_path)
    at = run_streamlit().run()
    next(b for b in at.button if b.label == "📂 Open Recent Project").click().run()
    at.text_input(key="home_folder").set_value(str(work)).run()
    next(b for b in at.button if b.label == "Open this folder").click().run()
    at.selectbox(key="own_recent").set_value(1).run()
    assert at.text_input(key="own_name").value == "Lee Example"
    assert at.text_input(key="own_email").value == "lee@example.com"
    at.button(key="own_save").click().run()
    assert at.session_state["project_owner"] == {"name": "Lee Example", "email": "lee@example.com"}
    saved = json.loads((settings_dir / "settings.json").read_text(encoding="utf-8"))
    assert [r["email"] for r in saved["owner_recents"]] == ["lee@example.com", "pat@example.com"]
