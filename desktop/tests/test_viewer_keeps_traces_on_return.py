"""The Viewer keeps its traces across a trip to another tool, and starts empty
when the software starts (Robert 2026-10-02).

The hub drops the Viewer's frame on every page switch, so a tech who loaded a
whole cable, looked at the Splice Report and came back found an empty Viewer
and loaded it all again.  Now the page keeps what it shows in sessionStorage
under the hub session's token (?s= on its address):

  same token, same folders, same report jump   put back as it was
  a new hub session (launch, reload)            new token: starts empty
  hub Clear Traces                              new token: starts empty
  another folder, or a new report cell click    not put back: the new thing shows

Run in JavaScriptCore where present; the hub side runs everywhere.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

from conftest import VIEWER_DIR, run_streamlit, FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR

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


_STUBS = r"""
var store = {};
var sessionStorage = { getItem: function (k) { return k in store ? store[k] : null; },
                       setItem: function (k, v) { store[k] = String(v); } };
var search = '?b=1&s=tok1';
var location = { get search() { return search; } };
function URLSearchParams(q) {             // JavaScriptCore has none
  var m = {};
  String(q).replace(/^\?/, '').split('&').forEach(function (kv) {
    if (!kv) return; var i = kv.indexOf('=');
    m[decodeURIComponent(i < 0 ? kv : kv.slice(0, i))] = i < 0 ? '' : decodeURIComponent(kv.slice(i + 1));
  });
  this.get = function (k) { return k in m ? m[k] : null; };
}
var KEEP_KEY = 'otdr_viewer_keep', gKeepToken = 'tok1', gKeepReady = false, gKeepLast = '';
var gInfo = { dir_a: '/A', dir_b: '/B' };
var gTraces = [], gLoadingKeys = new Set(), gRemovedFiles = new Set(), gDirOverride = {};
var gAutoFit = true, gView = null, renders = 0;
async function selectFiles(want) {
  gTraces = [...want].map(function (k) { return { key: k, visible: true }; });
}
function renderChips() {} function draw() {} function renderEventTable() { renders++; }
function fresh() {          // the page a trip back builds: nothing in memory
  gTraces = []; gLoadingKeys = new Set(); gRemovedFiles = new Set(); gDirOverride = {};
  gAutoFit = true; gView = null; gKeepReady = false; gKeepLast = '';
}
function loadAsTheTechDid() {
  gKeepReady = true;
  gTraces = [{ key: 'a-1', visible: true }, { key: 'b-1', visible: false },
             { key: 'a-2', visible: true }];
  gLoadingKeys = new Set(['b-2']);          // still on its way when they left
  gRemovedFiles = new Set(['a-9']);
  gDirOverride = { 'a-2': 'b' };
  gAutoFit = false; gView = { x0: 10, x1: 20, y0: -5, y1: 0 };
  keepState();
}
"""

_CASES = r"""
(async function () {
  var out = {};
  // nothing is written before boot has tried to restore
  keepState();
  out.notReady = Object.keys(store).length;

  loadAsTheTechDid();
  fresh();
  out.back = await restoreKept();
  out.backState = { keys: gTraces.map(function (t) { return t.key; }).sort(),
                    hidden: gTraces.filter(function (t) { return !t.visible; }).map(function (t) { return t.key; }),
                    removed: [...gRemovedFiles], dirOverride: gDirOverride,
                    view: gView, autoFit: gAutoFit, ready: gKeepReady };

  // a new hub session: another token
  fresh(); gKeepToken = 'tok2'; search = '?b=1&s=tok2';
  out.newSession = [await restoreKept(), gTraces.length];
  gKeepToken = 'tok1'; search = '?b=1&s=tok1';

  // other folders
  fresh(); gInfo = { dir_a: '/C', dir_b: '/B' };
  out.otherFolders = [await restoreKept(), gTraces.length];
  gInfo = { dir_a: '/A', dir_b: '/B' };

  // a new report cell click: the link moved
  fresh(); search = '?b=1&s=tok1&fiber=7&km=3.2&dir=both';
  out.newJump = [await restoreKept(), gTraces.length];

  // the jump's own fiber, then a trip and back with the same link
  gKeepReady = true; gTraces = [{ key: 'a-7', visible: true }, { key: 'b-7', visible: true },
                                { key: 'a-8', visible: true }];
  keepState();
  fresh();
  out.sameJump = [await restoreKept(), gTraces.map(function (t) { return t.key; }).sort()];

  // a Clear All is kept: back on the Viewer it is still empty, link spent
  gKeepReady = true; gTraces = []; keepState();
  fresh();
  out.cleared = [await restoreKept(), gTraces.length];

  // no token (the pop-out window, an old hub): nothing kept, nothing restored
  fresh(); gKeepToken = ''; store = {};
  gKeepReady = true; gTraces = [{ key: 'a-1', visible: true }]; keepState();
  out.noToken = Object.keys(store).length;
  print('OUT ' + JSON.stringify(out));
})().catch(function (e) { print('ERR ' + e + '\n' + e.stack); });
"""


@pytest.fixture(scope='module')
def res(tmp_path_factory):
    if not os.path.exists(JSC):
        pytest.skip('JavaScriptCore shell (macOS) not present')
    funcs = '\n'.join(_js_func(n) for n in ('keepLinkSig', 'keepState', 'restoreKept'))
    path = tmp_path_factory.mktemp('keep') / 'keep.js'
    path.write_text(_STUBS + funcs + '\n' + _CASES, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and 'OUT ' in out, out[-3000:]
    return json.loads(out.split('OUT ', 1)[1].strip())


@needs_jsc
def test_nothing_is_written_before_boot_tried_to_restore(res):
    assert res['notReady'] == 0


@needs_jsc
def test_back_from_another_tool_everything_is_as_it_was(res):
    assert res['back'] is True
    st = res['backState']
    assert st['keys'] == ['a-1', 'a-2', 'b-1', 'b-2']    # b-2 was still on its way
    assert st['hidden'] == ['b-1']
    assert st['removed'] == ['a-9']
    assert st['dirOverride'] == {'a-2': 'b'}
    assert st['view'] == {'x0': 10, 'x1': 20, 'y0': -5, 'y1': 0} and st['autoFit'] is False
    assert st['ready'] is True


@needs_jsc
def test_a_new_session_starts_empty(res):
    assert res['newSession'] == [False, 0]


@needs_jsc
def test_other_folders_are_not_given_the_old_traces(res):
    assert res['otherFolders'] == [False, 0]


@needs_jsc
def test_a_new_cell_click_shows_its_own_fiber(res):
    assert res['newJump'] == [False, 0]


@needs_jsc
def test_the_same_jump_after_a_trip_keeps_what_was_added(res):
    assert res['sameJump'] == [True, ['a-7', 'a-8', 'b-7']]


@needs_jsc
def test_a_clear_all_is_kept(res):
    assert res['cleared'] == [True, 0]


@needs_jsc
def test_no_token_keeps_nothing(res):
    assert res['noToken'] == 0


def test_bootload_restores_before_the_link_and_writes_only_with_folders():
    f = _js_func('bootLoad')
    assert f.index('await restoreKept()') < f.index('applyTarget(')
    assert 'if (gInfo.dir_a || gInfo.dir_b) gKeepReady = true;' in f
    for name in ('addFibers', 'applyFileSelection', 'clearAll'):
        assert 'gKeepReady = true' in _js_func(name), name


# ── the hub: a token per session, a new one after Clear Traces ───────────
A, B = str(FIXTURE_SPLICE_A_DIR), str(FIXTURE_SPLICE_B_DIR)


def _box(at, label):
    return next(t for t in at.sidebar.text_input if t.label == label)


def test_the_hub_gives_the_viewer_a_session_token_and_clear_traces_renews_it():
    at = run_streamlit().run()
    _box(at, 'A Folder').input(A).run()
    _box(at, 'B Folder').input(B).run()
    assert not at.exception, at.exception
    tok = at.session_state['_viewer_q']['s']
    assert re.fullmatch(r'[0-9a-f]{12}', tok)
    at.run()                                        # a plain rerun: same address
    assert at.session_state['_viewer_q']['s'] == tok
    at.sidebar.radio[0].set_value('Splice Report').run()
    at.sidebar.radio[0].set_value('Viewer').run()   # a trip: same token
    assert at.session_state['_viewer_q']['s'] == tok
    next(b for b in at.sidebar.button if b.label == 'Clear Traces').click().run()
    next(b for b in at.button if b.key == 'clear_traces_allow').click().run()
    _box(at, 'A Folder').input(A).run()
    _box(at, 'B Folder').input(B).run()
    assert at.session_state['_viewer_q']['s'] != tok  # nothing old comes back
    other = run_streamlit().run()                     # a new session
    other.sidebar.radio[0].set_value('Viewer').run()
    assert other.session_state['_viewer_q']['s'] != tok
