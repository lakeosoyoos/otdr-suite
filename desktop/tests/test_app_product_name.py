"""The App's product is "OTDR App" (Robert, 2026-10-01).

  "everything people see": the window, the page and its titles, the sidebar
  footer, the installer, the exe, the release asset and the file-type names
  say OTDR App.  What a person never sees keeps the old spelling, so nothing
  installed moves and main's code keeps merging: the app folder
  ~/.otdrSuiteApp, the OTDR_SUITE_* variables, the update channel "app",
  the Inno AppId and the ProgID ids.

    launcher     EDITION "OTDR App" is the window title and is handed to the
                 hub and the engines as OTDR_SUITE_EDITION
    hub          PRODUCT_NAME follows OTDR_SUITE_EDITION; unset (the regular
                 exe) it is "OTDR Suite", so main's screens are unchanged
    Viewer       the trace server marks the page with the product's name
    installer    OTDRApp-Setup.exe installs OTDRApp.exe as "OTDR App", same
                 AppId; an install under the old name is cleared
"""
from __future__ import annotations

import json
import re
import socket
import threading
import urllib.request

import pytest

from conftest import REPO_ROOT, run_streamlit, import_trace_server

DESKTOP = REPO_ROOT / "desktop"
LAUNCHER = (DESKTOP / "launcher.py").read_text(encoding="utf-8")
ISS = (DESKTOP / "OTDRSuite.iss").read_text(encoding="utf-8")
SPEC = (DESKTOP / "OTDRSuite.spec").read_text(encoding="utf-8")
CI = (REPO_ROOT / ".github" / "workflows" / "build-windows.yml").read_text(encoding="utf-8")
APP = "OTDR App"


# ── the packaging ────────────────────────────────────────────────────────

def test_the_launcher_names_the_product_and_keeps_its_folder():
    assert 'EDITION      = "OTDR App"' in LAUNCHER
    assert 'WINDOW_TITLE = EDITION' in LAUNCHER
    assert 'os.environ["OTDR_SUITE_EDITION"] = EDITION' in LAUNCHER
    # Hidden names stay, so nothing installed moves.
    assert 'APP_DIR_NAME = ".otdrSuiteApp"' in LAUNCHER
    assert 'UPDATE_CHANNEL' in LAUNCHER and '"app"' in LAUNCHER
    assert 'path="OTDRApp-Setup.exe"' in LAUNCHER


def test_the_installer_is_otdr_app_under_the_same_appid():
    assert '#define AppName     "OTDR App"' in ISS
    assert '#define AppExeName  "OTDRApp.exe"' in ISS
    assert 'AppId={{90A888DE-8422-4311-885D-74C12BE36AC2}' in ISS
    assert 'OutputBaseFilename=OTDRApp-Setup' in ISS
    assert 'DefaultDirName={autopf}\\{#AppName}' in ISS
    assert 'Source: "dist\\OTDRApp\\*"' in ISS
    # An install made under the old name moves to the new folder and
    # Start-menu name, and what the old name left is cleared.
    assert 'UsePreviousAppDir=no' in ISS and 'UsePreviousGroup=no' in ISS
    for old in ('Name: "{autopf}\\OTDR Suite App"',
                'Name: "{autoprograms}\\OTDR Suite App"',
                'Name: "{autodesktop}\\OTDR Suite App.lnk"'):
        assert old in ISS, old
    # File types: the ids stay, the text a person sees is the product's.
    assert ('Subkey: "Software\\Classes\\OTDRSuiteApp.zdb"; ValueType: string; '
            'ValueName: ""; ValueData: "{#AppName} Project"') in ISS


def test_the_exe_and_the_build_files_are_otdr_app():
    assert 'APP_NAME  = "OTDRApp"' in SPEC
    assert 'dist\\OTDRSuite\\' not in CI and 'OTDRSuiteApp-' not in CI
    assert 'Get-Process -Name "OTDRSuite"' not in CI
    assert 'Get-Process -Name "OTDRApp"' in CI
    assert 'python frozen_selftest.py dist\\OTDRApp\\OTDRApp.exe' in CI
    assert 'name: OTDRApp-Windows' in CI
    assert '- name: Publish OTDR App release (app-release only)' in CI


# ── the hub ──────────────────────────────────────────────────────────────

@pytest.fixture
def settings_dir(tmp_path, monkeypatch):
    d = tmp_path / "settings"
    d.mkdir()
    (tmp_path / "Projects").mkdir()
    json.dump({"projects_root": str(tmp_path / "Projects")},
              open(d / "settings.json", "w", encoding="utf-8"))
    monkeypatch.setenv("OTDR_SETTINGS_DIR", str(d))
    monkeypatch.setenv("OTDR_TEST_HOME", "1")
    return d


def _seen(at):
    """Every piece of text the page shows."""
    out = []
    for group in (at.markdown, at.caption, at.title, at.header, at.subheader,
                  at.info, at.warning, at.error, at.success):
        out += [e.value for e in group]
    out += [b.label for b in at.button]
    out += [e.label for e in at.expander]
    return out


