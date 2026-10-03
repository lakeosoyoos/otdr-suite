"""The one-direction event grid renders without throwing (2026-10-02).

PR #594 dropped renderFastReporterGrid's 4th parameter (`opts = {}`, then
used only for `opts.oneDir`), but the FEC Combined column from #589 still
reads `opts.fec` inside it.  Every one-direction load (A only, B only,
nothing paired; FR and OTDR Suite mode) threw `ReferenceError: opts is not
defined` and left the event panel empty with no hint; the FEC fallback grid
threw too.

Runs the Viewer's REAL script in JavaScriptCore over a stub DOM (any
property or call on an element works), then calls renderFastReporterGrid
the two ways renderEventTable does: three arguments for a one-direction
load, and with `{ fec: ... }` for the FEC fallback.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from conftest import VIEWER_DIR

HTML = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")
JSC = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/"
           "Current/Helpers/jsc")
needs_jsc = pytest.mark.skipif(not JSC.exists(), reason="no JavaScriptCore shell here")

SHIMS = r"""
var HARNESS_LOG = [];
globalThis.console = {log: function () {}, info: function () {}, debug: function () {},
                      warn: function () {}, error: function () {}};
function stub(name) {
  var store = {};
  return new Proxy(function () {}, {
    get: function (t, p) {
      if (p === Symbol.toPrimitive) return function (h) { return h === 'number' ? 0 : ''; };
      if (p === Symbol.iterator) return function* () {};
      if (p === 'then') return undefined;
      if (p === 'length') return 0;
      if (Object.prototype.hasOwnProperty.call(store, p)) return store[p];
      if (typeof p === 'symbol') return undefined;
      return (store[p] = stub(name + '.' + String(p)));
    },
    set: function (t, p, v) { store[p] = v; return true; },
    has: function () { return true; },
    apply: function () { return stub(name + '()'); },
    construct: function () { return stub('new ' + name); },
  });
}
globalThis.window = globalThis; globalThis.self = globalThis;
globalThis.top = globalThis; globalThis.parent = globalThis; globalThis.opener = null;
globalThis.devicePixelRatio = 1; globalThis.innerWidth = 1400; globalThis.innerHeight = 900;
globalThis.addEventListener = function () {}; globalThis.removeEventListener = function () {};
globalThis.dispatchEvent = function () { return true; };
// timers never fire: the boot stays idle while the test drives the grid
globalThis.setTimeout = globalThis.setInterval = function () { return 1; };
globalThis.clearTimeout = globalThis.clearInterval = function () {};
globalThis.requestAnimationFrame = function () { return 0; };
globalThis.cancelAnimationFrame = function () {};
globalThis.performance = {now: function () { return Date.now(); }};
globalThis.queueMicrotask = function (fn) { Promise.resolve().then(fn); };
globalThis.getComputedStyle = function () { return stub('style'); };
globalThis.matchMedia = function () { return {matches: false, addEventListener: function () {}, addListener: function () {}}; };
function _Obs() {} _Obs.prototype.observe = _Obs.prototype.disconnect = _Obs.prototype.unobserve = function () {};
globalThis.ResizeObserver = globalThis.MutationObserver = globalThis.IntersectionObserver = _Obs;
var _ls = {};
globalThis.localStorage = globalThis.sessionStorage = {
  getItem: function (k) { return k in _ls ? _ls[k] : null; },
  setItem: function (k, v) { _ls[k] = String(v); }, removeItem: function (k) { delete _ls[k]; }};
globalThis.navigator = {userAgent: 'jsc', platform: 'Win32', clipboard: stub('clipboard')};
globalThis.Image = globalThis.Blob = globalThis.FileReader = function () { return stub('obj'); };
globalThis.Event = globalThis.CustomEvent = function (t) { this.type = t; };
globalThis.HTMLElement = function () {};
globalThis.AbortController = function () { this.signal = {}; this.abort = function () {}; };
globalThis.TextDecoder = function () { return {decode: function () { return ''; }}; };
globalThis.TextEncoder = function () { return {encode: function () { return new Uint8Array(0); }}; };
globalThis.structuredClone = function (v) { return JSON.parse(JSON.stringify(v)); };
var ELEMENTS = {};
globalThis.document = stub('document');
document.getElementById = function (id) { return (ELEMENTS[id] = ELEMENTS[id] || stub('#' + id)); };
document.querySelector = function (q) { return stub('q:' + q); };
document.querySelectorAll = document.getElementsByClassName = document.getElementsByTagName =
  function () { return []; };
