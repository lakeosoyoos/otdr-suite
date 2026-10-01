"""A foreign page that POSTs to the Viewer gets a 403 it can read.

Every POST route refuses a page from another site (Handler._origin_is_local).
The refusal used to go out with the request's body still unread, and this
server closes the connection after every answer.  Closing a socket that holds
unread data resets the connection instead of ending it, and on Windows the
reset can reach the client before it has read the answer.  It happened once in
the Windows build: a refused POST to /api/report_begin came back as WinError
10053, "an established connection was aborted", where the 403 should have
been.  Which one the client saw was down to whether the body arrived before or
after the server had answered.

The body is read and thrown away first now (Handler._refuse_foreign).  These
tests pin the order, which does not depend on the machine: no answer until the
body is in.  They also pin the two bounds on that read, and that nothing a
foreign page sends is acted on.
"""
from __future__ import annotations

import inspect
import os
import re
import select
import socket
import sys
import threading
import time
from http.server import HTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'viewer'))
import trace_server as T                        # noqa: E402

FOREIGN = 'http://evil.example'
ROUNDS = 25

SRC = open(os.path.join(ROOT, 'viewer', 'trace_server.py'), encoding='utf-8').read()
DO_POST = SRC[SRC.index('    def do_POST(self):'):]
DO_POST = DO_POST[:DO_POST.index("self.send_error(404, 'unknown route')")]
# Every route do_POST answers, read from the source so a route added later is
# held to the same refusal without anyone remembering to list it here.
ROUTES = list(dict.fromkeys(re.findall(r"'(/api/[a-z_]+)'", DO_POST)))
REPORT_ROUTES = ('/api/report_begin', '/api/report_image', '/api/report', '/api/report_open')
# What the routes do once a request is let in.  Each is swapped for a recorder,
# so a refusal that let a request through shows as a call.
ACTIONS = ('report_error', 'span_decl_set', 'drop_begin', 'drop_file', 'drop_files', 'drop_end',
           'pick_folder_native', 'report_begin', 'report_put_image', 'write_viewer_report',
           'open_report', 'find_originals', 'locate_originals', 'rename_files', 'edit_traces',
           # OTDR Suite App: the Viewer pop-out's Back raises the App window.
           'raise_app_window')


def foreign_status(port, path, body, origin=FOREIGN):
    """The status a page is given.  A dropped connection is not caught here:
    it is the failure, and its traceback says which kind."""
    req = Request(f'http://127.0.0.1:{port}{path}', data=body,
                  headers={'Content-Type': 'application/json',
                           **({'Origin': origin} if origin else {})})
    try:
        with urlopen(req, timeout=60) as r:
            r.read()
            return r.status
    except HTTPError as e:
        e.read()
        return e.code


def headers_then_body(port, path, body, declared=None, pause=0.5):
    """Send the headers, wait, then send the body: the order of arrival that
    lost the answer.  Returns (the server answered before the body was sent,
    everything it sent, the error met on the way or None)."""
    n = len(body) if declared is None else declared
    early, got, err = False, b'', None
    s = socket.create_connection(('127.0.0.1', port), timeout=60)
    try:
        s.sendall((f'POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n'
                   f'Origin: {FOREIGN}\r\nContent-Type: application/json\r\n'
                   f'Content-Length: {n}\r\n\r\n').encode('ascii'))
        early = bool(select.select([s], [], [], pause)[0])
        try:
            if body:
                s.sendall(body)
            while True:
                part = s.recv(65536)
                if not part:
                    break
                got += part
        except OSError as e:
            err = e
    finally:
        s.close()
    return early, got, err


