"""The number beside an event on the chart is its column's number in the table.

Customer-demo audit, 2026-10-02 (a 24-fiber bidirectional span, OTDR Suite
mode, a small job so the table has Event 1..12 plus A-End / B-End): a click
on the Splice Report's "Event 10" cell for one fiber opened the Viewer with
the table headed Event 10, while the chart labeled that same splice "7" on
the A trace and "4" on the B trace.  Each trace counted its OWN events
(spanEventNumbers): A skipped the columns where it had no event, B counted
from its own end and its own launch connector.  One splice, three numbers.

Now every table records, with each event it printed, the header of the
column it printed it in (tableMark), and the chart prints that column's
number, for A and B alike: the OTDR Suite table's Event n / Splice n, the
FastReporter table's Event n, the one-direction table's Event n, and the
Summary Report fibre page's own Event n.  A column whose name carries no
number (A-End / B-End, End, Connector, the FEC Panel Connector) keeps its
tick with no number, and its name on the hover tip.

Run in JavaScriptCore: the real drawEventMarkers, label pool, table-mark
helpers, the tables' own marking code (cut from each table builder) and the
Summary Report's fibre-table builder.
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


def _fn(name, required=True):
    key = "function " + name + "("
    if key not in SRC:
        assert not required, name
        return ""
    i = SRC.index(key)
    return SRC[i:SRC.index("\n}\n", i) + 3]


def _const(name):
    m = re.search(r"^const " + name + r" = [^\n]*;", SRC, re.M)
    assert m, name
    return m.group(0)


def _decl(name, end):
    """A top-level `const name = ...` running over several lines, as a var."""
    i = SRC.index("const " + name + " = ")
    return "var" + SRC[i + len("const"):SRC.index(end, i) + len(end)]


def _block(fn_name, start, end="draw();"):
    """The code of `fn_name` from `start` up to (not including) `end`."""
    body = _fn(fn_name)
    i = body.index(start)
    return body[i:body.index(end, i)]


# The canvas stub keeps every text with its x; each trace is drawn in a
# frame of its own, so a number sits centred over its own tick.
PRELUDE = r"""
var gView = {x0: -2, x1: 70, y0: -30, y1: 5};
var gPickKey = null, gLabelHits = [], gSpanDecl = null, gNumbersDrawn = new Set();
var gTableKm = null, gReportColumnNames = null;
var texts = [];
var st = {align: 'start'};
var ctx = {
  set font(v) {}, set textAlign(v) { st.align = v; }, set textBaseline(v) {},
  set fillStyle(v) {}, set strokeStyle(v) {}, set lineWidth(v) {}, set globalAlpha(v) {},
  measureText: function (s) { return {width: String(s).length * 7}; },
  fillText: function (s, x, y) { texts.push({s: String(s), x: x, align: st.align}); },
  beginPath: function () {}, moveTo: function () {}, lineTo: function () {}, stroke: function () {},
};
var R = {x: 50, y: 10, w: 1400, h: 600};
function xToPx(x, r) { return r.x + (x - gView.x0) / (gView.x1 - gView.x0) * r.w; }
function yToPx(y, r) { return r.y + r.h - (y - gView.y0) / (gView.y1 - gView.y0) * r.h; }
// B is drawn mirrored into A's frame on a 64 km span
function dispKm(t, km) { return t.dir === 'b' ? 64 - km : km; }
function dispDb(t, v) { return v; }
function isPicked(k) { return k === gPickKey; }
var LIGHT_EDGE = {};
function trace(dir, fiber, kms, endKm) {
  var xs = [], ys = [];
  for (var i = 0; i <= 700; i++) { xs.push(i / 10); ys.push(-i / 50); }
  return {key: dir + '-' + fiber, dir: dir, fiber: fiber, visible: true, color: '#2f6fb3',
          data: {dist_km: xs, trace_db: ys,
                 events: kms.map(function (k) { return {dist_km: k, is_reflective: false,
                                                        is_end: k === endKm}; })}};
}
// Draw one trace alone; each event's printed number (null: none) and its
// hit record's hover text, keyed by the event's own km.
function labels(t) {
  texts = []; gLabelHits = []; gNumbersDrawn = new Set();
  chartLabels(R);
  drawEventMarkers(t, R);
  var out = {};
  t.data.events.forEach(function (e) {
    var px = xToPx(dispKm(t, e.dist_km), R);
    var hit = gLabelHits.filter(function (h) { return h.e === e; })[0];
    var txt = texts.filter(function (x) { return x.align === 'center' && Math.abs(x.x - px) < 0.01; })[0];
    out[e.dist_km] = hit ? {n: txt ? txt.s : null, tip: hit.hiddenText || null} : 'not drawn';
  });
  return out;
}
// Draw A then B in ONE frame, as draw() does (A->B first): each column's
// number prints once.
function together(ta, tb) {
  texts = []; gLabelHits = []; gNumbersDrawn = new Set();
  chartLabels(R);
  drawEventMarkers(ta, R); drawEventMarkers(tb, R);
  var out = {a: {}, b: {}};
  [['a', ta], ['b', tb]].forEach(function (p) {
    p[1].data.events.forEach(function (e) {
      var px = xToPx(dispKm(p[1], e.dist_km), R);
      var hit = gLabelHits.filter(function (h) { return h.e === e; })[0];
      var txt = texts.filter(function (x) { return x.align === 'center' && Math.abs(x.x - px) < 0.01; })[0];
      out[p[0]][e.dist_km] = hit ? {n: txt ? txt.s : null, tip: hit.hiddenText || null} : 'not drawn';
    });
  });
  out.printed = texts.map(function (x) { return x.s; });
  return out;
}
"""

# One fibre of the finding's kind, in each trace's OWN km.  A: launch
# connector at 0, an event of A's alone at 3.2 (no column), splices at 10,
# 20 and 53.774, end at 64.  B, shot from the far end: its launch connector
# at 0, the 53.774 splice at 10.226, an event of B's alone at 30.1, then
# 44 (= 20), 54 (= 10), end at 64.
FIBRE = r"""
var ta = trace('a', 18, [0, 3.2, 10, 20, 53.774, 64], 64);
var tb = trace('b', 18, [0, 10.226, 30.1, 44, 54, 64], 64);
"""


def _run(tmp_path, body):
    code = "\n".join([
        PRELUDE,
        _const("SPAN_SNAP_KM"), _const("EVENT_NUM_H"),
        _fn("lowerBound"), _fn("declaredEdgeKm"), _fn("ownSpanWindow"), _fn("inDeclaredSpan"),
        _fn("spanEventNumbers"), _fn("chartLabels"), _fn("eventNumberSpots"),
        # bidiMarkTip's extract runs on through gBidiNumberKeys and bidiNumberKey
        _fn("tableKmKey"), _fn("tableMarkReset"), _fn("tableMark"), _fn("bidiMarkTip"), _fn("inTable"),
        # the new helpers (absent before the fix: the test then fails on them)
        _fn("tableColumnNumber", False), _fn("tableColumnOf", False),
        _fn("chartColumnTitle", False), _fn("chartColumnNumber", False),
        _fn("drawEventMarkers"),
        FIBRE,
        body,
    ])
    p = tmp_path / "numbers.js"
    p.write_text(code, encoding="utf-8")
    out = subprocess.run([str(JSC), str(p)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    return json.loads(out.stdout.strip().splitlines()[-1])


# The OTDR Suite table's columns for this fibre (/api/suite_table): the two
# ends and Event 1..12, the fibre's readings in Event 4, 7 and 10 only.
SUITE = r"""
function suiteCols() {
  var titles = ['A-End ILA'];
  for (var i = 1; i <= 12; i++) titles.push('Event ' + i);
  titles.push('B-End ILA');
  var at = {'A-End ILA': [0, 64], 'Event 4': [10, 54], 'Event 7': [20, 44], 'Event 10': [53.774, 10.226],
            'B-End ILA': [64, 0]};
  return titles.map(function (title) {
    var r = at[title];
    return {title: title, ev: [r ? {a: {km: r[0]}, b: {km: r[1]}} : null]};
  });
}
var have = [{fiber: 18, ta: ta, tb: tb}];
var cols = suiteCols();
"""


@needs_jsc
def test_suite_table_a_and_b_print_the_columns_number(tmp_path):
    """The finding itself: Event 10's splice reads 10 on A, and B's tick
    there carries 10 on its hover tip (bidiMarkTip: in a two-direction table
    the number prints on the A->B reading, as FastReporter prints it)."""
    res = _run(tmp_path, SUITE + _block("paintSuiteBidiGrid", "tableMarkReset(have.flatMap(")
               + "print(JSON.stringify({a: labels(ta), b: labels(tb)}));")
    a, b = res["a"], res["b"]
    assert a["53.774"]["n"] == "10" and b["10.226"] == {"n": None, "tip": "10"}
    assert a["20"]["n"] == "7" and b["44"] == {"n": None, "tip": "7"}
    assert a["10"]["n"] == "4" and b["54"] == {"n": None, "tip": "4"}


@needs_jsc
def test_suite_end_columns_keep_their_tick_and_name_but_no_number(tmp_path):
    res = _run(tmp_path, SUITE + _block("paintSuiteBidiGrid", "tableMarkReset(have.flatMap(")
               + "print(JSON.stringify({a: labels(ta), b: labels(tb)}));")
    a, b = res["a"], res["b"]
    # A's launch connector is the A-End column, its fibre end the B-End;
    # B's launch connector is the B-End, its own end the A-End
    assert a["0"] == {"n": None, "tip": "A-End ILA"}
    assert a["64"] == {"n": None, "tip": "B-End ILA"}
    assert b["0"] == {"n": None, "tip": "B-End ILA"}
    assert b["64"] == {"n": None, "tip": "A-End ILA"}
    # an in-span event the table did not print is not drawn (as before)
    assert a["3.2"] == "not drawn" and b["30.1"] == "not drawn"


@needs_jsc
def test_splice_n_columns_print_their_number(tmp_path):
    """Past the event-job size the report heads its columns "Splice n"."""
    body = SUITE + "cols = cols.map(function (c) { return {title: c.title.replace('Event', 'Splice'), ev: c.ev}; });\n"
    res = _run(tmp_path, body + _block("paintSuiteBidiGrid", "tableMarkReset(have.flatMap(")
               + "print(JSON.stringify({a: labels(ta), b: labels(tb)}));")
    assert res["a"]["53.774"]["n"] == "10" and res["b"]["10.226"] == {"n": None, "tip": "10"}


@needs_jsc
def test_fastreporter_table_numbers_both_legs_by_its_row(tmp_path):
    """FastReporter's table: Event i + 1, each leg at its own pos_m."""
    body = r"""
var have = [{fiber: 18, ta: ta, tb: tb}], allT = [ta, tb];
var rows = [[0, 64], [10, 54], [20, 44], [33, 31], [53.774, 10.226], [64, 0]];
var cols = rows.map(function (r, i) {
  return {ev: [i === 3 ? null : {row: {a: {pos_m: r[0] * 1000}, b: {pos_m: r[1] * 1000}}, k: i}]};
});
// B's 53.774 leg is one FastReporter filled in itself: it marks nothing
cols[4].ev[0].row.b.synthetic = true;
"""
    res = _run(tmp_path, body + _block("paintFrBidiGrid", "tableMarkReset(allT);")
               + "print(JSON.stringify({a: labels(ta), b: labels(tb)}));")
    a, b = res["a"], res["b"]
    assert [a[k]["n"] for k in ("0", "10", "20", "53.774", "64")] == ["1", "2", "3", "5", "6"]
    # B's legs keep the number for the tip: the A->B reading prints it
    assert [b[k]["n"] for k in ("64", "54", "44", "0")] == [None] * 4
    assert [b[k]["tip"] for k in ("64", "54", "44", "0")] == ["1", "2", "3", "6"]
    assert b["10.226"] == "not drawn"


