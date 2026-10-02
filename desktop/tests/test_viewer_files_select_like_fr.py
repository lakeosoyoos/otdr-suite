"""The FILES panel selects the way FastReporter's Files panel does.

Robert, 2026-09-29: "drive FR and understand exactly all the options available
to a user when they are selecting fibers in the right list panel and what mouse
clicks do what so we can replicate those in our tools".  FR3 was driven click
by click (real Windows Ctrl and Shift clicks sent inside the VM), and on
2026-09-30: "when several are selected and click just one, do as FR does".

What FR does, and so what the Viewer now does:

  click                    only this file
  click on a file that is already one of several selected
                           FR keeps the selection; the Viewer selects just
                           that file (the boss, 2026-10-01: with all of them
                           selected, click one fiber and only that one shows)
  Shift+click              the files from the anchor to this one, in screen
                           order, in place of the selection
  Ctrl+click               add or remove one file, but never the last one (the
                           Viewer used to empty the chart)
  drag from a file outside the selection
                           the files swept (the Viewer did nothing)
  right-click a file outside the selection
                           that file is selected first, then the menu (the
                           Viewer left the old marks lit while the menu acted
                           on the clicked row alone)
  Down / Up, Shift+Down / Shift+Up
                           move the selection, or grow the range (nothing
                           before; Home / End added the Windows way)
  Remove                   the file that slides into the removed spot is
                           selected, so the list is never left with nothing

The selection is ONE thing, as in FR: gSelectedFiles, which once a load
settles is exactly what is loaded (so a trace the cap cut off is not left
highlighted, and one the Fibers box loaded is selected too).

The rules live in plain functions (fileClickSelection, fileKeySelection,
fileRange, fileAfterRemove).  The behaviour tests run those functions in
JavaScriptCore (the jsc shell macOS ships) and skip where there is none; the
source checks below them run everywhere.
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
    """Source of `function name(...) {...}`, brace-matched."""
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


# ─── behaviour: the rules, run as written ────────────────────────────────────

# Ten rows the way the panel lists them, A then B within a fiber here so a
# range crosses directions the way it does in FR's list.
KEYS = ['a-70', 'b-70', 'a-71', 'b-71', 'a-72', 'b-72', 'a-73', 'b-73', 'a-74', 'b-74']

_CASES = r"""
var out = {};
function R(r) {
  return r === null ? null
    : { want: r.want === null ? null : Array.from(r.want), anchor: r.anchor, focus: r.focus };
}
var K = %(keys)s;
function sel(a) { return new Set(a); }
out.plain         = R(fileClickSelection(K, sel(['a-70']), 'a-70', 'a-72', false, false));
out.keep          = R(fileClickSelection(K, sel(['a-71', 'b-71', 'a-72']), 'a-71', 'b-71', false, false));
out.one_of_all    = R(fileClickSelection(K, sel(K), 'a-70', 'a-73', false, false));
out.only_again    = R(fileClickSelection(K, sel(['a-72']), 'a-72', 'a-72', false, false));
out.shift         = R(fileClickSelection(K, sel(['b-72']), 'b-72', 'a-74', true, false));
out.shift_up      = R(fileClickSelection(K, sel(['b-72']), 'b-72', 'a-71', true, false));
out.shift_lost    = R(fileClickSelection(K, sel(['b-72']), 'gone-1', 'a-74', true, false));
out.ctrl_add      = R(fileClickSelection(K, sel(['a-70']), 'a-70', 'b-73', false, true));
out.ctrl_remove   = R(fileClickSelection(K, sel(['a-70', 'b-73']), 'b-73', 'a-70', false, true));
out.ctrl_last     = R(fileClickSelection(K, sel(['a-70']), 'a-70', 'a-70', false, true));
out.ctrl_shift    = R(fileClickSelection(K, sel(['a-70']), 'a-72', 'b-73', true, true));
// FR's own example: click b-74, Ctrl+click a-73, Shift+click b-71.
var s1 = fileClickSelection(K, sel([]), null, 'b-74', false, false);
var s2 = fileClickSelection(K, s1.want, s1.anchor, 'a-73', false, true);
var s3 = fileClickSelection(K, s2.want, s2.anchor, 'b-71', true, false);
out.fr_example    = R(s3);
out.down          = R(fileKeySelection(K, 'a-71', 'a-71', 'ArrowDown', false));
out.up            = R(fileKeySelection(K, 'a-71', 'a-71', 'ArrowUp', false));
out.down_bottom   = R(fileKeySelection(K, 'b-74', 'b-74', 'ArrowDown', false));
out.up_top        = R(fileKeySelection(K, 'a-70', 'a-70', 'ArrowUp', false));
out.down_nowhere  = R(fileKeySelection(K, null, null, 'ArrowDown', false));
out.up_nowhere    = R(fileKeySelection(K, null, null, 'ArrowUp', false));
out.home          = R(fileKeySelection(K, 'a-72', 'a-72', 'Home', false));
out.end           = R(fileKeySelection(K, 'a-72', 'a-72', 'End', false));
out.shift_down    = R(fileKeySelection(K, 'a-71', 'b-71', 'ArrowDown', true));
out.shift_back    = R(fileKeySelection(K, 'a-71', 'b-71', 'ArrowUp', true));
out.shift_past    = R(fileKeySelection(K, 'a-71', 'a-71', 'ArrowUp', true));
out.shift_nowhere = R(fileKeySelection(K, null, null, 'ArrowDown', true));
out.none          = R(fileKeySelection([], null, null, 'ArrowDown', false));
out.range_rev     = fileRange(K, 'a-73', 'b-71');
out.range_lost    = fileRange(K, 'gone-1', 'b-71');
out.after_mid     = fileAfterRemove(K, sel(['a-72', 'b-72']));
out.after_bottom  = fileAfterRemove(K, sel(['a-74', 'b-74']));
out.after_all     = fileAfterRemove(K, sel(K));
out.after_none    = fileAfterRemove(K, sel(['gone-1']));
print('OUT ' + JSON.stringify(out));
"""


@pytest.fixture(scope='module')
def rules(tmp_path_factory):
    funcs = '\n'.join(_js_func(n) for n in
                      ('fileRange', 'fileClickSelection', 'fileKeySelection', 'fileAfterRemove'))
    path = tmp_path_factory.mktemp('files_select') / 'rules.js'
    path.write_text(funcs + '\n' + _CASES % {'keys': json.dumps(KEYS)}, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and 'OUT ' in out, out[-2000:]
    return json.loads(out.split('OUT ', 1)[1].strip())


def _sel(want, anchor, focus):
    return {'want': want, 'anchor': anchor, 'focus': focus}


@needs_jsc
def test_a_click_selects_just_that_file(rules):
    assert rules['plain'] == _sel(['a-72'], 'a-72', 'a-72')


@needs_jsc
def test_a_click_on_one_of_several_selected_shows_just_that_one(rules):
    """The boss, 2026-10-01: "when all are selected, click one fiber in the
    list and have only that one show".  This replaces the FR rule (FR keeps
    the selection) Robert picked on 2026-09-30."""
    assert rules['keep'] == _sel(['b-71'], 'b-71', 'b-71')
    # every file selected, one clicked: that one alone
    assert rules['one_of_all'] == _sel(['a-73'], 'a-73', 'a-73')
    # the only file selected, clicked again: the same one file
    assert rules['only_again'] == _sel(['a-72'], 'a-72', 'a-72')


@needs_jsc
def test_shift_click_takes_the_range_from_the_anchor(rules):
    assert rules['shift'] == _sel(['b-72', 'a-73', 'b-73', 'a-74'], 'b-72', 'a-74')
    assert rules['shift_up'] == _sel(['a-71', 'b-71', 'a-72', 'b-72'], 'b-72', 'a-71')
    # an anchor that has left the list (removed, renamed) leaves just the row
    assert rules['shift_lost'] == _sel(['a-74'], 'gone-1', 'a-74')


@needs_jsc
def test_ctrl_click_adds_and_removes_but_never_the_last_file(rules):
    assert rules['ctrl_add'] == _sel(['a-70', 'b-73'], 'b-73', 'b-73')
    assert rules['ctrl_remove'] == _sel(['b-73'], 'a-70', 'a-70')
    assert rules['ctrl_last'] is None
    # Ctrl+Shift: the range, added to what is selected
    assert sorted(rules['ctrl_shift']['want']) == sorted(['a-70', 'a-72', 'b-72', 'a-73', 'b-73'])


@needs_jsc
def test_fr_example_ctrl_moves_the_anchor_and_shift_replaces(rules):
    """Driven on FR: click F74_B, Ctrl+click F73_A, Shift+click F71_B gives
    F73_A to F71_B only.  The Ctrl+click moved the anchor and the Shift+click
    replaced the selection, dropping F74_B."""
    assert rules['fr_example'] == _sel(['b-71', 'a-72', 'b-72', 'a-73'], 'a-73', 'b-71')


@needs_jsc
def test_the_arrow_keys_move_the_selection(rules):
    assert rules['down'] == _sel(['b-71'], 'b-71', 'b-71')
    assert rules['up'] == _sel(['b-70'], 'b-70', 'b-70')
    assert rules['down_bottom'] == _sel(['b-74'], 'b-74', 'b-74')     # stops at the ends
    assert rules['up_top'] == _sel(['a-70'], 'a-70', 'a-70')
    assert rules['down_nowhere'] == _sel(['a-70'], 'a-70', 'a-70')
    assert rules['up_nowhere'] == _sel(['b-74'], 'b-74', 'b-74')
    assert rules['home'] == _sel(['a-70'], 'a-70', 'a-70')
    assert rules['end'] == _sel(['b-74'], 'b-74', 'b-74')
    assert rules['none'] is None


@needs_jsc
def test_shift_and_the_arrow_keys_grow_and_shrink_the_range(rules):
    assert rules['shift_down'] == _sel(['a-71', 'b-71', 'a-72'], 'a-71', 'a-72')
    assert rules['shift_back'] == _sel(['a-71'], 'a-71', 'a-71')
    assert rules['shift_past'] == _sel(['b-70', 'a-71'], 'a-71', 'b-70')
    assert rules['shift_nowhere'] == _sel(['a-70'], 'a-70', 'a-70')


@needs_jsc
def test_ranges_and_what_a_remove_selects(rules):
    assert rules['range_rev'] == ['b-71', 'a-72', 'b-72', 'a-73']
    assert rules['range_lost'] == ['b-71']
    assert rules['after_mid'] == 'a-73'          # slides into the removed spot
    assert rules['after_bottom'] == 'b-73'       # the last one left
    assert rules['after_all'] is None
    assert rules['after_none'] is None


# ─── source checks that run everywhere (CI has no jsc) ───────────────────────

def test_the_click_goes_through_the_rules_and_one_load_at_a_time():
    click = _body('onFileClick')
    assert 'fileClickSelection(fileRowKeys(), gSelectedFiles, gFileAnchor, key,' in click
    # a click that changes nothing (the one file selected, clicked again)
    # moves the anchor and loads nothing
    assert "if (!s.want || sameKeys(s.want, gSelectedFiles)) { renderFilesPanel(); return; }" in click
    assert 'await selectFiles(s.want);' in click
    # the dot still shows and hides a loaded trace
    assert "ev.target.closest('.file-vis')" in click
    sel = _body('selectFiles')
    assert sel.index('markFiles(want);') < sel.index('while (gSelNext)')
    assert 'if (gSelLoading) return;' in sel


def test_the_selection_is_what_is_loaded_once_a_load_settles():
    sync = _body('syncFileMarks')
    assert 'if (gSelNext) return;' in sync       # a newer pick keeps its marks
    assert 'for (const t of gTraces) gSelectedFiles.add(t.key);' in sync
    ap = _body('applyFileSelection')
    assert ap.index('syncFileMarks();') < ap.index('renderChips();')
    assert _body('addFibers').count('syncFileMarks();') == 2   # overview + detail
    for fn in ('removeTrace', 'forgetSide', 'afterRename'):
        assert 'syncFileMarks();' in _body(fn), fn


def test_a_right_click_outside_the_selection_selects_that_file_first():
    cm = SRC.split("getElementById('files-list').addEventListener('contextmenu'", 1)[1].split('\n});', 1)[0]
    assert cm.index('selectForMenu(row);') < cm.index('showFileDirMenu(')
    pick = _body('selectForMenu')
    assert 'if (gSelectedFiles.has(key)) return;' in pick
    assert 'selectFiles(new Set([key]));' in pick


def test_a_drag_from_an_unselected_file_selects_the_rows_swept():
    down = SRC.split("filesList.addEventListener('mousedown', (ev) => {", 1)[1].split('\n});', 1)[0]
    assert "if (ev.target.closest('.file-vis')) return;" in down
    assert 'if (gSelectedFiles.has(key)) return;' in down       # from a selected file: nothing
    assert down.index('filesList.focus(') < down.index('gFileDrag = {')
    end = _body('endFileDrag')
    assert 'if (!d || !d.moved) return;' in end                  # a plain click stays a click
    assert 'selectFiles(new Set(fileRange(fileRowKeys(), d.from, d.to)));' in end
    assert 'gFileDragClick = true;' in end
    assert 'if (gFileDragClick) { gFileDragClick = false; return; }' in _body('onFileClick')


def test_the_list_takes_the_arrow_keys():
    assert '<div id="files-list" tabindex="0">' in SRC
    kd = SRC.split("filesList.addEventListener('keydown', (ev) => {", 1)[1].split('\n});', 1)[0]
    assert "['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(ev.key)" in kd
    assert 'fileKeySelection(fileRowKeys(), gFileAnchor, gFileFocus, ev.key, ev.shiftKey)' in kd
    assert 'ev.preventDefault();' in kd and 'scrollIntoView' in kd
    assert '#files-list:focus .file-row.focus {' in SRC


def test_a_remove_selects_the_file_that_slides_into_its_place():
    for fn in ('removeFile', 'removeSelectedFiles'):
        body = _body(fn)
        assert body.index('fileAfterRemove(fileRowKeys(),') < body.index('gRemovedFiles.add(')
        assert 'selectAfterRemove(next, msg);' in body, fn
    after = _body('selectAfterRemove')
    assert 'if (!next || gSelectedFiles.size) return;' in after   # only when nothing is left


def test_the_row_says_its_name_and_the_header_counts_the_selection():
    panel = _body('renderFilesPanel')
    assert "title=\"${esc(files[i] || 'F' + f)}\"" in panel
    assert 'click to remove' not in panel and 'click to load' not in panel
    assert '`${gSelectedFiles.size} of ${rows.length} selected`' in panel


def test_a_key_shared_by_two_rows_is_listed_once():
    """Two files of one fibre on a side (two wavelengths) share a key; listed
    twice, Down found the first copy every time and stopped on the second."""
    keys = _body('fileRowKeys')
    assert "return [...new Set([...document.querySelectorAll('#files-list .file-row')]" in keys


def test_a_queued_selection_does_not_outlive_a_clear_or_the_fibers_box():
    assert 'gSelNext = null;                   // a FILES selection still queued is dropped too' in _body('clearAll')
    assert 'gSelNext = null;                   // the box (or a report jump) is the newer ask' in _body('addFibers')
    sel = _body('selectFiles')
    assert 'await applyFileSelection(w);' in sel and 'reportJsError(' in sel   # one bad load does not stop the queue
    # and a pick goes with its trace before the load draws behind it
    ap = _body('applyFileSelection')
    assert ap.index('prunePick();') < ap.index('await loadOverview(tasks, ld);')
