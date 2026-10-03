"""A drop on the Viewer goes by what was dropped, draws what was dropped, and
works on the chart too (demo list #31, 2026-10-01).

Three things went wrong after a drop:

* Its staging folder's names reached the tech: the hub's A/B boxes read
  "/var/folders/.../T/otdr_viewer_drop_jnwvux7o/A", and the Summary Report's
  title, footer, folder rows and default file name said
  "otdr_viewer_drop_jnwvux7o".  Each staged side is now named after the folder
  its files came from (or the one file, or the site the names carry, or
  "Dropped files (N)"): trace_server.drop_name, served as dir_a_name /
  dir_b_name, and shown in the hub's boxes as "<name> (dropped)" while the box
  still stands for the staged folder the tools run on.
* The chart stayed blank, "0 of 4 selected": the drop now selects (loads)
  every file of the sides it wrote.
* A drop on the chart was thrown away: the chart and table take it as the
  FILES panel does.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import urllib.request
import zipfile

import pytest

from conftest import (run_streamlit, import_trace_server, go_tab, trace_box,
                      trace_box_value, VIEWER_DIR, APP_PATH)
from test_sor_writer import make_sor

TS = import_trace_server()
SRC = (VIEWER_DIR / 'viewer.html').read_text(encoding='utf-8')
JSC = ('/System/Library/Frameworks/JavaScriptCore.framework/'
       'Versions/Current/Helpers/jsc')
needs_jsc = pytest.mark.skipif(not os.path.exists(JSC),
                               reason='JavaScriptCore shell (macOS) not present')


@pytest.fixture(autouse=True)
def _own_temp(tmp_path, monkeypatch):
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    import tempfile
    tempfile.tempdir = None
    TS.set_dirs(None, None)
    TS.CONFIG.pop('dropped_at', None)
    yield
    tempfile.tempdir = None
    TS.CONFIG.pop('dropped_at', None)
    TS.set_dirs(None, None)


A_NAMES = ['SITEA.SITEB.1550.%04d.sor' % n for n in (349, 350, 351)]
B_NAMES = ['SITEB.SITEA.1550.%04d.sor' % n for n in (349, 350, 351)]


def _drop(names, folders=None, zips=()):
    tok = TS.drop_begin()
    for n in names:
        assert TS.drop_file(tok, n, make_sor(ior=1.47))['files'] == 1
    for zname, body in zips:
        TS.drop_file(tok, zname, body)
    return TS.drop_end(tok, folders=folders)


def _no_staging_name(*texts):
    for t in texts:
        assert 'otdr_viewer_drop_' not in str(t) and '/T/' not in str(t), t


# ── what each staged side is called ─────────────────────────────────────

def test_a_side_goes_by_the_folder_it_was_dropped_from():
    folders = {n: 'A side' for n in A_NAMES}
    folders.update({n: 'B side' for n in B_NAMES})
    out = _drop(A_NAMES + B_NAMES, folders)
    assert (out['a_name'], out['b_name']) == ('A side', 'B side')
    assert TS.drop_name(out['dir_a']) == 'A side'
    assert TS.drop_name(out['dir_b']) == 'B side'
    _no_staging_name(out['a_name'], out['b_name'])


def test_loose_files_go_by_their_site_one_file_by_its_name():
    out = _drop(A_NAMES + B_NAMES)              # dragged as files: no folder
    assert (out['a_name'], out['b_name']) == ('SITEA', 'SITEB')
    TS.set_dirs(None, None)
    out = _drop([A_NAMES[0]])
    assert out[out['added'].lower() + '_name'] == 'SITEA.SITEB.1550.0349'


def test_files_whose_names_carry_no_site_are_counted():
    out = _drop(['0001_1550.sor', '0002_1550.sor', '0003_1550.sor'])
    assert out[out['added'].lower() + '_name'] == 'Dropped files (3)'


def test_a_zip_names_each_side_by_its_folder_inside():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for n in A_NAMES:
            zf.writestr('Span review/A side/' + n, make_sor(ior=1.47))
        for n in B_NAMES:
            zf.writestr('Span review/B side/' + n, make_sor(ior=1.47))
    out = _drop([], zips=[('Span review.zip', buf.getvalue())])
    assert (out['a_name'], out['b_name']) == ('A side', 'B side')
    # members at the zip's top go by the zip's own name
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for n in A_NAMES:
            zf.writestr(n, make_sor(ior=1.47))
    TS.set_dirs(None, None)
    out = _drop([], zips=[('A shots.zip', buf.getvalue())])
    assert TS.drop_name(TS.CONFIG['dir_a'] if out['added'] == 'A' else TS.CONFIG['dir_b']) \
        == 'A shots'


def test_a_folder_name_from_the_page_is_plain_text():
    assert TS._clean_folder_name('../../etc\x00') == 'etc'
    assert TS._clean_folder_name('..') == ''
    assert len(TS._clean_folder_name('x' * 500)) == 120


def test_the_listing_serves_the_drop_name_not_the_staging_folder():
    folders = {n: 'A side' for n in A_NAMES}
    folders.update({n: 'B side' for n in B_NAMES})
    _drop(A_NAMES + B_NAMES, folders)
    port = TS.start_in_thread(8795)
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/list', timeout=10) as r:
        info = json.loads(r.read())
    assert (info['dir_a_name'], info['dir_b_name']) == ('A side', 'B side')
    assert info['dir_a_dropped'] is True and info['dir_b_dropped'] is True
    # a folder the tech picked is still its own name
    TS.set_dirs(os.environ['TMPDIR'], None)
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/list', timeout=10) as r:
        info = json.loads(r.read())
    assert info['dir_a_name'] == os.path.basename(os.environ['TMPDIR'].rstrip('/'))
    assert info['dir_a_dropped'] is False


# ── the hub's A/B boxes ─────────────────────────────────────────────────

def _box(at, label):
    """The Traces tab's box (goes to the Traces tab first)."""
    return trace_box(at, label[0])


