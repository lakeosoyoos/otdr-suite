"""Remove This File / Remove This Fiber (Robert 2026-10-01).

With the whole span marked, the right-click menu's only Remove was "Remove N
Marked Files": one file could not come off without first narrowing the chart
down to it, and after the Remove the chart held the next file alone.  Now,
with more than one file marked, the menu offers

  Remove This File (F200 A->B)                the row under the mouse
  Remove This Fiber (F200, Both Directions)   both files of that fibre
  Remove N Marked Files from Viewer           every marked file, as before

and with one file marked just "Remove This File".  The first two leave the
rest of the chart on it; the Fibers box follows ("1-199, 201-432").  Put Back
brings removed files back to the list and, next to traces on the chart, onto
the chart (as overview traces next to a whole cable).

The page runs in JavaScriptCore where present.
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


_STUBS = r"""
var gRemovedFiles = new Set(), gSelectedFiles = new Set(), COLORS_USED = new Set();
var gTraces = [], gAutoFit = true, gSelNext = null, readout = '', nexts = [], overview = [];
var MAX_DETAIL_TRACES = 48;
function span() {
  gTraces = []; gRemovedFiles.clear();
  for (var f = 1; f <= 432; f++) ['a', 'b'].forEach(function (d) {
    gTraces.push({ key: d + '-' + f, fiber: f });
  });
  syncFileMarks();
}
function fileRowKeys() {
  var out = [];
  for (var f = 1; f <= 432; f++) ['a', 'b'].forEach(function (d) {
    if (!gRemovedFiles.has(d + '-' + f)) out.push(d + '-' + f);
  });
  return out;
}
function syncFileMarks() { gSelectedFiles.clear(); gTraces.forEach(function (t) { gSelectedFiles.add(t.key); }); }
function splitFileKey(key) { var i = key.indexOf('-'); return [key.slice(0, i), +key.slice(i + 1)]; }
function fileAfterRemove(keys, removed) {
  var rest = keys.filter(function (k) { return !removed.has(k); });
  return rest.length ? rest[0] : null;
}
function selectAfterRemove(next) { if (next && !gSelectedFiles.size) nexts.push(next); }
function renderChips() {} function fit() {} function draw() {} function renderEventTable() {}
function unloadEmptiedSides() {} function setReadout(t) { readout = t; }
async function loadOverview(tasks, ld) {
  tasks.forEach(function (t) { overview.push(t.key); gTraces.push({ key: t.key, fiber: t.f }); });
  if (ld) loadProgress(ld, tasks.length);
  return true;
}
var loads = [];
function beginLoad(tasks) { var ld = { done: 0, total: tasks.length, ended: false }; loads.push(ld); return ld; }
function loadProgress(ld, n) { ld.done = Math.min(ld.total, ld.done + n); }
function endLoad(ld) { ld.ended = true; }
async function loadOne(key, f) { gTraces.push({ key: key, fiber: f }); }
function fibersOnChart() {
  var s = {}; gTraces.forEach(function (t) { s[t.fiber] = 1; }); return Object.keys(s).length;
}
"""

_CASES = r"""
(async function () {
  var out = {};
  span();
  removeOnly(['a-200'], 'F200 A→B');
  out.one = [gTraces.length, gSelectedFiles.size, [...gRemovedFiles], nexts.length, readout];
  removeOnly(['a-300', 'b-300'], 'F300 (both directions)');
  out.fiber = [gTraces.length, fibersOnChart(), [...gRemovedFiles].sort()];
  removeOnly(['a-200'], 'again');                      // already out of the list
  out.again = gTraces.length;
  await putBackRemovedFiles();
  out.back = [gTraces.length, gRemovedFiles.size, overview.sort(), readout];
  out.backLoads = loads.map(function (ld) { return [ld.done, ld.total, ld.ended]; });
  // the last file on the chart: FastReporter's next file
  gTraces = [{ key: 'a-5', fiber: 5 }]; syncFileMarks(); nexts = [];
  removeOnly(['a-5'], 'F5 A→B');
  out.last = [gTraces.length, nexts];
  print('OUT ' + JSON.stringify(out));
})().catch(function (e) { print('ERR ' + e + '\n' + e.stack); });
"""


@pytest.fixture(scope='module')
def res(tmp_path_factory):
    funcs = '\n'.join(_js_func(n) for n in ('removeOnly', 'putBackRemovedFiles'))
    path = tmp_path_factory.mktemp('remove_this') / 'rt.js'
    path.write_text(_STUBS + funcs + '\n' + _CASES, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and 'OUT ' in out, out[-2000:]
    return json.loads(out.split('OUT ', 1)[1].strip())


@needs_jsc
def test_remove_this_file_keeps_the_rest_of_the_chart(res):
    n, marked, removed, nexts, readout = res['one']
    assert (n, marked, removed, nexts) == (863, 863, ['a-200'], 0)
    assert readout.startswith('F200 A→B removed from the viewer and its reports')


@needs_jsc
def test_remove_this_fiber_takes_both_directions(res):
    n, fibers, removed = res['fiber']
    assert n == 861 and fibers == 431 and removed == ['a-200', 'a-300', 'b-300']
    assert res['again'] == 861


@needs_jsc
def test_put_back_returns_them_to_the_chart_next_to_a_whole_cable(res):
    n, removed, overview, readout = res['back']
    assert n == 864 and removed == 0
    assert overview == ['a-200', 'a-300', 'b-300']
    assert 'put back in the list, the chart and the reports' in readout


@needs_jsc
def test_put_back_is_counted_like_any_other_load(res):
    """The put-back shows on the status line and in the Fibers box while it
    loads (beginLoad), as an Add does, and the count ends when it lands."""
    assert res['backLoads'] == [[3, 3, True]]


@needs_jsc
def test_removing_the_last_file_on_the_chart_moves_to_the_next(res):
    assert res['last'] == [0, ['a-1']]


def test_the_menu_offers_this_file_this_fiber_and_the_marked_set():
    menu = SRC.split('function showFileDirMenu(', 1)[1].split('\nasync function ', 1)[0]
    assert "const fileLabel = `F${fiber} ${effDir(src, fiber) === 'a' ? 'A→B' : 'B→A'}`;" in menu
    assert 'splitFileKey(key)[1] === +fiber' in menu
    assert '<button data-remove-one="1">Remove This File (${fileLabel})</button>' in menu
    assert '<button data-remove-fiber="1">Remove This Fiber (F${fiber}, Both Directions)</button>' in menu
    assert 'fiberKeys.length > 1' in menu
    assert "removeOnly([k], fileLabel)" in menu
    assert "removeOnly(fiberKeys, `F${fiber} (both directions)`)" in menu
    # one file marked: only Remove This File
    single = menu.split(': `<button data-remove="1">', 1)[1]
    assert single.startswith('Remove This File (${fileLabel})</button>`)')
