"""The product's name comes from one place (2026-10-01).

OTDR App (the app-window edition) is called "OTDR App" wherever a person
reads its name.  Its launcher hands the name down as OTDR_SUITE_EDITION; the
hub, the Viewer page, the Field Capture page and the report stamp read it
from there.  The regular exe leaves it unset, so everything here still says
"OTDR Suite", exactly as before.
"""
from __future__ import annotations

import socket
import threading
import urllib.request

from conftest import REPO_ROOT, run_streamlit, import_trace_server

APP = "OTDR App"


def _logo(at):
    """The name at the left of the top bar (it was the sidebar's heading).
    OTDR App: the logo is the Home button (key go_home)."""
    return at.button(key="go_home").label


def _bar(at):
    """Every place the top bar names the product: the logo, and the build
    line in its update menu (it was the sidebar's footer)."""
    (menu,) = at.get("popover")
    return [_logo(at)] + [c.value for c in menu.caption]


def test_unset_the_hub_says_otdr_suite(monkeypatch):
    monkeypatch.delenv("OTDR_SUITE_EDITION", raising=False)
    at = run_streamlit().run()
    assert not at.exception, list(at.exception)
    assert _logo(at) == "OTDR Suite"
    bar = _bar(at)
    assert any(t.startswith("OTDR Suite · ") for t in bar)
    assert not [t for t in bar if APP in t]


def test_the_edition_names_the_hub(monkeypatch):
    monkeypatch.setenv("OTDR_SUITE_EDITION", APP)
    at = run_streamlit().run()
    assert not at.exception, list(at.exception)
    assert _logo(at) == APP
    bar = _bar(at)
    assert any(t.startswith(f"{APP} · ") for t in bar)
    assert not [t for t in bar if "OTDR Suite" in t]


def _get(make):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    srv, port = make(free)
    th = threading.Thread(target=srv.handle_request, daemon=True)
    th.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
            return r.read().decode("utf-8")
    finally:
        th.join(5)
        srv.server_close()


def test_the_viewer_page_is_marked_only_for_another_name(monkeypatch):
    T = import_trace_server()
    monkeypatch.setitem(T.CONFIG, "theme", "light")
    monkeypatch.setattr(T, "PRODUCT_NAME", "OTDR Suite")
    assert '<html lang="en">' in _get(lambda p: T.find_free_port(p, count=20))
    monkeypatch.setattr(T, "PRODUCT_NAME", APP)
    page = _get(lambda p: T.find_free_port(p, count=20))
    assert f'<html data-product="{APP}" lang="en">' in page
    assert "document.documentElement.dataset.product || 'OTDR Suite'" in page


def test_the_field_capture_page_follows_the_name(monkeypatch, tmp_path):
    import fieldcapture.server as fc
    page = tmp_path / "index.html"
    page.write_text("<p>Scan the QR code from OTDR Suite.</p>", encoding="utf-8")
    monkeypatch.setattr(fc, "resolve_web_file", lambda rel: page)
    monkeypatch.setattr(fc, "PRODUCT_NAME", "OTDR Suite")
    assert "QR code from OTDR Suite" in _get(lambda p: fc.find_free_port(p, count=20))
    monkeypatch.setattr(fc, "PRODUCT_NAME", APP)
    got = _get(lambda p: fc.find_free_port(p, count=20))
    assert f"QR code from {APP}" in got and "OTDR Suite" not in got


def test_the_report_stamp_follows_the_name(monkeypatch):
    import importlib, sys
    sys.path.insert(0, str(REPO_ROOT / "splicereport"))
    aa = importlib.import_module("acquisition_audit")
    monkeypatch.delenv("OTDR_SUITE_EDITION", raising=False)
    assert aa.engine_stamp_text().startswith("OTDR Suite · ")
    monkeypatch.setenv("OTDR_SUITE_EDITION", APP)
    assert aa.engine_stamp_text().startswith(f"{APP} · ")