@needs_jsc
def test_one_direction_table_numbers_and_its_end_column(tmp_path):
    """The one-direction table: Event i + 1 in the full table's numbering,
    a column of fibre ends only headed "End" (no number)."""
    body = r"""
var opts = {};
var traces = [ta];
var E = function (km) { return ta.data.events.filter(function (e) { return e.dist_km === km; })[0]; };
var cols = [{ev: [E(0)]}, {ev: [E(3.2)]}, {ev: [null]}, {ev: [E(10)]}, {ev: [E(20)]},
            {ev: [E(53.774)]}, {ev: [E(64)]}];
"""
    res = _run(tmp_path, body + _block("renderFastReporterGrid",
                                        "const cols = clusterColumns(traces, items, TOL);")
               .replace("const cols = clusterColumns(traces, items, TOL);", "")
               + "print(JSON.stringify(labels(ta)));")
    assert [res[k]["n"] for k in ("0", "3.2", "10", "20", "53.774")] == ["1", "2", "4", "5", "6"]
    assert res["64"] == {"n": None, "tip": "End"}


@needs_jsc
def test_a_one_way_fibre_among_pairs_counts_its_own_and_keeps_the_ab_numbers(tmp_path):
    """A fibre loaded one way among paired ones is left out of the panel
    (Robert 2026-10-02: one event panel, see noteOneDirFibers): no table
    holds its trace, so it counts its own events, and the A+B table's marks
    stay."""
    body = SUITE + _block("paintSuiteBidiGrid", "tableMarkReset(have.flatMap(") + r"""
var tc = trace('a', 19, [0, 12, 64], 64);
""" + "print(JSON.stringify({a: labels(ta), c: labels(tc)}));"
    res = _run(tmp_path, body)
    assert res["a"]["53.774"]["n"] == "10"
    assert res["c"]["12"]["n"] is not None and res["c"]["12"]["tip"] is None


