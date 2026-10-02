"""The Viewer's burst of trace requests must not bounce off a full backlog.

2026-09-29 click-through audit: selecting fibres 1-24 A+B on a real job showed
"47 traces loaded, could not load F13 A: Failed to fetch" in 2 of 4 loads.
addFibers fires 6 fibres x 2 directions = 12 fetches at once, and the
single-threaded trace server listened with HTTPServer's default backlog of 5.
While it parsed one trace, connects past the fifth waiting one were reset
(macOS) or refused outright (Windows).  Measured with urllib: 12 concurrent
GETs -> 25 of 240 reset, 8 -> 5, 6 or fewer -> 0.

The server stays single-threaded (its handlers share many module-level caches
without locks); it now keeps a deep listen queue so a burst waits its turn
instead of being turned away.  viewer.html's loadOne also retries a thrown
fetch, which covers the rest.
"""
import json
import os
import re
import subprocess
import threading
import urllib.request

import pytest

from conftest import FIXTURE_A_DIR, FIXTURE_B_DIR, VIEWER_DIR, import_trace_server

T = import_trace_server()

N_CLIENTS = 16


def _fetch(port, fiber, direction, errors, oks):
    url = f'http://127.0.0.1:{port}/api/trace?dir={direction}&fiber={fiber}'
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            r.read()
            oks.append(r.status)
    except Exception as e:      # any connection failure is the bug
        errors.append(f'F{fiber} {direction}: {e!r}')


def test_concurrent_trace_burst_gets_no_connection_errors():
    before = (T.CONFIG.get('dir_a'), T.CONFIG.get('dir_b'))
    T.set_dirs(str(FIXTURE_A_DIR), str(FIXTURE_B_DIR))
    srv, port = T.find_free_port(20000 + threading.get_ident() % 20000)
    server_thread = threading.Thread(target=srv.serve_forever, daemon=True)
    try:
        assert srv.request_queue_size >= N_CLIENTS
        errors, oks = [], []
        clients = [threading.Thread(target=_fetch,
                                    args=(port, 1 + i % 4, 'ab'[i // 4 % 2], errors, oks))
                   for i in range(N_CLIENTS)]
        # Every client connects while the server is not yet accepting, the
        # way a Viewer burst lands while the server is busy parsing a trace:
        # all of them must fit in the listen queue.
        for c in clients:
            c.start()
        for c in clients:
            c.join(0.05)
        server_thread.start()
        for c in clients:
            c.join(90)
        assert errors == []
        assert oks == [200] * N_CLIENTS
    finally:
        if server_thread.is_alive():
            srv.shutdown()
        srv.server_close()
        T.set_dirs(*before)


# ─── the Viewer's end: loadOne retries a thrown fetch, not an HTTP error ────

VIEWER_HTML = os.path.join(str(VIEWER_DIR), 'viewer.html')
JSC = ('/System/Library/Frameworks/JavaScriptCore.framework/'
       'Versions/Current/Helpers/jsc')
needs_jsc = pytest.mark.skipif(not os.path.exists(JSC),
                               reason='JavaScriptCore shell (macOS) not present')


def _load_one_src():
    """fetchRetry + loadOne, cut out of the real viewer.html."""
    html = open(VIEWER_HTML, encoding='utf-8').read()
    m = re.search(r'async function fetchRetry\(.*?\n}\n\nasync function loadOne\(.*?\n}\n',
                  html, re.S)
    assert m, 'fetchRetry/loadOne not found in viewer.html'
    return m.group(0)


def _run_load_one(replies):
    """Run loadOne(F1 A) against a fetch that plays `replies` in order:
    'throw' -> TypeError('Failed to fetch'), an int -> that HTTP status."""
    prog = '''
var __replies = %s, __calls = 0;
var gLoadFailures = [], gTraces = [], gStoredDir = {}, gRemovedFiles = new Set();
var gLoadingKeys = new Set(), gLoadEpoch = 0;
function effDir(d) { return d; }
function nextColor() { return '#000'; }
function traceColor() { return '#000'; }
var console = { warn: function () {} };
function setTimeout(fn) { Promise.resolve().then(fn); }
function fetch() {
  var r = __replies[Math.min(__calls++, __replies.length - 1)];
  if (r === 'throw') return Promise.reject(new TypeError('Failed to fetch'));
  return Promise.resolve({ ok: r === 200, status: r,
                           json: function () { return Promise.resolve({}); } });
}
%s
loadOne('a1', 1, 'a').then(function () {
  print(JSON.stringify({ calls: __calls, traces: gTraces.length, fails: gLoadFailures }));
}, function (e) { print('THREW ' + e); });
''' % (json.dumps(replies), _load_one_src())
    r = subprocess.run([JSC, '-e', prog], capture_output=True, text=True, timeout=60)
    out = (r.stdout + r.stderr).strip()
    assert 'THREW' not in out and r.returncode == 0, out
    return json.loads(out.splitlines()[-1])


@needs_jsc
def test_load_one_retries_a_thrown_fetch():
    got = _run_load_one(['throw', 'throw', 200])
    assert got == {'calls': 3, 'traces': 1, 'fails': []}


@needs_jsc
def test_load_one_gives_up_after_three_thrown_fetches():
    got = _run_load_one(['throw'])
    assert got['calls'] == 3 and got['traces'] == 0
    assert got['fails'] == ['F1 A: Failed to fetch']


@needs_jsc
def test_load_one_does_not_retry_an_http_error():
    got = _run_load_one([404])
    assert got == {'calls': 1, 'traces': 0, 'fails': ['F1 A: HTTP 404']}
