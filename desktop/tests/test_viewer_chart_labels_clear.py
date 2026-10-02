"""Labels on the Viewer's trace chart never run together or overlap.

The drawer's values ("A 0.175") already found a clear spot among the tags
and one another.  The rest of the chart did not: the files' own event
numbers (drawn before a table is in) were printed above every tick whatever
was there, so a few hundred traces made one unreadable band of digits, and
the dB labels on a short plot ran into one another.

Now every label takes its box from one pool per frame (chartLabels): the
axis labels first, then the drawer's tags, then the values and the event
numbers (a picked trace's first).  A label takes the first clear spot round
its tick or is left off; a label left off keeps its text on its tick's hit
record for the hover tip.  Pinned here by running the real functions in
JavaScriptCore with a canvas stub that keeps every text's box.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")
JSC = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/"
           "Current/Helpers/jsc")

needs_jsc = pytest.mark.skipif(not JSC.exists(), reason="no JavaScriptCore shell here")


def _fn(name):
    i = SRC.index("function " + name + "(")
    return SRC[i:SRC.index("\n}\n", i) + 3]


def _opt_fn(name):
    # Present only with the pool; without it the old drawing runs (so each
    # test below can be seen to fail on the code before it).
    return _fn(name) if "function " + name + "(" in SRC else ""


def _const(name, required=True):
    m = re.search(r"^const " + name + r" = [^\n]*;", SRC, re.M)
    assert m or not required, name
    return m.group(0) if m else ""


# The canvas stub keeps every text's box from its align, baseline and font
# size (7 px a character), skipping the rotated axis title.
PRELUDE = r"""
var gView = {x0: 0, x1: 60, y0: -30, y1: 5};
var gPickKey = null, gLabelHits = [], gShowEvents = true, gMarkerMode = false, gDragging = null;
var gMouse = null, gDistUnit = 'km';
var texts = [];
var st = {font: '10px x', align: 'start', base: 'alphabetic', fill: '', rot: 0};
var ctx = {
  set font(v) { st.font = v; }, get font() { return st.font; },
  set textAlign(v) { st.align = v; }, set textBaseline(v) { st.base = v; },
  set fillStyle(v) { st.fill = v; }, set strokeStyle(v) {}, set lineWidth(v) {},
  set globalAlpha(v) {}, setLineDash: function () {},
  measureText: function (s) { return {width: String(s).length * 7}; },
  fillText: function (s, x, y) {
    if (st.rot) return;
    s = String(s);
    var w = s.length * 7, h = +(/(\d+)px/.exec(st.font) || [0, 10])[1];
    var x0 = st.align === 'right' ? x - w : st.align === 'center' ? x - w / 2 : x;
    var y0 = st.base === 'bottom' ? y - h : st.base === 'middle' ? y - h / 2
           : st.base === 'top' ? y : y - h * 0.8;
    texts.push({s: s, x0: x0, x1: x0 + w, y0: y0, y1: y0 + h, fill: st.fill});
  },
  fillRect: function () {}, strokeRect: function () {},
  beginPath: function () {}, moveTo: function () {}, lineTo: function () {},
  stroke: function () {}, arc: function () {},
  save: function () {}, restore: function () { st.rot = 0; },
  translate: function () {}, rotate: function () { st.rot = 1; },
};
var canvas = {width: 1600, height: 900};
function canvasDpr() { return 1; }
function xToPx(x, r) { return r.x + (x - gView.x0) / (gView.x1 - gView.x0) * r.w; }
function yToPx(y, r) { return r.y + r.h - (y - gView.y0) / (gView.y1 - gView.y0) * r.h; }
function dispKm(t, km) { return t.dir === 'b' ? 55 - km : km; }
function dataKmFromDisp(t, km) { return t.dir === 'b' ? 55 - km : km; }
function dispDb(t, v) { return v; }
function isPicked(k) { return k === gPickKey; }
function axisZeroKm() { return 0; }
function distU() { return {f: 1, label: 'km'}; }
function fmtDist(km, dp) { return km.toFixed(dp); }
function spanEventNumbers(t) {
  var m = new Map(); t.data.events.forEach(function (e, i) { m.set(e, i + 1); }); return m;
}
function inTable() { return true; }
var LIGHT_EDGE = {};
// A flat trace at `db` with `n` events from 5 km, every `step` km.
function flat(dir, fiber, db, n, step, jitter) {
  var xs = [], ys = [];
  for (var i = 0; i <= 550; i++) { xs.push(i / 10); ys.push(db); }
  var evs = [];
  for (var k = 0; k < n; k++) evs.push({dist_km: 5 + k * step + (jitter || 0), is_reflective: false});
  return {key: dir + '-' + fiber, dir: dir, fiber: fiber, visible: true, color: '#2f6fb3',
          data: {dist_km: xs, trace_db: ys, events: evs}};
}
function overlaps(list) {
  var bad = [];
  for (var i = 0; i < list.length; i++) for (var j = i + 1; j < list.length; j++) {
    var a = list[i], b = list[j];
    if (a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1) bad.push(a.s + ' X ' + b.s);
  }
  return bad;
}
function outside(list, r) {
  return list.filter(function (b) {
    return b.x0 < r.x || b.x1 > r.x + r.w || b.y0 < r.y || b.y1 > r.y + r.h;
  }).map(function (b) { return b.s; });
}
// As draw() does it: a new frame's hits, its pool, the axes, then the numbers.
function frame(traces, R) {
  texts = []; gLabelHits = [];
  if (typeof chartLabels === 'function') chartLabels(R);
  drawGrid(R);
  // the axis title is not a label the pool places: it has the margin's room
  texts = texts.filter(function (b) { return !/^distance \(/.test(b.s); });
  var axis = texts.length;
  var order = gPickKey
    ? traces.filter(function (t) { return isPicked(t.key); })
        .concat(traces.filter(function (t) { return !isPicked(t.key); }))
    : traces;
  order.forEach(function (t) { drawEventMarkers(t, R); });
  return {axis: texts.slice(0, axis), nums: texts.slice(axis)};
}
"""


def _jsc(tmp_path, body):
    code = "\n".join([
        PRELUDE,
        _const("X_LABEL_GAP_PX"), _const("Y_LABEL_GAP_PX", required=False),
        _const("EVENT_NUM_H", required=False),
        _const("DRAWER_DETAIL_MAX"), _const("DRAWER_TICK_MAX"), _const("DRAWER_LANES"),
        _const("DRAWER_COLOR"),
        _fn("lowerBound"), _fn("niceTicks"), _fn("drawGrid"), _fn("labelHit"),
        _opt_fn("chartLabels"), _opt_fn("eventNumberSpots"), _opt_fn("drawLabelTip"),
        _fn("drawEventMarkers"), _fn("drawerColumnSummary"), _fn("drawerCellFailed"),
        _fn("layoutDrawerTags"),
        _fn("drawPairing"),
        body,
    ])
    p = tmp_path / "labels.js"
    p.write_text(code, encoding="utf-8")
    out = subprocess.run([str(JSC), str(p)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_jsc
def test_a_whole_cables_event_numbers_never_overlap(tmp_path):
    """432 traces, nine events each, a few metres apart from trace to
    trace: before, every number was printed above its tick and the 3,888 of
    them made one band.  Now a readable subset is drawn, none touching
    another, and every tick keeps its click box."""
    res = _jsc(tmp_path, r"""
var R = {x: 56, y: 12, w: 1100, h: 420};
var traces = [];
for (var f = 0; f < 432; f++) traces.push(flat('a', f + 1, -6 - (f % 7) * 0.05, 9, 6, (f % 5) * 0.01));
gPickKey = 'a-200';
var t0 = Date.now();
var got = frame(traces, R);
var ms = Date.now() - t0;
var hidden = gLabelHits.filter(function (h) { return h.hiddenText; }).length;
var picked = got.nums.filter(function (b) { return b.fill === '#2f6fb3'; }).length;
var pickedDrawn = gLabelHits.filter(function (h) { return h.t && h.t.key === 'a-200' && !h.hiddenText; }).length;
print(JSON.stringify({nums: got.nums.length, hidden: hidden, hits: gLabelHits.length,
                      bad: overlaps(got.nums).length, out: outside(got.nums, R).length,
                      pickedDrawn: pickedDrawn, ms: ms}));
""")
    assert res["bad"] == 0, res
    assert res["out"] == 0, res
    assert res["hits"] == 432 * 9                  # every tick keeps its click box
    assert res["nums"] + res["hidden"] == 432 * 9  # drawn, or kept for the hover tip
    assert 0 < res["nums"] < 400                   # a readable subset, not a band
    assert res["pickedDrawn"] == 9                 # the picked trace's numbers go first
    assert res["ms"] < 1000


@needs_jsc
def test_no_chart_label_sits_on_an_axis_label(tmp_path):
    """A short plot (the event panel pulled up): the dB labels keep clear of
    one another, the axis labels take their boxes first, and every number
    stays inside the plot, clear of them all."""
    res = _jsc(tmp_path, r"""
var R = {x: 56, y: 12, w: 600, h: 70};
gView = {x0: 0, x1: 60, y0: -30, y1: 5};
var traces = [];
// numbers hugging the top, the bottom and the left edge of the plot
traces.push(flat('a', 1, 4.8, 6, 9, -4.6));
traces.push(flat('a', 2, -29.5, 6, 9, -4.6));
traces.push(flat('a', 3, -12, 6, 9, -4.9));
var got = frame(traces, R);
var all = got.axis.concat(got.nums);
var pool = typeof chartLabels === 'function' ? chartLabels(R) : null;
var axisTaken = !!pool && got.axis.every(function (b) { return !pool.free(b); });
print(JSON.stringify({axis: got.axis.length, nums: got.nums.length,
                      bad: overlaps(all), out: outside(got.nums, R), axisTaken: axisTaken}));
""")
    assert res["bad"] == [], res["bad"]
    assert res["out"] == [], res["out"]
    assert res["axisTaken"]
    assert res["nums"] > 0 and res["axis"] > 0


@needs_jsc
def test_with_room_to_spare_nothing_moves(tmp_path):
    """One trace, a tall plot: every event number sits centered above its
    tick with the same click box as before, and every dB label is drawn."""
    res = _jsc(tmp_path, r"""
var R = {x: 56, y: 12, w: 1100, h: 600};
var t = flat('a', 1, -6, 9, 6, 0);
var got = frame([t], R);
var py = yToPx(-6, R), same = 0;
t.data.events.forEach(function (e, i) {
  var px = xToPx(e.dist_km, R), b = got.nums[i], h = gLabelHits[i], tw = String(i + 1).length * 7;
  if (b && b.s === String(i + 1) && b.x0 === px - tw / 2 && b.y1 === py - 11
      && h.x0 === px - tw / 2 - 3 && h.x1 === px + tw / 2 + 3
      && h.y0 === py - 11 - 10 - 3 && h.y1 === py - 11 + 3 && !h.hiddenText) same++;
});
var y = niceTicks(gView.y0, gView.y1, 8).length;
var yDrawn = got.axis.filter(function (b) { return /^-?\d+\.\d$/.test(b.s); }).length;
print(JSON.stringify({same: same, y: y, yDrawn: yDrawn}));
""")
    assert res["same"] == 9
    assert res["yDrawn"] == res["y"]


@needs_jsc
def test_a_label_left_off_keeps_its_text_for_the_hover_tip(tmp_path):
    """A value and an event number with no room are left off; the tick's hit
    record keeps the text and color, the tick still goes to its cell, and
    hovering it prints the text in the tip."""
    res = _jsc(tmp_path, r"""
var R = {x: 56, y: 12, w: 300, h: 60};
// the drawer: three fibres on one splice, six values for one spot
function trace(dir, fiber) {
  var xs = [], ys = [];
  for (var i = 0; i <= 550; i++) { xs.push(i / 10); ys.push(-i / 50); }
  return {key: dir + '-' + fiber, dir: dir, fiber: fiber, visible: true, data: {dist_km: xs, trace_db: ys}};
}
var have = [], cells = [];
for (var f = 0; f < 3; f++) {
  have.push({fiber: f + 1, ta: trace('a', f + 1), tb: trace('b', f + 1)});
  cells.push({fi: f, avg: 0.116, km: 30, avgFail: true, avgWarn: false,
              legs: {a: {km: 30, loss: 0.175, hollow: false, fail: false, warn: false},
                     b: {km: 25, loss: 0.058, hollow: false, fail: false, warn: false}}});
}
var went = [];
var D = {mode: 'suite', have: have, cols: [{i: 0, title: 'Splice 1', km: 30, cells: cells}], went: [],
         goCell: function (fi, w, col) { went.push([fi, w, col]); }};
texts = []; gLabelHits = [];
if (typeof chartLabels === 'function') chartLabels(R);
drawPairing([D], R);
var vals = gLabelHits.filter(function (h) { return h.go && h.t; });
var hiddenVals = vals.filter(function (h) { return h.hiddenText; });
// then two traces' numbers on one spot
var ta = flat('a', 7, -10, 1, 1, 9), tb = flat('a', 8, -10, 1, 1, 9);
drawEventMarkers(ta, R); drawEventMarkers(tb, R);
var num = gLabelHits.filter(function (h) { return h.e && h.t === tb; })[0];
// hover the hidden number's tick: the tip prints its text
gMouse = {px: (num.x0 + num.x1) / 2, py: (num.y0 + num.y1) / 2};
var n0 = texts.length, tip = null;
if (typeof drawLabelTip === 'function') drawLabelTip();
tip = texts.slice(n0).map(function (b) { return b.s; });
// one fibre on the same short plot: its values find no room, and hovering
// the tick on top shows that value's text
var D1 = {mode: 'suite', have: [have[0]], cols: [{i: 0, title: 'Splice 1', km: 30, cells: [cells[0]]}],
          went: [], goCell: function () {}};
texts = []; gLabelHits = [];
if (typeof chartLabels === 'function') chartLabels(R);
drawPairing([D1], R);
var top1 = gLabelHits.filter(function (h) { return h.t; }).pop();
gMouse = {px: (top1.x0 + top1.x1) / 2, py: (top1.y0 + top1.y1) / 2};
n0 = texts.length;
if (typeof drawLabelTip === 'function') drawLabelTip();
var vtip = {hidden: top1.hiddenText || null, tip: texts.slice(n0).map(function (b) { return b.s; })};
var hv = hiddenVals[0];
if (hv) hv.go();
print(JSON.stringify({values: vals.length, hiddenVals: hiddenVals.length,
                      hvText: hv ? hv.hiddenText : null, hvColor: hv ? hv.hiddenColor : null,
                      numText: num ? num.hiddenText || null : null, tip: tip, vtip: vtip,
                      went: went.length}));
""")
    assert res["values"] == 6                      # every leg keeps its tick and click
    assert res["hiddenVals"] > 0
    assert re.match(r"^[AB] 0\.\d{3}$", res["hvText"] or "")
    assert res["hvColor"]
    assert res["numText"] == "1"                   # the second number, left off
    assert res["tip"] == ["1"]
    assert res["vtip"]["hidden"] == "B 0.058"
    assert res["vtip"]["tip"] == ["B 0.058"]
    assert res["went"] == 1                        # the tick's click still goes to its cell


def test_draw_resets_the_pool_and_shows_the_tip():
    fn = _fn("draw")
    assert fn.index("gLabelHits = [];") < fn.index("chartLabels(r);") < fn.index("drawGrid(r);")
    assert "for (const t of numOrder)" in fn
    assert "drawOverlay(r);" in fn
    assert "if (gMouse && !hovered) drawLabelTip();" in _fn("drawOverlay")
