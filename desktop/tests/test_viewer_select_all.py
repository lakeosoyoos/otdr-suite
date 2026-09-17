"""Ctrl+A marks every file in the FILES list — and does nothing else.

The boss: "can we do control A on a windows machine to select all? right now
it selects the entire webpage" and then "control A should just select all
the files in the FILES list. not take any action beyond that."  So: no load.
A right-click Remove on a marked row removes them all; a click clears the
mark.  Plain JS, no runtime here: pins the source.
"""
from __future__ import annotations

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def test_ctrl_a_off_an_input_marks_the_files_and_not_the_page():
    kd = SRC.split("window.addEventListener('keydown', (ev) => {", 1)[1].split("\n});", 1)[0]
    assert kd.index("if (ev.target.tagName === 'INPUT') return;") < kd.index("ev.key === 'a'")
    assert "(ev.ctrlKey || ev.metaKey) && (ev.key === 'a' || ev.key === 'A')" in kd
    block = kd.split("ev.key === 'A')) {", 1)[1].split("}", 1)[0]
    assert "ev.preventDefault();" in block and "selectAllFiles();" in block


def test_select_all_only_marks_rows_it_loads_nothing():
    fn = SRC.split("function selectAllFiles() {", 1)[1].split("\n}", 1)[0]
    assert "r.classList.add('selected')" in fn
    for verb in ("loadTasks", "loadOne", "loadOverview", "applyFileSelection", "fetch("):
        assert verb not in fn, verb
    assert ".file-row.selected {" in SRC


def test_the_mark_is_used_by_remove_and_cleared_by_a_click():
    menu = SRC.split("function showFileDirMenu(", 1)[1].split("\nasync function ", 1)[0]
    assert "if (gSelectedFiles.has(k)) removeSelectedFiles(); else removeFile(k, fiber);" in menu
    rm = SRC.split("function removeSelectedFiles() {", 1)[1].split("\n}", 1)[0]
    assert "keys.forEach(key => gRemovedFiles.add(key));" in rm
    click = SRC.split("async function onFileClick(ev) {", 1)[1].split("\n}", 1)[0]
    assert "clearFileSelection();" in click