document.createElement = function (tag) { return stub('<' + tag + '>'); };
document.createTextNode = function () { return stub('text'); };
document.addEventListener = document.removeEventListener = function () {};
document.hidden = false; document.visibilityState = 'visible';
globalThis.location = {search: '', href: 'http://127.0.0.1:8771/', origin: 'http://127.0.0.1:8771',
  protocol: 'http:', host: '127.0.0.1:8771', hostname: '127.0.0.1', port: '8771',
  pathname: '/', reload: function () {}};
globalThis.URLSearchParams = function () {};
URLSearchParams.prototype.get = function () { return null; };
URLSearchParams.prototype.has = function () { return false; };
URLSearchParams.prototype.set = URLSearchParams.prototype.append = function () {};
URLSearchParams.prototype.toString = function () { return ''; };
globalThis.URL = function (u) { this.href = String(u); this.searchParams = new URLSearchParams(); };
// the trace server never answers: boot stays idle, the test drives the grid
globalThis.fetch = function () { return new Promise(function () {}); };
"""

DRIVER = r"""
function trace(dir, fiber) {
  var xs = [], ys = [];
  for (var i = 0; i <= 200; i++) { xs.push(i / 10); ys.push(-i / 50); }
  var ev = function (km, refl, end) {
    return {dist_km: km, loss_db: refl ? 0.3 : 0.05, reflectance_db: refl ? -50 : null,
            is_reflective: !!refl, is_end: !!end, slope_db_km: 0.2};
  };
  return {key: dir + '-' + fiber, dir: dir, src: dir, fiber: fiber, visible: true,
          name: 'F' + fiber + '.sor', color: '#2f6fb3',
          data: {dist_km: xs, trace_db: ys,
                 events: [ev(0, true), ev(5.1, false), ev(12.3, false), ev(20, true, true)]}};
}
var out = {};
var attached = 0;
function host() {
  var h = document.createElement('div');
  h.appendChild = function () { attached++; };
  return h;
}
// 'ok' only when the grid got as far as putting its table in the host
function attempt(name, fn) {
  attached = 0;
  try { fn(); out[name] = attached ? 'ok' : 'nothing attached'; } catch (e) { out[name] = String(e); }
}
var A = [trace('a', 1), trace('a', 2), trace('a', 3), trace('a', 4)];
var B = [trace('b', 1), trace('b', 2)];
attempt('a_only', function () {
  renderFastReporterGrid(A, host(), document.createElement('div'));
});
attempt('b_only', function () {
  renderFastReporterGrid(B, host(), document.createElement('div'));
});
attempt('fec_fallback', function () {
  renderFastReporterGrid(A, host(), document.createElement('div'),
                         {fec: {grades: {A: {'1': {found: true}}}}});
});
attempt('fec_fallback_no_grades', function () {
  renderFastReporterGrid(A, host(), document.createElement('div'),
                         {fec: fecGradesFor(A)});
});
print(JSON.stringify(out));
"""


def _script():
    (block,) = re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', HTML, re.S)
    return block


def test_every_caller_matches_the_signature():
    m = re.search(r'^function renderFastReporterGrid\(([^)]*)\)', HTML, re.M)
    assert m and 'opts' in m.group(1), m and m.group(0)
    assert 'opts.fec' in HTML.split('function renderFastReporterGrid(', 1)[1] \
        .split('\nfunction renderFrBidiGrid(', 1)[0]


@needs_jsc
def test_one_direction_and_fec_fallback_grids_render(tmp_path):
    p = tmp_path / "grid.js"
    p.write_text(SHIMS + "\n" + _script() + "\n" + DRIVER, encoding="utf-8")
    res = subprocess.run([str(JSC), str(p)], capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr + res.stdout
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out == {"a_only": "ok", "b_only": "ok", "fec_fallback": "ok",
                   "fec_fallback_no_grades": "ok"}, out
