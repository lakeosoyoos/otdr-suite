"""Remove every file, then type a fiber: it loads (demo list #34).

Removing every file in the FILES panel ("Remove N marked files") let go of
the trace server's folders (/api/unload_sides), while the hub's A/B boxes
still showed them until its next run.  Typing a fiber and Add then said
"no A/B folder is set".  Removing some files never did that.  Now Remove only
takes rows out of the list, whatever it takes; the folders stay what the
hub set, and a fiber typed in the box loads from them (and is listed again).

Run in JavaScriptCore where present.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / 'viewer.html').read_text(encoding='utf-8')
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


def _const(name):
    m = re.search(r'^const ' + re.escape(name) + r' = [^;]+;', SRC, re.M)
    assert m, name
    return m.group(0)


_STUBS = r"""
var gSelNext = null, gAddDir = 'both', gTraces = [], gLoadFailures = [], gStoredDir = {};
var gRemovedFiles = new Set(), gAutoFit = true, gLoadingKeys = new Set();
var gSelectedFiles = new Set(), COLORS_USED = new Set();
var gInfo = { dir_a: '/span/A side', dir_b: '/span/B side', fibers_a: [353, 354], fibers_b: [353, 354] };
var box = { value: '' }, readout = '';
var document = { getElementById: function () { return box; } };
var console = { warn: function () {} };
var posts = [];
function fetch(url, opt) {
  if (opt && opt.method === 'POST') posts.push(url);
  var m = /dir=(\w)&fiber=(\d+)/.exec(url);
  return Promise.resolve({ ok: true, json: function () {
    return Promise.resolve({ dist_km: [0, 1], trace_db: [0, 1], events: [], fiber: m ? +m[2] : 0 }); } });
}
function effDir(d) { return d; }
function nextColor() { return 0; }
function fileRowKeys() { return ['a-353', 'a-354', 'b-353', 'b-354'].filter(function (k) { return !gRemovedFiles.has(k); }); }
function prunePick() {}
async function selectAfterRemove() {}
function syncFileMarks() {} function renderChips() {} function fit() {} function draw() {}
function renderEventTable() {}
function setReadout(s) { readout = String(s); }
async function loadOverview() { throw new Error('not in this test'); }
// What the server did when the page let go of an emptied side (the page's
// own unloadEmptiedSides, before #34): the folder is gone.
async function unloadEmptiedSides() {
  var s = emptiedSides();
  if (!s) return;
  await fetch('/api/unload_sides?sides=' + s, { method: 'POST' });
  if (s.indexOf('a') >= 0) gInfo.dir_a = '';
  if (s.indexOf('b') >= 0) gInfo.dir_b = '';
}
"""

_CASES = r"""
(async function () {
  var out = {};
  box.value = '354';
  await addFibers();
  // every file marked and removed
  ['a-353', 'a-354', 'b-353', 'b-354'].forEach(function (k) { gSelectedFiles.add(k); });
  removeSelectedFiles();
  await Promise.resolve();
  out.after_remove = [gTraces.length, posts.slice(), gInfo.dir_a, emptiedSides()];
  out.remove_says = readout;
  // the tech types a fiber
  box.value = '354';
  await addFibers();
  out.typed = [gTraces.map(function (t) { return t.key; }).sort(), readout];
  out.listed_again = fileRowKeys();
  print('OUT ' + JSON.stringify(out));
})().catch(function (e) { print('ERR ' + e + '\n' + e.stack); });
"""


@pytest.fixture(scope='module')
def res(tmp_path_factory):
    funcs = '\n'.join([_const('MAX_DETAIL_TRACES'), _const('MAX_OVERVIEW_FIBERS')]
                      + [_js_func(n) for n in ('parseFibers', 'planFiberLoad', 'absentNote',
                                               'planOrSay', 'loadFailNote', 'fetchRetry',
                                               'loadOne', 'addFibers', 'fileAfterRemove',
                                               'removeSelectedFiles', 'emptiedSides')])
    path = tmp_path_factory.mktemp('remove_all') / 'rm.js'
    path.write_text(_STUBS + funcs + '\n' + _CASES, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and 'OUT ' in out, out[-2000:]
    return json.loads(out.split('OUT ', 1)[1].strip())


@needs_jsc
def test_removing_every_file_keeps_the_hub_folders(res):
    n, posts, dir_a, emptied = res['after_remove']
    assert n == 0
    assert posts == []                          # no /api/unload_sides
    assert dir_a == '/span/A side'
    assert emptied == 'ab'                      # a drop still treats both sides as free
    assert 'type a fiber number to load it again' in res['remove_says']


@needs_jsc
def test_a_typed_fiber_loads_after_everything_was_removed(res):
    keys, said = res['typed']
    assert keys == ['a-354', 'b-354']
    assert 'no A/B folder is set' not in said
    assert res['listed_again'] == ['a-354', 'b-354']


def test_nothing_in_the_page_lets_go_of_the_folders_on_remove():
    assert 'fetch(`/api/unload_sides' not in SRC
    assert 'unloadEmptiedSides()' not in SRC


def test_a_message_never_moves_the_chart():
    """The status line said "no A/B folder is set" in the toolbar, took a line
    of its own there (360 px wide) and pushed the chart and table down a line.
    It now floats over the top of the chart and takes no room."""
    toolbar = SRC.split('<div id="toolbar">', 1)[1].split('<div id="toolbar-resizer"', 1)[0]
    assert 'id="readout"' not in toolbar
    wrap = SRC.split('<div id="canvas-wrap">', 1)[1].split('<div id="event-resizer"', 1)[0]
    assert '<div id="readout"></div>' in wrap
    css = SRC.split('  #readout {', 1)[1].split('}', 1)[0]
    assert 'position: absolute;' in css
    assert 'min-width' not in css
    assert 'overflow: hidden;' in css and 'text-overflow: ellipsis;' in css
