"""The Viewer's panels fit the window at every size (window-size audit 2026-10-01).

Driven at 520 to 1920 px wide, in the hub and in its own window, with each
divider dragged to both ends:

  files     a Files panel widened on a big window kept that width when the
            window got smaller, so the chart and the event table went to no
            width at all, and the remembered width did it again on reload
  menu      the gear's menu always opened downward; with the table dragged
            low, five of its switches were below the window's edge
  footer    a short table box drew the pinned Min/Max/Average strip on top
            of the column headers and hid every row
  header    a narrow Viewer cut the Refl Band boxes off the panel's edge
  share     the table dragged to the top came back to a 150 px chart on any
            window resize (the 150 px floor is for opening the Viewer only)

The JavaScript runs in JavaScriptCore against stand-in elements where present;
the CSS is checked everywhere.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / 'viewer.html').read_text(encoding='utf-8')
JSC = ('/System/Library/Frameworks/JavaScriptCore.framework/'
       'Versions/Current/Helpers/jsc')
needs_jsc = pytest.mark.skipif(not os.path.exists(JSC),
                               reason='JavaScriptCore shell (macOS) not present')


def _between(start, end):
    i = SRC.index(start)
    return SRC[i:SRC.index(end, i)]


def _run(tmp_path, js):
    path = tmp_path / 'fit.js'
    path.write_text(js, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and 'OUT ' in out, out[-2000:]
    return json.loads(out.split('OUT ', 1)[1].strip())


_COMMON = r"""
function El() {
  this.style = {}; this.handlers = {};
  var self = this;
  this.classList = { set: {}, add: function (c) { self.classList.set[c] = 1; },
                     remove: function (c) { delete self.classList.set[c]; },
                     contains: function (c) { return !!self.classList.set[c]; },
                     toggle: function (c, on) { if (on) self.classList.set[c] = 1; else delete self.classList.set[c]; } };
}
El.prototype.addEventListener = function (n, f) { this.handlers[n] = f; };
El.prototype.setPointerCapture = function () {};
var store = {};
var localStorage = { getItem: function (k) { return k in store ? store[k] : null; },
                     setItem: function (k, v) { store[k] = String(v); },
                     removeItem: function (k) { delete store[k]; } };
var winHandlers = {};
var window = { innerWidth: 1024, innerHeight: 768,
               addEventListener: function (n, f) { (winHandlers[n] = winHandlers[n] || []).push(f); } };
