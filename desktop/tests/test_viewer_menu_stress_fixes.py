"""Viewer menu stress audit, 2026-10-01 (a 432-fiber span loaded both ways, 864 traces).

  1. Clear All in the middle of a cable load did not stick: the rest of the
     load kept landing, and 720 of the 864 traces came back under a Fibers box
     reading "1-432".  A load now belongs to a load epoch; Clear All and a new
     FILES selection start a new one, and a fetch that comes back for an
     older one pushes nothing and stops its load.  The Fibers box and the Dir
     buttons wait for the load to settle (the first chunk is A's alone, and a
     Clear All there left the Dir on A).
  2. A second Enter (or Add) during a cable load fetched all 864 traces again:
     36 s for a 13 s load.  The overview now marks its keys as on their way,
     and a repeat of a load still coming does nothing.
  3. A double-click on Summary Report stacked two dialogs; only the latest
     click builds one now (Trace Settings the same).
  4. A Settings box or customer profile change never reached a loaded Viewer:
     /api/mode carries the gates and the Viewer re-reads when they move.
  5. Summary Report with nothing on the chart did nothing visible.
  6. M behind the Summary Report dialog turned the markers on.
  7. Escape shut the gear menus but not a right-click menu.
  8. The event panel said "add a trace to see events" for the whole load.

Run in JavaScriptCore where present; the server route and the wiring are
checked everywhere.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request

import pytest

from conftest import VIEWER_DIR, import_trace_server

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


def _run(tmp_path, body):
    path = tmp_path / 'case.js'
    path.write_text(body, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and 'OUT ' in out, out[-3000:]
    return json.loads(out.split('OUT ', 1)[1].strip())


# ── 1 + 2: the load paths ────────────────────────────────────────────────
# 100 fibers a side: past MAX_DETAIL_TRACES, so the overview (one chunk a side).
_LOAD_STUBS = r"""
var gSelNext = null, gAddDir = 'both', gTraces = [], gLoadFailures = [], gStoredDir = {};
var gRemovedFiles = new Set(), gAutoFit = true, gLoadingKeys = new Set(), gLoadEpoch = 0;
var gView = null, gPickKey = null, gMarkers = {}, gDragMarker = null;
var COLORS_USED = new Set();
var fibers = []; for (var i = 1; i <= 100; i++) fibers.push(i);
var gInfo = { dir_a: '/a', dir_b: '/b', fibers_a: fibers, fibers_b: fibers };
var box = { value: '1-100', textContent: '' }, hint = { textContent: '' };
var document = { getElementById: function (id) { return id === 'event-hint' ? hint : box; } };
var console = { warn: function () {} };
var fetches = [], pending = [];
// Each /api/traces answer waits until the case lets it go (release()).
function fetch(url) {
  fetches.push(url);
  var m = /dir=(\w)&fibers=([\d,]+)/.exec(url);
  var fibs = m ? m[2].split(',').map(Number) : [+/fiber=(\d+)/.exec(url)[1]];
  return new Promise(function (res) {
    pending.push(function () {
      res({ ok: true, json: function () {
        return Promise.resolve(m ? { traces: fibs.map(function (f) {
          return { fiber: f, dist_km: [0, 1], trace_db: [0, 1], events: [] }; }) }
          : { fiber: fibs[0], dist_km: [0, 1], trace_db: [0, 1], events: [] }); } });
    });
  });
}
function tick() { return new Promise(function (r) { setTimeout(r, 0); }); }
async function release() { while (pending.length) { pending.shift()(); for (var k = 0; k < 6; k++) await tick(); } }
function effDir(d) { return d; }
function nextColor() { return 0; }
function traceColor() { return 0; }
function dataBounds() { return null; }
function syncFileMarks() {} function renderChips() {} function fit() {} function draw() {}
function renderEventTable() {} function setReadout() {} function clearFileSelection() {}
"""

_LOAD_CASES = r"""
(async function () {
  var out = {};
  // a second Enter while the first load is still coming
  var p1 = addFibers(), p2 = addFibers();
  out.hintMidLoad = hint.textContent;
  await release(); await p1; await p2;
  out.double = [gTraces.length, fetches.length, gLoadingKeys.size];

  // Clear All while A's chunk is on its way: nothing of that load comes back
  clearAll(); fetches = []; box.value = '1-100';
  var p3 = addFibers();
  await tick();
  clearAll();
  await release(); await p3;
  out.cleared = [gTraces.length, fetches.length, gLoadingKeys.size];

  // ...and a load asked for straight after a clear is planned again
  box.value = '1-100';
  var p4 = addFibers();
  await tick();
  clearAll(); box.value = '1-100';     // the tech types the range again
  var p5 = addFibers();
  await release(); await p4; await p5;
  out.reload = [gTraces.length, new Set(gTraces.map(function (t) { return t.key; })).size];

  // a detail load (loadOne) cleared mid-fetch pushes nothing
  clearAll(); fetches = [];
  var p6 = loadOne('a-7', 7, 'a');
  clearAll();
  await release(); await p6;
  out.one = [gTraces.length, gLoadingKeys.size];
  print('OUT ' + JSON.stringify(out));
})().catch(function (e) { print('ERR ' + e + '\n' + e.stack); });
"""


@pytest.fixture(scope='module')
def loads(tmp_path_factory):
    if not os.path.exists(JSC):
        pytest.skip('JavaScriptCore shell (macOS) not present')
    funcs = '\n'.join([_const('MAX_DETAIL_TRACES'), _const('MAX_OVERVIEW_FIBERS'),
                       _const('OVERVIEW_PTS')]
                      + [_js_func(n) for n in ('newLoadEpoch', 'parseFibers', 'planFiberLoad',
                                               'absentNote', 'planOrSay', 'loadFailNote',
                                               'fetchRetry', 'loadOne', 'loadOverview',
                                               'loadProgress', 'addFibers', 'clearAll')])
    return _run(tmp_path_factory.mktemp('loads'), _LOAD_STUBS + funcs + '\n' + _LOAD_CASES)


@needs_jsc
def test_second_enter_during_a_cable_load_fetches_nothing_again(loads):
    traces, fetches, in_flight = loads['double']
    assert traces == 200
    assert fetches == 2                  # one chunk a side, asked once
    assert in_flight == 0


@needs_jsc
def test_the_event_panel_counts_the_load(loads):
    assert loads['hintMidLoad'] == 'loading 0 of 200 traces…'


@needs_jsc
def test_clear_all_mid_load_sticks(loads):
    traces, fetches, in_flight = loads['cleared']
    assert traces == 0                   # was 100+ back on the chart
    assert fetches == 1                  # B's chunk is never asked for
    assert in_flight == 0


@needs_jsc
def test_a_load_right_after_clear_loads_everything_once(loads):
    assert loads['reload'] == [200, 200]


@needs_jsc
def test_a_detail_fetch_cleared_meanwhile_pushes_nothing(loads):
    assert loads['one'] == [0, 0]


def test_everything_that_replaces_the_chart_starts_a_new_load():
    for name in ('clearAll', 'applyFileSelection'):
        assert 'newLoadEpoch()' in _js_func(name), name
    sel = _js_func('applyFileSelection')
    assert 'if (epoch !== gLoadEpoch) return;' in sel


# ── 4: the gates reach a loaded Viewer ───────────────────────────────────
_GATE_STUBS = r"""
var gAnalysisMode = 'suite', gTraces = [{ key: 'a-1' }], gThresholds = null;
var gLaunchA = 0, gSpanDecl = null, gEndRefl = [], gFrameWarn = '';
var gDropInFlight = false, gModePollBusy = false, gLoadingKeys = new Set();
var calls = { table: 0, list: 0 };
var T0 = { reburn: 0.16, single_dir: 0.2 }, T1 = { reburn: 0.1, single_dir: 0.3 };
var listT = T0, modeT = T0;
var gInfo = { analysis_mode: 'suite', thresholds: T0, gate_source: 'settings', flags_off: false,
              dir_a: '/a', dir_b: '/b' };
