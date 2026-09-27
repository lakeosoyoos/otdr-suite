"""Release gaps for the always-on home screen (2026-09-24): the phone job
expander, a Recent entry whose project file is gone, the Field Capture page
inside a project, the phone test notice, first-use help, Documents lookup."""
from __future__ import annotations

import json
import os
import zipfile

import pytest

from conftest import open_in_project, run_streamlit
from test_project_status import production_sheet


@pytest.fixture
def hub():
    import app
    return app


@pytest.fixture
def settings_dir(tmp_path, monkeypatch):
    d = tmp_path / ".settings"
    d.mkdir()
    monkeypatch.setenv("OTDR_SETTINGS_DIR", str(d))
    return d


def _project_with_sheet(tmp_path, monkeypatch):
    work = tmp_path / "Span"
    (work / "Production").mkdir(parents=True)
    production_sheet(work / "Production" / "prod.xlsx")
    return work, open_in_project(work, monkeypatch)


def _phone_test_pkg(field, created="2026-09-24T15:30:00Z"):
    field.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(field / "OTDR_PhoneTest_test-ab12cd.zip", "w") as z:
        z.writestr("capture.json", json.dumps({
            "format": "otdr-capture", "v": 1, "test": True, "job": "test-ab12cd",
            "created": created, "gps": {"lat": 39.4, "lon": -102.9}}))
        z.writestr("photos/test.jpg", b"\xff\xd8\xff")


# ── the phone job expander ──────────────────────────────────────────────
def test_phone_job_defaults_to_the_hosted_address_and_shows_the_link(tmp_path, monkeypatch):
    work, at = _project_with_sheet(tmp_path, monkeypatch)
    assert not at.exception, list(at.exception)
    assert any(e.label.startswith("📱 Phone Job") for e in at.expander)
    box = at.text_input(key="field_capture_url_box")
    assert box.value == "https://field-capture.rcolbert.workers.dev"
    assert any("needs this https address" in c.value for c in at.caption)
    test_btn = at.button(key="ps_phone_test")
    assert not test_btn.disabled
    # The link is printed and the QR expander drew a picture (qrcode is here).
    assert any(c.value.startswith("https://field-capture.rcolbert.workers.dev") for c in at.code)
    assert any(e.label == "QR Code (for a Tech at This Computer)" for e in at.expander)


def test_phone_job_with_no_address_says_so_and_greys_the_test(tmp_path, monkeypatch):
    work, at = _project_with_sheet(tmp_path, monkeypatch)
    at.text_input(key="field_capture_url_box").set_value("").run()
    assert not at.exception, list(at.exception)
    assert at.button(key="ps_phone_test").disabled
    assert any("then the QR code appears here" in i.value for i in at.info)
    assert not at.code


def test_phone_job_without_a_production_sheet_asks_for_it(tmp_path, monkeypatch):
    at = open_in_project(tmp_path / "Span", monkeypatch)
    assert not at.exception, list(at.exception)
    assert any("needs the production sheet" in c.value for c in at.caption)


def test_test_phone_connection_writes_the_email(tmp_path, monkeypatch):
    work, at = _project_with_sheet(tmp_path, monkeypatch)
    import fieldcapture.email_draft as ed
    monkeypatch.setattr(ed, "open_with_default_app", lambda p: (True, ""))
    at.button(key="ps_phone_test").click().run()
    assert not at.exception, list(at.exception)
    assert any("test email is open" in s.value for s in at.success)


def test_a_phone_test_in_field_shows_received_with_its_time(tmp_path, monkeypatch, hub):
    work = tmp_path / "Span"
    _phone_test_pkg(work / "Field")
    at = open_in_project(work, monkeypatch)
    assert not at.exception, list(at.exception)
    got = [s.value for s in at.success if "Phone Test Received ✓" in s.value]
    assert got, [s.value for s in at.success]
    assert hub._phone_test_time({"created": "2026-09-24T15:30:00Z"}) in got[0]


def test_phone_test_time_reads_iso_and_tolerates_junk(hub):
    assert hub._phone_test_time({"created": "2026-09-24T15:30:00"}) == "2026-09-24 15:30"
    assert hub._phone_test_time({"created": "nonsense"}) == ""
    assert hub._phone_test_time({}) == ""


# ── Recent list ──────────────────────────────────────────────────────────
def test_a_recent_project_whose_file_was_deleted_is_not_listed(tmp_path, monkeypatch):
    work = tmp_path / "Span 9"
    at = open_in_project(work, monkeypatch)
    proj = at.session_state["project_path"]
    os.remove(proj)
    at = run_streamlit().run()
    next(b for b in at.button if b.label == "📂 Open Recent Project").click().run()
    assert not at.exception, list(at.exception)
    assert not [b for b in at.button if (b.key or "").startswith("home_recent_")]
    assert any("None yet" in c.value for c in at.caption)


# ── Field Capture page in a project ──────────────────────────────────────
def test_the_field_capture_page_opens_inside_a_project(tmp_path, monkeypatch):
    at = open_in_project(tmp_path / "Span", monkeypatch)
    at.sidebar.radio(key="nav_radio").set_value("Field Capture").run()
    assert not at.exception, list(at.exception)
    assert any("Field Capture" in m.value for m in at.markdown)


# ── first-use help + Documents ───────────────────────────────────────────
def test_home_and_new_project_explain_the_folder_layout(settings_dir, monkeypatch, hub):
    monkeypatch.setenv("OTDR_TEST_HOME", "1")
    at = run_streamlit().run()
    assert any(c.value == hub.PROJECT_LAYOUT_HELP for c in at.caption)
    for word in ("Traces (A and B)", "Production", "Field", "FQA", "Reports", "saves itself"):
        assert word in hub.PROJECT_LAYOUT_HELP
    at.button(key="home_new").click().run()
    assert not at.exception, list(at.exception)
    assert any(c.value == hub.PROJECT_LAYOUT_HELP for c in at.caption)


def test_documents_folder_off_windows_is_home_documents(hub):
    assert hub._documents_folder() == os.path.join(os.path.expanduser("~"), "Documents")


def test_home_screen_is_always_on(hub):
    src = open(hub.__file__, encoding="utf-8").read()
    assert "OTDR_HOME_SCREEN" not in src and "TOOLS_ALL" not in src
