"""A click on an event-table row brings its trace to the front.

FastReporter's event table has a current measurement (driven 2026-09-30):
click any cell of a row and that row's trace is drawn dark with the rest
light, the row marked; clicking a table cell never changes the file
selection.  Robert: "build as you suggest".

The Viewer already did this for a Minimum or Maximum cell (the "pick").  A
click on any row now picks too, in all three tables:

  one-direction table     that row's trace
  two-direction tables    an A->B or B->A row: that direction's trace;
                          the Average row: both of its fibre's traces
  the same row again      lets go, as a Minimum/Maximum cell does

A pick whose trace leaves the chart (a new FILES selection, a remove) is
dropped, or every trace left would stay faint behind nothing.

The pick rules are plain functions (isPicked, samePick, pickRow,
prunePick), run in JavaScriptCore where present; the wiring is checked
everywhere.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

from conftest import VIEWER_DIR

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


_CASES = r"""
var gPickKey = null, gTraces = [], draws = 0;
function draw() { draws++; }
var out = {};
pickRow(['a-7']);               out.one = gPickKey;
out.one_is = [isPicked('a-7'), isPicked('b-7')];
pickRow(['a-7']);               out.again = gPickKey;             // lets go
pickRow(['a-7', 'b-7']);        out.pair = gPickKey;
out.pair_is = [isPicked('a-7'), isPicked('b-7'), isPicked('a-8')];
out.same_pair = [samePick(['b-7', 'a-7']), samePick(['a-7']), samePick(['a-7', 'b-7', 'a-8'])];
pickRow(['b-7', 'a-7']);        out.pair_again = gPickKey;        // the same pair lets go
pickRow(['a-7', 'b-7']);
pickRow(['b-7']);               out.to_one = gPickKey;            // another row moves the pick
pickRow([null, undefined]);     out.nothing = gPickKey;           // no keys: unchanged
out.draws = draws;
// prune: only what is still loaded stays picked
gTraces = [{ key: 'a-7' }, { key: 'a-9' }];
gPickKey = ['a-7', 'b-7'];      prunePick(); out.prune_pair = gPickKey;
gPickKey = 'b-7';               prunePick(); out.prune_gone = gPickKey;
gPickKey = 'a-9';               prunePick(); out.prune_kept = gPickKey;
print('OUT ' + JSON.stringify(out));
"""


@pytest.fixture(scope='module')
def rules(tmp_path_factory):
    funcs = '\n'.join(_js_func(n) for n in ('isPicked', 'samePick', 'pickRow', 'prunePick'))
    path = tmp_path_factory.mktemp('pick_row') / 'rules.js'
    path.write_text(funcs + '\n' + _CASES, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and 'OUT ' in out, out[-2000:]
    return json.loads(out.split('OUT ', 1)[1].strip())


@needs_jsc
def test_a_row_picks_its_trace_and_the_same_row_lets_go(rules):
    assert rules['one'] == 'a-7'
    assert rules['one_is'] == [True, False]
    assert rules['again'] is None
    assert rules['to_one'] == 'b-7'
    assert rules['nothing'] == 'b-7'
    assert rules['draws'] == 6                  # every pick redraws the chart


@needs_jsc
def test_an_average_row_picks_both_directions(rules):
    assert rules['pair'] == ['a-7', 'b-7']
    assert rules['pair_is'] == [True, True, False]
    assert rules['same_pair'] == [True, False, False]
    assert rules['pair_again'] is None


@needs_jsc
def test_a_pick_goes_with_its_trace(rules):
    assert rules['prune_pair'] == 'a-7'         # the direction still loaded stays
    assert rules['prune_gone'] is None
    assert rules['prune_kept'] == 'a-9'


def test_every_table_picks_a_row_on_click():
    single = SRC.split("function renderFastReporterGrid(", 1)[1].split("\n// ─── FastReporter mode", 1)[0]
    click = single.split("tb.addEventListener('click', (ev) => {", 1)[1].split('\n  });', 1)[0]
    assert "pickRow([`${tr.dataset.src}-${tr.dataset.fiber}`]);" in click
    assert 'paintRows();' in click
    for name in ('paintFrBidiGrid', 'paintSuiteBidiGrid'):
        body = SRC.split('function %s(' % name, 1)[1].split('\nfunction ', 1)[0]
        click = body.split("tb.addEventListener('click', (ev) => {", 1)[1].split('\n  });', 1)[0]
        assert "pickRow(tr.dataset.avg ? [p.ta.key, p.tb.key] : [`${tr.dataset.src}-${tr.dataset.fiber}`]);" in click, name
        # the picked row is marked, the Average row by its fibre's two keys
        assert "const pk = which === 'avg' ? samePick([p.ta.key, p.tb.key]) : gPickKey === t.key;" in body, name
        assert 'data-avg="1" data-fiber="${p.fiber}"' in body, name
        # the right-click span menu is still there, and still names a direction
        assert "tb.addEventListener('contextmenu', (ev) => wireCellMenu(ev, tb));" in body, name


def test_a_pick_is_dropped_with_its_trace_when_a_load_settles():
    sync = SRC.split('function syncFileMarks() {', 1)[1].split('\n}', 1)[0]
    assert sync.index('prunePick();') < sync.index('if (gSelNext) return;')


def test_a_double_click_or_text_selection_does_not_pick():
    """The second click of a double-click would let go straight away, and a
    click that ends selecting text to copy is not a pick."""
    for name in ('renderFastReporterGrid', 'paintFrBidiGrid', 'paintSuiteBidiGrid'):
        body = SRC.split('function %s(' % name, 1)[1]
        click = body.split("tb.addEventListener('click', (ev) => {", 1)[1].split('\n  });', 1)[0]
        assert "if (ev.detail > 1 || String(window.getSelection() || '').trim()) return;" in click, name
