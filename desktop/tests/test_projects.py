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


def test_default_save_path_is_beside_the_traces_named_after_the_sites(hub, span_dir):
    snap = {"spans": [{"mode": "two", "dir_a": str(span_dir / "A"),
                       "dir_b": str(span_dir / "B"), "folder": "",
                       "site_a": "WSC", "site_b": "SUI"}]}
    assert hub.project_default_path(snap) == str(span_dir / "WSC_to_SUI.otdrproj")


def test_uploads_and_temp_copies_are_reported_as_not_coming_back(hub, tmp_path):
    import tempfile
    tmp = tempfile.mkdtemp(prefix="otdr_span_")     # the hub's own staging name
    try:
        snap = {"spans": [{"mode": "one", "folder": "", "dir_a": "", "dir_b": ""},
                          {"mode": "two", "dir_a": tmp, "dir_b": tmp, "folder": ""}]}
        why = hub.project_unsaveable(snap)
        assert any("Span 1 has no folder" in w and "dropped upload" in w for w in why)
        assert any("Span 2 points at a temporary copy" in w for w in why)
        # An ordinary folder that merely lives under the temp dir is fine.
        assert hub.project_unsaveable({"spans": [{"mode": "two", "dir_a": str(tmp_path),
                                                  "dir_b": str(tmp_path)}]}) == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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


# ── the app: save, restart, open ────────────────────────────────────────
def test_save_then_open_in_a_fresh_session_restores_the_span(hub, settings_dir, span_dir, monkeypatch):
    a, b = str(span_dir / "A"), str(span_dir / "B")
    at = _sr_page(view_dir_a_input=a, view_dir_b_input=b)
    at.session_state["sr_site_a"] = "WSC"
    at.session_state["sr_site_b"] = "SUI"
    _button(at, "➕ Add span…").click().run()
    at.session_state["sr2_dir_a"] = b
    at.session_state["sr2_dir_b"] = a
    at.run()
    proj = str(span_dir / "WSC_to_SUI.otdrproj")
    at.session_state["project_path_input"] = proj
    at.run()
    _button(at, "Save as…").click().run()
    assert not at.exception, list(at.exception)
    assert os.path.isfile(proj)
    assert any("Project saved" in s.value for s in at.success)
    data = json.loads(open(proj, encoding="utf-8").read())
    assert [s["dir_a"]["rel"] for s in data["spans"]] == ["A", "B"]
    assert data["spans"][0]["site_a"] == "WSC"

    # "Restart": a brand-new session with nothing in it, on the Viewer.
    at2 = run_streamlit().run()
    assert not at2.exception, list(at2.exception)
    # Recent lists it; one click opens it and lands on the Splice Report.
    _button(at2, "WSC_to_SUI").click().run()
    assert not at2.exception, list(at2.exception)
    assert at2.session_state["nav_radio"] == "Splice Report"
    assert at2.session_state["view_dir_a_input"] == a
    assert at2.session_state["view_dir_b_input"] == b
    assert at2.session_state["sr_n_spans"] == 2
    assert at2.session_state["sr2_dir_a"] == b
    # The saved site names survive the page's re-derive-on-new-folders.
    assert at2.session_state["sr_site_a"] == "WSC"
    assert at2.session_state["sr_site_b"] == "SUI"
    # Freshly opened reads as saved, even after the page settled.
    at2.run()
    assert at2.session_state["project_saved"] == hub._project_snapshot(
        dict(at2.session_state.filtered_state), at2.session_state["project_saved"])
    assert not any("Unsaved" in c.value for c in at2.sidebar.caption)


def test_an_edit_marks_the_project_unsaved_and_save_clears_it(settings_dir, span_dir):
    a, b = str(span_dir / "A"), str(span_dir / "B")
    at = _sr_page(view_dir_a_input=a, view_dir_b_input=b)
    at.session_state["project_path_input"] = str(span_dir / "p.otdrproj")
    at.run()
    _button(at, "Save as…").click().run()
    assert not any("Unsaved" in c.value for c in at.sidebar.caption)
    at.session_state["sr_site_a"] = "CHANGED"
    at.run()
    assert any("Unsaved" in c.value for c in at.sidebar.caption)
    _button(at, "💾 Save").click().run()
    assert not any("Unsaved" in c.value for c in at.sidebar.caption)
    data = json.loads((span_dir / "p.otdrproj").read_text(encoding="utf-8"))
    assert data["spans"][0]["site_a"] == "CHANGED"


def test_open_from_the_path_box_with_missing_folders_warns(hub, settings_dir, tmp_path):
    proj = tmp_path / "gone.otdrproj"
    snap = {"spans": [{"mode": "two", "dir_a": str(tmp_path / "noA"),
                       "dir_b": str(tmp_path / "noB"), "folder": "",
                       "site_a": "", "site_b": ""}]}
    hub.project_write(str(proj), hub.project_to_file_data(snap, str(proj)))
    at = run_streamlit().run()
    at.session_state["project_path_input"] = str(proj)
    at.run()
    _button(at, "📂 Open…").click().run()
    assert not at.exception, list(at.exception)
    assert any("not on this machine" in w.value for w in at.sidebar.warning)


def test_close_detaches_without_touching_the_inputs(settings_dir, span_dir):
    a, b = str(span_dir / "A"), str(span_dir / "B")
    at = _sr_page(view_dir_a_input=a, view_dir_b_input=b)
    at.session_state["project_path_input"] = str(span_dir / "p.otdrproj")
    at.run()
    _button(at, "Save as…").click().run()
    _button(at, "Close").click().run()
    assert "project_path" not in at.session_state
    assert at.session_state["view_dir_a_input"] == a
    data = json.loads((settings_dir / "settings.json").read_text(encoding="utf-8"))
    assert data["last_project"] is None


def test_viewer_click_through_reattaches_the_open_project(settings_dir, span_dir):
    # The cell click into the Viewer is a URL nav that wipes session_state;
    # the project (and its profile) come back when the seeded A folder is the
    # last project's.
    a, b = str(span_dir / "A"), str(span_dir / "B")
    at = _sr_page(view_dir_a_input=a, view_dir_b_input=b)
    at.session_state["sr_site_a"] = "WSC"
    at.session_state["project_path_input"] = str(span_dir / "p.otdrproj")
    at.run()
    _button(at, "Save as…").click().run()

    at2 = run_streamlit()
    at2.query_params["nav"] = "sr"
    at2.query_params["sra"] = a
    at2.query_params["srb"] = b
    at2.run()
    assert not at2.exception, list(at2.exception)
    assert at2.session_state["project_path"] == str(span_dir / "p.otdrproj")
    assert at2.session_state["sr_site_a"] == "WSC"


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
