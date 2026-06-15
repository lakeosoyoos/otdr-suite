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


def test_pair_click_switches_to_viewer_with_multifiber_target(tmp_path):
    """A Duplicate Check pair click arrives as ?nav=viewer&fibers=1,2&dir=a; the
    hub must switch to the Viewer page AND stash a multi-fiber target so the
    iframe overlays BOTH fibers.  The ssfolder is pushed into the A-dir slot."""
    ssfolder = tmp_path / "ssfolder"
    ssfolder.mkdir()
    at = run_streamlit()
    at.query_params["nav"] = "viewer"
    at.query_params["fibers"] = "1,2"
    at.query_params["dir"] = "a"
    at.query_params["ssfolder"] = str(ssfolder)
    at.run()
    assert not at.exception, f"pair nav raised: {list(at.exception)}"
    assert at.session_state["nav_radio"] == "Viewer", "pair click did not switch to Viewer"
    # viewer_target is consumed (popped) by page_viewer() in this same run when
    # it builds the iframe URL — so assert on the signals that survive: the
    # A-dir folder was pointed at the Secret Sauce folder (the wrinkle), and the
    # came-from-dupcheck flag (drives the Back button) is set.
    assert at.session_state["view_dir_a_input"] == str(ssfolder)
    assert "came_from_dupcheck" in at.session_state
    assert at.session_state["came_from_dupcheck"] is True


def test_pair_click_back_button_returns_to_dupcheck(tmp_path):
    """The Viewer shows a Back-to-Duplicate-Check button after a pair click."""
    ssfolder = tmp_path / "ssfolder2"
    ssfolder.mkdir()
    at = run_streamlit()
    at.query_params["nav"] = "viewer"
    at.query_params["fibers"] = "1,2"
    at.query_params["dir"] = "a"
    at.query_params["ssfolder"] = str(ssfolder)
    at.run()
    assert not at.exception
    labels = [b.label for b in at.button]
    assert any("Back to Duplicate Check" in lbl for lbl in labels), labels


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
