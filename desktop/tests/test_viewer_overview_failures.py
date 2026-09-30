"""A whole-cable (overview) load must say which fibers did not load, and why.

WHAT WENT WRONG.  Two gaps, one on each side of /api/traces.

1. The readout.  loadOverview() pushed an ``F57: not found`` note into
   gLoadFailures for every fiber in the reply's ``missing`` list, but the
   overview readout never printed gLoadFailures.  It only said "Bulk load
   failed" when the whole request died.  So a bad file left the count one
   short and named nothing:

       before   56 traces loaded in overview (~2000 pts each, spikes preserved)
       after    56 traces loaded in overview (~2000 pts each, spikes preserved),
                could not load F57 A: not found; F58 A: unreadable (unpack
                requires a buffer of 4 bytes); F59 A: not found (+1 more)

   The detail load (48 traces or fewer) has always printed that tail.  All
   three readouts now build it with one helper, loadFailNote().

   Since 2026-09-30 (Robert, "no plain notes") the line shows only the
   failure: "could not load F57 A: ..." without the count in front.

2. The label.  ``missing`` holds fibers whose file is absent AND fibers whose
   file is there but failed to parse, so a broken file read "not found".  The
   server now also sends ``failed``: the parse failures, each with a one-line
   reason.  ``missing`` is unchanged, so anything reading it still works.

The server half needs fix/viewer-list-hangup: before it, report_error was
local to do_GET and the parse-failure branch raised UnboundLocalError, so the
whole request dropped instead of listing the bad fiber.

The readout tests run the Viewer's real <script> in JavaScriptCore (the jsc
shell macOS ships) against the real route's JSON, and skip where there is no
jsc.  The source checks below them run everywhere.
"""
from __future__ import annotations

import copy
import io
import json
import os
import re
import subprocess
from unittest import mock

import pytest

from conftest import FIXTURE_A_DIR, VIEWER_DIR, import_trace_server

T = import_trace_server()
VIEWER_HTML = os.path.join(str(VIEWER_DIR), 'viewer.html')
JSC = ('/System/Library/Frameworks/JavaScriptCore.framework/'
       'Versions/Current/Helpers/jsc')


# ─── the route: `missing` as before, plus `failed` with a reason ────────────

def _get_json(path):
    """One GET through the real handler with in-memory streams -> (status, body)."""
    h = T.Handler.__new__(T.Handler)
    h.rfile = io.BytesIO(('GET %s HTTP/1.0\r\n\r\n' % path).encode('ascii'))
    h.wfile = io.BytesIO()
    h.client_address = ('127.0.0.1', 1)
    h.server = mock.Mock()
    h.request = mock.Mock()
    h.close_connection = True
    h.handle()
    head, _, body = h.wfile.getvalue().partition(b'\r\n\r\n')
    return int(head.split()[1]), json.loads(body)


@pytest.fixture(scope='module')
def real_trace():
    """One real decimated trace from the fixtures, to stand in for good files."""
    saved = dict(T.CONFIG)
    T.CONFIG['dir_a'] = str(FIXTURE_A_DIR)
    try:
        fiber = T.list_fibers(str(FIXTURE_A_DIR))[0][0]
        t = T.load_trace('a', fiber, max_pts=200)
    finally:
        T.CONFIG.clear()
        T.CONFIG.update(saved)
    assert t and t['dist_km'], 'fixture trace did not load'
    return t


def _fake_load_trace(real, absent=(), bad=None):
    """load_trace as the route sees it: None for an absent file, an exception
    for a file that will not parse, a real trace for everything else."""
    bad = bad or {}

    def load(direction, fiber, max_pts=None):
        if fiber in absent:
            return None
        if fiber in bad:
            raise bad[fiber]
        return copy.deepcopy(real)
    return load


def _bulk(path, loader):
    rep = mock.Mock()
    with mock.patch.object(T, 'load_trace', loader), \
            mock.patch.object(T, 'report_error', rep):
        status, body = _get_json(path)
    return status, body, rep


def test_a_parse_failure_is_listed_as_failed_with_its_reason(real_trace):
    status, body, rep = _bulk(
        '/api/traces?dir=a&fibers=1-4',
        _fake_load_trace(real_trace, absent={2}, bad={3: ValueError('bad file')}))
    assert status == 200
    assert [t['fiber'] for t in body['traces']] == [1, 4]
    assert body['missing'] == [2, 3]            # unchanged: absent AND failed
    assert body['failed'] == [{'fiber': 3, 'error': 'bad file'}]
    assert rep.call_count == 1                  # the failure still reaches Slack
    assert rep.call_args[0][0] == 'viewer bulk trace load'


