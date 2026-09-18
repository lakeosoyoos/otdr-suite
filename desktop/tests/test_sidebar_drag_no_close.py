"""Dragging the sidebar edge must widen it, not close it (Robert, 2026-09-17:
"when I go to click and drag it closes it instead").

Streamlit 1.50 closes the sidebar on any mouse press outside its content box
when the window is narrow, and its resize handle sits outside that box.  The
hub installs a small script that stops a press on the handle at the app root,
after React has started the resize and before Streamlit's document-level
close listener sees it.  Verified in a browser at a 700 px window: without
the script the drag closes the sidebar; with it the sidebar widens.
"""
from __future__ import annotations

from conftest import REPO_ROOT

SRC = (REPO_ROOT / "app.py").read_text(encoding="utf-8")


def _js():
    return SRC.split('SIDEBAR_DRAG_FIX_JS = """', 1)[1].split('"""', 1)[0]


def test_the_script_stops_only_a_press_on_the_resize_handle():
    js = _js()
    assert "getElementById('root')" in js
    assert "root.addEventListener('mousedown'" in js
    # bubble phase on the root: React's resize handler has already run
    assert "}, false);" in js
    assert "ev.stopPropagation()" in js
    # handle = inside the sidebar but outside its content box
    assert "closest('[data-testid=\"stSidebar\"]')" in js
    assert "!t.closest('[data-testid=\"stSidebarContent\"]')" in js
    # never preventDefault: that would block the drag itself
    assert "preventDefault" not in js


def test_installed_once_per_tab_and_never_fatal():
    js = _js()
    assert "w.__otdrSidebarDragFix" in js
    assert "catch (e) { return; }" in js
    helper = SRC.split("def _install_sidebar_drag_fix():", 1)[1].split("\ndef ", 1)[0]
    assert "st_components_html(SIDEBAR_DRAG_FIX_JS, height=0)" in helper
    assert "except Exception:" in helper


def test_it_runs_on_every_page():
    i_nav = SRC.index("\n_handle_nav()\n")
    i_fix = SRC.index("\n_install_sidebar_drag_fix()\n")
    i_sidebar = SRC.index("# ─── Sidebar nav")
    assert i_nav < i_fix < i_sidebar