function activeGateDb() { return gThresholds ? gThresholds.reburn : 0.16; }
function pollEndVerdicts() {} function syncGateUI() {} function renderFilesPanel() {}
function renderBackButton() {} function setReadout() {} function draw() {}
function applyHubTheme() {} function forgetSide() {} function renderChips() {} function fit() {}
function frameWarnText() { return ''; }
function renderEventTable() { calls.table++; }
function fetch(url) {
  var body;
  if (url.indexOf('/api/list') === 0) {
    calls.list++;
    body = { analysis_mode: 'suite', thresholds: listT, gate_source: 'settings', flags_off: false,
             dir_a: '/a', dir_b: '/b', fibers_a: [1], fibers_b: [1] };
  } else if (url.indexOf('/api/mode') === 0) {
    body = { analysis_mode: 'suite', dir_a: '/a', dir_b: '/b', theme: 'light',
             thresholds: modeT, gate_source: 'settings', flags_off: false };
  } else throw new Error('unexpected fetch ' + url);
  return Promise.resolve({ ok: true, json: function () { return Promise.resolve(body); } });
}
"""

_GATE_CASES = r"""
(async function () {
  var out = {};
  await pollAnalysisMode();
  out.same = [calls.list, calls.table];
  // the hub's Settings box moved the gate, but a load is still landing
  modeT = T1; listT = T1; gLoadingKeys.add('b-1');
  await pollAnalysisMode();
  out.loading = [calls.list, calls.table];
  gLoadingKeys.clear();
  await pollAnalysisMode();
  out.moved = [calls.list, calls.table, gThresholds.reburn];
  await pollAnalysisMode();
  out.settled = [calls.list, calls.table];
  // a warning-level or connector gate alone re-grades too
  modeT = listT = { reburn: 0.1, single_dir: 0.25 };
  await pollAnalysisMode();
  out.other = [calls.list, calls.table];
  print('OUT ' + JSON.stringify(out));
})().catch(function (e) { print('ERR ' + e + '\n' + e.stack); });
"""


@needs_jsc
def test_a_settings_change_regrades_a_loaded_viewer(tmp_path):
    funcs = '\n'.join(_js_func(n) for n in ('gateSig', 'pollAnalysisMode', 'loadInfo'))
    out = _run(tmp_path, _GATE_STUBS + funcs + '\n' + _GATE_CASES)
    assert out['same'] == [0, 0]                 # same gates: no re-read
    assert out['loading'] == [0, 0]              # not while traces are landing
    assert out['moved'] == [1, 1, 0.1]           # re-read, re-graded at the new gate
    assert out['settled'] == [1, 1]              # and only once
    assert out['other'] == [2, 2]                # any gate, not only the loss box's


def test_api_mode_carries_the_gates():
    T = import_trace_server()
    port = T.start_in_thread(8798)
    was = T.CONFIG.get('settings')
    try:
        T.set_settings({'REBURN_THRESHOLD': 0.1})
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/mode', timeout=4) as r:
            j = json.loads(r.read())
        assert j['thresholds'] == T.engine_thresholds()
        assert j['thresholds']['reburn'] == 0.1
        assert j['gate_source'] == T.gate_source()
        assert j['flags_off'] is T.flags_off()
    finally:
        T.set_settings(was)


# ── 3, 5, 6, 7: the dialogs and the keys ─────────────────────────────────
def test_only_the_latest_summary_report_click_builds_a_dialog():
    f = _js_func('showReportDialog')
    seq, wait, check = (f.index('++gReportDlgSeq'), f.index('await'),
                        f.index('if (seq !== gReportDlgSeq) return;'))
    assert seq < wait < check < f.index("document.createElement('div')")


def test_only_the_latest_trace_settings_ask_builds_a_dialog():
    f = _js_func('showEditDialog')
    assert f.index('++gEditDlgSeq') < f.index('await') < f.index('if (seq !== gEditDlgSeq) return;')


def test_summary_report_with_nothing_loaded_says_so():
    f = _js_func('showReportDialog')
    empty = f[f.index('if (!vis.length)'):f.index('let dest')]
    assert '{ warn:' in empty                     # a warning shows; a plain note never did
    assert 'still loading' in empty and 'load a trace first' in empty


def test_chart_keys_wait_behind_the_summary_report():
    m = re.search(r"window\.addEventListener\('keydown', \(ev\) => \{(.*?)\n\}\);", SRC, re.S)
    assert m
    body = m.group(1)
    guard = body[:body.index('selectAllFiles')]
    assert "getElementById('report-dlg')" in guard
    assert "ev.key === 'Escape' && document.getElementById('span-menu')" in guard
    assert 'closeSpanMenu()' in guard


def test_box_and_dir_wait_for_the_load_to_settle():
    f = _js_func('syncFiberBox')
    assert f.index('if (gLoadingKeys.size) return;') < f.index('setAddDir(')


def test_an_empty_table_while_loading_says_loading():
    assert "gLoadingKeys.size ? 'loading traces…' : 'add a trace to see events'" \
        in _js_func('renderEventTable')