function fireResize() { (winHandlers.resize || []).forEach(function (f) { f(); }); }
var resized = 0;
function resizeCanvas() { resized++; }
"""


# ── Files panel ─────────────────────────────────────────────────────────────
_FILES_STUBS = r"""
var ws = new El(), bar = new El(), panel = new El();
ws.clientWidth = 1920; bar.offsetWidth = 7;
Object.defineProperty(panel, 'offsetWidth', { get: function () { return parseInt(panel.style.width, 10) || 300; } });
var document = {
  getElementById: function (id) { return { 'files-resizer': bar, 'files-panel': panel, 'workspace': ws }[id]; },
  body: new El(),
};
"""
_FILES_CASES = r"""
var out = {};
out.open = panel.style.width || '';
// the tech drags the panel as wide as it goes on the big window
bar.handlers.pointerdown({ button: 0, clientX: 1613, pointerId: 1, preventDefault: function () {} });
bar.handlers.pointermove({ clientX: 0 });
bar.handlers.pointerup({});
out.dragged = panel.style.width;
out.saved = store['viewer.filesPanelWidth'];
ws.clientWidth = 1024; fireResize();
out.smaller = panel.style.width;
ws.clientWidth = 1920; fireResize();
out.bigger_again = panel.style.width;
bar.handlers.dblclick();
out.reset = [panel.style.width, 'viewer.filesPanelWidth' in store];
print('OUT ' + JSON.stringify(out));
"""


def _files_block():
    return _between('// ─── Files-panel resizer', '// ─── Event-panel resizer')


@needs_jsc
def test_a_wide_files_panel_gives_way_when_the_window_shrinks(tmp_path):
    res = _run(tmp_path, _COMMON + _FILES_STUBS + _files_block() + _FILES_CASES)
    assert res['open'] == ''                          # nothing remembered: CSS width
    assert res['dragged'] == '1613px'                 # 1920 - 7 - 300 px of chart
    assert res['saved'] == '1613'
    assert res['smaller'] == '717px'                  # 1024 - 7 - 300: the chart keeps 300
    assert res['bigger_again'] == '1613px'            # the tech's width comes back
    assert res['reset'] == ['', False]


@needs_jsc
def test_a_remembered_width_wider_than_the_window_opens_fitted(tmp_path):
    pre = "store['viewer.filesPanelWidth'] = '1613'; ws.clientWidth = 1024;\n"
    cases = "print('OUT ' + JSON.stringify({ open: panel.style.width }));\n"
    res = _run(tmp_path, _COMMON + _FILES_STUBS + pre + _files_block() + cases)
    assert res['open'] == '717px'


# ── Event panel share ───────────────────────────────────────────────────────
_SHARE_STUBS = r"""
var main = new El(), bar = new El(), panel = new El();
main.clientHeight = 599; bar.offsetHeight = 7;
Object.defineProperty(panel, 'offsetHeight', { get: function () { return parseInt(panel.style.height, 10) || 240; } });
var document = {
  getElementById: function (id) { return { 'event-resizer': bar, 'event-panel': panel, 'main': main }[id]; },
  body: new El(),
};
"""
_SHARE_CASES = r"""
var out = {};
// the table dragged all the way up: the chart folds away
bar.handlers.pointerdown({ button: 0, clientY: 300, pointerId: 1, preventDefault: function () {} });
bar.handlers.pointermove({ clientY: -2000 });
bar.handlers.pointerup({});
out.dragged = panel.offsetHeight;
main.clientHeight = 945; fireResize();
out.bigger = panel.offsetHeight;
main.clientHeight = 500; fireResize();
out.smaller = panel.offsetHeight;
bar.handlers.dblclick();
main.clientHeight = 599; fireResize();
out.reset = panel.offsetHeight;
print('OUT ' + JSON.stringify(out));
"""


@needs_jsc
def test_the_table_dragged_to_the_top_stays_there_when_the_window_changes(tmp_path):
    block = _between('// ─── Event-panel resizer', '// ─── Fiber-list parser')
    res = _run(tmp_path, _COMMON + _SHARE_STUBS + block + _SHARE_CASES)
    assert res['dragged'] == 599 - 7                  # the whole column, chart 0
    assert res['bigger'] == 945 - 7                   # was 945 - 7 - 150
    assert res['smaller'] == 500 - 7
    assert res['reset'] == round((599 - 7) * 2 / 3)   # double-click: the default again


# ── Gear menu ───────────────────────────────────────────────────────────────
_MENU_STUBS = r"""
var btn = new El(), menu = new El();
btn.setAttribute = function () {};
menu.hidden = true; menu.offsetHeight = 140; menu.offsetWidth = 180;
var btnTop = 300;
btn.getBoundingClientRect = function () { return { left: 12, top: btnTop, bottom: btnTop + 22 }; };
var document = {
  getElementById: function (id) { return { 'evt-view-btn': btn, 'evt-view-menu': menu }[id]; },
};
"""
_MENU_CASES = r"""
var out = {};
function at(top, h) { btnTop = top; window.innerHeight = h; setViewMenu(true);
                      return [parseInt(menu.style.top, 10), menu.hidden]; }
