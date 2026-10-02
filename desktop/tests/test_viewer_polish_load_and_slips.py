"""Viewer polish for a 432-fiber span shown to customers (audit #35, #39).
Display only: no number, event or flag moves.

#35  A whole-span load (864 traces) looked idle: a blank status line, "add a
     trace to see events" under the chart while traces streamed in, and a
     Fibers box and Dir buttons that followed the chunks ("1-144", "1-288",
     A→) before settling.  In FastReporter mode the table step after it took
     40 s or more with nothing said.  Now the line reads "Loading 312 of 864
     traces…", then "Building the table…" until the table paints; the hint
     says nothing meanwhile; the box and the buttons show what was asked for
     from the start.
#39  1. The A/B marker box sat on marker B's flag (top right of the chart).
        Main's window-size audit moves it (placeMarkerReadout), tested there.
     2. A report link (?fiber=354&km=44.099) zoomed the chart but left the
        table on its first column.  The table goes to the column nearest the
        km and flashes its header.
     3. The dB title ran off a short chart ("signal (dB, descending = los").
        Main's window-size audit draws the longest wording that fits.
     4. Empty cells: "-" in the one-direction table, "---" in the A+B ones.
        "---" everywhere, FastReporter's own mark.
     5. The browser tab read "Streamlit" while the hub reran
        (test_hub_title_and_fiber_count).
     6. "A: 1 fibers" (the Viewer's counts here, the hub's sidebar in
        test_hub_title_and_fiber_count).
"""
from __future__ import annotations

import json
import re
import subprocess

import pytest

from conftest import VIEWER_DIR
from test_viewer_overview_failures import (  # noqa: F401 (real_trace is a fixture)
    JSC, _SHIMS, _scenario, _viewer_script, needs_jsc, real_trace)

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def _js_func(src, name):
    """`function name(...) {...}`, whole, brace-matched."""
    m = re.search(r'(?:async\s+)?function\s+' + re.escape(name) + r'\s*\([^)]*\)\s*\{', src)
    assert m, 'viewer.html no longer defines %s()' % name
    i, depth = m.end(), 1
    while i < len(src) and depth:
        depth += {'{': 1, '}': -1}.get(src[i], 0)
        i += 1
    return src[m.start():i]


def _jsc(prog, tmp_path, name='prog.js'):
    path = tmp_path / name
    path.write_text(prog, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=180)
    out = r.stdout + r.stderr
    assert 'THREW' not in out and r.returncode == 0, out[-3000:]
    line = [ln for ln in out.splitlines() if ln.startswith('OUT ')][-1]
    return json.loads(line[4:])


# ─── #35: the load, replayed in the real script ─────────────────────────────

# Every time the status line is redrawn, and every chunk drawn: the line, the
# Fibers box, the Dir choice and the hint under the chart.
_LOAD_DRIVER = r"""
(async function () {
  var out = {};
  var box = document.getElementById('fiber-input');
  var snaps = [];
  var snap = function () {
    snaps.push([String(document.getElementById('readout').textContent), String(box.value),
                gAddDir, String(document.getElementById('event-hint').textContent)]);
  };
  try {
    var rr = refreshReadout;
    refreshReadout = function () { rr(); snap(); };
    var dr = draw;
    draw = function () { dr(); snap(); };
    await __hSleep(150);                              // boot's /api/list
    document.getElementById('event-hint').textContent = 'add a trace to see events';
    if (__hScenario.files) {
      await applyFileSelection(new Set(__hScenario.files));
    } else if (__hScenario.jump) {
      await applyTarget(__hScenario.jump);
    } else {
      box.value = __hScenario.fibers; gAddDir = __hScenario.dir;
      await addFibers();
    }
    snap();
    // from the load's first word on (boot, and a jump's Clear All, come first)
    var i0 = snaps.findIndex(function (x) { return x[0].indexOf('Loading ') === 0; });
    out.during = i0 < 0 ? snaps.slice() : snaps.slice(i0);
    out.justAfter = String(document.getElementById('readout').textContent);
    await __hSleep(120);                              // the table's server step answers
    out.settled = [String(document.getElementById('readout').textContent), box.value, gAddDir];
    out.traces = gTraces.length;
  } catch (e) { print('THREW ' + e + '\n' + (e && e.stack)); }
  print('OUT ' + JSON.stringify(out));
  quit();
})();
"""