def _held(at, label):
    """What a Traces tab box holds, read without leaving the page."""
    return trace_box_value(at, label[0])


def test_the_hub_boxes_name_the_drop_and_the_tools_still_run_on_it():
    at = run_streamlit().run()
    folders = {n: 'A side' for n in A_NAMES}
    folders.update({n: 'B side' for n in B_NAMES})
    out = _drop(A_NAMES + B_NAMES, folders)
    go_tab(at, 'Splice Report')
    assert not at.exception, at.exception
    assert _held(at, 'A Folder') == 'A side (dropped)'
    assert _held(at, 'B Folder') == 'B side (dropped)'
    # The Splice Report runs on the staged folders behind the names.
    assert any('the A and B folders loaded on the Traces tab' in c.value for c in at.caption)
    assert (TS.CONFIG['dir_a'], TS.CONFIG['dir_b']) == (out['dir_a'], out['dir_b'])
    # The Traces tab's boxes show the names, not the staging folder.
    assert _box(at, 'A Folder').value == 'A side (dropped)'
    assert _box(at, 'B Folder').value == 'B side (dropped)'
    # A new session seeded from the trace server shows the names as well.
    at2 = run_streamlit().run()
    assert not at2.exception, at2.exception
    assert _box(at2, 'A Folder').value == 'A side (dropped)'
    assert _box(at2, 'B Folder').value == 'B side (dropped)'
    # A folder typed over the name is that folder again.
    other = os.path.join(os.environ['TMPDIR'], 'picked_a')
    os.makedirs(other)
    _box(at, 'A Folder').input(other).run()
    assert not at.exception, at.exception
    assert _box(at, 'A Folder').value == other


def test_a_link_back_with_the_staged_path_shows_the_name():
    """The Viewer's Back button carries the staged path (?sra=): the box
    names the drop instead of printing it."""
    run_streamlit().run()                       # the first hub of a run starts empty
    out = _drop(A_NAMES + B_NAMES, {n: 'A side' for n in A_NAMES})
    at = run_streamlit()
    at.session_state['view_dir_a_input'] = out['dir_a']
    at.run()
    assert not at.exception, at.exception
    assert _held(at, 'A Folder') == 'A side (dropped)'
    assert _box(at, 'A Folder').value == 'A side (dropped)'


def test_the_hub_label_rule():
    """The boxes are labeled on every run, after a drop is picked up and
    before any page (the Traces tab included) draws them."""
    app = open(APP_PATH, encoding='utf-8').read()
    m = re.search(r'^_label_drop_boxes\(\)$', app, re.M)
    assert m, 'the boxes are not labeled at module level on every run'
    assert app.index("_drop_at = trace_server.CONFIG.get('dropped_at')") < m.start()
    assert m.start() < app.index('\ndef page_traces(')
    body = app.split('def _panel_boxes():', 1)[1].split('\ndef ', 1)[0]
    assert "_drop_box_{side}" in body


# ── the page: names, what it draws, where it takes a drop ────────────────