@needs_jsc
def test_fec_columns_have_no_number(tmp_path):
    """Viewer FEC's table numbers nothing: the panel connector and the events
    combined with it keep their ticks, named on the hover tip."""
    res = _run(tmp_path, r"""
tableMarkReset([ta]);
tableMark(ta, 10, 'Panel Connector'); tableMark(ta, 20, 'Combined With');
print(JSON.stringify(labels(ta)));
""")
    assert res["10"] == {"n": None, "tip": "Panel Connector"}
    assert res["20"] == {"n": None, "tip": "Combined With"}
    assert res["53.774"] == "not drawn"
    fec = _fn("paintFecGrid")
    assert "tableMark(t, best.e.dist_km, k === 0 ? 'Panel Connector' : 'Combined With');" in fec


@needs_jsc
def test_no_table_yet_each_trace_counts_its_own_events(tmp_path):
    """Before a table is in (or when none can be built) the chart numbers
    each trace's own events, as it always did."""
    res = _run(tmp_path, "print(JSON.stringify(labels(ta)));")
    assert [res[k]["n"] for k in ("0", "3.2", "10", "20", "53.774", "64")] == \
        ["1", "2", "3", "4", "5", "6"]


@needs_jsc
def test_every_number_is_still_a_click_target_on_its_own_event(tmp_path):
    """A click on a number finds the cell by the event's own km
    (gGridGoTo(hit.t, hit.e)); the column number changes only the text."""
    res = _run(tmp_path, SUITE + _block("paintSuiteBidiGrid", "tableMarkReset(have.flatMap(") + r"""
labels(tb);
print(JSON.stringify(gLabelHits.map(function (h) { return [h.t === tb, h.e && h.e.dist_km]; })));
""")
    assert sorted(km for own, km in res if own) == [0, 10.226, 44, 54, 64]


