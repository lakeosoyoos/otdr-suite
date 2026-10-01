"""The Viewer's drawer (the trace chart) follows the analysis mode, as the
event panel does, at any number of fibres.

Robert, 2026-09-30: "we need to have suite and FR mode in Viewer for drawer
and event panel and we need it to work no matter how many traces we drop
in."  Before this the panel changed with the mode and the drawer did not:
it drew each file's own events, so a splice OTDR Suite reads as one event
(Average over the gate, FAIL) and FastReporter splits in two (both pass)
looked the same on the chart in both modes.

Pinned here:
  * the panel that prints a mode's table hands the drawer the SAME columns
    and the SAME verdicts (the Suite table in OTDR Suite mode, FR's table
    in FastReporter mode; FR's table standing in for a missing Suite one
    marks nothing);
  * with no table (pending, error, one direction only) the drawer draws the
    files' own events, and a trace the table does not cover always does;
  * the events checkbox still hides every mark;
  * a click on a drawer mark goes to its own cell in the panel, and the
    span menu opens only on a real reading;
  * past a few fibres each column gets ONE tag, and past DRAWER_TICK_MAX
    only fibres whose Average fails keep their ticks;
  * the tags never overlap (layoutDrawerTags), checked by running the real
    functions in JavaScriptCore when the Mac's jsc is there.
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


def _fn(name):
    i = SRC.index("function " + name + "(")
    return SRC[i:SRC.index("\n}\n", i) + 3]


def _const(name):
    m = re.search(r"^const " + name + r" = [^\n]*;", SRC, re.M)
    assert m, name
    return m.group(0)


# ─── the source ──────────────────────────────────────────────────────────

def test_draw_marks_the_table_and_falls_back_per_trace():
    fn = _fn("draw")
    ev = fn[fn.index("if (gShowEvents) {"):]
    assert "gDrawerMarks.length ? drawPairing(gDrawerMarks, r) : null" in ev
    # a trace the table does not cover keeps its file's own event numbers
    assert "covered && covered.has(t)" in ev and "drawEventMarkers(t, r)" in ev


def test_no_prototype_left():
    assert "gSuiteDrawer" not in SRC
    assert "drawSuitePairing" not in SRC
    assert "PROTOTYPE" not in SRC


def test_every_repaint_clears_the_marks_until_a_table_is_in():
    assert "gDrawerMarks = [];" in _fn("renderEventTable")
    # each table adds its own set once it is painted
    for name in ("paintSuiteBidiGrid", "paintFrBidiGrid", "renderFastReporterGrid"):
        assert "gDrawerMarks.push(marks);" in _fn(name), name


def test_suite_marks_use_the_panels_own_verdicts():
    fn = _fn("paintSuiteBidiGrid")
    i = fn.index("const marks = { mode: 'suite'")
    block = fn[fn.index("const drawerLeg"):i + 900]
    # a one-direction table has no Average: its tag takes that direction's verdict
    assert "avgFail: lossFails(c, x, oneDir || 'avg')" in block
    assert "avgWarn: lossWarns(c, x, oneDir || 'avg')" in block
    assert "single: !!oneDir" in block
    assert "fail: cellFails(c, x, w)" in block
    assert "hollow: !!leg.grey" in block
    # the panel's column filter decides the drawer's columns too
    assert "cells: keepCol[i]" in block
    assert fn.index("const keepCol") < i


def test_fr_marks_only_in_fr_mode_and_use_frs_verdicts():
    fn = _fn("paintFrBidiGrid")
    i = fn.index("if (gAnalysisMode === 'fr') {")
    block = fn[i:i + 1300]
    assert "marks = { mode: 'fr'" in block
    assert "avgFail: cellFails(x, 'avg')" in block
    assert "fail: cellFails(x, w)" in block
    # FR's transplanted (synthetic) and launch-level legs are not readings
    assert "hollow: !legOk(leg)" in block
    # the leg's km is the one the panel's cell carries
    assert "km: rawKm(leg.pos_m)" in block
    assert "title: `Event ${i + 1}`" in block


def test_both_tables_name_their_rows_and_wire_the_drawer():
    for name in ("paintSuiteBidiGrid", "paintFrBidiGrid"):
        fn = _fn(name)
        assert 'data-row="${fi}-${which}"' in fn, name
        assert "marks.goCell = (fi, which, col) => gridGoCell(" in fn, name
        # a click anywhere on a row, its fibre name included, picks it
        assert "pickRow(tr.dataset.avg ? [p.ta.key, p.tb.key]" in fn, name
        # a fibre loaded one way among paired ones gets its own table
        assert "appendOneDirGrid(singles, host);" in fn, name
    one = _fn("renderFastReporterGrid")
    assert 'data-row="${ti}-${t.dir}"' in one
    assert "marks.goCell = (ti, which, col) => gridGoCell(" in one
    go = _fn("gridGoCell")
    assert 'tr[data-row="${descs[k][0]}-${descs[k][1]}"]' in go
    assert 'td[data-col="${col}"]' in go


def test_clicks_on_drawer_marks():
    up = SRC[SRC.index("window.addEventListener('mouseup'"):]
    up = up[:up.index("\n});")]
    assert "if (!still) return;" in up
    assert "if (c.hit.go) c.hit.go();" in up
    assert "else if (gGridGoTo) gGridGoTo(c.hit.t, c.hit.e);" in up
    menu = SRC[SRC.index("canvas.addEventListener('contextmenu'"):]
    menu = menu[:menu.index("\n});")]
    assert "if (!lh || !lh.t) return;" in menu
    assert "if (!gShowEvents) return;" in menu


def test_event_labels_switch_still_hides_every_mark():
    i = SRC.index("getElementById('set-event-labels').onchange")
    assert "gShowEvents = e.target.checked;" in SRC[i:i + 300]
    assert "draw();" in SRC[i:i + 300]
    fn = _fn("draw")
    assert fn.index("if (gShowEvents) {") < fn.index("drawPairing(")


def test_one_direction_is_marked_in_either_mode():
    fn = _fn("renderFastReporterGrid")
    i = fn.index("const marks = { mode: gAnalysisMode, single: true")
    block = fn[i:i + 1400]
    # the cells' own verdicts: the gate the table judges at, its warning
    assert "fail = overGate(v), warn = !fail && clearsAt(v, warnGate)" in block
    assert "cells: keepCol[i]" in block
    for cell in ("overGate(v)) cls = ' class=\"fr-hi\"'", "clearsAt(v, warnGate)) cls = ' class=\"fr-warn\""):
        assert cell in fn, cell


def test_one_way_fibres_among_pairs_are_judged_one_way():
    fn = _fn("renderFastReporterGrid")
    assert "const overGate = opts.oneDir ? (v => clearsAt(v, gateFor(false, true))) : clearsGate;" in fn
    assert "const warnGate = warnFor(false, !!opts.oneDir);" in fn
    assert "clearsGate(" not in fn.replace("opts.oneDir ? (v => clearsAt(v, gateFor(false, true))) : clearsGate;", "")
    # the A+B table keeps the event-number clicks and the Report's rows
    assert "if (!opts.oneDir) gGridGoTo = (t, e) => {" in fn
    assert "if (!opts.oneDir) gTableExport = " in fn
    app = _fn("appendOneDirGrid")
    assert "renderFastReporterGrid(traces, box, document.createElement('div'), { oneDir: true })" in app
    assert "One direction only: F" in app


# ─── the real functions, in JavaScriptCore ───────────────────────────────

needs_jsc = pytest.mark.skipif(not JSC.exists(), reason="no JavaScriptCore shell here")


def _jsc(tmp_path, body):
    """Run the viewer's drawer functions with a recording canvas stub."""
    prelude = r"""
var gView = {x0: 0, x1: 60, y0: -30, y1: 5};
var gPickKey = null;
var gLabelHits = [];
var calls = {fillText: [], fillRect: 0, stroke: 0, arc: 0};
var ctx = {
  set font(v) {}, set textAlign(v) {}, set textBaseline(v) {}, set fillStyle(v) {},
  set strokeStyle(v) {}, set lineWidth(v) {}, set globalAlpha(v) {},
  measureText: function (s) { return {width: s.length * 7}; },
  fillText: function (s) { calls.fillText.push(s); },
  fillRect: function () { calls.fillRect++; },
  beginPath: function () {}, moveTo: function () {}, lineTo: function () {},
  stroke: function () { calls.stroke++; }, arc: function () { calls.arc++; },
};
function xToPx(x, r) { return r.x + (x - gView.x0) / (gView.x1 - gView.x0) * r.w; }
function yToPx(y, r) { return r.y + r.h - (y - gView.y0) / (gView.y1 - gView.y0) * r.h; }
function dispKm(t, km) { return t.dir === 'b' ? 55 - km : km; }
function dataKmFromDisp(t, km) { return t.dir === 'b' ? 55 - km : km; }
function dispDb(t, v) { return v; }
function trace(dir, fiber) {
  var xs = [], ys = [];
  for (var i = 0; i <= 550; i++) { xs.push(i / 10); ys.push(-i / 50); }
  return {key: dir + '-' + fiber, dir: dir, fiber: fiber, visible: true,
          data: {dist_km: xs, trace_db: ys}};
}
// n fibres, one column at 44.1 km: fibre 0 fails its Average, fibre 1's B
// leg is filled in by the table, the rest pass.
function marks(n, cols) {
  var have = [];
  for (var f = 0; f < n; f++) have.push({fiber: f + 1, ta: trace('a', f + 1), tb: trace('b', f + 1)});
  var out = [];
  for (var c = 0; c < (cols || 1); c++) {
    var km = 5 + c * 5;
    out.push({i: c, title: 'Splice ' + (c + 1), km: km, cells: have.map(function (_p, fi) {
      return {fi: fi, avg: fi === 0 ? 0.116 : 0.02, km: km, avgFail: fi === 0, avgWarn: false,
              legs: {a: {km: km, loss: 0.175, hollow: false, fail: false, warn: false},
                     b: {km: 55 - km - 0.115, loss: fi === 1 ? 0.01 : 0.058,
                         hollow: fi === 1, fail: false, warn: false}}};
    })});
  }
  var went = [];
  return {mode: 'suite', have: have, cols: out, went: went,
          goCell: function (fi, w, col) { went.push([fi, w, col]); }};
}
var R = {x: 56, y: 12, w: 1100, h: 600};
"""
    code = "\n".join([
        prelude,
        _const("DRAWER_DETAIL_MAX"), _const("DRAWER_TICK_MAX"), _const("DRAWER_LANES"),
        _const("DRAWER_COLOR"),
        _fn("lowerBound"), _fn("drawerColumnSummary"), _fn("layoutDrawerTags"),
        _fn("chartLabels"), _fn("drawPairing"),
        body,
    ])
    p = tmp_path / "drawer.js"
    p.write_text(code, encoding="utf-8")
    out = subprocess.run([str(JSC), str(p)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_jsc
def test_tags_never_overlap(tmp_path):
    res = _jsc(tmp_path, r"""
var tags = [];
for (var i = 0; i < 60; i++) tags.push({x: 60 + (i * 37) % 1000, full: 'Splice ' + i + '  avg 0.116  FAIL', short: '' + i});
var w = function (s) { return s.length * 7 + 12; };
var g = layoutDrawerTags(tags, w, 56, 1156, 3);
var bad = 0, lanes = {}, dots = 0, shorts = 0;
g.forEach(function (a, i) {
  if (a.text == null) { dots++; return; }
  if (a.text === tags[i].short) shorts++;
  if (a.x0 < 56 || a.x1 > 1156) bad++;
  (lanes[a.lane] = lanes[a.lane] || []).push(a);
});
Object.keys(lanes).forEach(function (k) {
  var l = lanes[k].sort(function (p, q) { return p.x0 - q.x0; });
  for (var j = 1; j < l.length; j++) if (l[j].x0 < l[j - 1].x1) bad++;
});
var dotLane = g.filter(function (a) { return a.text == null; }).every(function (a) { return a.lane === 3; });
print(JSON.stringify({bad: bad, dots: dots, shorts: shorts, dotLane: dotLane, n: g.length}));
""")
    assert res["n"] == 60
    assert res["bad"] == 0
    assert res["shorts"] > 0 and res["dots"] > 0      # crowded: both fallbacks used
    assert res["dotLane"]


@needs_jsc
def test_column_summary_points_at_the_worst_failing_fibre(tmp_path):
    res = _jsc(tmp_path, r"""
var s = drawerColumnSummary({cells: [
  {fi: 0, avg: 0.30, avgFail: false, avgWarn: true},
  {fi: 3, avg: 0.12, avgFail: true, avgWarn: false},
  {fi: 5, avg: 0.15, avgFail: true, avgWarn: false},
  {fi: 7, avg: null, avgFail: false, avgWarn: false}]});
print(JSON.stringify(s));
""")
    assert res == {"n": 4, "nFail": 2, "nWarn": 1, "worst": 0.15, "worstFi": 5}


@needs_jsc
def test_one_fibre_is_drawn_in_full(tmp_path):
    res = _jsc(tmp_path, r"""
var D = marks(1, 1);
var cov = drawPairing([D], R);
var tag = gLabelHits.filter(function (h) { return !h.t && !h.e; });
tag[tag.length - 1].go();
var legs = gLabelHits.filter(function (h) { return h.t; });
legs[0].go();
print(JSON.stringify({covered: cov.size, text: calls.fillText, went: D.went,
                      legs: legs.map(function (h) { return [h.t.dir, h.e.dist_km]; })}));
""")
    assert res["covered"] == 2
    assert "A 0.175" in res["text"] and "B 0.058" in res["text"]
    assert "Splice 1  avg 0.116  FAIL" in res["text"]
    assert res["went"] == [[0, "avg", 0], [0, "a", 0]]
    assert sorted(res["legs"]) == [["a", 5], ["b", 49.885]]


@needs_jsc
def test_filled_in_leg_is_a_ring_with_no_span_menu(tmp_path):
    res = _jsc(tmp_path, r"""
var D = marks(2, 1);
D.have = D.have.slice(1); D.cols[0].cells = [D.cols[0].cells[1]]; D.cols[0].cells[0].fi = 0;
drawPairing([D], R);
var leg = gLabelHits.filter(function (h) { return h.go && !h.t && h.y1 - h.y0 !== 18; });
print(JSON.stringify({arcs: calls.arc, hollowHits: leg.length}));
""")
    assert res["arcs"] == 1
    assert res["hollowHits"] == 1


@needs_jsc
def test_many_fibres_one_tag_per_column(tmp_path):
    res = _jsc(tmp_path, r"""
var D = marks(12, 8);
drawPairing([D], R);
var tags = calls.fillText.filter(function (s) { return /^Splice|^\d|^0\./.test(s); });
var ticks = gLabelHits.filter(function (h) { return h.t; }).length;
print(JSON.stringify({tags: tags.length, text: calls.fillText, ticks: ticks}));
""")
    # one tag per column, no per-leg labels, every leg still ticked (12 <= 24)
    assert res["tags"] <= 8
    assert not any(s.startswith(("A ", "B ")) for s in res["text"])
    assert any("1/12 FAIL" in s for s in res["text"]) or any(s == "1/12" for s in res["text"])
    assert res["ticks"] == 8 * 12 * 2 - 8          # the filled-in leg is a ring


@needs_jsc
def test_a_whole_cable_ticks_only_the_failing_fibres(tmp_path):
    res = _jsc(tmp_path, r"""
var D = marks(432, 10);
var t0 = Date.now();
for (var k = 0; k < 5; k++) { gLabelHits = []; drawPairing([D], R); }
var ms = (Date.now() - t0) / 5;
var ticks = gLabelHits.filter(function (h) { return h.t; }).length;
print(JSON.stringify({ticks: ticks, ms: ms}));
""")
    assert res["ticks"] == 10 * 2          # fibre 0's two legs, per column
    assert res["ms"] < 250


@needs_jsc
def test_a_picked_fibre_is_drawn_in_full_among_many(tmp_path):
    res = _jsc(tmp_path, r"""
var D = marks(40, 1);
gPickKey = 'b-5';
drawPairing([D], R);
print(JSON.stringify({text: calls.fillText}));
""")
    assert "A 0.175" in res["text"]
    assert any(s.startswith("F5 Splice 1") or s.startswith("F5 ") for s in res["text"])


@needs_jsc
def test_one_direction_set_ticks_and_tags_each_event(tmp_path):
    res = _jsc(tmp_path, r"""
var t = trace('b', 354);
var D = {mode: 'suite', single: true, have: [{fiber: 354, ta: null, tb: t}], went: [],
  goCell: function (fi, w, col) { D.went.push([fi, w, col]); },
  cols: [{i: 2, title: 'Event 3', km: 44.2, cells: [{fi: 0, avg: 0.058, km: 44.2, avgFail: false, avgWarn: false,
          legs: {a: null, b: {km: 10.79, loss: 0.058, hollow: false, fail: false, warn: false}}}]}]};
var P = marks(1, 1);
var cov = drawPairing([P, D], R);
gLabelHits.filter(function (h) { return !h.t; }).forEach(function (h) { h.go(); });
print(JSON.stringify({covered: cov.size, text: calls.fillText, went: D.went}));
""")
    assert res["covered"] == 3                      # the pair's two traces and the one-way one
    assert "Event 3  loss 0.058" in res["text"]
    assert res["text"].count("B 0.058") == 1          # the pair's; the one-way number is on its tag
    assert res["went"] == [[0, "b", 2]]


@needs_jsc
def test_the_reports_one_fibre_page_is_drawn_in_full(tmp_path):
    # The Report draws a page per fibre by hiding every other trace; that
    # page gets the fibre's own marks, not the cable's summary.
    res = _jsc(tmp_path, r"""
var D = marks(40, 1);
D.have.forEach(function (p, fi) { p.ta.visible = p.tb.visible = fi === 0; });
var cov = drawPairing([D], R);
print(JSON.stringify({text: calls.fillText}));
""")
    assert "A 0.175" in res["text"] and "B 0.058" in res["text"]
    assert "Splice 1  avg 0.116  FAIL" in res["text"]
    assert not any("/1" in s for s in res["text"])


# ─── value labels never run together (Robert 2026-10-01) ────────────────

_BOXES = r"""
var st = {align: 'left', base: 'top'};
var boxes = [];
ctx = {
  set font(v) {}, set fillStyle(v) {}, set strokeStyle(v) {}, set lineWidth(v) {},
  set globalAlpha(v) {},
  set textAlign(v) { st.align = v; }, set textBaseline(v) { st.base = v; },
  measureText: function (s) { return {width: s.length * 7}; },
  fillText: function (s, x, y) {
    var w = s.length * 7, h = st.base === 'middle' ? 18 : 13;
    var x0 = st.align === 'right' ? x - w : st.align === 'center' ? x - w / 2 : x;
    var y0 = st.base === 'bottom' ? y - h : st.base === 'middle' ? y - h / 2 : y;
    boxes.push({s: s, x0: x0, x1: x0 + w, y0: y0, y1: y0 + h});
  },
  fillRect: function () {}, beginPath: function () {}, moveTo: function () {},
  lineTo: function () {}, stroke: function () {}, arc: function () {},
};
function overlaps() {
  var vals = boxes.filter(function (b) { return /^[AB] -?\d\.\d{3}$/.test(b.s); });
  var bad = [];
  for (var i = 0; i < vals.length; i++) {
    for (var j = 0; j < boxes.length; j++) {
      var a = vals[i], b = boxes[j];
      if (a === b) continue;
      if (a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1) bad.push(a.s + ' x ' + b.s);
    }
  }
  return {values: vals.length, bad: bad};
}
"""


@needs_jsc
def test_crowded_values_never_overlap_each_other_or_a_tag(tmp_path):
    """Three identical fibres at one splice: six value labels want the same
    spot.  Each takes the first clear one of its eight; any with none is
    left off; none touches another or a tag."""
    res = _jsc(tmp_path, _BOXES + r"""
var D = marks(3, 1);
drawPairing([D], R);
var o = overlaps();
var ticks = gLabelHits.filter(function (h) { return h.go; }).length;
print(JSON.stringify({values: o.values, bad: o.bad, ticks: ticks}));
""")
    assert res["bad"] == [], res["bad"]
    assert 2 <= res["values"] <= 6
    assert res["ticks"] >= 6                     # every leg keeps its tick and click


@needs_jsc
def test_a_short_plot_drops_values_rather_than_overlap(tmp_path):
    res = _jsc(tmp_path, _BOXES + r"""
var D = marks(3, 8);
var Rs = {x: 56, y: 12, w: 300, h: 60};
drawPairing([D], Rs);
var o = overlaps();
var inside = boxes.filter(function (b) { return /^[AB] /.test(b.s); }).every(function (b) {
  return b.x0 >= Rs.x && b.x1 <= Rs.x + Rs.w && b.y0 >= Rs.y && b.y1 <= Rs.y + Rs.h; });
print(JSON.stringify({bad: o.bad, inside: inside}));
""")
    assert res["bad"] == [], res["bad"]
    assert res["inside"]


@needs_jsc
def test_one_fibre_keeps_its_values_where_they_were(tmp_path):
    """Room to spare: A sits above and right of its tick, B below and right,
    exactly as before the collision rule."""
    res = _jsc(tmp_path, _BOXES + r"""
var D = marks(1, 1);
drawPairing([D], R);
var a = boxes.filter(function (b) { return b.s === 'A 0.175'; })[0];
var b = boxes.filter(function (b) { return b.s === 'B 0.058'; })[0];
var ax = xToPx(5, R), bx = xToPx(55 - (55 - 5 - 0.115), R);
print(JSON.stringify({aRight: a.x0 > ax, aAbove: a.y1 <= yToPx(-1.0, R),   // the stub trace is at -1.0 dB at 5 km
                      bRight: b.x0 > bx}));
""")
    assert res == {"aRight": True, "aAbove": True, "bRight": True}
