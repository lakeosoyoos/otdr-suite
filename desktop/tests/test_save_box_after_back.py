"""The "Save reports to" box shows what it holds, on the Splice Report and
Unidirectional pages, after every way back to the page.

The box on screen only shows a value written in the run that draws it.  A
report-cell click starts a new session that takes the box's folder with it
(the settings carry-over), filed on the Viewer page; on "← Back" the page
drew the box with that folder on the server and an EMPTY box on screen, and
the next keystroke or run sent the empty box back (2026-10-01).  So these
tests look at what the browser is told (the element's value when the run
sets it, else its default), not only at the server's slot.

Also pinned: a Viewer trip and Clear Traces keep the box, and a
Unidirectional run into a folder that does not exist yet makes it, as the
Splice Report already did (it failed with FileNotFoundError).
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from conftest import (run_streamlit, go_tab, clear_traces, SPLICEREPORT_DIR,
                      FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR)

A, B = str(FIXTURE_SPLICE_A_DIR), str(FIXTURE_SPLICE_B_DIR)
PAGES = [("Splice Report", "sr"), ("Unidirectional", "uni")]


@pytest.fixture(autouse=True)
def _own_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("OTDR_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("OTDR_SETTINGS_DIR", str(tmp_path / "settings"))


def _box(at):
    return next(t for t in at.main.text_input if t.label == "Save Reports To")


def _on_screen(at):
    """What the browser shows in the box after this run."""
    p = _box(at).proto
    return p.value if p.set_value else p.default


def _open(at, page):
    go_tab(at, page)
    assert not at.exception, at.exception
    return at


def _hub():
    at = run_streamlit(default_timeout=180)
    at.session_state["view_dir_a_input"] = A
    at.session_state["view_dir_b_input"] = B
    at.run()
    assert not at.exception, at.exception
    return at


def _typed(page, keep):
    at = _open(_hub(), page)
    _box(at).input(keep).run()
    assert not at.exception, at.exception
    assert _on_screen(at) == keep
    return at


@pytest.mark.parametrize("page,prefix", PAGES)
def test_back_from_a_cell_click_shows_the_folder(tmp_path, page, prefix):
    keep = str(tmp_path / "my reports")
    at = _typed(page, keep)
    view = run_streamlit(default_timeout=180)
    for k, v in {"nav": "viewer", "fiber": "3", "km": "1.0", "dir": "a",
                 "sra": A, "srb": B, "src": prefix,
                 "cs": at.session_state["_carry_id"]}.items():
        view.query_params[k] = v
    view.run()
    assert not view.exception, view.exception
    view.run()                                   # the Viewer page draws again
    next(b for b in view.button if b.label == f"← Back to {page}").click().run()
    assert not view.exception, view.exception
    assert view.session_state["nav_radio"] == page
    assert _on_screen(view) == keep
    view.run()
    assert _on_screen(view) == keep
    assert view.session_state[f"{prefix}_report_dest"] == keep


def test_the_other_page_s_folder_survives_a_cell_click_too(tmp_path):
    """The Splice Report's box is not on screen when a Unidirectional cell
    is clicked: its folder rides the click in the box's kept copy."""
    keep_sr, keep_uni = str(tmp_path / "sr reports"), str(tmp_path / "uni reports")
    at = _typed("Splice Report", keep_sr)
    _open(at, "Unidirectional")
    _box(at).input(keep_uni).run()
    view = run_streamlit(default_timeout=180)
    for k, v in {"nav": "viewer", "fiber": "3", "km": "1.0", "dir": "a",
                 "sra": A, "src": "uni", "pa": A, "pb": B,
                 "cs": at.session_state["_carry_id"]}.items():
        view.query_params[k] = v
    view.run()
    next(b for b in view.button if b.label == "← Back to Unidirectional").click().run()
    assert _on_screen(view) == keep_uni
    _open(view, "Splice Report")
    assert _on_screen(view) == keep_sr


@pytest.mark.parametrize("page,prefix", PAGES)
def test_a_viewer_trip_keeps_the_folder(tmp_path, page, prefix):
    keep = str(tmp_path / "my reports")
    at = _typed(page, keep)
    _open(at, "Viewer")
    _open(at, page)
    assert _on_screen(at) == keep


@pytest.mark.parametrize("page,prefix", PAGES)
def test_clear_traces_keeps_the_folder(tmp_path, page, prefix):
    """Clear Traces clears traces and reports; where reports are saved is a
    choice of the tech's, not part of the span."""
    keep = str(tmp_path / "my reports")
    at = _typed(page, keep)
    clear_traces(at, allow=True)                 # on the Traces tab
    assert not at.exception, at.exception
    assert at.session_state["view_dir_a_input"] == ""
    _open(at, page)
    _hub_again = at
    _hub_again.session_state["view_dir_a_input"] = A
    _hub_again.session_state["view_dir_b_input"] = B
    _hub_again.run()
    assert _on_screen(_hub_again) == keep


def test_a_unidirectional_run_makes_a_new_save_folder(tmp_path):
    out = tmp_path / "not there yet" / "unidirectional_events.xlsx"
    p = subprocess.run(
        [sys.executable, str(SPLICEREPORT_DIR / "run_splicereport.py"), "--uni",
         "--dir-a", A, "--out", str(out), "--analysis", "suite"],
        capture_output=True, text=True)
    man = json.loads(p.stdout.strip().splitlines()[-1])
    assert man.get("ok"), man
    assert out.exists()
