"""The FastReporter table is built once on a whole-span load.

WHAT WENT WRONG.  On a whole-span bidirectional load (864 traces) in
FastReporter mode the Viewer asked /api/fr_table for the table, painted it,
and a moment later the end-connector verdicts landed (pollEndVerdicts).  That
called renderEventTable(), which emptied the table area and asked for the
whole FastReporter table again: the same rows, only the end marks and the
P/F column could change.  The area sat blank under "Building the table…"
until the second answer was painted.  Every other repaint of the panel (a
gate, a unit, a check box) asked for the table again the same way.

Now:
  1. The server's tables are kept for the traces that asked for them (the
     same trace objects, the same mode and folders): a repaint uses them, and
     a second request for the same table while the first is on its way
     waits for that one.  End verdicts landing update the table on screen in
     place: its end marks, P/F and the chart's drawer marks.
  2. A table that has to come from the server again (a mode switch, other
     traces) leaves the old one on screen, dimmed, under "Updating the
     table…", until the new one paints.

The tests run the Viewer's real <script> in JavaScriptCore (the jsc shell
macOS ships), as test_viewer_overview_failures does, and skip without it.
"""
from __future__ import annotations

import json
import re
import subprocess

import pytest

from conftest import VIEWER_DIR
from test_viewer_overview_failures import (  # noqa: F401 (real_trace is a fixture)
    JSC, _SHIMS, _scenario, _viewer_script, needs_jsc, real_trace)

SRC = (VIEWER_DIR / 'viewer.html').read_text(encoding='utf-8')

# The event panel's box as a real element would keep it: its children, in
# order, and what innerHTML does to them.  Everything else stays a stub.
_HOST = r"""
function __hFakeEl(tag) {
  var el = this;
  this.tagName = tag; this.children = []; this.dataset = {}; this.style = {};
  this.textContent = ''; this.className = '';
  var cls = new Set();
  this.classList = { add: function () { for (var a of arguments) cls.add(a); },
                     remove: function () { for (var a of arguments) cls.delete(a); },
                     contains: function (c) { return cls.has(c); },
                     toggle: function (c, on) { if (on === undefined ? !cls.has(c) : on) cls.add(c); else cls.delete(c); },
                     list: function () { return [...cls]; } };
  Object.defineProperty(this, 'childNodes', { get: function () { return el.children.slice(); } });
  Object.defineProperty(this, 'firstChild', { get: function () { return el.children[0] || null; } });
  Object.defineProperty(this, 'innerHTML', {
    get: function () { return el._html || ''; },
    set: function (v) { el.children = []; el._html = String(v); if (v) el.children.push({ html: String(v) }); } });
}
__hFakeEl.prototype.appendChild = function (c) { this.children.push(c); try { c.parentNode = this; } catch (e) {} return c; };
__hFakeEl.prototype.insertBefore = function (c, ref) {
  var i = this.children.indexOf(ref);
  if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
  try { c.parentNode = this; } catch (e) {}
  return c;
};
__hFakeEl.prototype.removeChild = function (c) {
  var i = this.children.indexOf(c);
  if (i >= 0) this.children.splice(i, 1);
  return c;
};
__hFakeEl.prototype.contains = function (c) { return this.children.indexOf(c) >= 0; };
__hFakeEl.prototype.querySelectorAll = function () { return []; };
__hFakeEl.prototype.querySelector = function () { return null; };
__hFakeEl.prototype.addEventListener = function () {};
__hEls['event-tbody-wrap'] = new __hFakeEl('div');

// The server: /api/list in FastReporter mode with the end verdicts still
// being worked out, /api/fr_table counted, and the two later answers held
// until the test lets them go.
var __hSrv = { mode: 'fr', endLanded: false, frAsked: [], endAsked: 0, suiteAsked: 0,
              suiteHold: null, busy: Promise.resolve() };
var __hFetch0 = globalThis.fetch;
function __hJson(body) {
  return { ok: true, status: 200,
           json: function () { return Promise.resolve(JSON.parse(JSON.stringify(body))); },
           text: function () { return Promise.resolve(JSON.stringify(body)); } };
}
function __hRow(km, loss, refl) {
  var leg = function (pos) { return { pos_m: pos, loss: loss, refl: refl, synthetic: false, status: 0 }; };
  return { mean_pos_m: km * 1000, type: refl == null ? 2 : 3, loss: loss,
           a: leg(km * 1000), b: leg(km * 1000), section: { loss: 0.1, att_db_km: 0.2, length_m: 1000,
           a: { loss: 0.1, att_db_km: 0.2 }, b: { loss: 0.1, att_db_km: 0.2 } } };
}
globalThis.fetch = function (url) {
  url = String(url);
  var q = new URLSearchParams(url.split('?')[1] || '');
  // /api/list carries the end verdicts too, as the real route does.  The
  // server answers one request at a time: a question asked while the table
  // is being built is answered after it (__hSrv.busy).
  var ends = function () {
    return __hSrv.endLanded
      ? { end_refl: [{ fiber: 1, dir: 'A', refl: -60.0 }], panel_span: null, end_pending: false }
      : { end_refl: null, panel_span: null, end_pending: true };
  };
  if (url.indexOf('/api/list') === 0) {
    return __hSrv.busy.then(function () {
      return __hJson(Object.assign({ dir_a: 'A', dir_b: 'B', fibers_a: [], fibers_b: [],
        files_a: [], files_b: [], analysis_mode: __hSrv.mode }, ends()));
    });
  }
  if (url.indexOf('/api/end_verdicts') === 0) {
    __hSrv.endAsked++;
    return __hSrv.busy.then(function () { return __hJson(ends()); });
  }
  if (url.indexOf('/api/fr_table') === 0) {
    __hSrv.frAsked.push(q.get('fibers'));
    var tables = {};
    q.get('fibers').split(',').forEach(function (f) {
      tables[f] = [__hRow(0.0, 0.01, -60.0), __hRow(1.0, 0.01, null)];
    });
    var slow = __hScenario.frDelay || 0;
    var done = __hSrv.busy.then(function () {
      return new Promise(function (r) { setTimeout(r, slow); });
    }).then(function () {
      // the report run finished while the table was being built
      if (__hScenario.landWithTable) __hSrv.endLanded = true;
      return __hJson({ tables: tables, missing: [], error: null });
    });
    __hSrv.busy = done.then(function () {});
    return done;
  }
  if (url.indexOf('/api/suite_table') === 0) {
    __hSrv.suiteAsked++;
    return new Promise(function (res) { __hSrv.suiteHold = function (b) { res(__hJson(b)); }; });
  }
  return __hFetch0(url);
};
"""

