"""The Viewer holds more than one span, and copies of a side (Robert
2026-10-02).

  * A different span dropped in while both sides are loaded is ADDED (a span
    of its own, 'a2' / 'b2').
  * Spans are drawn together, each in its own frame: a B is mirrored only to
    meet an A of its own span.
  * The event panel with traces of more than one span shows one plain line
    and no table at all ("we should have an error that says we are trying
    to display multiple spans and show nothing. we don't want to attempt").
  * Every trace shows its events and losses: a copy row ("12 (copy 2)",
    id 100012) pairs with the other side's same copy, a file set to the
    other direction than its folder's stands alone in a one-direction table.
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


def _js_func(name):
    m = re.search(r'(?:async\s+)?function\s+' + re.escape(name) + r'\s*\([^)]*\)\s*\{', SRC)
    assert m, 'viewer.html no longer defines %s()' % name
    i, depth = m.end(), 1
    while i < len(SRC) and depth:
        depth += {'{': 1, '}': -1}.get(SRC[i], 0)
        i += 1
    return SRC[m.start():i]


def _const(name):
    m = re.search(r'^const ' + re.escape(name) + r' = [^\n]+;', SRC, re.M)
    assert m, name
    return m.group(0)


_STUBS = r"""
var gInfo = { srcs: ['a', 'b', 'a2', 'b2'],
              spans: [{ span: 2, name_a: 'ROMTUC', name_b: 'TUCROM', launch_a_km: 0.5,
                        span_decl: { a: null, b: null } }] };
var gTraces = [], gFecMode = false, gStacked = true, gHaveA = false, gLaunchA = 1.0;
var gStackSpacingDb = 3, gMirrorDelta = 0, gMirrorNote = '', gLoadingKeys = new Set();
var gSpanDecl = { a: null, b: null }, gTableBusy = false, gHeldNodes = [], gTableLive = null;
var gGridGoTo = null, gTableExport = null, gDrawerMarks = [], gTablePass = 0;
var MIRROR_MAX_FIBERS = 40, MIRROR_SNAP_KM = 0.01;
var host = { innerHTML: 'old table', childNodes: [], classList: { remove: function () {} } };
var hint = { textContent: 'old hint', style: {} };
var other = { style: {}, closest: function () { return other; } };
var document = { getElementById: function (id) {
  return id === 'event-tbody-wrap' ? host : id === 'event-hint' ? hint : other; } };