# ── Summary Report: a fibre page numbers its own events 1..n ─────────────

def _report_snapshot():
    """The event table as the Report reads it (reportEventTable): three
    header rows, then one fibre's A->B / B->A / Average rows."""
    H = lambda t, **k: dict(t=t, s=0, **k)
    head0 = [H("Identifiers", rs=3), H("P/F", rs=3), H("λ (nm)", rs=3), H("Dir.", rs=3),
             H("A-End ILA (1/1)", cs=2), H("Event 4 (1/1)", cs=2), H("Event 7 (1/1)", cs=2),
             H("Event 10 (1/1)", cs=2), H("Splice 11 (1/1)", cs=2)]
    head1 = [H("0.000 km", cs=2), H("10.000 km", cs=2), H("20.000 km", cs=2),
             H("53.774 km", cs=2), H("58.000 km", cs=2)]
    head2 = [H("Loss\n(dB)"), H("Refl.\n(dB)")] * 5
    vals = lambda: [H("0.100"), H("-50.0")] * 5
    body = [[H("F18", rs=3), H("P", rs=3), H("1550", rs=3), H("A→B"), *vals()],
            [H("B→A"), *vals()],
            [H("Average"), *vals()]]
    return {"head": [head0, head1, head2], "body": body, "lead": 4, "leafRow": 2}


