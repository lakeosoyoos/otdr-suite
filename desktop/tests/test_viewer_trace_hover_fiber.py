"""Hover a trace in the Viewer's chart and it says which fiber it is; click it
and the event table goes to that fiber's row.

Robert 2026-09-30: "if we hover over a trace in the viewer drawer it will
tell us what number fiber it is", then "build the basic version with the
click jump".  FastReporter has neither (driven 2026-09-30: no tooltip and no
pick on a trace line; only an event mark's Type/Position/Loss tooltip), so
the rules here are the Viewer's own:

  hover        a box beside the cursor with a color swatch and "F0002 A->B"
               for the ONE trace whose drawn line is nearest, within
               TRACE_HIT_PX (Robert: "only show one fiber we are hovering
               over"); the trace itself is drawn as it was (the boss
               2026-10-01: "just give us the box")
  click        the same nearest trace is picked (as a table row click picks) and
               the table centres its row between the pinned header and the
               pinned Minimum/Maximum rows; a second click on the picked
               trace lets it go; a press that moves is still a pan
  no numbers   the box names the fiber only (no km/dB on hover, 2026-09-29)

The hit test is a plain function (traceHits), run in JavaScriptCore where
present; the wiring is checked everywhere.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

from conftest import VIEWER_DIR
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


def _const(name):
    m = re.search(r'^const ' + re.escape(name) + r' = [^;]+;', SRC, re.M)
    assert m, name
    return m.group(0)


# A 1000 x 500 px plot over 0..10 km and 0..50 dB: 100 px per km, 10 px per dB.
_CASES = r"""
var gView = { x0: 0, x1: 10, y0: 0, y1: 50 };
var r = { x: 0, y: 0, w: 1000, h: 500 };
// Stand-ins for the display transforms: a flipped trace runs from `origin`.
function dispKm(t, k) { return t.flip ? t.origin - k : k; }
function dataKmFromDisp(t, d) { return t.flip ? t.origin - d : d; }
function dispDb(t, v) { return v; }
function line(key, fiber, dir, f, opts) {
  var xs = [], ys = [];
  for (var i = 0; i <= 1000; i++) { var k = i / 100; xs.push(k); ys.push(f(k)); }
  var t = { key: key, fiber: fiber, dir: dir, visible: true, data: { dist_km: xs, trace_db: ys } };
  for (var p in (opts || {})) t[p] = opts[p];
  return t;
}
var A = line('a-1', '0001', 'a', function (k) { return 40 - 2 * k; });
var B = line('b-1', '0001', 'b', function (k) { return 30 - 2 * k; });
var HID = line('a-9', '0009', 'a', function (k) { return 40 - 2 * k; }, { visible: false });
var gTraces = [A, B, HID];
var keys = function (hs) { return hs.map(function (h) { return h.t.key; }); };
var out = {};
// km 5 = px 500; A is 30 dB there = py 200, B is 20 dB = py 300
out.on_a = keys(traceHits(500, 200, r));
out.near_a = keys(traceHits(500, 204, r));
out.off_a = keys(traceHits(500, 206, r));
out.on_b = keys(traceHits(500, 300, r));
out.between = keys(traceHits(500, 250, r));
out.outside = keys(traceHits(1200, 200, r));
// two traces a pixel apart: both, the nearer first
var A2 = line('a-2', '0002', 'a', function (k) { return 40.1 - 2 * k; });
gTraces = [A, A2];
out.both_low = keys(traceHits(500, 201, r));
out.both_high = keys(traceHits(500, 198, r));
// a steep fall between two samples is on the line all the way down
var END = line('a-3', '0003', 'a', function (k) { return k <= 5 ? 30 : 0; });
gTraces = [END];
out.fall = keys(traceHits(500.5, 350, r));
// a flipped B drawn over A's km frame is hit where it is DRAWN
var FL = line('b-4', '0004', 'b', function (k) { return 40 - 2 * (10 - k); }, { flip: true, origin: 10 });
gTraces = [FL];
out.flipped = keys(traceHits(300, 500 - 34 * 10, r));
// one trace spikes 15 dB away from a bundle of three at km 5: on the spike
// only it is named; on the bundle beside the spike, all three are
var S1 = line('a-5', '0005', 'a', function (k) { return Math.abs(k - 5) < 0.005 ? 45 : 30; });
var S2 = line('a-6', '0006', 'a', function (k) { return 30.05; });
var S3 = line('a-7', '0007', 'a', function (k) { return 29.95; });
gTraces = [S1, S2, S3];
out.spike_top = keys(traceHits(500, 50, r));          // 45 dB = py 50
out.spike_side = keys(traceHits(520, 200, r)).sort(); // 30 dB, 0.2 km on
// zoomed out, 20 samples a pixel: drawTrace strokes every 10th sample, and
// the hit test reads the same ones -- the line is hit, a lone sample's spike
// the chart never drew is not
var dx = [], dy = [];
for (var i = 0; i <= 20000; i++) { dx.push(i / 2000); dy.push(i === 10005 ? 45 : 30); }
gTraces = [{ key: 'a-8', fiber: '0008', dir: 'a', visible: true, data: { dist_km: dx, trace_db: dy } }];
out.dense_line = keys(traceHits(500, 200, r));
out.undrawn_spike = keys(traceHits(500.25, 50, r));
out.seg = [segDist(5, 0, 0, 0, 10, 0), segDist(-3, 4, 0, 0, 10, 0), segDist(1, 1, 2, 2, 2, 2)];
// a click on a trace picks it and asks the grid for its ROW (no event)
var gPickKey = null, draws = 0, asked = null;
function draw() { draws++; }
var gGridGoTo = function (t, e) { asked = [t.key, e]; };
goToTrace(A);  out.click = [gPickKey, asked, draws];
draws = 0; asked = null;
goToTrace(A);  out.click_again = [gPickKey, asked, draws];   // lets go, no jump
goToTrace(A);  out.click_third = gPickKey;            // picks it again
gGridGoTo = null;
goToTrace(B);  out.no_grid = gPickKey;                // no table: still picks
print('OUT ' + JSON.stringify(out));
"""


@pytest.fixture(scope='module')
def hits(tmp_path_factory):
    funcs = '\n'.join([_const('TRACE_HIT_PX')]
                      + [_js_func(n) for n in ('xToPx', 'yToPx', 'pxToX', 'lowerBound',
                                               'upperBound', 'segDist', 'traceHits',
                                               'goToTrace')])
    path = tmp_path_factory.mktemp('trace_hover') / 'hits.js'
    path.write_text(COPY_HELPERS_JS + funcs + '\n' + _CASES, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and 'OUT ' in out, out[-2000:]
    return json.loads(out.split('OUT ', 1)[1].strip())


@needs_jsc
def test_a_trace_is_hit_within_a_few_pixels_of_its_line(hits):
    assert hits['on_a'] == ['a-1']           # the hidden copy under it is not named
    assert hits['near_a'] == ['a-1']
    assert hits['off_a'] == []
    assert hits['on_b'] == ['b-1']
    assert hits['between'] == []
    assert hits['outside'] == []


@needs_jsc
def test_overlapping_traces_sort_nearest_first_so_the_nearest_is_named(hits):
    assert hits['both_low'] == ['a-1', 'a-2']
    assert hits['both_high'] == ['a-2', 'a-1']


@needs_jsc
def test_a_spike_away_from_the_group_names_only_its_own_fiber(hits):
    assert hits['spike_top'] == ['a-5']
    assert hits['spike_side'] == ['a-5', 'a-6', 'a-7']


@needs_jsc
def test_zoomed_out_the_hit_test_reads_the_samples_the_chart_draws(hits):
    assert hits['dense_line'] == ['a-8']
    assert hits['undrawn_spike'] == []
    th = _js_func('traceHits')
    # drawTrace's own window and step
    assert 'const step = Math.max(1, Math.floor(Math.max(1, (v1 - v0) / r.w) / 2));' in th
    dt = _js_func('drawTrace')
    assert 'const samplesPerPx = Math.max(1, (i1 - i0) / r.w);' in dt
    assert 'const stride = Math.max(1, Math.floor(samplesPerPx / 2));' in dt


@needs_jsc
def test_steep_falls_and_flipped_traces_are_hit_where_drawn(hits):
    assert hits['fall'] == ['a-3']
    assert hits['flipped'] == ['b-4']
    assert hits['seg'] == [0, 5, pytest.approx(2 ** 0.5)]


@needs_jsc
def test_a_click_picks_the_trace_and_goes_to_its_row(hits):
    assert hits['click'] == ['a-1', ['a-1', None], 1]
    assert hits['click_again'] == [None, None, 1]
    assert hits['click_third'] == 'a-1'
    assert hits['no_grid'] == 'b-1'


def test_the_box_names_one_fiber_and_nothing_else():
    box = _js_func('drawHoverBox')
    assert "const text = `F${fiberLabel(t.fiber)} ${t.dir === 'a' ? 'A→B' : 'B→A'}`" in box
    assert "+ (t.src && t.src.length > 1 ? srcTag(t.src) : '');" in box   # another span's
    assert 'more' not in box and 'forEach' not in box      # one fiber, never a list
    assert 'km' not in box.replace('ctx.', '') and 'dB' not in box
    draw = _js_func('draw')
    over = _js_func('drawOverlay')
    assert 'if (gMouse && hovered) drawHoverBox(hovered);' in over
    assert 'const hovered = hoverTrace();' in over
    # hovering changes no trace: no bold redraw on top, no other width, and
    # the chart under the overlay is drawn without asking what is hovered
    assert 'drawTrace(hovered' not in draw and 'drawTrace(' not in over
    assert 'hoverTrace()' not in draw and 'gHoverKey' not in draw
    assert 'gHoverKey' not in _js_func('drawTrace')
    assert draw.index('drawEventMarkers(t, r)') < draw.index('keepBase();') < draw.index('drawOverlay(r);')


def test_hover_follows_the_mouse_and_stays_off_links_drags_and_paper():
    move = SRC.split("canvas.addEventListener('mousemove'", 1)[1].split("window.addEventListener('mouseup'", 1)[0]
    assert "const near = (gDragging || gLabelClick || overLabel) ? []" in move
    assert "gHoverKey = near.length ? near[0].t.key : null;" in move   # the nearest only
    assert "gHoverKey != null ? 'pointer' : 'crosshair'" in move
    assert "canvas.addEventListener('mouseleave', () => { gMouse = null; gHoverKey = null; drawHover(); });" in SRC
    snap = _js_func('snapChart')
    assert 'gHoverKey = null;' in snap
    keep = _js_func('withPrintCanvas')
    assert 'hover: gHoverKey' in keep and 'gHoverKey = keep.hover;' in keep


def test_a_still_click_on_a_trace_jumps_and_a_moving_press_still_pans():
    down = SRC.split("canvas.addEventListener('mousedown'", 1)[1].split("canvas.addEventListener('mousemove'", 1)[0]
    assert "if (!gMarkerMode && ev.button === 0) {" in down
    # a press on the empty chart is a click too, with no trace
    assert "gTraceClick = { t: th.length ? th[0].t : null," in down
    assert down.index('gTraceClick = { t: th.length') < down.index("kind: 'pan'")
    up = SRC.split("window.addEventListener('mouseup'", 1)[1].split("canvas.addEventListener('dblclick'", 1)[0]
    blk = up.split('if (gTraceClick) {', 1)[1].split('\n  }\n', 1)[0]
    assert 'Math.abs(ev.offsetX - c.x) <= 3 && Math.abs(ev.offsetY - c.y) <= 3' in blk
    assert 'gView = { x0: d.vx0, x1: d.vx1, y0: d.vy0, y1: d.vy1 };' in blk   # the jiggle is undone
    assert 'else if (c.t) goToTrace(c.t);' in blk
    # a still click on the empty chart lets go of the picked fiber (the boss,
    # 2026-10-01: "cant unselect his fibers"); a double-click's second click
    # (a Fit) changes no pick
    assert 'else { gPickKey = null; draw(); }' in blk
    assert blk.index('if (ev.detail > 1) draw();') < blk.index('goToTrace(c.t)')


def test_every_table_centres_the_row_when_given_no_event():
    for name in ('renderFastReporterGrid', 'paintFrBidiGrid', 'paintSuiteBidiGrid'):
        body = SRC.split('function %s(' % name, 1)[1]
        goto = body.split('gGridGoTo = (t, e) => {', 1)[1].split('\n  };', 1)[0]
        row = goto.split('if (!e) {', 1)[1].split('\n    }', 1)[0]
        assert goto.index('if (!e) {') < goto.index('e.dist_km'), name
        # centred in what the pinned header and pinned footer leave, always
        assert 'pinnedFootH(table)' in row or 'footH' in row, name
        assert 'scroller.scrollTop = Math.max(0, rowTop - ' in row, name
        assert '(mid - FR_ROW_H) / 2' in row, name
        assert 'paintRows(' in row and 'return;' in row, name