var built = [];
function queueMicrotask() {} function tableStepEnd() {} function suiteTableReset() {}
function syncGateUI() {} function renderFecGrid() { built.push('fec'); return true; }
function fecTable() { return false; } function fecCombinedFor() { return null; }
function renderSuiteBidiGrid() { built.push('suite'); return true; }
function renderFrBidiGrid() { built.push('fr'); return true; }
function renderFastReporterGrid() { built.push('onedir'); }
function holdOldTable() {}
function halfSpanWarning() { return ''; }
function measuredOriginKm() { return null; }
function eofKm(t) { return 10; }
var gAnalysisMode = 'suite';
function T(src, fiber, dir) {
  return { key: src + '-' + fiber, src: src, fiber: fiber, dir: dir || src[0],
           span: src.length > 1 ? spanOf(src) : 1, visible: true,
           data: { far_conn_km: 9, events: [] } };
}
"""

_CASES = r"""
var out = {};
// two spans on the chart: one line, no table, nothing built
gTraces = [T('a', 1), T('b', 1), T('a2', 1)];
renderEventTable();
out.multi = { host: host.innerHTML, hint: hint.textContent, built: built.slice() };
// one span again: the table is built as ever
built = []; host.innerHTML = 'x';
gTraces = [T('a2', 1), T('b2', 1)];
renderEventTable();
out.one = { built: built.slice(), multi: host.innerHTML.indexOf('more than one span') >= 0 };
// a copy row pairs with the other side's same copy; a B-folder file set to
// A beside A's own file of that fiber stands alone
var p = pairTraces([T('a', 241), T('a', 100241), T('b', 241), T('b', 100241), T('b', 5, 'a'), T('a', 5)]);
out.pairs = p.pairs.map(function (x) { return [x.fiber, x.ta.src, x.tb.src, x.tkey]; });
out.singles = p.singles.map(function (t) { return t.key; });
out.q_span1 = frTableQuery(p.pairs);
// a file set to its right direction meets its partner by the FIBRE: fibre
// 3's B file loaded on A as "3 (copy 2)" and set back to B->A (drop audit
// S05), and the A file loaded on B and set back to A->B (S08)
gCopies = { '100003': [3, 2], '100241': [241, 2] };
var p3 = pairTraces([T('a', 1), T('b', 1), T('a', 3), T('a', 100003, 'b')]);
out.fix_s05 = p3.pairs.map(function (x) { return [x.fiber, x.ta.key, x.tb.key, x.tkey]; });
out.fix_s05_singles = p3.singles.length;
out.fix_s05_q = frTableQuery(p3.pairs);
var p4 = pairTraces([T('b', 3, 'a'), T('b', 100003)]);
out.fix_s08 = p4.pairs.map(function (x) { return [x.fiber, x.ta.key, x.tb.key, x.tkey]; });
// copies on both sides still pair copy with copy
var p5 = pairTraces([T('a', 241), T('a', 100241), T('b', 241), T('b', 100241)]);
out.copies = p5.pairs.map(function (x) { return [x.ta.key, x.tb.key, x.tkey]; });
gCopies = {};
// FastReporter pairs on the files' own Cable ID and Fiber ID (S11-S13)
function TI(src, fiber, cable, fid, dir) {
  var t = T(src, fiber, dir); t.data.cable_id = cable; t.data.fiber_id = fid; return t;
}
var q1 = pairTraces([TI('a', 3, '', '0003'), TI('b', 3, '', '0033')]);           // S11
var q2 = pairTraces([TI('a', 3, '', '0003'), TI('b', 3, 'OTHER', '0003')]);      // S12
var q3 = pairTraces([TI('a', 242, '', 'AAABBB0242'), TI('b', 242, '', 'BBBAAA0242')]);  // S13
var q4 = pairTraces([TI('a', 1, '', '0001'), TI('b', 1, '', '0001')]);           // S01
var q5 = pairTraces([T('a', 1), TI('b', 1, '', '0001')]);                        // no IDs to read
out.ids = [q1, q2, q3, q4, q5].map(function (q) { return [q.pairs.length, q.singles.length]; });
var p2 = pairTraces([T('a2', 17), T('b2', 17)]);
out.pairs2 = p2.pairs.map(function (x) { return x.tkey; });
out.q_pairs = frTableQuery(p2.pairs);
// names
out.labels = ['a', 'b', 'a2', 'b31'].map(srcLabel);
out.tags = ['a', 'a2', 'b31'].map(srcTag);
// each span mirrors its B only to meet its own A
gTraces = [T('a', 1), T('b', 1), T('b2', 1)];
refreshMirrorFrame();
out.flip = gTraces.map(function (t) { return [t.key, isFlipped(t)]; });
gTraces = [T('a2', 1), T('b2', 1)];
refreshMirrorFrame();
out.flip2 = gTraces.map(function (t) { return [t.key, isFlipped(t), mirrorOriginKm(t)]; });
print('OUT ' + JSON.stringify(out));
"""


@pytest.fixture(scope='module')
def res(tmp_path_factory):
    funcs = '\n'.join([_const('SRC_RE'), _const('MULTI_SPAN_NOTE')]
                      + [_js_func(n) for n in (
                          'srcParts', 'sideOf', 'spanOf', 'srcLabel', 'srcTag', 'spanInfo',
                          'launchAOf', 'spanDeclOf', 'isFlipped', 'yOffsetFor', 'reelOriginKm',
                          'measuredDeltas', 'measuredDelta', 'refreshMirrorFrame', 'spanIsDeclared',
                          'declaredOriginKm', 'declaredEdgeKm', 'mirrorOriginKm',
                          'renderEventTable', 'realFiber', 'idsMatch', 'pairTraces', 'frTableQuery')])
    funcs += '\nconst gMirrorDeltas = new Map();\nvar gCopies = {};\n'
    path = tmp_path_factory.mktemp('multi_span') / 'ms.js'
    path.write_text(_STUBS + funcs + '\n' + _CASES, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and 'OUT ' in out, out[-2000:]
    return json.loads(out.split('OUT ', 1)[1].strip())


@needs_jsc
def test_two_spans_on_the_chart_show_one_line_and_no_table(res):
    m = res['multi']
    assert m['host'] == ('<div class="tbl-multispan">These traces are from more than one span.'
                         ' Show one span to see its events.</div>')
    assert m['hint'] == ''
    assert m['built'] == []              # no Suite, FR, FEC or one-direction table at all


@needs_jsc
def test_one_span_again_brings_the_table_back(res):
    assert res['one'] == {'built': ['suite'], 'multi': False}


@needs_jsc
def test_a_copy_pairs_with_the_other_sides_same_copy_and_the_rest_stand_alone(res):
    assert res['pairs'] == [[241, 'a', 'b', '241'], [100241, 'a', 'b', '100241']]
    # F5: A's own file and a B-folder file set to A, no B: each its own table
    assert res['singles'] == ['a-5', 'b-5']
    assert res['q_span1'] == 'fibers=241,100241'         # span 1's own, asked as ever
    # another span: each pair asked by its two sources
    assert res['pairs2'] == ['17:a2:b2']
    assert res['q_pairs'] == 'pairs=17:a2:b2'


@needs_jsc
def test_a_file_set_to_its_right_direction_pairs_with_its_partner(res):
    """Robert 2026-10-03, as FastReporter re-pairs on Files > Direction (drop
    audit S05: after the fix FR's table is S01's exactly).  Pairing goes by
    the fibre, so the corrected copy meets the other folder's own file."""
    assert res['fix_s05'] == [[1, 'a-1', 'b-1', '1'], [3, 'a-3', 'a-100003', '3:a:a:100003']]
    assert res['fix_s05_singles'] == 0
    # span 1's own pair spelt out beside the other, so one query holds both
    assert res['fix_s05_q'] == 'pairs=1:a:b,3:a:a:100003'
    assert res['fix_s08'] == [[3, 'b-3', 'b-100003', '3:b:b:100003']]
    assert res['copies'] == [['a-241', 'b-241', '241'], ['a-100241', 'b-100241', '100241']]