_DRIVER = r"""
(async function () {
  var out = {};
  var host = document.getElementById('event-tbody-wrap');
  var hint = document.getElementById('event-hint');
  var tableNode = function () {
    return host.children.find(function (c) { return !(c.className === 'tbl-updating'); }) || null;
  };
  var note = function () {
    var n = host.children.find(function (c) { return c.className === 'tbl-updating'; });
    return n ? String(n.textContent) : null;
  };
  try {
    await __hSleep(150);                                 // boot's /api/list
    document.getElementById('fiber-input').value = '1-3';
    gAddDir = 'both';
    await addFibers();
    await __hSleep(200);                                 // the table answers and paints
    out.painted = { asks: __hSrv.frAsked.length, kids: host.children.length,
                    busy: gTableBusy, pf: gTableExport ? gTableExport.rows()[0].indexOf('✗') >= 0 : null };
    var painted = tableNode();

    // the end verdicts land (the poll asks every 3 s)
    __hSrv.endLanded = true;
    await __hSleep(3400);
    await __hSleep(200);
    out.ends = { asks: __hSrv.frAsked.length, same: tableNode() === painted,
                 kids: host.children.length, endRefl: gEndRefl.length,
                 pf: gTableExport ? gTableExport.rows()[0].indexOf('✗') >= 0 : null };

    // a repaint of the panel for something else (the distance unit): the
    // table is laid out again from what is kept, not asked for again
    renderEventTable();
    await __hSleep(100);
    out.repaint = { asks: __hSrv.frAsked.length, kids: host.children.length, busy: gTableBusy };
    var before = tableNode();

    // the hub switches to OTDR Suite mode: that table has to come from the
    // server, and the FastReporter one stays up until it paints
    __hSrv.mode = 'suite';
    await loadInfo();
    await __hSleep(50);
    out.waiting = { suiteAsked: __hSrv.suiteAsked, old: host.children.indexOf(before) >= 0,
                    note: note(), dim: host.classList.contains('tbl-stale'), busy: gTableBusy };
    __hSrv.suiteHold({ pending: false, error: 'no table here' });
    await __hSleep(50);
    out.settled = { old: host.children.indexOf(before) >= 0, note: note(),
                    dim: host.classList.contains('tbl-stale'), busy: gTableBusy,
                    kids: host.children.length };
  } catch (e) { print('THREW ' + e + '\n' + (e && e.stack)); }
  print('OUT ' + JSON.stringify(out));
  quit();
})();
"""


