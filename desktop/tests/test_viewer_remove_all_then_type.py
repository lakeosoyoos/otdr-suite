"""Remove every file: the folders clear and the Fibers box switches off
(demo list #34; Robert 2026-10-01: "after we remove the files on the right
panel it should clear the boxes and then we should not be able to type fiber
numbers ... a greyed out example Fiber number").

Before, removing every file let go of the trace server's folders while the
hub's A/B boxes kept the old path until something else ran the hub, and a
fiber typed in the meantime said "no A/B folder is set".  Now:

  Remove      the last file off a side lets go of that side's folder
              (/api/unload_sides); removing some files keeps it
  hub boxes   a fragment looks every VIEWER_FOLDERS_TICK_S and runs the hub
              when the Viewer moved the folders, so the boxes empty at once
  Fibers box  with no file listed on either side the box and Add are off and
              the box shows FIBERS_OFF_PLACEHOLDER, grayed; both come back as
              soon as a folder lists a file again (a box typed in the hub, a
              drop, another window), which the page hears through /api/mode

The page runs in JavaScriptCore where present; the server and hub parts run
everywhere.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request

import pytest

from conftest import (VIEWER_DIR, APP_PATH, run_streamlit, import_trace_server,
                      go_tab, trace_box, trace_box_value,
                      FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR)

SRC = (VIEWER_DIR / 'viewer.html').read_text(encoding='utf-8')
TS = import_trace_server()
JSC = ('/System/Library/Frameworks/JavaScriptCore.framework/'
       'Versions/Current/Helpers/jsc')
needs_jsc = pytest.mark.skipif(not os.path.exists(JSC),
                               reason='JavaScriptCore shell (macOS) not present')
OFF_TEXT = 'e.g. 354, add files first'


def _js_func(name):
    m = re.search(r'(?:async\s+)?function\s+' + re.escape(name) + r'\s*\([^)]*\)\s*\{', SRC)
    assert m, 'viewer.html no longer defines %s()' % name
    i, depth = m.end(), 1
    while i < len(SRC) and depth:
        depth += {'{': 1, '}': -1}.get(SRC[i], 0)
        i += 1
    return SRC[m.start():i]


def _const(name):
    m = re.search(r'^const ' + re.escape(name) + r' = [^;]+;', SRC, re.M)
    assert m, name
    return m.group(0)


_STUBS = r"""
var gTraces = [], gRemovedFiles = new Set(), gSelectedFiles = new Set(), COLORS_USED = new Set();
var gAutoFit = true, gDropInFlight = false, gModePollBusy = false, gAnalysisMode = 'suite';
var server = { dir_a: '/span/A side', dir_b: '/span/B side' };
var lists = { fibers_a: [353, 354], fibers_b: [353, 354] };
var gInfo = null;
var els = { 'fiber-input': { value: '354', disabled: false, placeholder: '', title: '' },
            'btn-add': { disabled: false } };