def test_the_app_says_otdr_app_on_home_and_in_quick_analysis(settings_dir, monkeypatch):
    monkeypatch.setenv("OTDR_SUITE_EDITION", APP)
    at = run_streamlit().run()
    assert not at.exception, list(at.exception)
    assert any(f"🔬 {APP}</h2>" in m.value for m in at.markdown)
    assert not [t for t in _seen(at) if "Suite" in t]
    # The top bar (2026-10-01) is on the Home screen too: its logo is the
    # product name, and its update menu names the build.
    assert at.button(key="go_home").label == APP
    at.button(key="home_traces").click().run()
    assert not at.exception, list(at.exception)
    assert at.button(key="go_home").label == APP
    assert f"{APP} · dev" in [c.value for c in at.caption]
    assert not [t for t in _seen(at) if "Suite" in t]


def test_the_sample_span_events_name_the_app(settings_dir, monkeypatch):
    monkeypatch.setenv("OTDR_SUITE_EDITION", APP)
    at = run_streamlit().run()
    at.button(key="home_demo").click().run()
    assert not at.exception, list(at.exception)
    assert at.session_state["app_mode"] == "project"
    assert not [t for t in _seen(at) if "Suite" in t]
    # Import the hub with the edition unset: a first import here would fix
    # PRODUCT_NAME at "OTDR App" for every later test in this process.
    with monkeypatch.context() as m:
        m.delenv("OTDR_SUITE_EDITION", raising=False)
        import app
    monkeypatch.setattr(app, "PRODUCT_NAME", APP)
    assert app._how_shown("OTDR Suite") == APP                # the log's own value
    assert app._how_shown(None) == APP
    assert app._how_shown(None, "") == ""
    assert app._how_shown("Found in folder") == "Found in folder"


def test_the_regular_exe_still_says_otdr_suite(settings_dir, monkeypatch):
    monkeypatch.delenv("OTDR_SUITE_EDITION", raising=False)
    at = run_streamlit().run()
    assert not at.exception, list(at.exception)
    assert any("🔬 OTDR Suite</h2>" in m.value for m in at.markdown)


# ── the Viewer, the Field Capture page and the share-file messages ───────

def _get(url_fn):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    srv, port = url_fn(free)
    th = threading.Thread(target=srv.handle_request, daemon=True)
    th.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
            return r.read().decode("utf-8")
    finally:
        th.join(5)
        srv.server_close()


def test_the_viewer_page_is_marked_with_the_product(monkeypatch):
    T = import_trace_server()
    monkeypatch.setitem(T.CONFIG, "theme", "light")
    monkeypatch.setattr(T, "PRODUCT_NAME", APP)
    page = _get(lambda p: T.find_free_port(p, count=20))
    assert f'<html data-product="{APP}" lang="en">' in page
    # The page reads the mark for every place it names the product.
    assert "document.documentElement.dataset.product || 'OTDR Suite'" in page
    js = page.split("dataset.product || 'OTDR Suite';", 1)[1]
    # No quoted text of the page's own names the old product (comments may).
    assert not re.search(r"""[`'">]OTDR Suite""", js), re.findall(r""".{40}[`'">]OTDR Suite.{20}""", js)
    monkeypatch.setattr(T, "PRODUCT_NAME", "OTDR Suite")
    assert '<html lang="en">' in _get(lambda p: T.find_free_port(p, count=20))


def test_share_file_messages_name_the_product(monkeypatch):
    import folder_intake as fi
    monkeypatch.setenv("OTDR_SUITE_EDITION", APP)
    assert str(fi.ShareFileError("x.zdb is not an OTDR Suite file.")) == \
        f"x.zdb is not an {APP} file."
    monkeypatch.delenv("OTDR_SUITE_EDITION")
    assert "OTDR Suite" in str(fi.ShareFileError("not an OTDR Suite file"))


def test_the_report_stamp_names_the_product(monkeypatch):
    import importlib, sys
    sys.path.insert(0, str(REPO_ROOT / "splicereport"))
    aa = importlib.import_module("acquisition_audit")
    monkeypatch.setenv("OTDR_SUITE_EDITION", APP)
    assert aa.engine_stamp_text().startswith(f"{APP} · ")
    monkeypatch.delenv("OTDR_SUITE_EDITION")
    assert aa.engine_stamp_text().startswith("OTDR Suite · ")
    # The frozen self-test lifts the stamp under either name.
    st = (DESKTOP / "frozen_selftest.py").read_text(encoding="utf-8")
    assert re.search(r'STAMP_RE = re\.compile\(r"\^OTDR \(Suite\|App\)', st)


def test_the_field_capture_page_names_the_product(monkeypatch):
    import fieldcapture.server as fc
    monkeypatch.setattr(fc, "PRODUCT_NAME", APP)
    page = _get(lambda p: fc.find_free_port(p, count=20))
    assert f"Scan the job's QR code from {APP}" in page
    assert "OTDR Suite" not in page
