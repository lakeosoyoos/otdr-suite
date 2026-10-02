"""Add clicked twice loads every trace once (demo list #33).

Double-clicking Add (or Enter, then Add) with "354" in the box loaded F354
twice: gTraces came back a-354, b-354, a-354, b-354, every trace was drawn
twice and the Summary Report PDF listed each one twice.  addFibers plans
what is missing and then awaits its fetches, so a second click planned the
same keys again.  Now a trace on its way is never planned again
(gLoadingKeys) and loadOne never pushes a key that is already loaded.

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
var gRemovedFiles = new Set(), gAutoFit = true, gLoadingKeys = new Set(), gLoadEpoch = 0;
var gInfo = { dir_a: '/a', dir_b: '/b', fibers_a: [353, 354], fibers_b: [353, 354] };
var box = { value: '354' };
var document = { getElementById: function () { return box; } };
var console = { warn: function () {} };
var fetches = 0;
function fetch(url) {
  fetches++;
  var m = /dir=(\w)&fiber=(\d+)/.exec(url);
  return new Promise(function (res) {
    setTimeout(function () {
      res({ ok: true, json: function () {
        return Promise.resolve({ dist_km: [0, 1], trace_db: [0, 1], events: [], fiber: +m[2] }); } });
    }, 20);
  });
}
function effDir(d) { return d; }
function nextColor() { return 0; }
function traceColor() { return 0; }
function syncFileMarks() {} function renderChips() {} function fit() {} function draw() {}
function renderEventTable() {} function setReadout() {}
async function loadOverview() { throw new Error('not in this test'); }
"""

_CASES = r"""
(async function () {
  var out = {};
  // a double-click: the second Add starts while the first is still fetching
  await Promise.all([addFibers(), addFibers()]);
  out.double = [gTraces.map(function (t) { return t.key; }), fetches];
  // a third Add after both settled: nothing new, nothing fetched
  await addFibers();
  out.again = [gTraces.length, fetches];
  // loadOne asked for a key that landed meanwhile does not push it again
  await Promise.all([loadOne('a-353', 353, 'a'), loadOne('a-353', 353, 'a')]);
  out.load_one = gTraces.filter(function (t) { return t.key === 'a-353'; }).length;
  out.in_flight_left = gLoadingKeys.size;
  print('OUT ' + JSON.stringify(out));
})().catch(function (e) { print('ERR ' + e + '\n' + e.stack); });
"""


@pytest.fixture(scope='module')
def res(tmp_path_factory):
    funcs = '\n'.join([_const('MAX_DETAIL_TRACES'), _const('MAX_OVERVIEW_FIBERS')]
                      + [_js_func(n) for n in ('parseFibers', 'planFiberLoad', 'absentNote',
                                               'planOrSay', 'loadFailNote', 'fetchRetry',
                                               'loadOne', 'addFibers')])
    path = tmp_path_factory.mktemp('add_twice') / 'add.js'
    path.write_text(_STUBS + funcs + '\n' + _CASES, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and 'OUT ' in out, out[-2000:]
    return json.loads(out.split('OUT ', 1)[1].strip())


@needs_jsc
def test_a_double_click_on_add_loads_each_trace_once(res):
    keys, fetches = res['double']
    assert sorted(keys) == ['a-354', 'b-354']
    assert fetches == 2                         # and asks the server once each


@needs_jsc
def test_adding_what_is_loaded_does_nothing(res):
    assert res['again'] == [2, 2]


@needs_jsc
def test_load_one_never_pushes_a_key_twice(res):
    assert res['load_one'] == 1
    assert res['in_flight_left'] == 0


def test_every_load_path_skips_a_loaded_key():
    over = _js_func('loadOverview')
    assert 'gTraces.some(t => t.key === key)' in over
    sel = _js_func('applyFileSelection')
    assert 'loadOne(' in sel                    # the FILES list goes through loadOne too
