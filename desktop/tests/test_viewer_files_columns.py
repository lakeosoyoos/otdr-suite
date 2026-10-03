"""FILES list columns and sorting, the way FastReporter's Files tab has them.

FR3's Files tab (driven 2026-09-30): right-click the header row for a check
list of columns and Restore Default; click a header to sort by it, again to
reverse, with no unsorted state; the selection is kept through a sort.
Robert: "build as you suggest".

The Viewer's list gains P/F, Fiber, λ (nm) and Date beside File Name and
Dir.  P/F is the event table's verdict (Robert 2026-10-02: "P/F should
always [be the] event table"), blank for a file not added.  λ and Date need
every file's header, so they are off
until asked for and then read in the background, once (a file that cannot
be read is not asked for again, so a render cannot loop on it).  Ties keep
the Viewer's own order, A then B by fiber, where FR scrambles them; a file
not read yet sorts last either way; Restore Default puts that order back.
The choice is remembered.  A Shift range, a drag and the arrow keys follow
the rows as shown, sorted or not, because they read the list's own order.

The sort rules are plain functions (fileSortValue, fileRowCompare), run in
JavaScriptCore where present; the wiring is checked everywhere.
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
var out = {};
// Rows as the panel builds them: `order` is the Viewer's own (A then B, by fiber).
function row(order, name, eff, fiber, pf, wl, acq) {
  return { order: order, name: name, eff: eff, fiber: fiber, acq: acq,
           pf: pf === undefined ? null : pf,
           tr: pf === undefined ? null : { wl: wl } };
}
var R = [
  row(0, 'ZA_0001.sor', 'a', 1,  false, 1550, 300),
  row(1, 'C069_1625.sor', 'a', 69, true, 1625, 500),
  row(2, 'F70_A.sor', 'a', 70, false, 1550, 400),
  row(3, 'YB_0001.sor', 'b', 1,  true, 1550, 200),
  row(4, 'F70_B.sor', 'b', 70, undefined, null, null),     // not read yet
  row(5, 'F10_B.sor', 'b', 10, false, 1550, 100),
];
function sorted(col, desc) {
  return R.slice().sort(fileRowCompare({ col: col, desc: desc })).map(function (r) { return r.order; });
}
out.name_asc  = sorted('name', false);
out.name_desc = sorted('name', true);
out.pf_asc    = sorted('pf', false);
out.pf_desc   = sorted('pf', true);
out.fiber_asc = sorted('fiber', false);
out.wl_desc   = sorted('wl', true);
out.date_asc  = sorted('date', false);
out.dir_desc  = sorted('dir', true);
print('OUT ' + JSON.stringify(out));
"""


@pytest.fixture(scope='module')
def rules(tmp_path_factory):
    funcs = '\n'.join(_js_func(n) for n in ('fileSortValue', 'fileRowCompare'))
    path = tmp_path_factory.mktemp('files_columns') / 'rules.js'
    path.write_text(funcs + '\n' + _CASES, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and 'OUT ' in out, out[-2000:]
    return json.loads(out.split('OUT ', 1)[1].strip())


@needs_jsc
def test_a_header_sorts_up_then_down(rules):
    # names compare as a person reads them: F10 before F70
    assert rules['name_asc'] == [1, 5, 2, 4, 3, 0]
    assert rules['name_desc'] == [0, 3, 4, 2, 5, 1]
    assert rules['fiber_asc'] == [0, 3, 5, 1, 2, 4]      # ties keep A before B
    assert rules['dir_desc'] == [3, 4, 5, 0, 1, 2]


@needs_jsc
def test_a_file_not_read_yet_sorts_last_both_ways(rules):
    assert rules['pf_asc'] == [0, 2, 5, 1, 3, 4]         # pass, fail, not judged
    assert rules['pf_desc'] == [1, 3, 0, 2, 5, 4]
    assert rules['wl_desc'] == [1, 0, 2, 3, 5, 4]
    assert rules['date_asc'] == [5, 3, 0, 2, 1, 4]        # by when it was shot


def test_the_columns_and_their_defaults():
    cols = re.findall(r"\['(\w+)', '([^']+)', '([^']+)'", SRC.split('const FILE_COLS = [', 1)[1].split('];', 1)[0])
    assert [c[0] for c in cols] == ['pf', 'name', 'dir', 'fiber', 'wl', 'date']
    assert [c[1] for c in cols] == ['P/F', 'File Name', 'Dir.', 'Fiber', 'λ (nm)', 'Date']
    assert "const FILE_COLS_DEFAULT = ['dir'];" in SRC     # File Name + Dir., as before
    assert "const FACT_COLS = ['wl', 'date'];" in SRC      # P/F reads the table, not the files
    assert 'grid-template-columns: var(--files-grid, 18px 1fr 40px);' in SRC
    shown = _body('shownFileCols')
    assert "c === 'name' || gFileCols.has(c)" in shown       # File Name always shows


def test_a_header_click_sorts_and_a_right_click_offers_the_columns():
    hd = SRC.split("document.getElementById('files-cols').addEventListener('click', (ev) => {", 1)[1].split('\n});', 1)[0]
    assert '{ col: c, desc: !gFileSort.desc } : { col: c, desc: false }' in hd
    assert 'saveFilesView();' in hd and 'renderFilesPanel();' in hd
    assert "document.getElementById('files-cols').addEventListener('contextmenu'" in SRC
    menu = _body('showFilesColsMenu')
    assert "'<div class=\"span-menu-hd\">Columns</div>'" in menu
    assert '<button data-reset="1">Restore Default</button>' in menu
    assert 'gFileCols = new Set(FILE_COLS_DEFAULT);' in menu and 'gFileSort = null;' in menu


def test_the_view_is_remembered():
    assert "localStorage.getItem('otdr_viewer_files_view')" in SRC
    save = _body('saveFilesView')
    assert "JSON.stringify({ cols: [...gFileCols], sort: gFileSort })" in save


def test_the_panel_sorts_its_rows_and_reads_details_only_when_shown():
    panel = _body('renderFilesPanel')
    assert 'if (gFileSort) rows.sort(fileRowCompare(gFileSort));' in panel
    assert "const tr = wantFacts ? fileTraits(`${dir}-${f}`) : null;" in panel
    assert "const pf = tableFileFails(`${dir}-${f}`);" in panel        # the event table's
    assert 'if (wantFacts && rows.length) ensureFileFacts();' in panel
    assert 'renderFilesHeader(cols);' in panel
    need = _body('filesNeedFacts')
    assert 'FACT_COLS.some(c => gFileCols.has(c))' in need


def test_a_file_that_cannot_be_read_is_not_asked_for_again():
    ens = _body('ensureFileFacts')
    assert 'if (gFactsLoading) return gFactsLoading;' in ens          # one read at a time
    assert '!gFactsTried.has(`${k}|${name}`)' in ens
    assert 'loadFileFacts(keys, (done, total) => {' in ens
    assert "gFactsNote = `reading ${done} of ${total}`;" in ens
    # a failed request pauses the reads rather than blanking the columns for
    # good, and only what the server said it could not read is not asked again
    assert 'if (!now && Date.now() < gFactsRetryAt) return Promise.resolve(false);' in ens
    load = _body('loadFileFacts')
    assert 'if (onProgress) onProgress(done, need.length);' in load
    assert "for (const f of (j.missing || [])) gFactsTried.add(`${d}-${f}|${fileNameOf(`${d}-${f}`)}`);" in load
    assert 'gFactsRetryAt = Date.now() + 10000;' in load
