"""The FILES panel's Fibers tab: a row per fibre, and whether its files pair.

FastReporter's panel (driven 2026-09-30) keeps four tabs along its foot:
Files, Measurements, Identifiers and Matched Files.  Identifiers is a tree
where a click on a fibre selects both of its directions; Matched Files shows
what FR paired with what, which is the quickest way to see why a pair did
not form.  Robert: "build as you suggest".

The Viewer's Fibers tab is those two in one list: a row per fibre, its A->B
and B->A files side by side, and a Pair column that says whether the two
make the pair the table's Average row needs, and if not, why:

  ✓            one file each way, drawn as opposite directions
  A only       no B->A file (and B only the other way round)
  both A->B    both files drawn as one direction (a file's own stamp or a
               Direction change), so no Average
  2 A files    two files of one fibre on a side (two wavelengths)

Clicks follow the Files rules a fibre at a time (fileClickSelection,
fileKeySelection): click, Shift+click, Ctrl/Cmd+click, the arrow keys; a
fibre only partly selected counts as not selected; a right-click selects
the fibre first and opens the file menu on the selection.  The tab is
remembered.  The pairing rules run in JavaScriptCore where present; the
wiring is checked everywhere.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")
JSC = ('/System/Library/Frameworks/JavaScriptCore.framework/'
       'Versions/Current/Helpers/jsc')
needs_jsc = pytest.mark.skipif(not os.path.exists(JSC),
                               reason='JavaScriptCore shell (macOS) not present')


def _js_func(name):
    m = re.search(r'(?:async\s+)?function\s+' + re.escape(name) + r'\s*\([^)]*\)\s*\{', SRC)
    assert m, 'viewer.html no longer defines %s()' % name
    i, depth = m.end(), 1
    while i < len(SRC) and depth:
        depth += {'{': 1, '}': -1}.get(SRC[i], 0)
        i += 1
    return SRC[m.start():i]


def _body(name):
    src = _js_func(name)
    return src[src.index('{') + 1:-1]


_CASES = r"""
var gInfo = {
  fibers_a: [1, 2, 3, 5, 5], files_a: ['A1.sor', 'A2.sor', 'A3.sor', 'A5_1550.sor', 'A5_1625.sor'],
  fibers_b: [1, 2, 4, 5],    files_b: ['B1.sor', 'B2.sor', 'B4.sor', 'B5.sor'],
};
var gRemovedFiles = new Set(['b-2']);
var gSelectedFiles = new Set(['a-1', 'b-1', 'a-3']);
var DIRS = { 'a-1': 'a', 'b-1': 'b', 'a-2': 'a', 'a-3': 'a', 'b-4': 'b', 'a-5': 'a', 'b-5': 'b' };
function dirOf(d, f) { return DIRS[d + '-' + f]; }
var out = {};
var T = fiberTable();
out.table = T.map(function (r) { return [r.fiber, r.a, r.b]; });
out.notes = T.map(function (r) { return fiberPairNote(r, dirOf).text; });
out.ok = T.map(function (r) { return fiberPairNote(r, dirOf).ok; });
DIRS['b-1'] = 'a';                              // B's file set to A->B
out.both = fiberPairNote(T[0], dirOf).text;
out.keys = T.map(fiberFileKeys);
out.state = T.map(fiberState);
print('OUT ' + JSON.stringify(out));
"""


@pytest.fixture(scope='module')
def rules(tmp_path_factory):
    funcs = '\n'.join(_js_func(n) for n in ('fiberTable', 'fiberPairNote', 'fiberFileKeys', 'fiberState'))
    path = tmp_path_factory.mktemp('fibers_view') / 'rules.js'
    path.write_text(funcs + '\n' + _CASES, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and 'OUT ' in out, out[-2000:]
    return json.loads(out.split('OUT ', 1)[1].strip())


@needs_jsc
def test_a_row_per_fibre_with_its_files_side_by_side(rules):
    assert rules['table'] == [
        [1, ['A1.sor'], ['B1.sor']],
        [2, ['A2.sor'], []],                  # B's file was removed from the list
        [3, ['A3.sor'], []],
        [4, [], ['B4.sor']],
        [5, ['A5_1550.sor', 'A5_1625.sor'], ['B5.sor']],
    ]


@needs_jsc
def test_the_pair_column_says_why_a_pair_did_not_form(rules):
    assert rules['notes'] == ['✓', 'A only', 'A only', 'B only', '2 A files']
    assert rules['ok'] == [True, False, False, False, False]
    assert rules['both'] == 'both A→B'


@needs_jsc
def test_a_fibre_is_selected_when_all_its_files_are(rules):
    assert rules['keys'] == [['a-1', 'b-1'], ['a-2'], ['a-3'], ['b-4'], ['a-5', 'b-5']]
    assert rules['state'] == ['selected', '', 'selected', '', '']


def test_the_tabs_along_the_foot_and_their_memory():
    assert ('<div id="files-tabs"><button data-view="files" class="active">Files</button>'
            '<button data-view="fibers">Fibers</button>'
            '<button data-view="meas" hidden') in SRC        # Measurements: a dropped .olts
    assert "localStorage.getItem('otdr_viewer_files_tab') === 'fibers'" in SRC
    tabs = SRC.split("document.getElementById('files-tabs').addEventListener('click', (ev) => {", 1)[1].split('\n});', 1)[0]
    assert "localStorage.setItem('otdr_viewer_files_tab', gFilesView);" in tabs
    assert 'renderFilesPanel();' in tabs
    panel = _body('renderFilesPanel')
    assert "if (gFilesView === 'fibers') { renderFibersView(list, esc); sayNextDrop(); return; }" in panel
    assert panel.count('sayNextDrop();') == 2                # both tabs say what the next drop does


def test_fibre_clicks_follow_the_files_rules():
    click = SRC.split("addEventListener('click', (ev) => {\n  const row = ev.target.closest('.fiber-row');", 1)[1].split('\n});', 1)[0]
    assert 'fileClickSelection(keys, sel, gFiberAnchor, row.dataset.fkey,' in click
    assert 'selectFibers(s.want);' in click
    kd = SRC.split("addEventListener('keydown', (ev) => {\n  if (gFilesView !== 'fibers') return;", 1)[1].split('\n});', 1)[0]
    assert 'fileKeySelection(keys, gFiberAnchor, gFiberFocus, ev.key, ev.shiftKey)' in kd
    cm = SRC.split("addEventListener('contextmenu', (ev) => {\n  const row = ev.target.closest('.fiber-row');", 1)[1].split('\n});', 1)[0]
    assert "if (fiberState(r) !== 'selected') {" in cm
    assert 'showFileDirMenu(ev.clientX, ev.clientY, dir, r.fiber,' in cm
    sel = _body('selectFibers')
    assert 'if (want.size) selectFiles(want);' in sel


def test_the_fibers_header_has_no_column_menu():
    cm = SRC.split("document.getElementById('files-cols').addEventListener('contextmenu', (ev) => {", 1)[1].split('\n});', 1)[0]
    assert "if (gFilesView === 'fibers') return;" in cm
    assert "hd.classList.remove('fibers');" in _body('renderFilesHeader')
    assert "hd.classList.add('fibers');" in _body('renderFibersView')


def test_ctrl_a_select_same_and_remove_still_see_every_file_from_the_fibers_tab():
    """Those read the list's files; the Fibers tab shows fibres, so the file
    keys come from the fibres there, each fibre's A then B."""
    keys = _body('fileRowKeys')
    assert "if (gFilesView === 'fibers') return fiberTable().flatMap(fiberFileKeys);" in keys
    # a name comes from the listing, not from a row that may not be drawn
    name = _body('fileNameOf')
    assert "gInfo['files_' + d]" in name and '`F${f}`' in name
    # and the Files tab's arrow keys stand down while the Fibers tab has them
    kd = SRC.split("filesList.addEventListener('keydown', (ev) => {", 1)[1].split('\n});', 1)[0]
    assert "if (gFilesView !== 'files') return;" in kd     # not on Fibers, nor Measurements


def test_a_remove_on_the_fibers_tab_selects_the_whole_next_fibre():
    after = _body('selectAfterRemove')
    assert "if (gFilesView === 'fibers') {" in after
    assert 'selectFibers([gFiberFocus]);' in after


def test_the_dir_buttons_name_the_folder_add_loads_from():
    """After a Direction change a trace is drawn as the other direction, but
    Add still loads from the folder it came from."""
    box = _body('syncFiberBox')
    assert 'const dirs = new Set(gTraces.map(t => t.src || t.dir));' in box