def test_a_clean_load_has_an_empty_failed_list(real_trace):
    _, body, rep = _bulk('/api/traces?dir=a&fibers=1-3', _fake_load_trace(real_trace))
    assert body['missing'] == [] and body['failed'] == []
    assert rep.call_count == 0


def test_the_reason_is_one_short_line(real_trace):
    """The readout is `white-space: pre`: a multi-line or path-long message
    would run off the toolbar.  The full error already went to Slack."""
    long_msg = 'line one\n   line two ' + 'x' * 300
    _, body, _ = _bulk('/api/traces?dir=a&fibers=1-2', _fake_load_trace(
        real_trace, bad={1: OSError(long_msg), 2: ValueError()}))
    reasons = {f['fiber']: f['error'] for f in body['failed']}
    assert reasons[1].startswith('line one line two xxx')
    assert '\n' not in reasons[1] and len(reasons[1]) <= 120
    assert reasons[2] == 'ValueError'           # an empty message still says something


# ─── the readout, in the Viewer's real script under JavaScriptCore ──────────

needs_jsc = pytest.mark.skipif(not os.path.exists(JSC),
                               reason='JavaScriptCore shell (macOS) not present')

# A stub browser: enough DOM for viewer.html's script to boot and load, with
# fetch answered from the route bodies built in Python.  Every name is
# prefixed __h so nothing collides with the Viewer's own globals.
_SHIMS = r"""
var __hLog = [];
(function () {
  var st = globalThis.setTimeout, n = 0, dead = new Set();
  globalThis.setTimeout = function (fn, ms) {
    var a = Array.prototype.slice.call(arguments, 2), id = ++n;
    st(function () { if (!dead.has(id)) fn.apply(null, a); }, ms || 0);
    return id;
  };
  globalThis.clearTimeout = function (id) { dead.add(id); };
  globalThis.setInterval = function (fn, ms) {
    var id = ++n;
    var tick = function () { if (dead.has(id)) return; fn(); st(tick, ms || 10); };
    st(tick, ms || 10);
    return id;
  };
  globalThis.clearInterval = globalThis.clearTimeout;
})();
globalThis.queueMicrotask = function (fn) { Promise.resolve().then(fn); };
globalThis.requestAnimationFrame = function (fn) { return setTimeout(function () { fn(Date.now()); }, 16); };
globalThis.cancelAnimationFrame = function (id) { clearTimeout(id); };
globalThis.performance = { now: function () { return Date.now(); } };
globalThis.console = {
  log: function () {}, info: function () {}, debug: function () {},
  warn: function () { __hLog.push('warn ' + Array.prototype.join.call(arguments, ' ')); },
  error: function () { __hLog.push('error ' + Array.prototype.join.call(arguments, ' ')); },
};
function __hStub(name) {
  var store = {};
  return new Proxy(function () {}, {
    get: function (t, p) {
      if (p === Symbol.toPrimitive) return function (hint) { return hint === 'number' ? 0 : ''; };
      if (p === Symbol.iterator) return function* () {};
      if (p === 'then') return undefined;
      if (p === 'length') return 0;
      if (Object.prototype.hasOwnProperty.call(store, p)) return store[p];
      if (typeof p === 'symbol') return undefined;
      return (store[p] = __hStub(name + '.' + String(p)));
    },
    set: function (t, p, v) { store[p] = v; return true; },
    has: function () { return true; },
    apply: function () { return __hStub(name + '()'); },
    construct: function () { return __hStub('new ' + name); },
  });
}
globalThis.window = globalThis; globalThis.self = globalThis;
globalThis.top = globalThis; globalThis.parent = globalThis;
globalThis.opener = null; globalThis.name = '';
globalThis.devicePixelRatio = 1; globalThis.innerWidth = 1400; globalThis.innerHeight = 900;
globalThis.addEventListener = function () {};
globalThis.removeEventListener = function () {};
globalThis.dispatchEvent = function () { return true; };
globalThis.focus = function () {}; globalThis.open = function () { return null; };
globalThis.alert = function () {}; globalThis.confirm = function () { return false; };
globalThis.prompt = function () { return null; };
globalThis.getComputedStyle = function () { return __hStub('computedStyle'); };
globalThis.matchMedia = function () { return { matches: false, addEventListener: function () {}, addListener: function () {} }; };
function __hObs() {}
__hObs.prototype.observe = __hObs.prototype.disconnect = __hObs.prototype.unobserve = function () {};
globalThis.ResizeObserver = globalThis.MutationObserver = globalThis.IntersectionObserver = __hObs;
var __hLs = {};
globalThis.localStorage = {
  getItem: function (k) { return Object.prototype.hasOwnProperty.call(__hLs, k) ? __hLs[k] : null; },
  setItem: function (k, v) { __hLs[k] = String(v); },
  removeItem: function (k) { delete __hLs[k]; },
};
globalThis.sessionStorage = globalThis.localStorage;
globalThis.navigator = { userAgent: 'jsc', platform: 'Win32', clipboard: __hStub('clipboard') };
globalThis.Image = function () { return __hStub('Image'); };
globalThis.Blob = function () { return __hStub('Blob'); };
globalThis.FileReader = function () { return __hStub('FileReader'); };
globalThis.TextDecoder = function () { return { decode: function () { return ''; } }; };
globalThis.TextEncoder = function () { return { encode: function () { return new Uint8Array(0); } }; };
globalThis.Event = function (t) { this.type = t; };
globalThis.CustomEvent = globalThis.Event;
globalThis.HTMLElement = function () {};
globalThis.AbortController = function () { this.signal = {}; this.abort = function () {}; };
globalThis.structuredClone = function (v) { return JSON.parse(JSON.stringify(v)); };
var __hEls = {};
globalThis.document = __hStub('document');
document.getElementById = function (id) { return (__hEls[id] = __hEls[id] || __hStub('#' + id)); };
document.querySelector = function (q) { return __hStub('q:' + q); };
document.querySelectorAll = function () { return []; };
document.getElementsByClassName = function () { return []; };
document.getElementsByTagName = function () { return []; };
document.createElement = function (tag) { return __hStub('<' + tag + '>'); };
document.createTextNode = function () { return __hStub('text'); };
document.addEventListener = function () {};
document.removeEventListener = function () {};
document.hidden = false; document.visibilityState = 'visible';
globalThis.location = {
  search: '', href: 'http://127.0.0.1:8771/', origin: 'http://127.0.0.1:8771',
  protocol: 'http:', host: '127.0.0.1:8771', hostname: '127.0.0.1', port: '8771',
  pathname: '/', reload: function () {},
};
globalThis.URLSearchParams = function (s) {
  var m = this._m = [];
  String(s || '').replace(/^\?/, '').split('&').filter(Boolean).forEach(function (kv) {
    var i = kv.indexOf('=');
    var dec = function (x) { return decodeURIComponent(x.replace(/\+/g, ' ')); };
    m.push([dec(i < 0 ? kv : kv.slice(0, i)), i < 0 ? '' : dec(kv.slice(i + 1))]);
  });
};
URLSearchParams.prototype.get = function (k) { var e = this._m.find(function (e) { return e[0] === k; }); return e ? e[1] : null; };
URLSearchParams.prototype.has = function (k) { return this._m.some(function (e) { return e[0] === k; }); };
URLSearchParams.prototype.set = function (k, v) { this._m = this._m.filter(function (e) { return e[0] !== k; }); this._m.push([k, String(v)]); };
URLSearchParams.prototype.append = function (k, v) { this._m.push([k, String(v)]); };
URLSearchParams.prototype.toString = function () { return this._m.map(function (e) { return encodeURIComponent(e[0]) + '=' + encodeURIComponent(e[1]); }).join('&'); };
globalThis.URL = function (u) { this.href = String(u); this.searchParams = new URLSearchParams(String(u).split('?')[1] || ''); };

// The trace server.  /api/traces answers from the route's own reply for the
// scenario's whole fiber range, cut down to the fibers this chunk asked for
// (the route treats every fiber on its own, so a cut is what it would send).
globalThis.fetch = function (url) {
  url = String(url);
  var q = new URLSearchParams(url.split('?')[1] || ''), status = 404, body = { error: 'unknown route' };
  if (url.indexOf('/api/list') === 0) {
    status = 200;
    body = { dir_a: 'A', dir_b: 'B', fibers_a: [], fibers_b: [], files_a: [], files_b: [] };
  } else if (url.indexOf('/api/traces?') === 0) {
    var full = __hScenario.bulk[q.get('dir')];
    status = __hScenario.bulk_status || 200;
    if (status !== 200) body = { error: 'boom' };
    else {
      var want = new Set(q.get('fibers').split(',').map(Number));
      body = JSON.parse(JSON.stringify(full));
      body.traces = body.traces.filter(function (t) { return want.has(t.fiber); });
      body.missing = body.missing.filter(function (f) { return want.has(f); });
      if (body.failed) body.failed = body.failed.filter(function (x) { return want.has(x.fiber); });
    }
  } else if (url.indexOf('/api/trace?') === 0) {
    var one = __hScenario.single[q.get('dir')][q.get('fiber')];
    if (one) { status = one[0]; body = one[1]; }
  }
  return Promise.resolve({
    ok: status >= 200 && status < 300, status: status,
    json: function () { return Promise.resolve(JSON.parse(JSON.stringify(body))); },
    text: function () { return Promise.resolve(JSON.stringify(body)); },
  });
};
function __hSleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
"""

