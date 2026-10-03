"""FastReporter's Select Same tools, in the FILES list's right-click menu.

FR3's Measurements menu (driven 2026-09-30) selects every file that shares
one property with the selection: Select Same Pass/Fail Status, Identifiers,
Type and Date, and Multiple Option Selection, which takes any of them
together against the right-clicked file.  Robert: "build as you suggest".
The Viewer's names for the same things:

  Select Same Pass/Fail Status   every file with the same verdict
  Select Same Fiber              both directions of the selected fibers
  Select Same Wavelength         FR's Type (1310, 1550, 1625 ...)
  Select Same Day                the day the trace was SHOT, local time
  Select By...                   any of them, all to match

What was seen on FR and is held here:

* each one REPLACES the selection;
* each is greyed while the selected files differ on its property;
* Date is the acquisition, not the file's date on disk, and the day is the
  viewer's local one (FR put 01:37 and 02:31 UTC shots on the previous day);
* Multiple Option Selection ANDs the ticks against the right-clicked file,
  and remembers them.

A file's verdict is the event table's (Robert 2026-10-02: "P/F should
always [be the] event table"; pinned in test_viewer_files_pf_follows_table.py):
a file no table has judged has none, and never matches on Pass/Fail.

The server half: every trace now says when it was shot (acq_time, from
FxdParams), and /api/traces?facts=1 sends a whole list's headers and events
without the samples, which is what the tools read.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
from unittest import mock

import pytest

from conftest import FIXTURE_A_DIR, VIEWER_DIR, import_trace_server

T = import_trace_server()
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


def _body(name):
    src = _js_func(name)
    return src[src.index('{') + 1:-1]


# ─── the server: when a trace was shot, and the list's facts ─────────────────

def _get_json(path):
    """One GET through the real handler with in-memory streams -> (status, body)."""
    h = T.Handler.__new__(T.Handler)
    h.rfile = io.BytesIO(('GET %s HTTP/1.0\r\n\r\n' % path).encode('ascii'))
    h.wfile = io.BytesIO()
    h.client_address = ('127.0.0.1', 1)
    h.server = mock.Mock()
    h.request = mock.Mock()
    h.close_connection = True
    h.handle()
    head, _, body = h.wfile.getvalue().partition(b'\r\n\r\n')
    return int(head.split()[1]), json.loads(body)


@pytest.fixture()
def span_a():
    saved = dict(T.CONFIG)
    T.CONFIG['dir_a'] = str(FIXTURE_A_DIR)
    try:
        yield [n for n, _fn in T.list_fibers(str(FIXTURE_A_DIR))]
    finally:
        T.CONFIG.clear()
        T.CONFIG.update(saved)


def test_a_trace_says_when_it_was_shot(span_a):
    """FxdParams' date/time, seconds since 1970: the acquisition, which is
    what FR's Select Same Date compares, not the file's date on disk."""
    fiber = span_a[0]
    t = T.load_trace('a', fiber)
    fmap = dict(T.list_fibers(str(FIXTURE_A_DIR)))
    r = T.parse_sor_full(os.path.join(str(FIXTURE_A_DIR), fmap[fiber]), trim=False)
    assert t['acq_time'] == int(r['date_time']) > 1_000_000_000
    assert T._acq_time({'date_time': 0}) is None and T._acq_time(None) is None


def test_facts_1_sends_the_headers_and_events_without_the_samples(span_a):
    spec = ','.join(str(f) for f in span_a[:3])
    status, full = _get_json('/api/traces?dir=a&fibers=%s&maxpts=200' % spec)
    assert status == 200 and len(full['traces']) == 3
    status, facts = _get_json('/api/traces?dir=a&fibers=%s&maxpts=200&facts=1' % spec)
    assert status == 200 and len(facts['traces']) == 3
    for big, small in zip(full['traces'], facts['traces']):
        assert 'dist_km' in big and 'trace_db' in big
        assert 'dist_km' not in small and 'trace_db' not in small
        for k in ('fiber', 'filename', 'wavelength_nm', 'acq_time', 'events'):
            assert small[k] == big[k], k
        assert small['events'], 'the verdict needs the events'


# ─── behaviour: the rules, run as written ────────────────────────────────────

_CASES = r"""
var out = {};
// Select Same / Select By on plain traits
var K = ['a-1', 'a-2', 'a-3', 'b-1', 'b-2', 'b-3'];
var TR = {
  'a-1': { fiber: 1, wl: 1550, day: '2026-09-13', pf: false },
  'a-2': { fiber: 2, wl: 1550, day: '2026-09-13', pf: true },
  'a-3': { fiber: 3, wl: 1625, day: '2026-09-13', pf: false },
  'b-1': { fiber: 1, wl: 1550, day: '2026-09-05', pf: false },
  'b-2': { fiber: 2, wl: 1550, day: '2026-09-05', pf: true },
  'b-3': null,
};
function tr(k) { return TR[k]; }
out.same_pf     = fileSameKeys(K, tr, TR['a-1'], ['pf']);
out.same_wl     = fileSameKeys(K, tr, TR['a-3'], ['wl']);
out.same_day    = fileSameKeys(K, tr, TR['b-2'], ['day']);
out.by_pf_day   = fileSameKeys(K, tr, TR['a-1'], ['pf', 'day']);
out.by_fiber_wl = fileSameKeys(K, tr, TR['a-2'], ['fiber', 'wl']);
// the day is the LOCAL one, whatever the zone
var noon = Math.floor(new Date(2026, 8, 13, 12, 0, 0).getTime() / 1000);
var late = Math.floor(new Date(2026, 8, 13, 23, 30, 0).getTime() / 1000);
var early = Math.floor(new Date(2026, 8, 14, 0, 30, 0).getTime() / 1000);
out.days = [fileDay(noon), fileDay(late), fileDay(early), fileDay(null), fileDay(0)];
print('OUT ' + JSON.stringify(out));
"""


@pytest.fixture(scope='module')
def rules(tmp_path_factory):
    funcs = '\n'.join(_js_func(n) for n in
                      ('fileSameKeys', 'fileDay'))
    path = tmp_path_factory.mktemp('select_same') / 'rules.js'
    path.write_text(funcs + '\n' + _CASES, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and 'OUT ' in out, out[-2000:]
    return json.loads(out.split('OUT ', 1)[1].strip())


@needs_jsc
def test_select_same_keeps_the_rows_that_match_on_every_tick(rules):
    assert rules['same_pf'] == ['a-1', 'a-3', 'b-1']
    assert rules['same_wl'] == ['a-3']
    assert rules['same_day'] == ['b-1', 'b-2']
    assert rules['by_pf_day'] == ['a-1', 'a-3']          # passing AND shot that day
    assert rules['by_fiber_wl'] == ['a-2', 'b-2']        # a file not read never matches


@needs_jsc
def test_the_day_is_the_viewers_local_day(rules):
    assert rules['days'] == ['2026-09-13', '2026-09-13', '2026-09-14', None, None]


# ─── the wiring, checked everywhere ──────────────────────────────────────────

def test_the_file_menu_carries_the_select_tools():
    menu = SRC.split("function showFileDirMenu(", 1)[1].split("\nasync function ", 1)[0]
    assert '+ selectSameMenuHtml(marks);' in menu
    assert "if (b.dataset.same === 'by') showSelectByDialog(k);" in menu
    assert 'else selectSame([b.dataset.same], k);' in menu
    html = _body('selectSameMenuHtml')
    for label in ('Select Same Pass/Fail Status', 'Select Same Fiber', 'Select Same Wavelength',
                  'Select Same Day', 'Select By…'):
        assert label in html, label
    # greyed while the selected files differ, as FR greys them
    assert "filesAgreeOn(marks, p) ? '' : `the selected files differ in" in html
    assert "flagsOff() ? 'the Settings box did not load, so nothing is graded'" in html


def test_select_same_replaces_the_selection_through_the_one_load_path():
    body = _body('selectSame')
    assert 'await selectFiles(new Set(want));' in body
    # Select Same Fiber takes every selected fiber, both directions
    assert 'new Set([...gSelectedFiles, refKey].map(k => splitFileKey(k)[1]))' in body
    # one read of the list at a time: the one in flight, then the rest
    assert body.index('if (gFactsLoading) await gFactsLoading;') < body.index('await ensureFileFacts(true);')
    assert 'want = fileSameKeys(keys, fileTraits, ref, kinds);' in body
    # a file's Pass/Fail is the event table's
    assert 'pf: tableFileFails(key)' in _body('fileTraits')
    assert 'function fileFails(' not in SRC


def test_the_list_is_read_once_without_its_samples():
    body = _body('loadFileFacts')
    assert 'for (const t of gTraces) gFileFacts.set(t.key, factsOf(t.data));' in body
    assert '&maxpts=200&facts=1' in body
    assert 'i += 144' in body                     # a ribbon's worth per request
    assert "setReadout(`could not read the files' details" in body   # a failure shows
    assert 'await fetchRetry(`/api/traces?' in body                  # a dropped connection is retried
    assert 'ok = false;                      // carry on: the rest may still come' in body
    # a new drop or a rename forgets what was read about that side
    assert 'forgetFileFacts(dir);' in _body('forgetSide')
    assert 'dirs.forEach(forgetFileFacts);' in _body('afterRename')


def test_select_by_remembers_its_ticks_and_the_keys_wait_behind_it():
    dlg = _body('showSelectByDialog')
    assert "localStorage.setItem('otdr_viewer_select_by', JSON.stringify(kinds));" in dlg
    assert "localStorage.getItem('otdr_viewer_select_by')" in dlg
    assert "if (ev.key === 'Escape')" in dlg and "else if (ev.key === 'Enter')" in dlg
    # nothing graded, nothing to match: the Pass/Fail tick is off and greyed
    assert "const off = p === 'pf' && flagsOff();" in dlg
    assert "${ticked.includes(p) && !off ? ' checked' : ''}${off ? ' disabled' : ''}" in dlg
    kd = SRC.split("window.addEventListener('keydown', (ev) => {", 1)[1].split("\n});", 1)[0]
    assert "document.getElementById('sel-dlg')" in kd
    assert '#sel-dlg {' in SRC
