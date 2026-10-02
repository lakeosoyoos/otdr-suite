"""The event table gets a usable share of the Viewer (demo list #32).

Embedded in the hub, the table's scroller was 206 px (about three and a half
rows under the pinned Min/Max/Average strip) at 1184x760, 1440x900 and
1920x1080 alike: the panel was a fixed 240 px and the chart took every extra
pixel.  Row text also showed through the lines between the pinned footer
rows, and the toolbar wrapped to three lines at 1184 with the sidebar open.

  share     the panel takes TABLE_SHARE of the column until the tech drags the
            bar; a drag is kept (and remembered) as a share, so the split
            survives a bigger or smaller window; double-click resets it
  footer    the grid's borders are separate, so each pinned cell paints its
            own edges and nothing scrolls through them
  toolbar   tighter padding, so the same controls fit in fewer lines

The resizer runs in JavaScriptCore against stand-in elements where present;
the CSS is checked everywhere.
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


def _resizer_block():
    i = SRC.index('// ─── Event-panel resizer')
    j = SRC.index('// ─── Fiber-list parser')
    return SRC[i:j]


_STUBS = r"""
function El(h) {
  this.clientHeight = h || 0; this.style = {}; this.handlers = {};
  var self = this;
  this.classList = { set: {}, add: function (c) { self.classList.set[c] = 1; },
                     remove: function (c) { delete self.classList.set[c]; },
                     contains: function (c) { return !!self.classList.set[c]; } };
}
El.prototype.addEventListener = function (n, f) { this.handlers[n] = f; };
El.prototype.setPointerCapture = function () {};
var main = new El(599), bar = new El(0), panel = new El(0);
bar.offsetHeight = 7;
Object.defineProperty(panel, 'offsetHeight', { get: function () { return parseInt(panel.style.height, 10) || 240; } });
var store = {};
var localStorage = { getItem: function (k) { return k in store ? store[k] : null; },
                     setItem: function (k, v) { store[k] = String(v); },
                     removeItem: function (k) { delete store[k]; } };
var winHandlers = {};
var window = { addEventListener: function (n, f) { winHandlers[n] = f; } };
var document = {
  getElementById: function (id) { return { 'event-resizer': bar, 'event-panel': panel, 'main': main }[id]; },
  body: new El(0),
};
var resized = 0;
function resizeCanvas() { resized++; }
"""

_CASES = r"""
var out = {};
out.open_1184 = panel.offsetHeight;                // no drag yet: the default share
main.clientHeight = 945;  winHandlers.resize();
out.open_1920 = panel.offsetHeight;
main.clientHeight = 599;  winHandlers.resize();
out.back_1184 = panel.offsetHeight;                // never ratchets down for good
// a drag down by 100 px, then a bigger window: the split is kept
bar.handlers.pointerdown({ button: 0, clientY: 300, pointerId: 1, preventDefault: function () {} });
bar.handlers.pointermove({ clientY: 400 });
bar.handlers.pointerup({});
out.dragged = panel.offsetHeight;
out.saved = [store['viewer.eventPanelHeight'], (+store['viewer.eventPanelShare']).toFixed(3)];
main.clientHeight = 945;  winHandlers.resize();
out.dragged_1920 = panel.offsetHeight;
bar.handlers.dblclick();
out.reset = [panel.offsetHeight, 'viewer.eventPanelShare' in store, 'viewer.eventPanelHeight' in store];
print('OUT ' + JSON.stringify(out));
"""

_LEGACY = r"""
store['viewer.eventPanelHeight'] = '500';           // remembered before the share
"""
_LEGACY_CASES = r"""
var out = { open: panel.offsetHeight };
main.clientHeight = 945;  winHandlers.resize();
out.bigger = panel.offsetHeight;
print('OUT ' + JSON.stringify(out));
"""


def _run(tmp_path, pre, cases):
    path = tmp_path / 'share.js'
    path.write_text(_STUBS + pre + _resizer_block() + '\n' + cases, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and 'OUT ' in out, out[-2000:]
    return json.loads(out.split('OUT ', 1)[1].strip())


@needs_jsc
def test_the_table_takes_two_thirds_of_the_column_at_every_size(tmp_path):
    res = _run(tmp_path, '', _CASES)
    avail_1184, avail_1920 = 599 - 7, 945 - 7
    assert res['open_1184'] == round(avail_1184 * 2 / 3)      # was a fixed 240
    assert res['open_1920'] == round(avail_1920 * 2 / 3)
    assert res['back_1184'] == res['open_1184']


@needs_jsc
def test_a_drag_is_kept_as_a_share_and_double_click_resets_it(tmp_path):
    res = _run(tmp_path, '', _CASES)
    assert res['dragged'] == round(592 * 2 / 3) - 100
    assert res['saved'][0] == str(res['dragged'])
    assert abs(float(res['saved'][1]) - res['dragged'] / 592) < 1e-3
    assert abs(res['dragged_1920'] - round(938 * res['dragged'] / 592)) <= 1
    assert res['reset'] == [round(938 * 2 / 3), False, False]


@needs_jsc
def test_a_height_remembered_before_the_share_still_opens_capped(tmp_path):
    res = _run(tmp_path, _LEGACY, _LEGACY_CASES)
    assert res['open'] == 599 - 7 - 150                     # LOAD_MIN_CHART still holds
    assert abs(res['bigger'] - round(938 * res['open'] / 592)) <= 1


def test_the_table_share_is_sensible():
    m = re.search(r'const TABLE_SHARE = ([0-9. /]+);', SRC)
    assert m and abs(eval(m.group(1)) - 2 / 3) < 1e-9


def test_nothing_shows_through_the_pinned_footer():
    # separate borders: each pinned cell paints its own edges
    assert 'table.fr-table { border-collapse: separate; border-spacing: 0; }' in SRC
    assert 'table.fr-table th, table.fr-table td { border-width: 0 1px 1px 0; }' in SRC
    assert 'table.fr-table tfoot { position: sticky; bottom: 0; z-index: 4; }' in SRC
    assert 'table.fr-table tfoot tr.fr-agg td { background: #eef3f8; }' in SRC


def test_the_toolbar_is_tighter():
    assert '#toolbar { gap: 6px; padding: 6px 10px; }' in SRC
    assert '#toolbar button { padding: 4px 7px; }' in SRC
    # every control is still there
    for ctl in ('id="fiber-input"', 'id="btn-add"', 'id="btn-clear"', 'id="btn-fit"',
                'id="btn-fit-x"', 'id="btn-fit-y"', 'id="btn-zoom-in"', 'id="btn-zoom-out"',
                'id="cb-stack"', 'id="num-yspace"', 'id="cb-colors"',
                'id="btn-markers"', 'id="btn-markers-clear"', 'id="btn-report"'):
        assert ctl in SRC, ctl