@pytest.fixture(scope='module')
def viewer():
    srv = HTTPServer(('127.0.0.1', 0), T.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_port
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def acted(monkeypatch):
    """The calls a request got through to, by name."""
    calls = []

    def recorder(name):
        def fn(*a, **k):
            calls.append(name)
            raise ValueError('reached ' + name)
        return fn
    for name in ACTIONS:
        monkeypatch.setattr(T, name, recorder(name))
    return calls


def test_the_lists_in_this_file_are_the_routes_and_what_they_do():
    assert set(REPORT_ROUTES) <= set(ROUTES) and len(ROUTES) >= 13
    called = {n for n in re.findall(r'\b([A-Za-z_]\w*)\(', DO_POST)
              if inspect.isfunction(getattr(T, n, None))} - {'urlparse', 'parse_qs'}
    assert called == set(ACTIONS)


def test_every_route_refuses_through_the_one_place():
    """The refusal that reads the body first is the only one do_POST has."""
    assert DO_POST.count('if not self._origin_is_local():') == DO_POST.count('self._refuse_foreign(')
    assert 'send_error(403' not in DO_POST
    for limit, routes in (('REPORT_IMAGE_MAX', "('/api/report_begin', '/api/report_image')"),
                          ('REPORT_BODY_MAX', "('/api/report', '/api/report_open')")):
        at = DO_POST.index('if u.path in ' + routes)
        assert f'self._refuse_foreign({limit})' in DO_POST[at:at + 400]


@pytest.mark.parametrize('path', ROUTES)
def test_the_refusal_waits_for_the_body(viewer, acted, path):
    """No answer while the body is still to come, then a 403 read to a clean
    end.  The old refusal answered at once and fails the first line here on
    any machine."""
    early, got, err = headers_then_body(viewer, path, b'{"dir": "a"}')
    assert not early, 'answered with the body still to come; the close then resets the connection'
    assert err is None, err
    assert got.startswith(b'HTTP/1.0 403 '), got[:80]
    assert acted == []


def test_a_foreign_page_gets_its_403_on_every_route_every_time(viewer, acted):
    for _ in range(ROUNDS):
        for path in ROUTES:
            assert foreign_status(viewer, path, b'{"dir": "a"}') == 403, path
    assert acted == []


@pytest.mark.parametrize('path', REPORT_ROUTES)
def test_a_body_bigger_than_the_sockets_hold_is_refused_cleanly(viewer, acted, path):
    """4 MB does not fit in the socket buffers, so the old refusal closed
    while the page was still sending and no machine ever saw the 403."""
    for _ in range(3):
        assert foreign_status(viewer, path, b'{}' + b' ' * (4 * 1024 * 1024)) == 403
    assert acted == []


def test_the_viewers_own_page_is_still_let_in(viewer, acted):
    """The recorders answer 400 when a request reaches them, so a 400 here is
    a request that got through: the same body, refused only for its origin."""
    for origin in (None, f'http://127.0.0.1:{viewer}', f'http://localhost:{viewer}'):
        del acted[:]
        assert foreign_status(viewer, '/api/report', b'{"dir": "a"}', origin=origin) == 400
        assert acted == ['write_viewer_report']


# ─── the two bounds on the read ─────────────────────────────────────────────

@pytest.mark.parametrize('path', REPORT_ROUTES)
def test_a_declared_length_past_the_limit_is_not_read_or_waited_for(viewer, acted, path, monkeypatch):
    """The length is the page's own word.  Past what the route takes, the 403
    goes out at once: with the wait set to two minutes, an answer inside the
    pause can only be one that did not wait."""
    monkeypatch.setattr(T, 'REFUSED_BODY_WAIT_S', 120.0)
    limit = T.REPORT_IMAGE_MAX if path in REPORT_ROUTES[:2] else T.REPORT_BODY_MAX
    for declared in (limit + 1, 10 ** 15):
        early, got, err = headers_then_body(viewer, path, b'', declared=declared, pause=30)
        assert early and err is None and got.startswith(b'HTTP/1.0 403 '), (declared, err, got[:80])
    assert acted == []


@pytest.mark.parametrize('declared', ['-5', 'lots', ''])
def test_a_length_that_is_not_a_length_is_refused_the_same(viewer, acted, declared, monkeypatch):
    monkeypatch.setattr(T, 'REFUSED_BODY_WAIT_S', 120.0)
    early, got, err = headers_then_body(viewer, '/api/report', b'', declared=declared, pause=30)
    assert early and err is None and got.startswith(b'HTTP/1.0 403 '), (err, got[:80])
    assert acted == []


def test_a_page_that_stalls_cannot_hold_the_server(viewer, acted, monkeypatch):
    """This server takes one request at a time.  A refused page that declares
    a body and never sends it is answered when the wait is up, not before and
    not much after, and the next request is served."""
    monkeypatch.setattr(T, 'REFUSED_BODY_WAIT_S', 0.5)
    t0 = time.monotonic()
    _early, got, err = headers_then_body(viewer, '/api/report', b'', declared=4096, pause=0)
    took = time.monotonic() - t0
    assert err is None and got.startswith(b'HTTP/1.0 403 '), (err, got[:80])
    assert 0.4 <= took < 20, took
    with urlopen(f'http://127.0.0.1:{viewer}/api/report_defaults', timeout=30) as r:
        assert r.status == 200
    assert acted == []


def test_the_wait_is_for_the_whole_body_not_for_each_piece(viewer, acted, monkeypatch):
    """A page that sends a byte every fifth of a second never lets a single
    read time out.  The wait is counted from the start, so it is cut off all
    the same."""
    monkeypatch.setattr(T, 'REFUSED_BODY_WAIT_S', 1.0)
    s = socket.create_connection(('127.0.0.1', viewer), timeout=60)
    try:
        s.sendall((f'POST /api/report HTTP/1.1\r\nHost: 127.0.0.1:{viewer}\r\n'
                   f'Origin: {FOREIGN}\r\nContent-Length: 4096\r\n\r\n').encode('ascii'))
        t0 = time.monotonic()
        answered = False
        while time.monotonic() - t0 < 20 and not answered:
            try:
                s.sendall(b' ')
            except OSError:
                answered = True                    # closed on us: it has answered
                break
            answered = bool(select.select([s], [], [], 0.2)[0])
        assert answered, 'still reading a trickle 20 s on'
    finally:
        s.close()
    with urlopen(f'http://127.0.0.1:{viewer}/api/report_defaults', timeout=30) as r:
        assert r.status == 200
    assert acted == []