def _load(tmp_path, sc):
    prog = ('var __hScenario = %s;\n' % json.dumps(sc) + _SHIMS + '\n'
            + _viewer_script(SRC) + '\n' + _LOAD_DRIVER)
    return _jsc(prog, tmp_path, 'load.js')


@needs_jsc
def test_a_whole_span_both_ways_counts_up_and_holds_the_box(real_trace, tmp_path):
    """150 fibers both ways, the overview's chunks of 144: the box read
    "1-144" and Dir A→ after the first chunk."""
    sc = _scenario(real_trace, 150, dirs=('a', 'b'), fibers='1-150', dir='both')
    out = _load(tmp_path, sc)
    assert out['traces'] == 300
    loading = [s for s in out['during'] if s[0].startswith('Loading ')]
    assert loading, out['during']
    counts = [re.match(r'Loading (\d+) of 300 traces…', s[0]) for s in loading]
    assert all(counts), loading
    done = [int(m.group(1)) for m in counts]
    # A's chunks (144, then 6), then B's: one direction per request
    assert done[0] == 0 and done == sorted(done)
    assert {0, 144, 150, 294} <= set(done)
    # the box and the buttons say where the load is going, all the way
    for line, value, d, hint in out['during']:
        assert value == '1-150', out['during']
        assert d == 'both', out['during']
        assert 'add a trace' not in hint
    # the table step is said, then cleared when the table answers
    assert out['justAfter'].startswith('Building the table…')
    assert 'Loading' not in out['settled'][0] and 'Building' not in out['settled'][0]
    assert out['settled'][1:] == ['1-150', 'both']


@needs_jsc
def test_a_files_panel_selection_counts_up_too(real_trace, tmp_path):
    """A drop and a FILES selection load through applyFileSelection."""
    sc = _scenario(real_trace, 150, dirs=('a', 'b'),
                   files=['%s-%d' % (d, f) for d in 'ab' for f in range(1, 151)])
    out = _load(tmp_path, sc)
    assert out['traces'] == 300
    assert any(re.match(r'Loading 144 of 300 traces…', s[0]) for s in out['during'])
    assert {s[1] for s in out['during'] if s[0].startswith('Loading')} == {'1-150'}
    assert {s[2] for s in out['during'] if s[0].startswith('Loading')} == {'both'}
    assert 'Loading' not in out['settled'][0] and 'Building' not in out['settled'][0]


@needs_jsc
def test_a_detail_load_counts_by_batch(real_trace, tmp_path):
    sc = _scenario(real_trace, 10, dirs=('a', 'b'), fibers='1-10', dir='both')
    good = {'direction': 'A', 'fiber': 0, **real_trace}
    for d in 'ab':
        sc['single'][d] = {str(f): [200, dict(good, fiber=f)] for f in range(1, 11)}
    out = _load(tmp_path, sc)
    assert out['traces'] == 20
    lines = [s[0] for s in out['during'] if s[0].startswith('Loading')]
    assert lines[0] == 'Loading 0 of 20 traces…' and 'Loading 18 of 20 traces…' in lines
    assert {s[1] for s in out['during']} <= {'1-10'}
    assert 'Loading' not in out['settled'][0] and 'Building' not in out['settled'][0]


@needs_jsc
def test_a_report_link_holds_its_fiber_and_direction(real_trace, tmp_path):
    sc = _scenario(real_trace, 3, dirs=('a', 'b'),
                   jump={'fiber': '2', 'km': '1.0', 'dir': 'both', 'replace': True})
    good = {'direction': 'A', 'fiber': 0, **real_trace}
    for d in 'ab':
        sc['single'][d] = {str(f): [200, dict(good, fiber=f)] for f in range(1, 4)}
    out = _load(tmp_path, sc)
    assert out['traces'] == 2
    assert {(s[1], s[2]) for s in out['during']} == {('2', 'both')}
    assert any(s[0] == 'Loading 0 of 2 traces…' for s in out['during'])


