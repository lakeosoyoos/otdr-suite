"""More of the window-size audit (2026-10-01), findings 6 to 12.

  hub       the Viewer started 432 px down the hub page: a heading, the
            profile heading and dropdown, the pop-out button and its line,
            and a blue "Pick an A and/or B folder" box.  Now one row (bold
            label, dropdown, button) above Settings; the heading and the
            blue box are gone (Robert), the button's line is its tooltip
  y title   the dB axis title was cut off on a short chart
  readout   the A/B marker readout cut its last trace row in half on a short
            chart, and sat over marker B
  table     one fiber on a big screen left most of the table empty; until the
            tech drags the bar it takes what its rows need, up to two thirds
  repaint   the event grids paint only the rows in view; a taller box showed
            blank space under them until a scroll
  names     at the Files panel's narrowest every name read the same; they
            are cut from the front now, so the fiber number shows

The JavaScript runs in JavaScriptCore against stand-in elements where present;
the rest is checked in the source.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from conftest import APP_PATH, VIEWER_DIR
from conftest import with_copy_helpers  # noqa: E402

SRC = (VIEWER_DIR / 'viewer.html').read_text(encoding='utf-8')
APP = APP_PATH.read_text(encoding='utf-8')
JSC = ('/System/Library/Frameworks/JavaScriptCore.framework/'
       'Versions/Current/Helpers/jsc')
needs_jsc = pytest.mark.skipif(not os.path.exists(JSC),
                               reason='JavaScriptCore shell (macOS) not present')


def _between(text, start, end):
    i = text.index(start)
    return text[i:text.index(end, i)]


def _js_func(name):
    i = SRC.index(f'function {name}(')
    j = SRC.index('\n}\n', i)
    return SRC[i:j + 2]


def _run(tmp_path, js):
    path = tmp_path / 'fit.js'
    path.write_text(with_copy_helpers(js), encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and 'OUT ' in out, out[-2000:]
    return json.loads(out.split('OUT ', 1)[1].strip())


# ── Hub: the Viewer page's header ───────────────────────────────────────────
def _page_viewer():
    return _between(APP, 'def page_viewer(', '\ndef ')


def test_the_viewer_page_has_no_heading_and_no_blue_box():
    page = _page_viewer()
    assert "st.markdown('#### Trace Viewer')" not in page
    assert '_note.info(' not in page
    assert 'Pick an A and/or B folder' not in page
    # the slot stays: the frame keeps its place, and the jump captions use it
    assert '_note = st.empty()' in page


def test_profile_and_pop_out_share_one_row_above_settings():
    page = _page_viewer()
    row = page.index("st.container(horizontal=True, vertical_alignment='center'")
    pick = page.index("_render_profile_picker_box('viewer', compact=True)")
    pop = page.index('st_components_html(theme_recolor(_pop_doc), height=36, width=270)')
    box = page.index("_render_settings_box('viewer')")
    assert row < pick < pop < box
    # the line beside the button is its tooltip now
    assert 'title="Keeps this page free for the report.' in page
    # no Viewer FEC heading or FEC pop-out: FEC shots load in this Viewer
    assert 'Viewer FEC</p>' not in page and '__POPQ__' not in page
    assert 'keeps this page free for the report &middot;' not in page


def test_the_compact_picker_is_one_label_and_the_others_keep_their_heading():
    picker = _between(APP, 'def _render_customer_profile_picker(', '\ndef ')
    assert "st.markdown('**Customer profile**' + _big, unsafe_allow_html=True," in picker
    assert "st.markdown('#### Select Customer Profile')" in picker
    assert "_render_profile_picker_box('splice report')" in APP
    assert "_render_profile_picker_box('unidirectional')" in APP


# ── The dB axis title ───────────────────────────────────────────────────────
def test_the_y_title_takes_the_longest_wording_that_fits():
    grid = _js_func('drawGrid')
    assert "['signal (dB, descending = loss)', 'signal (dB)', 'dB']" in grid
    assert '.find(t => ctx.measureText(t).width <= yRoom)' in grid
    assert 'ctx.fillText(yTitle, 0, 0);' in grid


# ── Marker readout ──────────────────────────────────────────────────────────
def test_the_readout_drops_rows_until_it_fits():
    mark = _js_func('updateMarkerReadout')
    assert 'while (rows > 1 && box.scrollHeight > box.clientHeight + 1) {' in mark
    assert 'placeMarkerReadout(box);' in mark


_PLACE = r"""
var box = { style: { left: '', right: '' }, offsetWidth: 284, offsetLeft: 0 };
var gMarkers = { a: null, b: null };
function plotRect() { return { x: 56, y: 12, w: 1000, h: 300 }; }
function xToPx(km, r) { return r.x + km * 10; }          // 100 km across
var out = {};
function at(a, b) {
  gMarkers = { a: a, b: b };
  box.offsetLeft = 1100 - 10 - 284;                      // the right corner
  placeMarkerReadout(box);
  return box.style.left || 'right';
}
out.clear_of_both = at(20, 40);       // A 256, B 456: nothing under 806..1090
out.b_under_right = at(40, 90);       // B 956 under it; A 456 clear of the left
out.both_corners = at(5, 90);         // A 106 under the left corner too
out.a_only = at(95, null);
print('OUT ' + JSON.stringify(out));
"""


@needs_jsc
def test_the_readout_moves_off_a_marker_when_the_other_corner_is_clear(tmp_path):
    res = _run(tmp_path, _js_func('placeMarkerReadout') + _PLACE)
    assert res['clear_of_both'] == 'right'
    assert res['b_under_right'] == '62px'                # the plot's left + 6
    assert res['both_corners'] == 'right'
    assert res['a_only'] == '62px'


_STATUS = r"""
var M = { l: 56, r: 16, t: 12, b: 36 };
var ro = { style: { left: '', transform: '', maxWidth: '' }, parentElement: { clientWidth: 1100 } };
var mk = { style: { display: 'block', left: '62px' }, offsetLeft: 62, offsetWidth: 284 };
var document = { getElementById: function (id) { return id === 'readout' ? ro : mk; } };
placeReadout();
print('OUT ' + JSON.stringify([ro.style.left, ro.style.transform, ro.style.maxWidth]));
"""


@needs_jsc
def test_the_status_line_goes_right_of_a_readout_on_the_left(tmp_path):
    res = _run(tmp_path, _js_func('placeReadout') + _STATUS)
    assert res == [f'{62 + 284 + 10}px', 'none', f'{1100 - 356 - 16}px']


# ── Table fitted to its rows ────────────────────────────────────────────────
_SHARE = r"""
function El() { this.style = {}; this.handlers = {}; var self = this;
  this.classList = { set: {}, add: function (c) { self.classList.set[c] = 1; },
    remove: function (c) { delete self.classList.set[c]; },
    contains: function (c) { return !!self.classList.set[c]; } }; }
