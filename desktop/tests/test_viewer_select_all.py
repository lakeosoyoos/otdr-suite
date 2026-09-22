"""Ctrl+A marks every file in the FILES list AND loads them.

The boss: "can we do control A on a windows machine to select all? right now
it selects the entire webpage" and then "control A should just select all
the files in the FILES list. not take any action beyond that." -- marking
only, which is what this pinned until 2026-09-22, when the same person asked
for the other half: "when we select all with control A that needs to then
load all of the selected fibers in viewer".  So the mark is painted first and
the load goes through the SAME applyFileSelection a click uses, which is what
takes a whole cable through the bulk overview instead of stopping at 48.
A right-click Remove on a marked row removes them all; a click re-marks
exactly the rows it selected, so the same Remove works on a shift-clicked
range (2026-09-18: "when we click to remove fiber it only removes one" -- the
click path cleared the mark set, so the menu always fell through to the
single-row path).  Plain JS, no runtime here: pins the source.
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


def test_select_all_marks_every_row_and_loads_them():
    fn = SRC.split("async function selectAllFiles() {", 1)[1].split("\n}", 1)[0]
    assert "#files-list .file-row" in fn
    assert "markFiles(want);" in fn            # the marks are painted first
    assert "await applyFileSelection(want);" in fn   # ... then the load
    assert ".file-row.selected {" in SRC


def test_select_all_loads_through_the_same_two_regimes_a_click_does():
    """applyFileSelection is the shared path: past MAX_DETAIL_TRACES it takes
    the bulk overview, so Ctrl+A on a 1,152-fiber cable is not cut off at 48."""
    ap = SRC.split("async function applyFileSelection(want) {", 1)[1].split("\n}\n", 1)[0]
    assert "const overview = tasks.length > MAX_DETAIL_TRACES;" in ap
    assert "await loadOverview(tasks);" in ap


def test_the_mark_is_used_by_remove_and_reset_by_a_click():
    menu = SRC.split("function showFileDirMenu(", 1)[1].split("\nasync function ", 1)[0]
    assert "if (gSelectedFiles.has(k)) removeSelectedFiles(); else removeFile(k, fiber);" in menu
    rm = SRC.split("function removeSelectedFiles() {", 1)[1].split("\n}", 1)[0]
    assert "keys.forEach(key => gRemovedFiles.add(key));" in rm
    # A click no longer empties the mark set, it REPLACES it with what the
    # click selected — one row for a plain click, the range for a shift-click.
    click = SRC.split("async function onFileClick(ev) {", 1)[1].split("\n}\n", 1)[0]
    assert "clearFileSelection();" not in click
    assert "markFiles(want);" in click
    mark = SRC.split("function markFiles(keys) {", 1)[1].split("\n}", 1)[0]
    assert "gSelectedFiles.clear();" in mark
    assert "for (const k of keys) gSelectedFiles.add(k);" in mark


def test_a_shift_clicked_range_is_what_remove_removes():
    """The reported bug: select a range in FILES, right-click Remove, and only
    the clicked file went.  The range is marked, so the marked-set branch runs
    and the menu says how many before it does."""
    click = SRC.split("async function onFileClick(ev) {", 1)[1].split("\n}\n", 1)[0]
    rng = click.split("if (ev.shiftKey && gFileAnchor) {", 1)[1].split("} else", 1)[0]
    assert "want = new Set(rows.slice(lo, hi + 1).map(keyOf));" in rng
    assert click.index("want = new Set(rows.slice") < click.index("markFiles(want);")
    menu = SRC.split("function showFileDirMenu(", 1)[1].split("\nasync function ", 1)[0]
    # the marked KEYS (Direction acts on them too now), and the count off them
    assert "const marks = gSelectedFiles.has(k) ? [...gSelectedFiles] : [k];" in menu
    assert "const marked = marks.length > 1 ? marks.length : 0;" in menu
    assert "marked + ' marked files'" in menu


def test_clear_all_drops_the_marks_with_the_traces():
    fn = SRC.split("function clearAll() {", 1)[1].split("\n}", 1)[0]
    assert "clearFileSelection();" in fn