def test_the_progress_leads_the_line_and_is_not_a_plain_note():
    fn = _js_func(SRC, 'setReadout')
    assert "const parts = [[progressNote(), 'ro-prog']];" in fn
    assert "gReadoutLast = [s, extra];" in fn
    note = _js_func(SRC, 'progressNote')
    assert "`Loading ${done} of ${total} trace${total === 1 ? '' : 's'}…`" in note
    assert "gTableBusy ? 'Building the table…' : ''" in note
    # every load path counts itself in and out
    for name in ('addFibers', 'applyFileSelection'):
        body = _js_func(SRC, name)
        assert 'beginLoad(tasks)' in body and 'endLoad(ld)' in body, name
        assert 'await buildTable();' in body and 'renderEventTable();' not in body, name
    assert 'if (ld) loadStep(ld, slice.length);' in _js_func(SRC, 'loadOverview')
    # both server-built tables hold the line until they paint
    assert 'gTableBusy = true;' in _js_func(SRC, 'renderFrBidiGrid')
    assert 'tableSettled();' in _js_func(SRC, 'renderFrBidiGrid')
    assert 'gTableBusy = true;' in _js_func(SRC, 'renderSuiteBidiGrid')
    assert _js_func(SRC, 'renderSuiteBidiGrid').count('tableSettled();') == 2


def test_no_add_a_trace_hint_while_loading():
    # main's wording (the menu stress fixes): the panel says it is loading
    body = _js_func(SRC, 'renderEventTable')
    assert "gLoadingKeys.size ? 'loading traces…' : 'add a trace to see events'" in body
    assert "hint.textContent = 'loading traces…';" in _js_func(SRC, 'beginLoad')


def test_the_box_counts_what_is_on_its_way():
    fn = _js_func(SRC, 'syncFiberBox')
    assert 'for (const ld of gLoadsInFlight) {' in fn
    assert 'box.value = fiberRangeText(fibers);' in fn
    # a new load epoch (Clear All, a new FILES selection) drops the old asks
    assert 'gLoadsInFlight.clear();' in _js_func(SRC, 'newLoadEpoch')


# ─── #39.2: a report link takes the table to its column ─────────────────────

_GO_STUBS = r"""
var timers = [];
function setTimeout(fn) { timers.push(fn); }
function cls() {
  var s = new Set();
  return { add: function () { for (var a of arguments) s.add(a); },
           remove: function () { for (var a of arguments) s.delete(a); },
           has: function (c) { return s.has(c); }, list: function () { return [...s]; } };
}
var scroller = { scrollLeft: 0, getBoundingClientRect: function () { return { left: 0, right: 600 }; } };
var lab = { getBoundingClientRect: function () { return { right: 200 }; } };
var thead = { querySelector: function () { return lab; } };
var table = { parentElement: scroller };
function head(km, x0) {
  return { dataset: { km: String(km) }, classList: cls(),
           closest: function (q) { return q === 'table' ? table : thead; },
           getBoundingClientRect: function () {
             return { left: x0 - scroller.scrollLeft, right: x0 + 100 - scroller.scrollLeft }; } };
}
var heads = [head(0, 200), head(21.8758, 300), head(29.2695, 400), head(36.8545, 500),
             head(44.0988, 900), head(50.2917, 1000)];
var host = { querySelectorAll: function () { return heads; } };
var document = { getElementById: function () { return host; } };
var gTraces = [{ fiber: 354, visible: true }], gGridGoTo = null, gTableGoKm = null;
"""


@needs_jsc
def test_the_link_scrolls_to_the_nearest_column_and_flashes_it(tmp_path):
    prog = (_GO_STUBS + 'const TABLE_GO_TOL_KM = 0.5;\n' + _js_func(SRC, 'tableGoKm') + r"""
var out = {};
gTableGoKm = { km: 44.099, fiber: 354 };
tableGoKm();
out.scroll = scroller.scrollLeft;
out.hit = heads.filter(h => h.classList.has('fr-hit')).map(h => h.dataset.km);
out.spent = gTableGoKm;
timers.forEach(f => f());
out.after = heads.filter(h => h.classList.has('fr-hit')).length;
// a column already in view is flashed, not scrolled to
scroller.scrollLeft = 0;
gTableGoKm = { km: 29.27, fiber: 354 };
tableGoKm();
out.inView = [scroller.scrollLeft, heads[2].classList.has('fr-hit')];
// no column within half a km: nothing moves
gTableGoKm = { km: 47.5, fiber: 354 };
tableGoKm();
out.far = [scroller.scrollLeft, gTableGoKm];
print('OUT ' + JSON.stringify(out));
""")
    out = _jsc(prog, tmp_path)
    # centered between the sticky labels (right edge 200) and the window's 600
    assert out['scroll'] == 900 + 50 - 400
    assert out['hit'] == ['44.0988'] and out['spent'] is None
    assert out['after'] == 0
    assert out['inView'] == [0, True]
    assert out['far'] == [0, None]


