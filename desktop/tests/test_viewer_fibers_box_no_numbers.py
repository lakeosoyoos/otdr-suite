"""Viewer Fibers box: text with no fiber number in it says so (audit
2026-10-02).

Typing "abc" (or "x-y", or ",,,") in the toolbar Fibers box and clicking Add
showed nothing at all, while "99" said "F99 is not in the A/B folder".  The
readout shows failures only, and "no valid fibers in input" was not one, so
the line stayed empty.  Now: 'No fiber numbers in "abc". Type numbers like
5 or 1-3, 7.'  A box that mixes good and bad parts ("1, abc") loads the
good ones and names the part it skipped.  An empty box still says nothing.
"""
from __future__ import annotations

import json
import re
import subprocess

from conftest import VIEWER_DIR
from test_viewer_overview_failures import (  # noqa: F401 (real_trace is a fixture)
    JSC, _SHIMS, _viewer_script, needs_jsc, real_trace)
from test_viewer_audit_load_toolbar_readout import _js_func

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def test_the_notes_read_as_failures():
    """The readout drops anything the classifier does not call a failure."""
    start = re.search(r"const READOUT_FAIL_START = /(.*)/;", SRC).group(1)
    tail = re.search(r"const READOUT_FAIL_TAIL = /(.*)/;", SRC).group(1)
    assert re.match(start, 'No fiber numbers in "abc". Type numbers like 5 or 1-3, 7.')
    assert re.search(tail, '3 traces loaded, ignored "abc": not a fiber number')
    assert not re.match(start, 'no valid fibers in input')    # the empty box


def test_add_fibers_says_why_nothing_loads():
    body = _js_func(SRC, 'addFibers')
    assert "const fibers = parseFibers(raw, ignored);" in body
    assert "noFibersNote(raw)" in body
    assert "ignoredPartsNote(ignored)" in body


@needs_jsc
def test_the_helpers_under_jsc(tmp_path):
    prog = '\n'.join(_js_func(SRC, n) for n in
                     ('parseFibers', 'noFibersNote', 'ignoredPartsNote')) + r"""
var cases = ['abc', 'x-y', ',,,', '1, abc', '2-4, F5, x-y, 7', ' 6 ', 'a,b,c,d,e'];
print(JSON.stringify(cases.map(function (c) {
  var ig = []; var f = parseFibers(c, ig);
  return [f, ig, f.length ? (ig.length ? ignoredPartsNote(ig) : '') : noFibersNote(c)];
})));
print(JSON.stringify(parseFibers('1-3, abc')));
print(JSON.stringify(noFibersNote('x'.repeat(60))));
"""
    path = tmp_path / 'fibers_box.js'
    path.write_text(prog, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    lines = r.stdout.splitlines()
    got = json.loads(lines[0])
    assert got[0] == [[], ['abc'], 'No fiber numbers in "abc". Type numbers like 5 or 1-3, 7.']
    assert got[1][2] == 'No fiber numbers in "x-y". Type numbers like 5 or 1-3, 7.'
    assert got[2][2] == 'No fiber numbers in ",,,". Type numbers like 5 or 1-3, 7.'
    assert got[3] == [[1], ['abc'], 'ignored "abc": not a fiber number']
    assert got[4] == [[2, 3, 4, 7], ['F5', 'x-y'], 'ignored "F5", "x-y": not fiber numbers']
    assert got[5] == [[6], [], '']
    assert got[6][2] == 'No fiber numbers in "a,b,c,d,e". Type numbers like 5 or 1-3, 7.'
    # the one-argument callers (report jumps) are unchanged
    assert json.loads(lines[1]) == [1, 2, 3]
    assert json.loads(lines[2]) == 'No fiber numbers in "%s…". Type numbers like 5 or 1-3, 7.' % ('x' * 40)


_DRIVER = r"""
(async function () {
  var out = {};
  var box = document.getElementById('fiber-input');
  var say = function () { return String(document.getElementById('readout').textContent); };
  var settle = function () { return __hSleep(60); };
  try {
    await __hSleep(150);                              // boot's /api/list
    gAddDir = 'both';
    var typed = ['abc', 'x-y', ',,,'];
    out.junk = [];
    for (var i = 0; i < typed.length; i++) {
      box.value = typed[i]; await addFibers(); await settle();
      out.junk.push([gTraces.length, say()]);
    }
    box.value = '   '; await addFibers(); await settle();
    out.blank = [gTraces.length, say()];
    box.value = '1, abc'; await addFibers(); await settle();
    out.mixed = [gTraces.map(function (t) { return t.key; }).sort(), say()];
  } catch (e) { print('THREW ' + e + '\n' + (e && e.stack)); }
  print('OUT ' + JSON.stringify(out));
  quit();
})();
"""


@needs_jsc
def test_typing_text_with_no_fiber_number_says_so(real_trace, tmp_path):
    good = {'direction': 'A', **real_trace}
    listing = {'dir_a': 'A', 'dir_b': 'B', 'dir_a_name': 'A', 'dir_b_name': 'B',
               'fibers_a': list(range(1, 11)), 'fibers_b': list(range(1, 11)),
               'files_a': [], 'files_b': []}
    shims = _SHIMS.replace(
        "body = { dir_a: 'A', dir_b: 'B', fibers_a: [], fibers_b: [], files_a: [], files_b: [] };",
        "body = __hScenario.list;")
    assert shims != _SHIMS, 'the overview-failures shim changed shape'
    sc = {'list': listing, 'bulk': {},
          'single': {d: {str(f): [200, dict(good, fiber=f)] for f in listing['fibers_' + d]}
                     for d in ('a', 'b')}}
    prog = ('var __hScenario = %s;\n' % json.dumps(sc) + shims + '\n'
            + _viewer_script(SRC) + '\n' + _DRIVER)
    path = tmp_path / 'viewer_fibers_box.js'
    path.write_text(prog, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    assert 'THREW' not in out and r.returncode == 0, out[-3000:]
    got = json.loads([ln for ln in out.splitlines() if ln.startswith('OUT ')][-1][4:])
    for (n, line), typed in zip(got['junk'], ('abc', 'x-y', ',,,')):
        assert n == 0
        assert line.startswith('No fiber numbers in "%s". Type numbers like 5 or 1-3, 7.'
                               % typed), line
    assert got['blank'] == [0, '']
    keys, line = got['mixed']
    assert keys == ['a-1', 'b-1']
    assert 'ignored "abc": not a fiber number' in line, line
    assert 'loaded' not in line              # the load note stays hidden