# A whole span's table holds the server for a minute: the poll's questions
# wait behind it, and the verdicts land while it is being built.
_SLOW_DRIVER = r"""
(async function () {
  var out = {};
  try {
    await __hSleep(150);
    document.getElementById('fiber-input').value = '1-3';
    gAddDir = 'both';
    await addFibers();
    await __hSleep(7600);                                // held 7 s, then the queue drains
    out.asks = __hSrv.frAsked.length;
    out.endAsked = __hSrv.endAsked;
    out.landed = gEndRefl.length;
    out.pf = gTableExport ? gTableExport.rows()[0].indexOf('✗') >= 0 : null;
  } catch (e) { print('THREW ' + e + '\n' + (e && e.stack)); }
  print('OUT ' + JSON.stringify(out));
  quit();
})();
"""


def _run(tmp_path, sc, driver=_DRIVER):
    prog = ('var __hScenario = %s;\n' % json.dumps(sc) + _SHIMS + '\n' + _HOST + '\n'
            + _viewer_script(SRC) + '\n' + driver)
    path = tmp_path / 'fr_once.js'
    path.write_text(prog, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=180)
    out = r.stdout + r.stderr
    assert 'THREW' not in out and r.returncode == 0, out[-3000:]
    line = [ln for ln in out.splitlines() if ln.startswith('OUT ')][-1]
    return json.loads(line[4:])


def _three_fibers(real_trace, **extra):
    sc = _scenario(real_trace, 3, dirs=('a', 'b'), **extra)
    good = {'direction': 'A', 'fiber': 0, **real_trace}
    for d in 'ab':
        sc['single'][d] = {str(f): [200, dict(good, fiber=f)] for f in range(1, 4)}
    return sc


@pytest.fixture(scope='module')
def run(real_trace, tmp_path_factory):
    return _run(tmp_path_factory.mktemp('fr_once'), _three_fibers(real_trace))


@needs_jsc
def test_questions_queued_behind_a_slow_table_build_it_once(real_trace, tmp_path):
    """Before: two polls queued behind the 7 s table, both answered with the
    verdicts, each asked for the whole table again (three asks)."""
    out = _run(tmp_path, _three_fibers(real_trace, frDelay=7000, landWithTable=True),
               _SLOW_DRIVER)
    assert out['landed'] == 1 and out['pf'] is True
    assert out['asks'] == 1, out
    assert out['endAsked'] <= 2, out          # one question at a time


@needs_jsc
def test_the_table_is_asked_for_once_and_painted(run):
    assert run['painted']['asks'] == 1
    assert run['painted']['kids'] >= 1 and run['painted']['busy'] is False
    assert run['painted']['pf'] is False             # nothing fails yet


@needs_jsc
def test_end_verdicts_landing_update_the_table_in_place(run):
    ends = run['ends']
    assert ends['endRefl'] == 1                      # they did land
    assert ends['asks'] == 1, 'the end verdicts asked for the whole table again'
    assert ends['same'] is True, 'the table on screen was thrown away'
    # F1 A's launch connector now fails on its row (the report's verdict)
    assert ends['pf'] is True


@needs_jsc
def test_a_repaint_lays_the_kept_table_out_again(run):
    assert run['repaint'] == {'asks': 1, 'kids': run['repaint']['kids'], 'busy': False}
    assert run['repaint']['kids'] >= 1


@needs_jsc
def test_a_needed_rebuild_keeps_the_old_table_up_with_a_note(run):
    w = run['waiting']
    assert w['suiteAsked'] == 1 and w['busy'] is True
    assert w['old'] is True, 'the table area went blank while the new table was built'
    assert w['note'] == 'Updating the table…'
    assert w['dim'] is True
    s = run['settled']
    assert s['old'] is False and s['note'] is None and s['dim'] is False
    assert s['busy'] is False and s['kids'] == 1     # the new answer (here, its error)


def test_the_kept_tables_follow_the_traces_that_asked():
    key = re.search(r'function serverTableKey\(.*?\n\}', SRC, re.S)
    assert key, 'viewer.html no longer defines serverTableKey()'
    body = key.group(0)
    assert 'traceStamp(' in body and 'gAnalysisMode' in body
    assert "gInfo.dir_a" in body and "gInfo.dir_b" in body
    # the end verdicts update the table on screen rather than lay out the panel
    poll = re.search(r'function pollEndVerdicts\(\) \{.*?\n\}', SRC, re.S).group(0)
    assert 'endVerdictsLanded();' in poll and 'renderEventTable();' not in poll


def _js_func(name):
    m = re.search(r'function\s+' + re.escape(name) + r'\s*\([^)]*\)\s*\{', SRC)
    assert m, 'viewer.html no longer defines %s()' % name
    i, depth = m.end(), 1
    while i < len(SRC) and depth:
        depth += {'{': 1, '}': -1}.get(SRC[i], 0)
        i += 1
    return SRC[m.start():i]