def test_the_link_asks_for_the_column_after_its_zoom():
    fn = _js_func(SRC, 'applyTarget')
    z = fn.index("zoomToKm(linkDispKm(km, t.src, t.dir || 'both', fiber), gFecMode ? 0.3 : 2.5);")
    assert z < fn.index("gTableGoKm = { km: linkDispKm(km, t.src, t.dir || 'both', fiber), fiber: +fiber };")
    assert z < fn.index('if (!gTableBusy) tableGoKm();')
    assert 'tableGoKm();' in _js_func(SRC, 'tableSettled')
    assert 'queueMicrotask(() => tableStepEnd(wasBusy));' in _js_func(SRC, 'renderEventTable')
    assert 'if (!gTableBusy) tableGoKm();' in _js_func(SRC, 'tableStepEnd')
    assert 'table.fr-table th.fr-evhdr.fr-hit {' in SRC


# ─── #39.4: one mark for an empty cell ──────────────────────────────────────

def test_every_table_prints_an_empty_cell_as_three_dashes():
    one = _js_func(SRC, 'renderFastReporterGrid')
    assert "? '---' : v.toFixed(3)" in one and "? '---' : v.toFixed(4)" in one
    assert "v === 0) ? '---' : v.toFixed(1)" in one
    assert "'-'" not in one
    for name in ('renderFrBidiGrid', 'paintFrBidiGrid', 'paintSuiteBidiGrid',
                 'renderFlatEventList', 'updateMarkerReadout'):
        assert not re.search(r"\? '-' :|\|\| '-'|= '-'", _js_func(SRC, name)), name
    assert not re.search(r"\? '-' :|\|\| '-'\)|let \w+ = '-'", SRC)


# ─── #39.6: one fiber (the hub's own count: test_hub_title_and_fiber_count) ──

def test_the_viewer_counts_say_one_fiber():
    info = _js_func(SRC, 'loadInfo')
    assert "(${nA} fiber${nA === 1 ? '' : 's'})" in info
    assert "(${nB} fiber${nB === 1 ? '' : 's'})" in info
    assert "of ${rows.length} fiber${rows.length === 1 ? '' : 's'} selected" in SRC


@needs_jsc
def test_a_clear_all_mid_load_leaves_no_ask_in_the_box(tmp_path):
    """With main's load epochs: a Clear All while a load is on its way stops
    that load, and the box must not keep showing what it had asked for."""
    prog = ('var gTraces = [], gLoadingKeys = new Set(), gLoadsInFlight = new Set(), gLoadEpoch = 0;\n'
            'var gAddDir = "both", box = { value: "" };\n'
            'var document = { activeElement: null, getElementById: function () { return box; } };\n'
            'function setAddDir(d) { gAddDir = d; }\n'
            + _js_func(SRC, 'fiberRangeText') + '\n' + _js_func(SRC, 'syncFiberBox') + '\n'
            + _js_func(SRC, 'newLoadEpoch') + r"""
var out = {};
// a whole span both ways asked for; A's first chunk has landed
gLoadsInFlight.add({ fibers: new Set([1, 2, 3, 4, 5]), dirs: new Set(['a', 'b']), done: 3, total: 10 });
['a-4', 'a-5', 'b-1', 'b-2', 'b-3', 'b-4', 'b-5'].forEach(function (k) { gLoadingKeys.add(k); });
gTraces = [{ fiber: 1, dir: 'a' }, { fiber: 2, dir: 'a' }, { fiber: 3, dir: 'a' }];
syncFiberBox();
out.mid = [box.value, gAddDir];
newLoadEpoch(); gTraces = [];             // Clear All
syncFiberBox();
out.cleared = [box.value, gAddDir, gLoadsInFlight.size];
print('OUT ' + JSON.stringify(out));
""")
    out = _jsc(prog, tmp_path, 'clear_mid.js')
    assert out['mid'] == ['1-5', 'both']
    assert out['cleared'] == ['', 'both', 0]
