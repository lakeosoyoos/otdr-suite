"""Viewer defects from the 2026-09-29 click-through audit (a 1152-fibre span).

1. A fibre not in the folder (a deep link, an otdr-jump with fiber=999, or
   typed) cleared the chart and read "could not load F999 A: HTTP 404; F999 B:
   HTTP 404".  Now: "F999 is not in the A/B folder", and the plot stays.
2. Dir "A + B" with no B folder set: 1-240 read "could not load F1 B: not
   found; F2 B: ... (+237 more)".  Now the side that exists loads, silently.
3. Dragging the toolbar grip up folded every control away, the fold is
   remembered, and the thin grip was the only way back.  The fold now leaves a
   "Show toolbar" button in its place.  The fold is still remembered: the boss
   asked for that.
4. After Clear All the Fibers box kept "1-240" and the readout kept its last
   words ("showing flagged rows only").  The box also ignored FILES-panel
   clicks.  Clear All empties both; the box follows what is loaded.
5. With 240 traces the marker box ran down over the event table.  It now
   names the picked trace and the first few, then counts the rest.  (The
   audit's other half, the hover line running off the right edge, goes with
   the hover numbers themselves: Robert removed them, see
   viewer/hide-cursor-readout.)

The planning and formatting helpers are mirrored in Python below (these run
everywhere; CI has no jsc).  Where the macOS jsc shell exists, the Viewer's
real functions are run against the same cases, and the real script is booted
to replay the audit's clicks.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

from conftest import VIEWER_DIR
from test_viewer_overview_failures import (  # noqa: F401 (real_trace is a fixture)
    JSC, _SHIMS, _viewer_script, needs_jsc, real_trace)

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


# ─── Python mirrors of the Viewer's helpers ─────────────────────────────────

def plan_fiber_load(fibers, add_dir, info):
    asked = ['a'] if add_dir == 'a' else ['b'] if add_dir == 'b' else ['a', 'b']
    if info is None:
        return {'dirs': asked, 'pairs': [{'f': f, 'd': d} for f in fibers for d in asked],
                'absent': [], 'gaps': [], 'noFolder': False}
    dirs = [d for d in asked if info.get('dir_' + d)]
    if not dirs:
        return {'dirs': asked, 'pairs': [], 'absent': list(fibers), 'gaps': [], 'noFolder': True}
    lists = [set(info.get('fibers_' + d) or []) or None for d in dirs]
    pairs, absent, gaps = [], [], []
    for f in fibers:
        has = [d for d, l in zip(dirs, lists) if l is None or f in l]
        if not has:
            absent.append(f)
            continue
        for d in dirs:
            if d in has:
                pairs.append({'f': f, 'd': d})
            else:
                gaps.append('F%d %s: not in the folder' % (f, d.upper()))
    return {'dirs': dirs, 'pairs': pairs, 'absent': absent, 'gaps': gaps, 'noFolder': False}


def absent_note(plan):
    where = '/'.join(d.upper() for d in plan['dirs'])
    if plan['noFolder']:
        return 'no %s folder is set' % where
    a = plan['absent']
    return (', '.join('F%d' % f for f in a[:3])
            + (' (+%d more)' % (len(a) - 3) if len(a) > 3 else '')
            + ' %s not in the %s folder' % ('is' if len(a) == 1 else 'are', where))


def fiber_range_text(fibers):
    f = sorted(set(fibers))
    out, i = [], 0
    while i < len(f):
        j = i
        while j + 1 < len(f) and f[j + 1] == f[j] + 1:
            j += 1
        if j - i >= 2:
            out.append('%d-%d' % (f[i], f[j]))
        else:
            out.extend(str(x) for x in f[i:j + 1])
        i = j + 1
    return ', '.join(out)


def cap_readout_rows(keys, pick_key, mx):
    """keys in load order -> (shown keys, more)."""
    if len(keys) <= mx:
        return list(keys), 0
    order = sorted(enumerate(keys), key=lambda x: (x[1] != pick_key, x[0]))
    return [k for _, k in order[:mx]], len(keys) - mx


BOTH = {'dir_a': 'A', 'dir_b': 'B', 'fibers_a': list(range(1, 241)),
        'fibers_b': list(range(1, 241))}
NO_B = {'dir_a': 'A', 'dir_b': '', 'fibers_a': list(range(1, 241)), 'fibers_b': []}
A_SHORT = {'dir_a': 'A', 'dir_b': 'B', 'fibers_a': [1, 2, 3], 'fibers_b': [1, 2]}
UNLISTED = {'dir_a': 'A', 'dir_b': 'B', 'fibers_a': [], 'fibers_b': []}
NONE = {'dir_a': '', 'dir_b': '', 'fibers_a': [], 'fibers_b': []}

PLAN_CASES = [
    ([999], 'both', BOTH),
    ([6, 7, 999, 1000], 'both', BOTH),
    (list(range(1, 241)), 'both', NO_B),
    ([5], 'b', NO_B),
    ([1, 2, 3], 'both', A_SHORT),
    ([3, 4], 'a', UNLISTED),
    ([1], 'both', NONE),
    ([1, 2], 'both', None),
]


# 1: a fibre in neither folder
def test_a_fibre_in_neither_folder_is_named_and_nothing_is_fetched():
    plan = plan_fiber_load([999], 'both', BOTH)
    assert plan['pairs'] == [] and plan['absent'] == [999]
    assert absent_note(plan) == 'F999 is not in the A/B folder'


def test_the_ones_that_are_there_still_load():
    plan = plan_fiber_load([6, 7, 999, 1000], 'both', BOTH)
    assert [(p['f'], p['d']) for p in plan['pairs']] == [(6, 'a'), (6, 'b'), (7, 'a'), (7, 'b')]
    assert absent_note(plan) == 'F999, F1000 are not in the A/B folder'
    many = plan_fiber_load([997, 998, 999, 1000, 1001], 'a', BOTH)
    assert absent_note(many) == 'F997, F998, F999 (+2 more) are not in the A folder'


# 2: A + B with no B folder
def test_a_plus_b_with_no_b_folder_loads_a_alone_and_says_nothing():
    plan = plan_fiber_load(list(range(1, 241)), 'both', NO_B)
    assert plan['dirs'] == ['a']
    assert len(plan['pairs']) == 240 and {p['d'] for p in plan['pairs']} == {'a'}
    assert plan['absent'] == [] and plan['gaps'] == []


def test_asking_for_b_alone_with_no_b_folder_says_so():
    plan = plan_fiber_load([5], 'b', NO_B)
    assert plan['pairs'] == [] and absent_note(plan) == 'no B folder is set'


def test_a_fibre_one_folder_lacks_is_a_note_not_a_404():
    plan = plan_fiber_load([1, 2, 3], 'both', A_SHORT)
    assert (3, 'b') not in [(p['f'], p['d']) for p in plan['pairs']]
    assert plan['gaps'] == ['F3 B: not in the folder']


def test_a_server_that_lists_nothing_is_tried_as_before():
    plan = plan_fiber_load([3, 4], 'a', UNLISTED)
    assert [(p['f'], p['d']) for p in plan['pairs']] == [(3, 'a'), (4, 'a')]
    before_list = plan_fiber_load([1, 2], 'both', None)
    assert len(before_list['pairs']) == 4


# 4: the Fibers box text
@pytest.mark.parametrize('fibers, text', [
    ([], ''),
    ([12, 6, 7], '6, 7, 12'),
    (list(range(1, 241)), '1-240'),
    ([1, 2, 3, 4, 7, 9, 10, 10], '1-4, 7, 9, 10'),
])
def test_the_box_reads_back_what_is_loaded(fibers, text):
    assert fiber_range_text(fibers) == text


# 5: the marker box cap
def test_up_to_the_cap_the_rows_are_untouched():
    keys = ['b-%d' % i for i in range(8)]
    assert cap_readout_rows(keys, 'b-5', 8) == (keys, 0)


def test_past_the_cap_the_pick_then_load_order():
    keys = ['a-%d' % i for i in range(240)]
    assert cap_readout_rows(keys, 'a-100', 8) == (['a-100'] + keys[:7], 232)
    assert cap_readout_rows(keys, None, 8) == (keys[:8], 232)


# ─── the source ─────────────────────────────────────────────────────────────

def test_add_fibers_plans_before_it_fetches():
    body = _js_func(SRC, 'addFibers')
    assert "const plan = planOrSay(fibers, gAddDir);" in body
    assert "if (!plan) return;" in body
    assert "for (const { f, d } of plan.pairs) {" in body
    assert body.count("gLoadFailures = plan.gaps.slice();") == 2
    assert body.count("loadFailNote() + absentTail") == 2


def test_a_jump_to_nothing_does_not_clear_the_plot():
    fn = _js_func(SRC, 'applyTarget')
    a = fn.index("if (!planOrSay(parseFibers(String(fibers)), t.dir || 'a')) return;")
    b = fn.index("if (!planOrSay([+fiber], t.dir || 'both')) return;")
    assert a < fn.index("if (t.replace) clearAll();") < b < fn.rindex("if (t.replace) clearAll();")


def test_the_fold_leaves_a_show_toolbar_button():
    assert '<button id="toolbar-show"' in SRC and 'Show toolbar' in SRC
    assert "#toolbar-show { display: none; }" in SRC
    assert "#toolbar.collapsed #toolbar-show { display: inline-block; }" in SRC
    assert ("document.getElementById('toolbar-show').addEventListener('click', "
            "() => setToolbarCollapsed(false));") in SRC


def test_clear_all_empties_the_box_and_the_readout():
    fn = _js_func(SRC, 'clearAll')
    assert "document.getElementById('fiber-input').value = '';" in fn
    assert "setReadout('');" in fn
    assert "syncFiberBox();" in _js_func(SRC, 'renderChips')
    assert "if (document.activeElement === box) return;" in _js_func(SRC, 'syncFiberBox')


def test_the_marker_box_is_capped():
    assert "const MARKER_MAX_ROWS = 8;" in SRC
    mark = _js_func(SRC, 'updateMarkerReadout')
    assert "capReadoutRows(vis.map(t => ({ t })), gPickKey, MARKER_MAX_ROWS)" in mark
    css = SRC.split("#marker-readout {", 1)[1].split("}", 1)[0]
    assert "max-height: calc(100% - 20px);" in css and "overflow: hidden;" in css


# ─── the real helpers under jsc, against the mirrors ────────────────────────

@needs_jsc
def test_the_viewer_helpers_match_the_mirrors(tmp_path):
    funcs = '\n'.join(_js_func(SRC, n) for n in
                      ('planFiberLoad', 'absentNote', 'fiberRangeText', 'capReadoutRows'))
    cap_rows = [['a-%d' % i for i in range(240)], 'a-100', 8]
    prog = funcs + '\nvar C = %s, F = %s, R = %s;\n' % (
        json.dumps(PLAN_CASES), json.dumps([[12, 6, 7], list(range(1, 241)), [1, 2, 3, 4, 7, 9, 10]]),
        json.dumps(cap_rows)) + r"""
