"""Ctrl+A selects every file in the FILES list, not the page's text.

The boss: "can we do control A on a windows machine to select all? right now
it selects the entire webpage."  Plain JS, no runtime here: pins the source.
"""
from __future__ import annotations

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def test_ctrl_a_off_an_input_selects_all_files_and_not_the_page():
    kd = SRC.split("window.addEventListener('keydown', (ev) => {", 1)[1].split("\n});", 1)[0]
    assert kd.index("if (ev.target.tagName === 'INPUT') return;") < kd.index("ev.key === 'a'")
    assert "(ev.ctrlKey || ev.metaKey) && (ev.key === 'a' || ev.key === 'A')" in kd
    block = kd.split("ev.key === 'A')) {", 1)[1].split("}", 1)[0]
    assert "ev.preventDefault();" in block and "selectAllFiles();" in block


def test_select_all_skips_loaded_and_removed_files_and_shares_the_load_path():
    fn = SRC.split("async function selectAllFiles() {", 1)[1].split("\n}", 1)[0]
    assert "gTraces.some(t => t.key === key) || gRemovedFiles.has(key)" in fn
    assert "await loadTasks(tasks, dirs);" in fn
    add = SRC.split("async function addFibers() {", 1)[1].split("\n}", 1)[0]
    assert "await loadTasks(tasks, dirs);" in add
    assert "async function loadTasks(tasks, dirs) {" in SRC
