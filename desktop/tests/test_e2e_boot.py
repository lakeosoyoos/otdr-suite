"""
End-to-end / boot tests for the OTDR Suite hub.

Closes the "UI calls the engine differently than expected" class of bug at
the top level: both hub pages must render without an exception, and the
Duplicate-Check engine path the UI invokes must produce a real report on a
real fixture.
"""
from __future__ import annotations

from conftest import run_streamlit, run_secretsauce, mixed_fixture_dir


# ── Page render (AppTest) ────────────────────────────────────────────────
def test_viewer_page_renders():
    at = run_streamlit().run()
    # AppTest.exception is an ElementList; empty == no exception (NOT None).
    assert not at.exception, f"Viewer page raised: {list(at.exception)}"


def test_duplicate_check_page_renders():
    at = run_streamlit().run()
    at.sidebar.radio[0].set_value("Duplicate Check").run()
    assert not at.exception, f"Duplicate Check page raised: {list(at.exception)}"


def test_splice_report_page_renders():
    at = run_streamlit().run()
    at.sidebar.radio[0].set_value("Splice Report").run()
    assert not at.exception, f"Splice Report page raised: {list(at.exception)}"


def test_grid_cell_click_switches_to_viewer():
    """A Splice Report cell click arrives as ?nav=viewer&fiber=&km=; the hub
    must switch to the Viewer page (which then deep-links the iframe)."""
    at = run_streamlit()
    at.query_params["nav"] = "viewer"
    at.query_params["fiber"] = "9"
    at.query_params["km"] = "32.537"
    at.run()
    assert not at.exception, f"nav raised: {list(at.exception)}"
    assert at.session_state["nav_radio"] == "Viewer", "click did not switch to the Viewer page"


# ── Engine path the Duplicate-Check UI invokes (subprocess, real fixture) ─
def test_secretsauce_e2e_produces_xlsx(tmp_path):
    folder = mixed_fixture_dir(tmp_path)
    out_dir = tmp_path / "out"
    rc, manifest, stderr = run_secretsauce(folder, out_dir, "xlsx")
    assert rc == 0, f"runner exited {rc}; stderr tail:\n{stderr[-1500:]}"
    assert manifest is not None, "runner printed no JSON manifest"
    assert manifest.get("ok") is True, f"manifest not ok: {manifest}"
    written = manifest.get("written", [])
    assert written, "no reports written"
    for w in written:
        from pathlib import Path
        assert Path(w["path"]).exists(), f"missing output file {w['path']}"
        assert w["path"].endswith(".xlsx")
    # 8 SOR (4+4) → the GenParams split yields >=1 direction group of >=2.
    assert manifest["counts"]["sor"] == 8
