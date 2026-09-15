"""Regression for otdr-suite-errors #31 and #23 (viewer/trace_server.py).

#31 — the boss's Viewer page died with WinError 10013 on start-up.  The old
find_free_port probed with a bare throwaway socket, closed it, and then
HTTPServer bound the same port with different socket options; when THAT bind
was refused nothing caught it.  Now the real server is bound inside the retry
loop, so a port that refuses the server (any OSError, 10013 included) simply
moves us to the next one.

#23 — a tech closed the Viewer tab while the file list was still being written;
Windows raised ConnectionAbortedError (10053) inside the handler thread and it
went to Slack as an app error.  The handler now swallows ConnectionError: the
client hung up, there is nobody to answer.
"""
import errno
import http.client
import io
import os
import socket
import threading
from unittest import mock

import pytest

from conftest import import_trace_server

T = import_trace_server()


@pytest.fixture
def base_port():
    """A port that is free right now, so the loop starts from a known place."""
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def test_bind_refusal_on_first_port_moves_to_the_next(base_port):
    """A port that refuses the SERVER bind (whatever the errno) must not crash
    start-up.  Pre-fix the probe accepted the port and HTTPServer raised."""
    real_init = T._TraceHTTPServer.__init__
    refused = []

    def flaky_init(self, addr, handler, *a, **k):
        if addr[1] == base_port:
            refused.append(addr[1])
            raise PermissionError(errno.EACCES, '[WinError 10013] forbidden')
        return real_init(self, addr, handler, *a, **k)

    with mock.patch.object(T._TraceHTTPServer, '__init__', flaky_init):
        srv, port = T.find_free_port(base_port, count=5)
    try:
        assert refused == [base_port]
        assert port == base_port + 1
        assert srv.server_address[1] == port
    finally:
        srv.server_close()


def test_every_port_refused_reports_the_last_error(base_port):
    with mock.patch.object(T._TraceHTTPServer, '__init__',
                           side_effect=PermissionError(errno.EACCES, 'nope')):
        with pytest.raises(RuntimeError, match='nope'):
            T.find_free_port(base_port, count=3)


def test_no_reuseaddr_on_windows():
    """SO_REUSEADDR on Windows lets a second hub bind the port a first one is
    still serving on; keep it only where it means 'rebind over TIME_WAIT'."""
    assert T._reuse_address_ok('nt') is False
    assert T._reuse_address_ok('posix') is True
    assert T._TraceHTTPServer.allow_reuse_address == T._reuse_address_ok(os.name)


def test_client_hangup_mid_response_is_not_an_error(tmp_path):
    """Serve a real request, have the client close before reading the body,
    and make sure the handler thread raises nothing (pre-fix: 10053 / EPIPE
    escaped and was reported to Slack)."""
    T.CONFIG['dir_a'] = str(tmp_path)
    T.CONFIG['dir_b'] = ''
    srv, port = T.find_free_port(_free(), count=1)
    errors = []

    def serve():
        try:
            srv.handle_request()
        except BaseException as e:            # noqa: BLE001 - the whole point
            errors.append(e)

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    try:
        c = socket.create_connection(('127.0.0.1', port))
        c.sendall(b'GET /api/list HTTP/1.0\r\n\r\n')
        c.close()
        th.join(5)
        assert not th.is_alive()
        assert errors == []
    finally:
        srv.server_close()


def test_handler_swallows_connection_error_from_write():
    """Direct unit check independent of socket buffering: a write that raises
    ConnectionAbortedError inside the request handler is absorbed by handle()."""
    class Boom(io.BytesIO):
        def write(self, b):
            raise ConnectionAbortedError(10053, 'aborted by the host')

    h = T.Handler.__new__(T.Handler)
    h.rfile = io.BytesIO(b'GET /api/list HTTP/1.0\r\n\r\n')
    h.wfile = Boom()
    h.client_address = ('127.0.0.1', 1)
    h.server = mock.Mock()
    h.request = mock.Mock()
    h.close_connection = True
    T.CONFIG['dir_a'] = ''
    T.CONFIG['dir_b'] = ''
    h.handle()                    # pre-fix: ConnectionAbortedError propagates


def _free():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