var document = { getElementById: function (id) { return els[id] || null; } };
var posts = [], forgot = [];
function listing() {
  return { dir_a: server.dir_a, dir_b: server.dir_b, analysis_mode: 'suite',
           fibers_a: server.dir_a ? lists.fibers_a : [], fibers_b: server.dir_b ? lists.fibers_b : [] };
}
async function loadInfo() { gInfo = listing(); renderFilesPanel(); return true; }
function renderFilesPanel() { syncFiberBoxEnabled(); }
function fetch(url, opt) {
  var body = { ok: true };
  if (opt && opt.method === 'POST') {
    posts.push(url);
    var m = /sides=(\w+)/.exec(url);
    if (m && m[1].indexOf('a') >= 0) server.dir_a = '';
    if (m && m[1].indexOf('b') >= 0) server.dir_b = '';
  } else if (url.indexOf('/api/mode') === 0) {
    body = { analysis_mode: 'suite', dir_a: server.dir_a, dir_b: server.dir_b };
  }
  return Promise.resolve({ ok: true, json: function () { return Promise.resolve(body); } });
}
function forgetSide(d) {
  forgot.push(d);
  for (const k of [...gRemovedFiles]) if (k.startsWith(d + '-')) gRemovedFiles.delete(k);
}
function fileRowKeys() { return ['a-353', 'a-354', 'b-353', 'b-354'].filter(function (k) { return !gRemovedFiles.has(k); }); }
function prunePick() {} async function selectAfterRemove() {}
function renderChips() {} function fit() {} function draw() {} function renderEventTable() {}
function setReadout() {}
"""

_CASES = r"""
(async function () {
  var out = {};
  var box = els['fiber-input'], add = els['btn-add'];
  var snap = function () { return [box.disabled, add.disabled, box.placeholder, box.value]; };
  await loadInfo();
  out.loaded = snap();
  // some files removed: nothing is let go of, the box stays on
  ['a-353', 'b-353'].forEach(function (k) { gSelectedFiles.add(k); });
  removeSelectedFiles();
  await new Promise(function (r) { setTimeout(r, 0); });
  out.some = [posts.slice(), server.dir_a, server.dir_b, snap()];
  // the rest removed: both folders go and the box switches off
  box.value = '354';
  ['a-354', 'b-354'].forEach(function (k) { gSelectedFiles.add(k); });
  removeSelectedFiles();
  await new Promise(function (r) { setTimeout(r, 0); });
  out.all = [posts.slice(), server.dir_a, server.dir_b, snap()];
  // a folder set again (the hub's box): the page hears it and the box is back
  server.dir_a = '/span/A side';
  await pollAnalysisMode();
  out.back = [snap(), forgot.slice(), gRemovedFiles.size];
  // no folder at all from the start
  server.dir_a = ''; server.dir_b = ''; gRemovedFiles = new Set();
  await loadInfo();
  out.none = snap();
  print('OUT ' + JSON.stringify(out));
})().catch(function (e) { print('ERR ' + e + '\n' + e.stack); });
"""


@pytest.fixture(scope='module')
def res(tmp_path_factory):
    funcs = '\n'.join([_const('FIBERS_PLACEHOLDER'), _const('FIBERS_OFF_PLACEHOLDER'),
                       _const('MODE_POLL_MS')]
                      + [_js_func(n) for n in ('filesListed', 'syncFiberBoxEnabled', 'emptiedSides',
                                               'unloadEmptiedSides', 'fileAfterRemove',
                                               'removeSelectedFiles', 'pollAnalysisMode')])
    path = tmp_path_factory.mktemp('remove_all') / 'rm.js'
    path.write_text(_STUBS + funcs + '\n' + _CASES, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and 'OUT ' in out, out[-2000:]
    return json.loads(out.split('OUT ', 1)[1].strip())


@needs_jsc
def test_removing_some_files_keeps_the_folders(res):
    posts, dir_a, dir_b, box = res['some']
    assert posts == [] and dir_a and dir_b
    assert box[:2] == [False, False]
    assert res['loaded'][:3] == [False, False, '64, 67, 169-180']


@needs_jsc
def test_removing_the_last_file_clears_the_folders_and_switches_the_box_off(res):
    posts, dir_a, dir_b, box = res['all']
    assert posts == ['/api/unload_sides?sides=ab']
    assert (dir_a, dir_b) == ('', '')
    assert box == [True, True, OFF_TEXT, '']


@needs_jsc
def test_a_new_folder_brings_the_box_back(res):
    box, forgot, removed_left = res['back']
    assert box[:3] == [False, False, '64, 67, 169-180']
    assert forgot == ['a']                      # the old A side's removed rows are forgotten
    assert res['none'] == [True, True, OFF_TEXT, '']


def test_the_box_is_grayed_when_off():
    assert '#fiber-input:disabled { background: #eef1f4; cursor: not-allowed; }' in SRC
    assert '#btn-add:disabled' in SRC
    assert "const FIBERS_OFF_PLACEHOLDER = 'e.g. 354, add files first';" in SRC
    assert 'syncFiberBoxEnabled();' in _js_func('renderFilesPanel')
    for fn in ('removeFile', 'removeSelectedFiles'):
        assert 'unloadEmptiedSides();' in _js_func(fn), fn


def test_the_page_hears_folders_move_through_api_mode():
    port = TS.start_in_thread(8795)
    was = (TS.CONFIG.get('dir_a'), TS.CONFIG.get('dir_b'))
    try:
        TS.set_dirs(str(FIXTURE_SPLICE_A_DIR), None)
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/mode', timeout=4) as r:
            j = json.loads(r.read())
        assert j['dir_a'] == str(FIXTURE_SPLICE_A_DIR) and j['dir_b'] == ''
    finally:
        TS.set_dirs(*was)


@pytest.fixture
def _clean_server():
    TS.set_dirs(None, None)
    TS.CONFIG.pop('dropped_at', None)
    yield
    TS.set_dirs(None, None)
    TS.CONFIG.pop('dropped_at', None)


def _box(at, label):
    """The Traces tab's box (goes to the Traces tab first)."""
    return trace_box(at, label[0])