@needs_jsc
def test_the_report_fibre_pages_chart_prints_the_pages_numbers(tmp_path):
    """The fibre page renumbers Event 4, 7, 10 as Event 1, 2, 3 (FastReporter's
    fibre report counts a fibre's events 1..n); its chart now prints 1, 2, 3
    beside them, a named column keeps its own number (Splice 11), and the
    screen's chart goes back to the table's numbers afterwards."""
    ev = json.dumps(_report_snapshot())
    body = SUITE + "cols.push({title: 'Splice 11', ev: [{a: {km: 58}, b: {km: 6}}]});\n" \
        + _block("paintSuiteBidiGrid", "tableMarkReset(have.flatMap(") + r"""
ta.data.events.push({dist_km: 58, is_reflective: false});
tb.data.events.push({dist_km: 6, is_reflective: false});
""" + "\n".join([_decl("REPORT_KINDS", "]];"), _fn("reportKind"),
                 _const("reportBlank").replace("const ", "var "),
                 _fn("reportLayout"), _fn("reportFibreTables")]) + f"""
var tabs = reportFibreTables({ev}, {{H: 0, B: 0, C: 0}});
var tab = tabs.get(18);
var page = tab.table.body.map(function (row) {{ return row[0].t; }});
gReportColumnNames = tab.names || null;
var onPage = {{a: labels(ta), b: labels(tb)}};
gReportColumnNames = null;
var onScreen = labels(ta);
print(JSON.stringify({{page: page, a: onPage.a, b: onPage.b, screen: onScreen}}));
"""
    res = _run(tmp_path, body)
    assert res["page"] == ["A-End ILA", "Event 1", "Event 2", "Event 3", "Splice 11"]
    a, b = res["a"], res["b"]
    assert [a[k]["n"] for k in ("10", "20", "53.774", "58")] == ["1", "2", "3", "11"]
    assert [b[k]["tip"] for k in ("54", "44", "10.226", "6")] == ["1", "2", "3", "11"]
    assert a["0"]["n"] is None
    assert res["screen"]["53.774"]["n"] == "10"


def test_the_report_hands_each_fibre_pages_names_to_its_chart():
    charts = _fn("reportFibreCharts")
    assert "gReportColumnNames = names ? (names(f) || new Map()) : null;" in charts
    # put back for the screen whatever happens on the way
    assert charts.index("} finally {") < charts.index("gReportColumnNames = null;")
    assert "byTable.get(f).names" in SRC
    # the drawer's tags name the column as the page does too
    assert "title: chartColumnTitle(col0.title) || col0.title" in _fn("drawPairing")


def test_the_mapping_is_worked_out_once_per_table_build_not_per_frame():
    # the column's number is parsed when the table marks it ...
    assert "num: tableColumnNumber(title)" in _fn("tableMark")
    # ... and the chart reads it straight off, outside a Summary Report page
    assert "return gReportColumnNames ? tableColumnNumber(chartColumnTitle(col.title)) : col.num;" \
        in _fn("chartColumnNumber")


@needs_jsc
def test_each_column_number_prints_once_on_the_chart(tmp_path):
    """Robert 2026-10-02, FastReporter's way (its fibre 0073 A+B screen): the
    table's Event numbers, each ONCE, not once per trace or per fibre.  A->B
    is drawn first and keeps the number; B->A's tick at the same column keeps
    it for the hover tip only."""
    res = _run(tmp_path, SUITE + _block("paintSuiteBidiGrid", "tableMarkReset(have.flatMap(")
               + "print(JSON.stringify(together(ta, tb)));")
    a, b = res["a"], res["b"]
    # A prints it; B's tick (drawn at the same place) keeps it for the tip
    assert a["53.774"] == {"n": "10", "tip": None} and b["10.226"]["tip"] == "10"
    assert a["20"] == {"n": "7", "tip": None} and b["44"]["tip"] == "7"
    assert a["10"] == {"n": "4", "tip": None} and b["54"]["tip"] == "4"
    assert res["printed"].count("10") == 1 and res["printed"].count("7") == 1


def test_draw_numbers_a_to_b_first_and_starts_each_frame_afresh():
    draw = _fn("draw")
    assert "const aFirst = ts => [...ts.filter(t => t.dir === 'a'), ...ts.filter(t => t.dir !== 'a')];" in draw
    assert "gNumbersDrawn = new Set();" in draw
    marks = _fn("drawEventMarkers")
    assert "if (once && gNumbersDrawn.has(txt)) {" in marks
    assert "if (once) gNumbersDrawn.add(txt);" in marks