def _js_func(name):
    m = re.search(r'(?:async\s+)?function\s+' + re.escape(name) + r'\s*\([^)]*\)\s*\{', SRC)
    assert m, 'viewer.html no longer defines %s()' % name
    i, depth = m.end(), 1
    while i < len(SRC) and depth:
        depth += {'{': 1, '}': -1}.get(SRC[i], 0)
        i += 1
    return SRC[m.start():i]


_STUBS = r"""
const DROP_EXTS = ['.sor', '.json', '.trc', '.zip', '.olts'];
const DROP_BATCH_FILES = 32, DROP_BATCH_BYTES = 4 * 1024 * 1024;
var gDropFolder = new WeakMap(), gDropInFlight = false, gAutoFit = true;
var gRemovedFiles = new Set(), gTraces = [], gInfo = null, gLoadFailures = [];
var sent = { end: null }, picked = null, readout = null, forgot = [];
var answer = {};
var el = { textContent: '' };
var document = { getElementById: function () { return el; } };
function _packDropBatch(files) { return 'batch'; }
function emptiedSides() { return ''; }
function forgetSide(d) { forgot.push(d); }
function renderChips() {} function fit() {} function draw() {} function renderEventTable() {}
function loadFailNote() { return ''; }
function reportDropFailure(e) { print('DROPFAIL ' + e); }
function setReadout(s, x) { readout = [s, x]; }
var signs = [];
function showDropSign(state, small) { signs.push([state, small || '']); }
function hideDropSign() { signs.push(['hide', '']); }
async function selectFiles(want) { picked = Array.from(want).sort(); }
var listing = null;
async function loadInfo() { gInfo = listing; return true; }
function fetch(url, opt) {
  var body = { ok: true };
  if (url.indexOf('/api/drop_begin') === 0) body.token = 't1';
  if (url.indexOf('/api/drop_end') === 0) { sent.end = opt.body; body = Object.assign({ ok: true }, answer); }
  return Promise.resolve({ ok: true, json: function () { return Promise.resolve(body); } });
}
"""

_CASES = r"""
(async function () {
  var out = {};
  // A side and B side dragged in as two folders: 2 + 2 files
  var fa = [{ name: 'A1.sor', size: 1 }, { name: 'A2.sor', size: 1 }];
  var fb = [{ name: 'B1.sor', size: 1 }, { name: 'B2.sor', size: 1 }];
  fa.forEach(function (f) { gDropFolder.set(f, 'A side'); });
  fb.forEach(function (f) { gDropFolder.set(f, 'B side'); });
  answer = { dir_a: '/t/A', dir_b: '/t/B', added: 'AB', a_count: 2, b_count: 2,
             new_keys: ['a-1', 'a-2', 'b-1', 'b-2'] };
  listing = { dir_a: '/t/A', dir_b: '/t/B', fibers_a: [1, 2], fibers_b: [1, 2] };
  await handleFilesDrop(fa.concat(fb));
  out.folders = JSON.parse(sent.end).folders;
  out.picked = picked;
  out.in_flight = gDropInFlight;
  out.signs = signs.slice();
  // B dropped on its own next to A traces already on the chart: A stays, B is added
  gTraces = [{ key: 'a-1' }, { key: 'a-2' }];
  picked = null;
  answer = { dir_a: '/t/A', dir_b: '/t/B2', added: 'B', b_count: 2, new_keys: ['b-1', 'b-2'] };
  listing = { dir_a: '/t/A', dir_b: '/t/B2', fibers_a: [1, 2], fibers_b: [1, 2] };
  await handleFilesDrop([{ name: 'B1.sor', size: 1 }, { name: 'B2.sor', size: 1 }]);
  out.second = picked;
  // a removed file stays out
  gTraces = []; picked = null; gRemovedFiles = new Set(['a-2']);
  answer = { dir_a: '/t/A3', added: 'A', a_count: 2, new_keys: ['a-1', 'a-2'] };
  listing = { dir_a: '/t/A3', dir_b: '', fibers_a: [1, 2], fibers_b: [] };
  await handleFilesDrop([{ name: 'A1.sor', size: 1 }, { name: 'A2.sor', size: 1 }]);
  out.removed = picked;
  // more of A dropped beside it: A grows in place, only the new rows join,
  // and a removed file dragged in again is put back
  gTraces = [{ key: 'a-1' }]; picked = null; gRemovedFiles = new Set(['a-2']);
  answer = { dir_a: '/t/A3', added: 'A', a_count: 3, grown: 'A', new_keys: ['a-3'],
             already: ['A2.sor'], already_keys: ['a-2'] };
  listing = { dir_a: '/t/A3', dir_b: '', fibers_a: [1, 2, 3], fibers_b: [] };
  await handleFilesDrop([{ name: 'A2.sor', size: 1 }, { name: 'A3.sor', size: 1 }]);
  out.grown = picked;
  out.grown_removed = [...gRemovedFiles];
  // the Summary Report's job name: a dropped side's served name, as is
  gInfo = { dir_a_name: 'A', dir_a: '/x/otdr_viewer_drop_q/A', dir_a_dropped: true,
            dir_b_name: 'B side', dir_b: '/x/otdr_viewer_drop_q/B', dir_b_dropped: true };
  out.job = reportJob();
  gInfo = { dir_a_name: 'A', dir_a: '/jobs/SPAN-1/A', dir_b_name: '(none)', dir_b: '' };
  out.job_picked = reportJob();
  out.parent = [_parentName('/A side/x.sor'), _parentName('/x.sor'), _parentName('/full/B side/y.sor')];
  print('OUT ' + JSON.stringify(out));
})().catch(function (e) { print('ERR ' + e + '\n' + e.stack); });
"""