El.prototype.addEventListener = function (n, f) { this.handlers[n] = f; };
El.prototype.setPointerCapture = function () {};
var main = new El(), bar = new El(), panel = new El(), head = new El(), host = new El();
main.clientHeight = 900; bar.offsetHeight = 7; head.offsetHeight = 34; host.children = [];
Object.defineProperty(panel, 'offsetHeight', { get: function () { return parseInt(panel.style.height, 10) || 240; } });
function scroller(tableH) { var s = new El(); s.classList.add('fr-scroll');
  s.offsetHeight = 300; s.clientHeight = 300; s.children = [{ offsetHeight: tableH }]; return s; }
var store = {};
var localStorage = { getItem: function (k) { return k in store ? store[k] : null; },
                     setItem: function (k, v) { store[k] = String(v); },
                     removeItem: function (k) { delete store[k]; } };
var mutCb = null;
function MutationObserver(cb) { mutCb = cb; } MutationObserver.prototype.observe = function () {};
var window = { MutationObserver: MutationObserver, addEventListener: function () {} };
var document = {
  getElementById: function (id) { return { 'event-resizer': bar, 'event-panel': panel, 'main': main,
                                           'event-panel-header': head, 'event-tbody-wrap': host }[id]; },
  body: new El(),
};
var resized = 0;
function resizeCanvas() { resized++; }
"""
_SHARE_CASES = r"""
var out = {};
out.empty = panel.offsetHeight;                     // nothing loaded yet
host.children = [scroller(209)]; mutCb();
out.one_fiber = panel.offsetHeight;
out.repainted = resized;
host.children = [scroller(3000)]; mutCb();
out.cable = panel.offsetHeight;
// a drag: the tech's split from then on
bar.handlers.pointerdown({ button: 0, clientY: 300, pointerId: 1, preventDefault: function () {} });
bar.handlers.pointermove({ clientY: 400 });
bar.handlers.pointerup({});
var dragged = panel.offsetHeight;
host.children = [scroller(209)]; mutCb();
out.after_drag = panel.offsetHeight === dragged;
print('OUT ' + JSON.stringify(out));
"""


@needs_jsc
def test_the_table_takes_what_its_rows_need_up_to_two_thirds(tmp_path):
    block = _between(SRC, '// ─── Event-panel resizer', '// ─── Fiber-list parser')
    res = _run(tmp_path, _SHARE + block + _SHARE_CASES)
    assert res['empty'] == 80                          # MIN_PANEL
    assert res['one_fiber'] == 34 + 209 + 2            # header + table, no more
    assert res['repainted'] == 1
    assert res['cable'] == round((900 - 7) * 2 / 3)    # the usual two thirds
    assert res['after_drag'] is True


def test_every_grid_repaints_its_rows_when_its_box_changes_size():
    assert SRC.count('// audit 2026-10-01).  Repaint when the box changes size.') == 3
    assert SRC.count('if (scroller.clientHeight === lastH) return;') == 3


# ── File names cut from the front ───────────────────────────────────────────
def test_long_file_names_are_cut_from_the_front():
    assert '.file-row .file-name { direction: rtl; text-align: left; }' in SRC
    assert 'name: `<span class="file-name"><bdi>${esc(files[i] || \'F\' + fiberLabel(f))}`' in SRC