out.room_below = at(300, 768);         // under the gear
out.near_bottom = at(700, 768);        // no room under it: above it
out.tiny = at(60, 150);                // room for neither: against the bottom
setViewMenu(false);
out.closed = menu.hidden;
print('OUT ' + JSON.stringify(out));
"""


@needs_jsc
def test_the_gear_menu_opens_upward_when_there_is_no_room_below(tmp_path):
    fn = _between('function setViewMenu(open)', "document.getElementById('evt-view-btn').addEventListener('click'")
    res = _run(tmp_path, _COMMON + _MENU_STUBS + fn + _MENU_CASES)
    assert res['room_below'] == [300 + 22 + 3, False]
    assert res['near_bottom'] == [700 - 3 - 140, False]
    assert res['tiny'] == [150 - 4 - 140, False]      # against the bottom edge
    assert res['closed'] is True


def test_a_menu_taller_than_the_window_scrolls():
    css = _between('#evt-view-menu {', '}')
    assert 'max-height: calc(100vh - 8px); overflow-y: auto;' in css


# ── Pinned footer needs room ────────────────────────────────────────────────
_FOOT_STUBS = r"""
function Scroller(h, headH, footH) {
  El.call(this); this.clientHeight = h;
  this.table = { tHead: headH == null ? null : { offsetHeight: headH },
                 tFoot: footH == null ? null : { offsetHeight: footH } };
}
Scroller.prototype = Object.create(El.prototype);
Scroller.prototype.querySelector = function () { return this.table; };
var scrollers = [];
var host = { querySelectorAll: function () { return scrollers; } };
var document = { getElementById: function (id) { return { 'event-tbody-wrap': host }[id]; } };
var resizeCb = null, mutCb = null, frames = [];
function ResizeObserver(cb) { resizeCb = cb; } ResizeObserver.prototype.observe = function () {};
window.ResizeObserver = ResizeObserver;
function MutationObserver(cb) { mutCb = cb; } MutationObserver.prototype.observe = function () {};
function requestAnimationFrame(f) { frames.push(f); }
function flush() { var f = frames; frames = []; f.forEach(function (g) { g(); }); }
"""
_FOOT_CASES = r"""
var out = {};
var short = new Scroller(46, 83, 60), tall = new Scroller(400, 83, 60), none = new Scroller(46, 83, null);
none.classList.add('foot-free');
scrollers = [short, tall, none];
mutCb(); mutCb(); out.queued = frames.length;     // one check per frame
flush();
out.short = short.classList.contains('foot-free');
out.tall = tall.classList.contains('foot-free');
out.none = none.classList.contains('foot-free');
short.clientHeight = 83 + 60 + 66;                 // exactly three rows: pinned again
resizeCb(); flush();
out.three_rows = short.classList.contains('foot-free');
short.clientHeight = 83 + 60 + 65;
resizeCb(); flush();
out.under_three = short.classList.contains('foot-free');
print('OUT ' + JSON.stringify(out));
"""


@needs_jsc
def test_the_strip_lets_go_when_the_box_has_no_room_for_rows(tmp_path):
    block = _between('// ─── Pinned Min/Max/Average needs room', "document.getElementById('cb-stack').onchange")
    res = _run(tmp_path, _COMMON + _FOOT_STUBS + block + _FOOT_CASES)
    assert res['queued'] == 1
    assert res['short'] is True                       # 46 px box under an 83 px header
    assert res['tall'] is False
    assert res['none'] is False                       # no strip, nothing to let go
    assert res['three_rows'] is False
    assert res['under_three'] is True


def test_a_free_strip_sits_under_the_last_row():
    assert '.fr-scroll.foot-free table.fr-table tfoot { position: static; }' in SRC


# ── Event panel header ──────────────────────────────────────────────────────
def test_the_event_header_wraps_instead_of_cutting_the_boxes_off():
    assert '#event-panel-header { flex-wrap: wrap; row-gap: 4px; }' in SRC
    css = _between('#evt-settings { margin-left: auto;', '}')
    assert 'flex-wrap: wrap; justify-content: flex-end;' in css
