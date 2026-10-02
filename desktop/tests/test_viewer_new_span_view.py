"""A new span starts the view over (Viewer 432 load audit, 2026-10-01).

When the folders move and nothing of the old span is left on the chart,
forgetSide starts the view over as Clear All does:

  zoom     a drop of a 2.07 km span onto a Viewer zoomed to 41.9-45.9 km of the
           55 km span kept that window, so the chart was blank until Fit
  Dir      a drop in the pop-out window reached the hub's Viewer one side per
           /api/mode tick: A went first, the B traces left set the Dir buttons
           to "<- B", B went next and the empty chart left them there, so the
           next Add loaded B alone (12 of 864)

A drop of the other direction of the same span leaves the first side on the
chart, so its zoom and Dir stay.

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
var gTraces = [], gRemovedFiles = new Set(), COLORS_USED = new Set();
var gLoadingKeys = new Set(), gLoadEpoch = 0;
var gDirOverride = {}, gStoredDir = {}, gFileAnchor = null, gFileFocus = null;
var gAutoFit = false, gView = { x0: 41.91, x1: 45.91, y0: -9.9, y1: -0.7 };
var gMarkers = { a: 43.9, b: 44.1 }, gDragMarker = null, gPickKey = 'a-146';
var gAddDir = 'both', gDropInFlight = false, gModePollBusy = false, gAnalysisMode = 'suite';
var server = { dir_a: '/span1/A', dir_b: '/span1/B' };
var gInfo = { dir_a: '/span1/A', dir_b: '/span1/B' };
var buttons = ['a', 'b', 'both'].map(function (d) {
  return { dataset: { dir: d }, classList: { on: d === 'both',
    toggle: function (c, v) { this.on = v; } } };
});
var box = { value: '' };
var document = {
  activeElement: null,
  getElementById: function (id) { return id === 'fiber-input' ? box : null; },
  querySelectorAll: function () { return buttons; },
};
var fits = 0;
function forgetFileFacts() {} function syncFileMarks() {}
function fetch(url) {
  var body = { analysis_mode: 'suite', dir_a: server.dir_a, dir_b: server.dir_b };
  return Promise.resolve({ json: function () { return Promise.resolve(body); } });
}
function applyHubTheme() {}
async function loadInfo() { gInfo = { dir_a: server.dir_a, dir_b: server.dir_b }; return true; }
function renderChips() { syncFiberBox(); }
function fit() { fits++; gAutoFit = true; } function draw() {} function renderEventTable() {}
function load(dirs) {
  gTraces = [];
  dirs.forEach(function (d) {
    for (var f = 1; f <= 12; f++) gTraces.push({ key: d + '-' + f, fiber: f, dir: d, src: d });
  });
  gAddDir = dirs.length === 2 ? 'both' : dirs[0];
}
function snap() {
  return { view: gView, autoFit: gAutoFit, markers: gMarkers, pick: gPickKey, dir: gAddDir,
           active: buttons.filter(function (b) { return b.classList.on; })
                          .map(function (b) { return b.dataset.dir; }),
           traces: gTraces.length };
}
"""

_CASES = r"""
(async function () {
  var out = {};
  // the other window's drop reaches this Viewer A first, then B
  load(['a', 'b']);
  server.dir_a = '/drop/A';
  await pollAnalysisMode();
  out.after_a = snap();
  server.dir_b = '/drop/B';
  await pollAnalysisMode();
  out.after_b = snap();
  out.fits = fits;

  // a drop of a new span on this Viewer: both sides forgotten in one go
  load(['a', 'b']);
  gAutoFit = false; gView = { x0: 41.91, x1: 45.91, y0: -9.9, y1: -0.7 };
  gMarkers = { a: 43.9, b: 44.1 }; gPickKey = 'a-146';
  forgetSide('a'); forgetSide('b');
  out.new_span = snap();

  // the other direction of the same span: A stays, so does its view
  load(['a']);
  gAutoFit = false; gView = { x0: 41.91, x1: 45.91, y0: -9.9, y1: -0.7 };
  gMarkers = { a: 43.9, b: null };
  forgetSide('b');
  out.same_span = snap();

  // nothing was loaded: the tech's own Dir choice is left alone
  gTraces = []; gAddDir = 'a';
  forgetSide('a');
  out.empty = snap();
  print('OUT ' + JSON.stringify(out));
})().catch(function (e) { print('ERR ' + e + '\n' + e.stack); });
"""


@pytest.fixture(scope='module')
def res(tmp_path_factory):
    funcs = '\n'.join(_js_func(n) for n in (
        'forgetSide', 'newSpanView', 'setAddDir', 'syncFiberBox', 'fiberRangeText',
        'pollAnalysisMode'))
    path = tmp_path_factory.mktemp('new_span_view') / 'nsv.js'
    path.write_text(_STUBS + funcs + '\n' + _CASES, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and 'OUT ' in out, out[-2000:]
    return json.loads(out.split('OUT ', 1)[1].strip())


@needs_jsc
def test_other_windows_drop_leaves_dir_on_a_plus_b(res):
    # A forgotten first: the B traces left on the chart set Dir to B, as before
    assert res['after_a']['traces'] == 12 and res['after_a']['dir'] == 'b'
    # B forgotten next: the chart is empty and Dir is back on A + B
    assert res['after_b']['traces'] == 0
    assert res['after_b']['dir'] == 'both' and res['after_b']['active'] == ['both']
    assert res['after_b']['view'] is None and res['after_b']['autoFit'] is True


@needs_jsc
def test_new_span_drop_starts_the_view_over(res):
    s = res['new_span']
    assert s['traces'] == 0
    assert s['view'] is None and s['autoFit'] is True
    assert s['markers'] == {'a': None, 'b': None} and s['pick'] is None
    assert s['dir'] == 'both'


@needs_jsc
def test_other_direction_of_same_span_keeps_the_view(res):
    s = res['same_span']
    assert s['traces'] == 12
    assert s['view'] == {'x0': 41.91, 'x1': 45.91, 'y0': -9.9, 'y1': -0.7}
    assert s['autoFit'] is False and s['markers'] == {'a': 43.9, 'b': None}


@needs_jsc
def test_forgetting_an_empty_side_leaves_the_dir_choice(res):
    assert res['empty']['dir'] == 'a'


def test_drop_forgets_before_it_loads_and_fits_on_auto_fit():
    body = _js_func('handleFilesDrop')
    assert body.index('forgetSide(d)') < body.index('await selectFiles(want)')
    assert 'if (gAutoFit) fit();' in body
