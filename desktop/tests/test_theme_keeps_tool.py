"""Flipping the Theme switch keeps the tech on the tool they are using.

Seen 2026-10-02: on the Splice Report or Unidirectional page, flipping the
sidebar Theme switch (Dark <-> Light) sent the tech to the Viewer and opened
"Thresholds Carried Over from Previous Tool".  The reports in the session
were still there; only the Tool choice was lost, and the page did not reload.

Cause: the switch reran the page, and the next run found the theme config
changed and reran once more to repaint, before the sidebar was drawn.
Streamlit 1.50 counts a st.rerun() as a finished run and drops the state of
every widget that run did not draw, so the Tool list (and every other box
with no `{key}_saved` slot) came back at its default.  The Analysis Mode
switch reruns from the sidebar too, but with no second rerun, so it kept
the page.  Streamlit 1.64 skips that cleanup on a run stopped for a rerun,
so these tests catch the bug on 1.50 (the Mac dev run) and guard both.

Fixed twice over: the switch puts the theme in the config itself, so a flip
needs no repaint rerun; and the repaint rerun that still runs when the config
changed under the session (another tab) keeps the boxes drawn on every page.
"""
from __future__ import annotations

import pytest

from conftest import run_streamlit

TITLE = "Thresholds Carried Over from Previous Tool"


@pytest.fixture(autouse=True)
def _own_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("OTDR_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("OTDR_SETTINGS_DIR", str(tmp_path / "settings"))


@pytest.fixture(autouse=True)
def _theme_config_back():
    """The theme lives in the process-wide Streamlit config: put back what
    was there, so a Light left by one test is not another test's start."""
    from streamlit import config as cfg
    keys = ["theme." + k for k in (
        "base", "primaryColor", "backgroundColor", "secondaryBackgroundColor",
        "textColor", "borderColor", "dataframeBorderColor",
        "dataframeHeaderBackgroundColor")]
    before = {k: cfg.get_option(k) for k in keys}
    yield
    for k, v in before.items():
        try:
            if cfg.get_option(k) != v:
                cfg.set_option(k, v)
        except Exception:
            pass


def _shows(at, page):
    """The Tool list holds `page` and the browser is told nothing else: a
    radio the run does not set keeps what the tech picked, one it sets shows
    the option sent.  Streamlit 1.64 (the Windows build) sends a radio's
    value as the option's text (`raw_value`), 1.50 as its index (`value`)."""
    el = at.sidebar.radio[0]
    assert el.value == page
    p = el.proto
    if not p.set_value:
        return True
    if 'raw_value' in {f.name for f in p.DESCRIPTOR.fields}:
        return p.raw_value == page
    return list(p.options)[p.value] == page


def _ok(at):
    assert not at.exception, at.exception
    return at


def _popup(at):
    return [d for d in at.get("dialog") if d.proto.dialog.title == TITLE]


def _theme_switch(at):
    return next(t for t in at.sidebar.toggle if t.label == "Theme")


def _flip_theme(at):
    sw = _theme_switch(at)
    _ok(sw.set_value(not sw.value).run())
    return at


@pytest.mark.parametrize("page", ["Splice Report", "Unidirectional",
                                  "Secret Sauce", "Splice Report FEC"])
def test_theme_switch_keeps_the_tool(page):
    at = _ok(run_streamlit(default_timeout=180).run())
    _ok(at.sidebar.radio[0].set_value(page).run())
    if _popup(at):                       # landing from the Viewer asks once
        _ok(next(b for b in at.button if b.key == "carry_ok").click().run())
    assert not _popup(at)
    was = at.session_state["ui_theme"]

    _flip_theme(at)
    assert at.session_state["ui_theme"] != was
    assert at.session_state["nav_radio"] == page
    assert _shows(at, page)
    assert not _popup(at), "a theme change is not a tool change"

    _flip_theme(at)                      # and back again
    assert at.session_state["ui_theme"] == was
    assert at.session_state["nav_radio"] == page
    assert _shows(at, page)
    assert not _popup(at)


def test_theme_switch_keeps_the_trace_folder_boxes(tmp_path):
    """The sidebar's A and B boxes are keyed widgets with no saved slot:
    what the tech typed there must not fall back to the trace server's
    folders on a theme change."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    at = _ok(run_streamlit(default_timeout=180).run())
    _ok(at.sidebar.radio[0].set_value("Secret Sauce").run())
    at.sidebar.text_input(key="view_dir_a_input").set_value(str(a))
    at.sidebar.text_input(key="view_dir_b_input").set_value(str(b))
    _ok(at.run())
    _flip_theme(at)
    assert at.sidebar.text_input(key="view_dir_a_input").value == str(a)
    assert at.sidebar.text_input(key="view_dir_b_input").value == str(b)
    assert at.session_state["nav_radio"] == "Secret Sauce"



def test_theme_switch_keeps_a_page_box_with_no_saved_slot(tmp_path):
    """Secret Sauce's own folder box keeps no `{key}_saved` slot.  A theme
    change is not a trip to another tool: the box must keep what the tech
    typed, so no run may be cut short before the page draws it."""
    f = tmp_path / "both"
    f.mkdir()
    at = _ok(run_streamlit(default_timeout=180).run())
    _ok(at.sidebar.radio[0].set_value("Secret Sauce").run())
    at.text_input(key="ss_folder_input").set_value(str(f))
    _ok(at.run())
    _flip_theme(at)
    assert at.session_state["nav_radio"] == "Secret Sauce"
    assert at.text_input(key="ss_folder_input").value == str(f)

def _config_set_to(theme):
    """Another tab flipping the switch: the config is the server's, so it
    changes under this session."""
    import app as hub
    from streamlit import config as cfg
    for k, v in hub.THEME_STREAMLIT[theme].items():
        cfg.set_option("theme." + k, v)


@pytest.mark.parametrize("page", ["Splice Report", "Unidirectional"])
def test_theme_changed_under_the_session_keeps_the_tool(page, tmp_path):
    """The repaint rerun still runs when the config changed under this
    session (another tab).  It comes before the sidebar is drawn: the Tool
    list and the trace folder boxes must come through it."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    at = _ok(run_streamlit(default_timeout=180).run())
    _ok(at.sidebar.radio[0].set_value(page).run())
    if _popup(at):
        _ok(next(bt for bt in at.button if bt.key == "carry_ok").click().run())
    at.sidebar.text_input(key="view_dir_a_input").set_value(str(a))
    at.sidebar.text_input(key="view_dir_b_input").set_value(str(b))
    _ok(at.run())
    mine = at.session_state["ui_theme"]

    _config_set_to("light" if mine == "dark" else "dark")
    _ok(at.run())
    assert at.session_state["ui_theme"] == mine
    assert at.session_state["_theme_rerun_for"] == mine     # it did rerun
    assert at.session_state["nav_radio"] == page
    assert _shows(at, page)
    assert not _popup(at)
    assert at.sidebar.text_input(key="view_dir_a_input").value == str(a)
    assert at.sidebar.text_input(key="view_dir_b_input").value == str(b)
    assert "_theme_kept" not in at.session_state