@pytest.fixture(scope='module')
def page(tmp_path_factory):
    funcs = '\n'.join(_js_func(n) for n in ('_parentName', '_dropOk', '_dropBatches',
                                            'handleFilesDrop', 'isOltsFile', 'folderLabel',
                                            'reportJob'))
    path = tmp_path_factory.mktemp('drop_names') / 'drop.js'
    path.write_text(_STUBS + funcs + '\n' + _CASES, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and 'OUT ' in out, out[-2000:]
    return json.loads(out.split('OUT ', 1)[1].strip())


@needs_jsc
def test_the_page_tells_the_server_where_each_file_came_from(page):
    assert page['folders'] == {'A1.sor': 'A side', 'A2.sor': 'A side',
                               'B1.sor': 'B side', 'B2.sor': 'B side'}
    assert page['parent'] == ['A side', '', 'B side']


@needs_jsc
def test_a_drop_draws_what_it_dropped(page):
    assert page['picked'] == ['a-1', 'a-2', 'b-1', 'b-2']
    assert page['in_flight'] is False
    # the other side's traces stay on the chart beside the new side
    assert page['second'] == ['a-1', 'a-2', 'b-1', 'b-2']
    assert page['removed'] == ['a-1']
    # a drop that grows a side adds its new rows and puts back a removed one
    assert page['grown'] == ['a-1', 'a-2', 'a-3']
    assert page['grown_removed'] == []


@needs_jsc
def test_the_files_panel_says_a_drop_is_loading_then_what_loaded(page):
    """Robert 2026-10-01: the panel lights up for a drop and shows it happened."""
    states = [st for st, _ in page['signs']]
    assert states[0] == 'load' and states[-1] == 'done'
    assert 'hide' not in states
    assert any('uploading 4 / 4 files' in small for st, small in page['signs'] if st == 'load')
    assert page['signs'][-1][1] == 'A: (2 files) · B: (2 files)'


@needs_jsc
def test_the_report_names_the_drop_never_its_staging_folder(page):
    assert page['job'] == 'A / B side'           # the served name, even one called "A"
    assert page['job_picked'] == 'SPAN-1'      # a picked folder "A": its parent, as before


def test_only_the_files_panel_takes_a_drop():
    """Robert 2026-10-01: a drop does nothing anywhere but the FILES panel,
    where the sign lights up; the rest of the Viewer refuses it and the panel
    shows where to go."""
    boot = SRC.split('// Only the FILES panel takes a drop', 1)[1].split('})();', 1)[0]
    panel = boot.split("panel.addEventListener('drop'", 1)[1].split('});', 1)[0]
    assert 'handleFilesDrop(ev.dataTransfer)' in panel
    over = boot.split("window.addEventListener('dragover'", 1)[1].split('});', 1)[0]
    assert "dropEffect = 'none'" in over and 'ev.preventDefault()' in over
    assert "showDropSign('hint')" in over and 'panel.contains(ev.target)' in over
    drop = boot.split("window.addEventListener('drop'", 1)[1].split('});', 1)[0]
    assert 'ev.preventDefault()' in drop and 'handleFilesDrop' not in drop
    # the chart and the event table no longer take a drop of their own
    assert "main.addEventListener('drop'" not in SRC
    assert "dropEffect = 'copy'" in boot.split("const take", 1)[1].split('};', 1)[0]
    # a drag over the hub page around the Viewer only shows the hint
    assert "if (d && d.type === 'otdr-drag') { showDropSign('hint'); return; }" in SRC
    assert "'otdr-drop'" not in SRC
    assert '<div id="drop-sign">' in SRC and '#drop-sign.hint' in SRC