def _held(at):
    """What the A and B boxes hold, read without leaving the page."""
    return trace_box_value(at, 'a'), trace_box_value(at, 'b')


def test_the_hub_boxes_empty_when_the_viewer_lets_go(_clean_server):
    at = run_streamlit().run()
    _box(at, 'A Folder').input(str(FIXTURE_SPLICE_A_DIR)).run()
    _box(at, 'B Folder').input(str(FIXTURE_SPLICE_B_DIR)).run()
    go_tab(at, 'Viewer')                        # where the files are removed
    assert not at.exception, at.exception
    TS.unload_sides('ab')                       # every file removed in the Viewer
    at.run()                                    # the run the fragment asks for
    assert not at.exception, at.exception
    assert _held(at) == ('', '')
    assert (_box(at, 'A Folder').value, _box(at, 'B Folder').value) == ('', '')
    # one side only: the other box keeps its folder
    _box(at, 'A Folder').input(str(FIXTURE_SPLICE_A_DIR)).run()
    _box(at, 'B Folder').input(str(FIXTURE_SPLICE_B_DIR)).run()
    go_tab(at, 'Viewer')
    TS.unload_sides('b')
    at.run()
    assert _held(at) == (str(FIXTURE_SPLICE_A_DIR), '')
    assert (_box(at, 'A Folder').value, _box(at, 'B Folder').value) == (str(FIXTURE_SPLICE_A_DIR), '')


def test_the_hub_runs_itself_when_the_viewer_moves_the_folders():
    app = open(APP_PATH, encoding='utf-8').read()
    assert '@st.fragment(run_every=VIEWER_FOLDERS_TICK_S)\ndef _follow_viewer_folders():' in app
    body = app.split('def _follow_viewer_folders():', 1)[1].split('\n\n\n', 1)[0]
    assert "trace_server.CONFIG.get('dropped_at')" in body and "view_drop_seen" in body
    assert 'st.rerun()' in body and "scope='fragment'" not in body
    # drawn in the sidebar (OTDR Suite App: inside the Quick Analysis left
    # panel's Trace Folders block, one level further in)
    assert re.search(r'^        _follow_viewer_folders\(\)', app, re.M)


@needs_jsc
def test_the_status_line_keeps_clear_of_the_marker_readout(tmp_path):
    """The A/B marker readout holds the chart's top right corner; a status
    message centred over the chart ran under it.  With the readout up the
    status moves left, between the dB axis and the readout, and is cut to
    fit; with it down the status is centred again."""
    js = r"""
    var M = { l: 56, r: 16, t: 12, b: 36 };
    var ro = { style: { left: '', transform: '', maxWidth: '' } };
    var mk = { style: { display: 'none' }, offsetLeft: 455, dataset: {} };
    var document = { getElementById: function (id) { return id === 'readout' ? ro : mk; } };
    var out = {};
    placeReadout(); out.off = [ro.style.left, ro.style.transform, ro.style.maxWidth];
    mk.style.display = 'block';
    placeReadout(); out.on = [ro.style.left, ro.style.transform, ro.style.maxWidth];
    mk.style.display = 'none';
    placeReadout(); out.back = [ro.style.left, ro.style.transform, ro.style.maxWidth];
    print('OUT ' + JSON.stringify(out));
    """
    path = tmp_path / 'ro.js'
    path.write_text(_js_func('placeReadout') + '\n' + js, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = json.loads((r.stdout + r.stderr).split('OUT ', 1)[1].strip())
    assert out['off'] == ['', '', '']
    assert out['on'] == ['56px', 'none', f'{455 - 56 - 10}px']
    assert out['back'] == ['', '', '']
    assert 'placeReadout();' in _js_func('setReadout')
    assert _js_func('updateMarkerReadout').count('placeReadout();') == 2