_DRIVER = r"""
(async function () {
  try {
    await __hSleep(150);                    // let the boot's /api/list settle
    if (__hScenario.files) {                // the FILES panel's path
      await applyFileSelection(new Set(__hScenario.files));
    } else {                                // the Fibers box's path
      document.getElementById('fiber-input').value = __hScenario.fibers;
      gAddDir = __hScenario.dir;
      await addFibers();
    }
    print('READOUT ' + JSON.stringify(String(document.getElementById('readout').textContent)));
    print('TRACES ' + gTraces.length);
  } catch (e) {
    print('THREW ' + e + '\n' + (e && e.stack));
  }
  print('LOG ' + JSON.stringify(__hLog));
  quit();
})();
"""


def _viewer_script(html):
    (block,) = re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', html, re.S)
    return block


def _run_jsc(html, scenario, tmp_path):
    prog = ('var __hScenario = %s;\n' % json.dumps(scenario) + _SHIMS + '\n'
            + _viewer_script(html) + '\n' + _DRIVER)
    path = tmp_path / 'viewer_run.js'
    path.write_text(prog, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    assert 'THREW' not in out and r.returncode == 0, out[-3000:]
    lines = dict(ln.split(' ', 1) for ln in out.splitlines() if ' ' in ln)
    return json.loads(lines['READOUT']), int(lines['TRACES']), json.loads(lines['LOG'])


def _scenario(real, n, absent=None, bad=None, dirs=('a',), **extra):
    """Route replies for fibers 1..n in each direction, from the real handler."""
    absent, bad = absent or {}, bad or {}
    bulk = {}
    for d in dirs:
        _, body, _ = _bulk('/api/traces?dir=%s&fibers=1-%d&maxpts=200' % (d, n),
                           _fake_load_trace(real, absent.get(d, ()), bad.get(d)))
        bulk[d] = body
    return {'bulk': bulk, 'single': {'a': {}, 'b': {}}, **extra}


@needs_jsc
def test_overview_readout_names_what_did_not_load(real_trace, tmp_path):
    sc = _scenario(real_trace, 60, fibers='1-60', dir='a',
                   absent={'a': {57, 59, 60}},
                   bad={'a': {58: ValueError('unpack requires a buffer of 4 bytes')}})
    readout, n, log = _run_jsc(open(VIEWER_HTML, encoding='utf-8').read(), sc, tmp_path)
    assert n == 56
    assert readout == (
        'could not load F57 A: not found; '
        'F58 A: unreadable (unpack requires a buffer of 4 bytes); '
        'F59 A: not found (+1 more)'), (readout, log)


@needs_jsc
def test_both_directions_say_which_one(real_trace, tmp_path):
    """F12 A and F12 B are different files: the note says which one failed."""
    sc = _scenario(real_trace, 30, dirs=('a', 'b'), fibers='1-30', dir='both',
                   absent={'a': {12}}, bad={'b': {12: KeyError('DataPts')}})
    readout, n, log = _run_jsc(open(VIEWER_HTML, encoding='utf-8').read(), sc, tmp_path)
    assert n == 58
    # setReadout adds the B-mirror note after this (A and B here are one
    # cloned trace, so B reads as mirrored); the failure comes first.  The
    # "N traces loaded" note in front of it is not shown (no plain notes).
    assert readout.startswith(
        "could not load F12 A: not found; F12 B: unreadable ('DataPts')"), (readout, log)


@needs_jsc
def test_files_panel_overview_counts_the_rest(real_trace, tmp_path):
    """The FILES panel's selection takes the same bulk load: same tail, and
    it now says how many more there are past the first three."""
    sc = _scenario(real_trace, 60, absent={'a': {3, 5, 7, 9, 11}},
                   files=['a-%d' % f for f in range(1, 61)])
    readout, n, log = _run_jsc(open(VIEWER_HTML, encoding='utf-8').read(), sc, tmp_path)
    assert n == 55
    assert readout == (
        'could not load F3 A: not found; F5 A: not found; F7 A: not found '
        '(+2 more)'), (readout, log)


@needs_jsc
def test_a_reply_without_failed_still_reads_not_found(real_trace, tmp_path):
    """An engine without the `failed` key: everything missing is "not found",
    as before, and nothing breaks."""
    sc = _scenario(real_trace, 60, fibers='1-60', dir='a', absent={'a': {60}},
                   bad={'a': {59: ValueError('bad file')}})
    del sc['bulk']['a']['failed']
    readout, _, log = _run_jsc(open(VIEWER_HTML, encoding='utf-8').read(), sc, tmp_path)
    assert readout == 'could not load F59 A: not found; F60 A: not found', (readout, log)


@needs_jsc
def test_a_clean_overview_readout_is_unchanged(real_trace, tmp_path):
    sc = _scenario(real_trace, 60, fibers='1-60', dir='a')
    readout, n, _ = _run_jsc(open(VIEWER_HTML, encoding='utf-8').read(), sc, tmp_path)
    assert n == 60
    # Robert 2026-09-30, "no plain notes": a clean load leaves the line empty.
    assert readout == ''


@needs_jsc
def test_a_dead_bulk_request_still_says_so(real_trace, tmp_path):
    sc = _scenario(real_trace, 60, fibers='1-60', dir='a', bulk_status=500)
    readout, n, _ = _run_jsc(open(VIEWER_HTML, encoding='utf-8').read(), sc, tmp_path)
    assert n == 0
    assert readout == 'could not load bulk HTTP 500. Bulk load failed, see console'


@needs_jsc
def test_the_detail_readout_is_unchanged(real_trace, tmp_path):
    """48 traces or fewer take one fetch per trace.  Its tail moved into the
    shared helper; the words must not have moved with it."""
    sc = _scenario(real_trace, 5, fibers='1-5', dir='a')
    good = {'direction': 'A', 'fiber': 0, **real_trace}
    sc['single']['a'] = {str(f): [200, dict(good, fiber=f)] for f in (1, 2, 4)}
    sc['single']['a']['3'] = [404, {'error': 'fiber 3 not found in dir a'}]
    sc['single']['a']['5'] = [500, {'error': 'parse failed: bad file'}]
    readout, n, _ = _run_jsc(open(VIEWER_HTML, encoding='utf-8').read(), sc, tmp_path)
    assert n == 3
    assert readout == 'could not load F3 A: HTTP 404; F5 A: HTTP 500'


# ─── source checks that run everywhere (CI has no jsc) ──────────────────────

def _viewer_src():
    return open(VIEWER_HTML, encoding='utf-8').read()


def _js_func(src, name):
    """Body of `async function name(...)` / `function name(...)`, brace-matched."""
    m = re.search(r'(?:async\s+)?function\s+' + re.escape(name) + r'\s*\([^)]*\)\s*\{', src)
    assert m, 'viewer.html no longer defines %s()' % name
    i, depth = m.end(), 1
    while i < len(src) and depth:
        depth += {'{': 1, '}': -1}.get(src[i], 0)
        i += 1
    return src[m.end():i - 1]


@pytest.mark.parametrize('func, readouts', [('addFibers', 2), ('applyFileSelection', 1)])
def test_every_finished_load_readout_carries_the_failures(func, readouts):
    """addFibers has two finished-load readouts (overview and detail),
    applyFileSelection one.  Each must end with the shared failure tail."""
    body = _js_func(_viewer_src(), func)
    assert len(re.findall(r'\bloadFailNote\(\)', body)) == readouts


def test_overview_labels_parse_failures_apart_from_absent_files():
    body = _js_func(_viewer_src(), 'loadOverview')
    assert 'payload.failed' in body
    assert 'unreadable (' in body and ': not found`' in body