print(JSON.stringify({
  plans: C.map(c => { const p = planFiberLoad(c[0], c[1], c[2]); return [p, absentNote(p)]; }),
  texts: F.map(fiberRangeText),
  cap: (x => [x.shown.map(r => r.t.key), x.more])(
         capReadoutRows(R[0].map(k => ({ t: { key: k } })), R[1], R[2])),
}));
"""
    path = tmp_path / 'helpers.js'
    path.write_text(prog, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    got = json.loads(r.stdout)
    for (fibers, d, info), (plan, note) in zip(PLAN_CASES, got['plans']):
        want = plan_fiber_load(fibers, d, info)
        assert plan == want and note == absent_note(want), (fibers, d, info)
    assert got['texts'] == ['6, 7, 12', '1-240', '1-4, 7, 9, 10']
    assert got['cap'] == list(cap_readout_rows(*cap_rows))


def test_every_absent_note_reads_as_a_failure():
    """The readout shows failures only (viewer/hide-cursor-readout); a note
    the classifier missed would leave the line empty."""
    start = re.search(r"const READOUT_FAIL_START = /(.*)/;", SRC).group(1)
    tail = re.search(r"const READOUT_FAIL_TAIL = /(.*)/;", SRC).group(1)
    notes = [absent_note(plan_fiber_load(f, d, info)) for f, d, info in PLAN_CASES
             if not plan_fiber_load(f, d, info)['pairs'] or plan_fiber_load(f, d, info)['absent']]
    notes.append(absent_note(plan_fiber_load([997, 998, 999, 1000, 1001], 'a', BOTH)))
    assert {'F999 is not in the A/B folder', 'no B folder is set',
            'F997, F998, F999 (+2 more) are not in the A folder'} <= set(notes)
    for n in notes:
        assert re.match(start, n), n
        if 'folder is set' not in n:      # that one only ever stands alone
            assert re.search(tail, '6 traces loaded, ' + n), n
    assert not re.match(start, '6 traces loaded')


# ─── the audit's clicks, replayed in the real script ────────────────────────

_DRIVER = r"""
(async function () {
  var out = {};
  var box = document.getElementById('fiber-input');
  var say = function () { return String(document.getElementById('readout').textContent); };
  try {
    await __hSleep(150);                              // boot's /api/list
    box.value = '1-3'; gAddDir = 'both'; await addFibers();
    out.typed = [gTraces.map(t => t.key).sort(), say(), box.value];
    await applyTarget({ fiber: '999', dir: 'both', replace: true });   // a report jump
    out.jump999 = [gTraces.length, say()];
    await applyTarget({ fibers: '998,999', dir: 'a', replace: true });
    out.pair999 = [gTraces.length, say()];
    box.value = '6, 999'; gAddDir = 'both'; await addFibers();         // some there, some not
    out.part999 = [gTraces.length, say()];
    clearAll();
    out.cleared = [gTraces.length, box.value, say()];
    await applyFileSelection(new Set(['a-2', 'a-3', 'a-7']));           // FILES panel
    out.files = [box.value];
  } catch (e) { print('THREW ' + e + '\n' + (e && e.stack)); }
  out.fetched = __hFetched;
  print('OUT ' + JSON.stringify(out));
  quit();
})();
"""


def _boot(tmp_path, listing, good):
    shims = _SHIMS.replace(
        "body = { dir_a: 'A', dir_b: 'B', fibers_a: [], fibers_b: [], files_a: [], files_b: [] };",
        "body = __hScenario.list;")
    assert shims != _SHIMS, 'the overview-failures shim changed shape'
    shims = shims.replace("globalThis.fetch = function (url) {\n  url = String(url);",
                          "var __hFetched = [];\nglobalThis.fetch = function (url) {\n"
                          "  url = String(url); if (url.indexOf('/api/list') !== 0) __hFetched.push(url);")
    sc = {'list': listing, 'bulk': {},
          'single': {d: {str(f): [200, dict(good, fiber=f)] for f in listing['fibers_' + d]}
                     for d in ('a', 'b')}}
    prog = ('var __hScenario = %s;\n' % json.dumps(sc) + shims + '\n'
            + _viewer_script(SRC) + '\n' + _DRIVER)
    path = tmp_path / 'viewer_audit.js'
    path.write_text(prog, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    assert 'THREW' not in out and r.returncode == 0, out[-3000:]
    line = [ln for ln in out.splitlines() if ln.startswith('OUT ')][-1]
    return json.loads(line[4:])


@needs_jsc
def test_the_audit_clicks_with_both_folders(real_trace, tmp_path):
    good = {'direction': 'A', **real_trace}
    listing = {'dir_a': 'A', 'dir_b': 'B', 'dir_a_name': 'A', 'dir_b_name': 'B',
               'fibers_a': list(range(1, 11)), 'fibers_b': list(range(1, 11)),
               'files_a': [], 'files_b': []}
    out = _boot(tmp_path, listing, good)
    assert out['typed'][0] == ['a-1', 'a-2', 'a-3', 'b-1', 'b-2', 'b-3']
    # a clean load is a plain note: not shown (viewer/hide-cursor-readout)
    assert 'loaded' not in out['typed'][1]
    # the jump to a fibre that is not there: the six stay, and it says why
    assert out['jump999'][0] == 6
    assert out['jump999'][1].startswith('F999 is not in the A/B folder')
    assert out['pair999'][0] == 6
    assert out['pair999'][1].startswith('F998, F999 are not in the A folder')
    # F6 loads; the load note goes, the missing fibre stays
    assert out['part999'][0] == 8
    assert out['part999'][1].startswith('F999 is not in the A/B folder')
    assert not any('fiber=99' in u for u in out['fetched'])
    assert out['cleared'][0] == 0 and out['cleared'][1] == ''
    assert 'loaded' not in out['cleared'][2] and 'not in the' not in out['cleared'][2]
    assert out['files'] == ['2, 3, 7']


@needs_jsc
def test_a_plus_b_with_no_b_folder_is_quiet(real_trace, tmp_path):
    good = {'direction': 'A', **real_trace}
    listing = {'dir_a': 'A', 'dir_b': '', 'dir_a_name': 'A', 'dir_b_name': '(none)',
               'fibers_a': list(range(1, 11)), 'fibers_b': [], 'files_a': [], 'files_b': []}
    out = _boot(tmp_path, listing, good)
    assert out['typed'][0] == ['a-1', 'a-2', 'a-3']
    assert out['typed'][1] == ''
    assert out['typed'][2] == '1-3'
    assert not any('dir=b' in u for u in out['fetched'])