@needs_jsc
def test_verdicts_landing_mid_load_wait_for_the_load(tmp_path):
    """Before: verdicts landing while a whole span was still loading built a
    table of the traces loaded so far (144 of 432 pairs in OTDR Suite mode),
    thrown away seconds later when the load built its own."""
    prog = (_js_func('endVerdictsLanded') + r"""
var renders = 0, refreshed = 0;
function renderEventTable() { renders++; }
var host = {}, scroller = { parentNode: host };
var gTableLive = null, gLoadsInFlight = new Set([{}]);
endVerdictsLanded();                       // mid-load: the load builds it
var midLoad = renders;
gLoadsInFlight.clear();
endVerdictsLanded();                       // no table yet: laid out
var noTable = renders;
gTableLive = { host: host, scroller: scroller, refreshVerdicts: function () { refreshed++; return true; } };
endVerdictsLanded();                       // a table up: in place
var inPlace = [renders, refreshed];
gTableLive.refreshVerdicts = function () { return false; };   // a filter on
endVerdictsLanded();
print('OUT ' + JSON.stringify({ midLoad: midLoad, noTable: noTable, inPlace: inPlace, filtered: renders }));
""")
    path = tmp_path / 'mid.js'
    path.write_text(prog, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = json.loads([ln for ln in r.stdout.splitlines() if ln.startswith('OUT ')][-1][4:])
    assert out == {'midLoad': 0, 'noTable': 1, 'inPlace': [1, 1], 'filtered': 2}


# ── The shared Viewer state (every Viewer window, the removed files) ───────
# The server runs the span again without the files removed in the Viewer, so
# the Splice Report answer kept for the traces follows the removed files the
# server has: those this window sent, or took from another window.  A post
# that moves nothing removed (a load, a Dir change) leaves the table on
# screen; one that moves them asks for the table again, once.

def test_the_kept_report_table_follows_the_removed_files():
    suite = SRC.split('function renderSuiteBidiGrid(', 1)[1].split('\nfunction ', 1)[0]
    assert "`${oneDir || ''}|${listSig}|${gServerRemovedSig}`" in suite
    taken = _js_func('applyViewerState')
    assert "gServerRemovedSig = [...gRemovedFiles].sort().join(',');" in taken


@needs_jsc
def test_only_a_moved_removed_set_lays_the_table_out_again(tmp_path):
    prog = r"""
var gInfo = { dir_a: 'A', dir_b: 'B' };
var gTraces = [{ key: 'a-1' }, { key: 'b-1' }], gRemovedFiles = new Set(), gAddDir = 'both';
var gClientId = 'me', gStateVer = 0, gStateSig = '', gStateReady = true, gStatePushT = null;
var gStateApplying = 0, gServerRemovedSig = '', timers = [], renders = 0, posts = 0;
var gLoadingKeys = new Set();            // keys on their way count too (main's load epochs)
function setTimeout(fn) { timers.push(fn); return timers.length; }
function clearTimeout() {}
async function flush() { var t = timers; timers = []; for (var i = 0; i < t.length; i++) await t[i](); }
function fetch() { posts++; return Promise.resolve({ json: function () { return Promise.resolve({ ok: true, ver: posts }); } }); }
function renderEventTable() { renders++; }
""" + _js_func('stateSig') + '\n' + _js_func('pushViewerState') + r"""
(async function () {
  var out = {};
  pushViewerState(); await flush();               // the first send after a load
  out.first = [posts, renders, gServerRemovedSig];
  gAddDir = 'a'; pushViewerState(); await flush(); // a Dir change
  out.dir = [posts, renders];
  gRemovedFiles.add('b-1'); gTraces = [{ key: 'a-1' }];   // a Remove
  pushViewerState(); await flush();
  out.removed = [posts, renders, gServerRemovedSig];
  gRemovedFiles.clear(); gTraces = [{ key: 'a-1' }, { key: 'b-1' }];   // Put Back
  pushViewerState(); await flush();
  out.back = [posts, renders, gServerRemovedSig];
  print('OUT ' + JSON.stringify(out));
})().catch(function (e) { print('ERR ' + e + '\n' + e.stack); });
"""
    path = tmp_path / 'removed.js'
    path.write_text(prog, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith('OUT ')]
    assert lines, r.stdout[-2000:] + r.stderr[-2000:]
    out = json.loads(lines[-1][4:])
    assert out['first'] == [1, 0, ''], 'the first send threw the table on screen away'
    assert out['dir'] == [2, 0]
    assert out['removed'] == [3, 1, 'b-1']
    assert out['back'] == [4, 2, '']
