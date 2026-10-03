"""Flipping the hub's Analysis Mode lays out a Viewer that already has traces
on screen again (demo list #30, 2026-10-01).

Load F354 both ways in FR Mode, flip to OTDR Mode: the event table stayed in
the FastReporter layout until Clear All.  The embedded Viewer never hears the
switch (it gets no focus event, and only loadInfo read the mode), and a pop-out
window that did re-read the mode set gAnalysisMode without laying the table
out again.  Now:

  loadInfo          a new mode from /api/list repaints the table (and the chart,
                    which marks only the events the table prints)
  pollAnalysisMode  asks the cheap /api/mode every MODE_POLL_MS and calls
                    loadInfo only when the mode changed; never while a drop is
                    going up, never two asks at once
  /api/mode         answers the mode from CONFIG, nothing else

Run in JavaScriptCore where present; the server route is checked everywhere.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request

import pytest

from conftest import VIEWER_DIR, import_trace_server
from conftest import COPY_HELPERS_JS  # noqa: E402

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


_STUBS = r"""
var gAnalysisMode = 'suite', gInfo = null, gTraces = [], gThresholds = null;
var gLaunchA = 0, gSpanDecl = null, gEndRefl = [], gFrameWarn = '';
var gDropInFlight = false, gModePollBusy = false, gFecMode = false;
var calls = { table: 0, draw: 0, list: 0, mode: 0 };
var listMode = 'suite', apiMode = 'suite', gate = 0.16;
function activeGateDb() { return gate; }
function pollEndVerdicts() {}
function syncGateUI() {}
function renderFilesPanel() {}
function renderBackButton() {}
function refreshMirrorFrame() {}
function setReadout() {}
function renderEventTable() { calls.table++; }
function draw() { calls.draw++; }
function fetch(url) {
  var body;
  if (url.indexOf('/api/list') === 0) {
    calls.list++;
    body = { analysis_mode: listMode, fibers_a: ['0354'], fibers_b: ['0354'],
             dir_a_name: 'A side', dir_b_name: 'B side' };
  } else if (url.indexOf('/api/mode') === 0) {
    calls.mode++;
    body = { analysis_mode: apiMode };
  } else throw new Error('unexpected fetch ' + url);
  return Promise.resolve({ ok: true, json: function () { return Promise.resolve(body); } });
}
"""

_CASES = r"""
(async function () {
  var out = {};
  var snap = function () { return [gAnalysisMode, calls.table, calls.draw]; };
  // nothing on screen: the mode is read, nothing to lay out
  listMode = 'fr';
  await loadInfo();
  out.empty = snap();
  // F354 both ways on screen in FR Mode; the hub flips to OTDR Mode
  gTraces = [{ key: 'a-354' }, { key: 'b-354' }];
  calls.table = calls.draw = 0;
  listMode = 'suite';
  await loadInfo();
  out.flip = snap();
  // the same mode again: no repaint
  await loadInfo();
  out.same = snap();
  // the poll: the same mode costs one /api/mode and no /api/list
  calls.list = calls.mode = calls.table = 0;
  apiMode = 'suite';
  await pollAnalysisMode();
  out.poll_same = [calls.mode, calls.list, calls.table];
  // the switch moved: the poll reads /api/list and the table is laid out again
  calls.list = calls.mode = calls.table = 0;
  apiMode = listMode = 'fr';
  await pollAnalysisMode();
  out.poll_moved = [calls.mode, calls.list, calls.table, gAnalysisMode];
  // a drop going up: no ask at all
  calls.mode = 0; gDropInFlight = true; apiMode = listMode = 'suite';
  await pollAnalysisMode();
  out.poll_drop = [calls.mode, gAnalysisMode];
  gDropInFlight = false;
  // an ask still out: a second tick does not stack another
  calls.mode = 0; gModePollBusy = true;
  await pollAnalysisMode();
  out.poll_busy = calls.mode;
  gModePollBusy = false;
  print('OUT ' + JSON.stringify(out));
})().catch(function (e) { print('ERR ' + e + '\n' + e.stack); });
"""


@pytest.fixture(scope='module')
def res(tmp_path_factory):
    funcs = '\n'.join(_js_func(n) for n in ('loadInfo', 'pollAnalysisMode', 'frameWarnText', 'fecShots'))
    path = tmp_path_factory.mktemp('mode_switch') / 'mode.js'
    path.write_text(_STUBS + COPY_HELPERS_JS + funcs + '\n' + _CASES, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and 'OUT ' in out, out[-2000:]
    return json.loads(out.split('OUT ', 1)[1].strip())


@needs_jsc
def test_a_new_mode_lays_out_the_table_on_screen_again(res):
    assert res['empty'] == ['fr', 0, 0]          # nothing loaded: nothing to paint
    assert res['flip'] == ['suite', 1, 1]        # table AND chart marks
    assert res['same'] == ['suite', 1, 1]        # no second repaint


@needs_jsc
def test_the_poll_is_cheap_and_only_a_change_reads_the_list(res):
    assert res['poll_same'] == [1, 0, 0]
    assert res['poll_moved'] == [1, 1, 1, 'fr']
    assert res['poll_drop'] == [0, 'fr']
    assert res['poll_busy'] == 0


def test_the_viewer_polls_the_mode_on_a_timer():
    assert re.search(r'setInterval\(pollAnalysisMode,\s*MODE_POLL_MS\)', SRC)
    m = re.search(r'const MODE_POLL_MS = (\d+);', SRC)
    assert m and 500 <= int(m.group(1)) <= 3000
    # a drop marks itself in flight, so no poll lands between its requests
    drop = _js_func('handleFilesDrop')
    assert 'gDropInFlight = true' in drop and 'gDropInFlight = false' in drop


def test_api_mode_answers_the_hub_mode():
    ts = import_trace_server()
    port = ts.start_in_thread(8795)
    was = ts.CONFIG.get('analysis_mode')
    try:
        for mode in ('fr', 'suite'):
            ts.CONFIG['analysis_mode'] = mode
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/mode', timeout=4) as r:
                assert json.loads(r.read())['analysis_mode'] == mode
        ts.CONFIG['analysis_mode'] = 'nonsense'
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/mode', timeout=4) as r:
            assert json.loads(r.read())['analysis_mode'] == 'suite'
    finally:
        ts.CONFIG['analysis_mode'] = was