@needs_jsc
def test_files_pair_only_when_their_cable_and_fiber_ids_match(res):
    """Robert 2026-10-03, FastReporter's rule: a B file with Fiber ID 0033
    (S11) or Cable ID OTHER (S12) stays unpaired, a span whose A and B files
    carry different Fiber IDs pairs nothing (S13), matching IDs pair (S01),
    and a file with no IDs to read pairs by the fibre as before."""
    assert res['ids'] == [[0, 2], [0, 2], [0, 2], [1, 0], [1, 0]]


@needs_jsc
def test_sources_are_named_by_span(res):
    assert res['labels'] == ['A', 'B', 'S2 A', 'S31 B']
    assert res['tags'] == ['', ' S2', ' S31']


@needs_jsc
def test_a_b_is_mirrored_only_to_meet_an_a_of_its_own_span(res):
    assert res['flip'] == [['a-1', False], ['b-1', True], ['b2-1', False]]
    # span 2 alone: its B meets its own A, about its own launch reel (0.5 km)
    assert res['flip2'] == [['a2-1', False, 9.5], ['b2-1', True, 9.5]]


def test_the_summary_report_refuses_more_than_one_span():
    dlg = _js_func('showReportDialog')
    assert "if (new Set(vis.map(t => t.span || 1)).size > 1) {" in dlg
    assert "setReadout('', { fail: MULTI_SPAN_NOTE });" in dlg


def test_the_files_panel_heads_each_span():
    panel = _js_func('renderFilesPanel')
    assert "`<div class=\"file-span-head\">Span ${n}${label ? ': ' + esc(label) : ''}</div>`" in panel
