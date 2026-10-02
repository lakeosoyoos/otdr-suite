"""Every Viewer window shares one state, and an open Viewer follows the gates
(Viewer -> Splice Report audit, 2026-10-01).

Robert: "traces should still be in viewer when we switch back to it", "we also
need to make sure we are updating the pop out", and "viewer's loss should
update when we select customer profile".

  server   /api/viewer_state keeps what is on the chart, the removed files and
           the Dir buttons, with the folders they were made on and a version;
           /api/mode serves the version and gate_sig, a fingerprint of the
           gates the Viewer judges by
  page     every change is sent (renderChips); a window that opens takes the
           state back (bootLoad) unless a report cell sent it to a fiber; an
           open window follows a newer state from another window and re-reads
           its gates when gate_sig moves (pollAnalysisMode)

The page runs in JavaScriptCore where present; the server parts run everywhere.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request

import pytest

from conftest import VIEWER_DIR, import_trace_server

SRC = (VIEWER_DIR / 'viewer.html').read_text(encoding='utf-8')
TS = import_trace_server()
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


# ── server ──────────────────────────────────────────────────────────────

@pytest.fixture
def _dirs():
    was = (TS.CONFIG.get('dir_a'), TS.CONFIG.get('dir_b'))
    TS.CONFIG['dir_a'], TS.CONFIG['dir_b'] = '/span/A', '/span/B'
    yield
    TS.CONFIG['dir_a'], TS.CONFIG['dir_b'] = was


def test_state_keeps_keys_removed_dir_and_its_folders(_dirs):
    v0 = TS.viewer_state()['ver']
    ver = TS.set_viewer_state({'by': 'w1', 'keys': ['a-1', 'b-1', 'a-1', 'x-2', 'a-'],
                               'removed': ['a-354', 'b-354'], 'add_dir': 'a'})
    st = TS.viewer_state()
    assert ver == v0 + 1 == st['ver']
    assert st['keys'] == ['a-1', 'b-1']               # deduped, junk dropped
    assert st['removed'] == ['a-354', 'b-354']
    assert (st['by'], st['add_dir']) == ('w1', 'a')
    assert (st['dir_a'], st['dir_b']) == ('/span/A', '/span/B')
    TS.set_viewer_state({'add_dir': 'sideways', 'keys': 'a-1'})
    st = TS.viewer_state()
    assert st['add_dir'] == 'both' and st['keys'] == []


def test_gate_sig_moves_with_the_settings():
    was = TS.CONFIG.get('settings'), TS.CONFIG.get('thresholds')
    try:
        TS.CONFIG['thresholds'] = None
        TS.set_settings({'REBURN_THRESHOLD': 0.16})
        a = TS.gate_sig()
        TS.set_settings({'REBURN_THRESHOLD': 0.10})
        b = TS.gate_sig()
        assert a != b and '0.1' in b
        assert TS.gate_sig() == b                     # same settings, same sig
    finally:
        TS.CONFIG['settings'], TS.CONFIG['thresholds'] = was


def _req(port, path, body=None, origin=None):
    headers = {'Content-Type': 'application/json'}
    if origin:
        headers['Origin'] = origin
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=4) as r:
        return json.loads(r.read())


def test_routes_serve_and_take_the_state_and_mode_names_it():
    port = TS.start_in_thread(8798)
    j = _req(port, '/api/viewer_state', {'by': 'w2', 'keys': ['a-7'], 'removed': [],
                                          'add_dir': 'both'}, origin=f'http://127.0.0.1:{port}')
    assert j['ok'] and j['ver'] >= 1
    st = _req(port, '/api/viewer_state')
    assert st['keys'] == ['a-7'] and st['ver'] == j['ver']
    mode = _req(port, '/api/mode')
    assert mode['state_ver'] == j['ver'] and isinstance(mode['gate_sig'], str)
    assert 'gate_sig' in _req(port, '/api/list')
    try:
        _req(port, '/api/viewer_state', {'keys': ['a-1']}, origin='https://evil.example')
        assert False, 'a foreign page set the Viewer state'
    except urllib.error.HTTPError as e:
        assert e.code == 403


# ── page ────────────────────────────────────────────────────────────────

_STUBS = r"""
var gInfo = { dir_a: '/span/A', dir_b: '/span/B' };
var gTraces = [], gRemovedFiles = new Set(), gAddDir = 'both', gClientId = 'me';
var gStateVer = 0, gStateSig = '', gStateReady = true, gStatePushT = null, gStateApplying = 0;
var posts = [], loads = [], clears = 0, timers = [], gSelLoading = false, gStateLoad = null;
function setTimeout(fn) { timers.push(fn); return timers.length; }
function clearTimeout() {}
async function flush() { var t = timers; timers = []; for (var i = 0; i < t.length; i++) await t[i](); }
function fetch(url, opt) {
  if (opt && opt.method === 'POST') posts.push(JSON.parse(opt.body));
  return Promise.resolve({ json: function () { return Promise.resolve({ ok: true, ver: 40 + posts.length }); } });
}
async function selectFiles(want) {
  loads.push([...want].sort());
  gTraces = [...want].map(function (k) { return { key: k }; });
  pushViewerState();                     // the load's own renderChips
}
function clearAll() { clears++; gTraces = []; pushViewerState(); }
function setAddDir(d) { gAddDir = d; }
function renderFilesPanel() {}
var tables = 0; function renderEventTable() { tables++; }
"""

_CASES = r"""
(async function () {
  var out = {};
  // this window loads two fibers: one post, then nothing new to send
  gTraces = [{ key: 'a-1' }, { key: 'b-1' }];
  pushViewerState(); await flush();
  pushViewerState(); await flush();
  out.posts = posts.length; out.first = posts[0];
  // another window's newer state: taken, and not echoed back
  await applyViewerState({ ver: 50, by: 'other', dir_a: '/span/A', dir_b: '/span/B',
                           keys: ['a-2', 'b-2', 'a-354'], removed: ['a-354'], add_dir: 'a' });
  await flush();
  out.applied = [loads[loads.length - 1], [...gRemovedFiles], gAddDir, gStateVer, posts.length];
  // this window's own state coming back: ignored
  var before = loads.length;
  await applyViewerState({ ver: 51, by: 'me', dir_a: '/span/A', dir_b: '/span/B', keys: ['a-9'] });
  out.own = [loads.length - before, gStateVer];
  // a state made on other folders: ignored
  await applyViewerState({ ver: 52, by: 'other', dir_a: '/old/A', dir_b: '/span/B', keys: ['a-9'] });
  out.other_folders = [loads.length - before, [...gRemovedFiles]];
  // an empty state from another window clears this one
  await applyViewerState({ ver: 53, by: 'other', dir_a: '/span/A', dir_b: '/span/B', keys: [], removed: [] });
  await flush();
  out.cleared = [clears, gTraces.length, posts.length];
  // removed files only (a report cell link): traces stay
  gTraces = [{ key: 'a-5' }];
  await applyViewerState({ ver: 54, by: 'other', dir_a: '/span/A', dir_b: '/span/B',
                           keys: ['a-1'], removed: ['b-5'] }, false);
  out.removed_only = [gTraces.map(function (t) { return t.key; }), [...gRemovedFiles]];
  await flush();
  // nothing is sent before boot has taken the shared state
  gStateReady = false; gTraces = [{ key: 'a-77' }]; var n = posts.length;
  pushViewerState(); await flush();
  out.before_ready = posts.length - n;
  print('OUT ' + JSON.stringify(out));
})().catch(function (e) { print('ERR ' + e + '\n' + e.stack); });
"""


@pytest.fixture(scope='module')
def res(tmp_path_factory):
    funcs = '\n'.join(_js_func(n) for n in ('stateSig', 'pushViewerState', 'stateFits',
                                            'applyViewerState'))
    path = tmp_path_factory.mktemp('shared_state') / 'ss.js'
    path.write_text(_STUBS + funcs + '\n' + _CASES, encoding='utf-8')
    r = subprocess.run([JSC, str(path)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and 'OUT ' in out, out[-2000:]
    return json.loads(out.split('OUT ', 1)[1].strip())


@needs_jsc
def test_a_change_is_sent_once(res):
    assert res['posts'] == 1
    assert res['first'] == {'by': 'me', 'keys': ['a-1', 'b-1'], 'removed': [], 'add_dir': 'both'}


@needs_jsc
def test_another_windows_state_is_taken_and_not_echoed(res):
    load, removed, add_dir, ver, posts = res['applied']
    assert load == ['a-2', 'b-2']               # the removed a-354 is not loaded
    assert removed == ['a-354'] and add_dir == 'a' and ver == 50
    assert posts == 1                            # nothing sent back


@needs_jsc
def test_own_state_and_other_folders_are_ignored(res):
    assert res['own'] == [0, 51]
    assert res['other_folders'][0] == 0 and res['other_folders'][1] == ['a-354']


@needs_jsc
def test_an_empty_state_clears_and_removed_alone_keeps_traces(res):
    clears, n, posts = res['cleared']
    assert clears == 1 and n == 0 and posts == 1
    assert res['removed_only'] == [['a-5'], ['b-5']]
    assert res['before_ready'] == 0


def test_page_wiring():
    assert 'pushViewerState();' in _js_func('renderChips')
    boot = _js_func('bootLoad')
    assert "const linked = !!(p.get('fibers') || p.get('fiber'));" in boot
    assert 'await applyViewerState(st, !linked);' in boot
    assert boot.index('gStateReady = true;') < boot.index('await applyTarget(')
    poll = _js_func('pollAnalysisMode')
    assert 'j.gate_sig !== gGateSig' in poll and 'await loadInfo();' in poll
    assert 'j.state_ver > gStateVer' in poll
    assert 'if (gInfo.gate_sig != null) gGateSig = gInfo.gate_sig;' in _js_func('loadInfo')
    # declared with the globals: draw() and renderChips run during boot
    assert SRC.index('let gStateReady = false;') < SRC.index('function resizeCanvas()')
    # the removed set is a const: refilled in place, never reassigned
    assert 'gRemovedFiles = ' not in _js_func('applyViewerState')
