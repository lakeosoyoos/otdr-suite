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
    next(b for b in at.button if b.label == "📁 Start Project").click().run()
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
                                          "Audit FQA"]
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
