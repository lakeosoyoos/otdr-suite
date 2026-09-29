"""Field Capture page: the server that shows the web app inside the hub, the
email draft it writes, and the packaging that ships it.

The web app itself (fieldcapture/web) is JavaScript and there is no Node on
the build machines, so its behaviour was verified in a browser; these tests
pin the Python around it and the contract between the two.
"""
import email
import inspect
import json
import re
import select
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from email import policy
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fieldcapture import email_draft, server  # noqa: E402

WEB = REPO_ROOT / 'fieldcapture' / 'web'


@pytest.fixture()
def fc(tmp_path, monkeypatch):
    """A Field Capture server on a free port, saving into tmp_path, with the
    'open in the mail program' step recorded instead of run."""
    opened = []
    monkeypatch.setattr(email_draft, 'open_with_default_app', lambda p: (opened.append(Path(p)) or (True, '')))
    monkeypatch.setattr(email_draft, 'reveal', lambda p: (opened.append(Path(p)) or (True, '')))
    monkeypatch.setitem(server.CONFIG, 'dest_dir', str(tmp_path))
    server._saved.clear()
    srv, port = server.find_free_port(8900, 100)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{port}'

    def get(path):
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.status, dict(r.headers), r.read()

    def post(path, data, headers=None):
        req = urllib.request.Request(base + path, data=data, method='POST', headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                return e.code, json.loads(body or b'{}')
            except ValueError:
                return e.code, {}

    yield type('FC', (), {'get': staticmethod(get), 'post': staticmethod(post), 'opened': opened, 'dest': tmp_path,
                          'port': port})
    srv.shutdown()
    srv.server_close()


# ── serving the page ─────────────────────────────────────────────────────

def test_the_page_and_its_code_are_served_with_types_a_browser_will_run(fc):
    code, h, body = fc.get('/')
    assert code == 200 and h['Content-Type'].startswith('text/html')
    assert b'OTDR Field Capture' in body
    for path in ('/app.js', '/fqa.js', '/labels.js', '/vendor/jszip.min.js', '/vendor/tesseract/tesseract.min.js'):
        code, h, _ = fc.get(path)
        assert code == 200 and h['Content-Type'].startswith('application/javascript'), path


def test_the_reader_language_data_is_not_sent_as_gzip_encoded(fc):
    """Tesseract fetches eng.traineddata.gz and un-gzips it itself.  A
    Content-Encoding: gzip header would make the browser un-gzip it first and
    the reader would then choke on plain bytes."""
    code, h, body = fc.get('/vendor/tesseract/lang/eng.traineddata.gz')
    assert code == 200 and 'Content-Encoding' not in h
    assert body[:2] == b'\x1f\x8b'


@pytest.mark.parametrize('path', ['/../app.py', '/%2e%2e/app.py', '/..%2fapp.py', '/vendor/../../server.py', '/nope.js'])
def test_nothing_outside_the_web_app_is_served(fc, path):
    with pytest.raises(urllib.error.HTTPError) as e:
        fc.get(path)
    assert e.value.code == 404


def test_the_blank_form_is_the_fqa_builders_own_template(fc):
    """One copy of Lumen's form in the repo: the FQA Builder's.  A fix to it
    (like the namespace-prefix repair) reaches this page too."""
    code, _, body = fc.get('/api/blank-form')
    assert code == 200
    assert body == (REPO_ROOT / 'fqa' / 'templates' / 'FQA_Site_Survey_v1_1.xlsm').read_bytes()


# ── saving and emailing ──────────────────────────────────────────────────

def test_save_writes_into_the_chosen_folder_and_never_overwrites(fc):
    c1, r1 = fc.post('/api/save?name=Span%204%20FQA.xlsm', b'first')
    c2, r2 = fc.post('/api/save?name=Span%204%20FQA.xlsm', b'second')
    assert c1 == c2 == 200
    assert Path(r1['path']) == fc.dest / 'Span 4 FQA.xlsm'
    assert Path(r2['path']) == fc.dest / 'Span 4 FQA (2).xlsm'
    assert Path(r1['path']).read_bytes() == b'first' and Path(r2['path']).read_bytes() == b'second'


def test_a_saved_name_cannot_climb_out_of_the_folder(fc):
    code, r = fc.post('/api/save?name=..%2F..%2Fevil.txt', b'x')
    assert code == 200
    p = Path(r['path'])
    assert p.parent == fc.dest and p.name == 'evil.txt.xlsm'


def test_email_opens_a_draft_with_the_saved_fqa_attached(fc):
    _, saved = fc.post('/api/save?name=FQA.xlsm', b'PK\x03\x04 workbook bytes')
    code, r = fc.post('/api/email', json.dumps({
        'path': saved['path'], 'to': 'office@example.com',
        'subject': 'FQA Site Survey section 1.2: Flagler to Bethune',
        'body': 'Section 1.2 filled in the field.\nLabel check: all match.'}).encode(),
        {'Content-Type': 'application/json'})
    assert code == 200 and r['opened'] is True
    eml = Path(r['eml'])
    assert eml == fc.dest / 'FQA.eml' and fc.opened == [eml]
    msg = email.message_from_bytes(eml.read_bytes(), policy=policy.default)
    assert msg['X-Unsent'] == '1'                       # Outlook opens it as a new draft
    assert msg['To'] == 'office@example.com'
    assert msg['Subject'] == 'FQA Site Survey section 1.2: Flagler to Bethune'
    assert 'Label check: all match.' in msg.get_body(('plain',)).get_content()
    (att,) = list(msg.iter_attachments())
    assert att.get_filename() == 'FQA.xlsm'
    assert att.get_content_type() == 'application/vnd.ms-excel.sheet.macroenabled.12'
    assert att.get_content() == b'PK\x03\x04 workbook bytes'


def test_email_refuses_a_file_this_page_did_not_save(fc, tmp_path):
    other = tmp_path / 'secret.xlsx'
    other.write_bytes(b'x')
    code, r = fc.post('/api/email', json.dumps({'path': str(other)}).encode(), {'Content-Type': 'application/json'})
    assert code == 400 and fc.opened == []


def test_a_cross_site_page_cannot_post(fc):
    code, _ = fc.post('/api/save?name=a.xlsm', b'x', {'Origin': 'https://evil.example'})
    assert code == 403
    assert not any(fc.dest.iterdir())


def test_a_subject_cannot_smuggle_in_extra_headers(tmp_path):
    f = tmp_path / 'a.xlsm'
    f.write_bytes(b'x')
    raw = email_draft.build_draft(f, 'a@b.com\r\nBcc: spy@evil.example', 'Hi\r\nBcc: spy@evil.example', 'body')
    msg = email.message_from_bytes(raw, policy=policy.default)
    assert msg['Bcc'] is None
    assert '\n' not in msg['Subject'] and '\n' not in msg['To']


# ── a refused page gets a 403 it can read ────────────────────────────────
#
# The refusal used to go out with the request's body still unread, and this
# server closes the connection after every answer.  Closing a socket that
# holds unread data resets the connection instead of ending it, and on Windows
# the reset can reach the client before it has read the answer: the refused
# page saw a dropped connection (WinError 10053) where the 403 should have
# been.  Which one it saw was down to whether the body arrived before or after
# the server had answered.
#
# The body is read and thrown away first now (Handler._refuse_foreign).  These
# tests pin the order, which does not depend on the machine: no answer until
# the body is in.  They also pin the two bounds on that read, and that nothing
# a foreign page sends is acted on or written.

FOREIGN = 'https://evil.example'
ROUNDS = 25
DO_POST = inspect.getsource(server.Handler.do_POST)
# Every route do_POST answers, read from the source so a route added later is
# held to the same refusal, and one it does not answer.
ROUTES = list(dict.fromkeys(re.findall(r"'(/api/[a-z_]+)'", DO_POST))) + ['/api/no_such_route']
# What do_POST calls once a request is let in, before anything is written.
ACTIONS = ('safe_name', 'default_dest', 'unique_path', '_body')


def foreign_status(port, path, body, origin=FOREIGN):
    """The status a page is given.  A dropped connection is not caught here:
    it is the failure, and its traceback says which kind."""
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=body, method='POST',
                                 headers={'Content-Type': 'application/json',
                                          **({'Origin': origin} if origin else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()
            return r.status
    except urllib.error.HTTPError as e:
        e.read()
        return e.code


def post_headers(port, path, declared):
    return (f'POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n'
            f'Origin: {FOREIGN}\r\nContent-Type: application/json\r\n'
            f'Content-Length: {declared}\r\n\r\n').encode('ascii')


def read_to_the_end(s):
    got = b''
    while True:
        part = s.recv(65536)
        if not part:
            return got
        got += part


def headers_then_body(port, path, body, declared=None, pause=0.5):
    """Send the headers, wait, then send the body: the order of arrival that
    lost the answer.  Returns (the server answered before the body was sent,
    everything it sent, the error met on the way or None)."""
    early, got, err = False, b'', None
    s = socket.create_connection(('127.0.0.1', port), timeout=60)
    try:
        s.sendall(post_headers(port, path, len(body) if declared is None else declared))
        early = bool(select.select([s], [], [], pause)[0])
        try:
            if body:
                s.sendall(body)
            got = read_to_the_end(s)
        except OSError as e:
            err = e
    finally:
        s.close()
    return early, got, err


@pytest.fixture()
def acted(fc, monkeypatch):
    """The calls a request got through to, by name.  Every route reads its
    body through Handler._body, and /api/save asks where to write before
    that, so a refusal that let a request in shows here as a call."""
    calls = []

    def record(owner, name):
        real = getattr(owner, name)

        def fn(*a, **k):
            calls.append(name)
            return real(*a, **k)
        monkeypatch.setattr(owner, name, fn)
    for name in ACTIONS:
        record(server.Handler if name == '_body' else server, name)
    return calls


def what_was_done(fc, acted):
    """(calls let through, files in the folder, files the server says it
    saved, files opened or mailed)."""
    return acted, sorted(f.name for f in fc.dest.iterdir()), sorted(server._saved), fc.opened


NOTHING = ([], [], [], [])


def test_the_lists_above_are_the_routes_and_what_they_do_first():
    assert ROUTES == ['/api/save', '/api/email', '/api/reveal', '/api/jserror', '/api/no_such_route']
    for route in ROUTES[1:-1]:
        at = DO_POST.index(f"'{route}'")
        nxt = DO_POST.index('self._body()', at)
        assert 'self._json(' not in DO_POST[at:nxt], route      # the body is read before any answer
    save = DO_POST[DO_POST.index("'/api/save'"):DO_POST.index('path.write_bytes(')]
    assert set(re.findall(r'(?<![.\w])([a-z_]+)\(', save)) - {'parse_qs'} == set(ACTIONS) - {'_body'}


def test_the_refusal_is_the_first_thing_a_post_meets():
    """One origin check, ahead of every route, and the refusal that reads the
    body first is the only one do_POST has."""
    assert DO_POST.count('self._origin_is_local()') == DO_POST.count('self._refuse_foreign()') == 1
    assert 'send_error(403' not in DO_POST
    assert DO_POST.index('self._refuse_foreign()') < DO_POST.index('u.path')


@pytest.mark.parametrize('path', ROUTES)
def test_the_refusal_waits_for_the_body(fc, acted, path):
    """No answer while the body is still to come, then a 403 read to a clean
    end.  The old refusal answered at once and fails the first line here on
    any machine."""
    early, got, err = headers_then_body(fc.port, path + '?name=a.xlsm', b'{"path": "a"}')
    assert not early, 'answered with the body still to come; the close then resets the connection'
    assert err is None, err
    assert got.startswith(b'HTTP/1.0 403 '), got[:80]
    assert what_was_done(fc, acted) == NOTHING


def test_a_cross_site_page_gets_its_403_on_every_route_every_time(fc, acted):
    for _ in range(ROUNDS):
        for path in ROUTES:
            assert foreign_status(fc.port, path + '?name=a.xlsm', b'{"path": "a"}') == 403, path
    assert what_was_done(fc, acted) == NOTHING


def test_a_body_bigger_than_the_sockets_hold_is_refused_cleanly(fc, acted):
    """4 MB does not fit in the socket buffers, so the old refusal closed
    while the page was still sending and no machine ever saw the 403."""
    for _ in range(3):
        assert foreign_status(fc.port, '/api/save?name=a.xlsm', b'x' * (4 * 1024 * 1024)) == 403
    assert what_was_done(fc, acted) == NOTHING


def test_the_pages_own_post_is_still_let_in(fc, acted):
    """The same request, refused only for its origin."""
    for n, origin in enumerate((None, f'http://127.0.0.1:{fc.port}', f'http://localhost:{fc.port}')):
        del acted[:]
        assert foreign_status(fc.port, f'/api/save?name=own{n}.xlsm', b'mine', origin=origin) == 200
        assert acted == list(ACTIONS)
        assert (fc.dest / f'own{n}.xlsm').read_bytes() == b'mine'


# The two bounds on the read.

def test_a_declared_length_past_the_upload_limit_is_not_read_or_waited_for(fc, acted, monkeypatch):
    """The length is the page's own word.  Past what any route takes, the 403
    goes out at once: with the wait set to two minutes, an answer inside the
    pause can only be one that did not wait."""
    monkeypatch.setattr(server, 'REFUSED_BODY_WAIT_S', 120.0)
    for declared in (server.MAX_UPLOAD + 1, 10 ** 15):
        early, got, err = headers_then_body(fc.port, '/api/save?name=a.xlsm', b'', declared=declared, pause=30)
        assert early and err is None and got.startswith(b'HTTP/1.0 403 '), (declared, err, got[:80])
    assert what_was_done(fc, acted) == NOTHING


@pytest.mark.parametrize('declared', ['-5', 'lots', ''])
def test_a_length_that_is_not_a_length_is_refused_the_same(fc, acted, declared, monkeypatch):
    monkeypatch.setattr(server, 'REFUSED_BODY_WAIT_S', 120.0)
    early, got, err = headers_then_body(fc.port, '/api/save?name=a.xlsm', b'', declared=declared, pause=30)
    assert early and err is None and got.startswith(b'HTTP/1.0 403 '), (err, got[:80])
    assert what_was_done(fc, acted) == NOTHING


def test_the_most_that_is_read_is_the_upload_limit_itself(fc, acted, monkeypatch):
    """A body of exactly MAX_UPLOAD is one the page itself could send, so it
    is read; one byte more is not.  The limit is made small here so the test
    does not send 300 MB."""
    limit = 64 * 1024
    monkeypatch.setattr(server, 'MAX_UPLOAD', limit)
    monkeypatch.setattr(server, 'REFUSED_BODY_WAIT_S', 120.0)
    early, got, err = headers_then_body(fc.port, '/api/save?name=a.xlsm', b'x' * limit)
    assert not early and err is None and got.startswith(b'HTTP/1.0 403 '), (early, err, got[:80])
    early, got, err = headers_then_body(fc.port, '/api/save?name=a.xlsm', b'', declared=limit + 1, pause=30)
    assert early and err is None and got.startswith(b'HTTP/1.0 403 '), (early, err, got[:80])
    assert what_was_done(fc, acted) == NOTHING


def test_a_page_that_stalls_is_answered_when_the_wait_is_up(fc, acted, monkeypatch):
    """A refused page that declares a body and never sends it gets its 403
    when the wait is up: not before, and not much after."""
    monkeypatch.setattr(server, 'REFUSED_BODY_WAIT_S', 0.5)
    t0 = time.monotonic()
    _early, got, err = headers_then_body(fc.port, '/api/save?name=a.xlsm', b'', declared=4096, pause=0)
    took = time.monotonic() - t0
    assert err is None and got.startswith(b'HTTP/1.0 403 '), (err, got[:80])
    assert 0.4 <= took < 20, took
    assert what_was_done(fc, acted) == NOTHING


def test_the_wait_is_for_the_whole_body_not_for_each_piece(fc, acted, monkeypatch):
    """A page that sends a byte every fifth of a second never lets a single
    read time out.  The wait is counted from the start, so it is cut off all
    the same, and not before the wait is up."""
    monkeypatch.setattr(server, 'REFUSED_BODY_WAIT_S', 1.0)
    s = socket.create_connection(('127.0.0.1', fc.port), timeout=60)
    try:
        t0 = time.monotonic()
        s.sendall(post_headers(fc.port, '/api/save?name=a.xlsm', 4096))
        answered = False
        while time.monotonic() - t0 < 20 and not answered:
            try:
                s.sendall(b' ')
            except OSError:
                answered = True                    # closed on us: it has answered
                break
            answered = bool(select.select([s], [], [], 0.2)[0])
        took = time.monotonic() - t0
        assert answered, 'still reading a trickle 20 s on'
        assert took >= 0.8, took
    finally:
        s.close()
    assert what_was_done(fc, acted) == NOTHING


def test_a_refused_page_that_stalls_does_not_hold_up_the_page(fc, monkeypatch):
    """Each request has a thread of its own.  While a refused page is being
    waited for, the page itself is served and its file is saved; the refused
    one still gets its 403 when its body is in."""
    monkeypatch.setattr(server, 'REFUSED_BODY_WAIT_S', 120.0)
    s = socket.create_connection(('127.0.0.1', fc.port), timeout=60)
    try:
        s.sendall(post_headers(fc.port, '/api/save?name=theirs.xlsm', 4096))
        assert not select.select([s], [], [], 0.3)[0]          # it is being waited for
        code, _, _ = fc.get('/api/config')
        assert code == 200
        code, r = fc.post('/api/save?name=mine.xlsm', b'mine')
        assert code == 200 and Path(r['path']).read_bytes() == b'mine'
        assert not select.select([s], [], [], 0)[0]            # and still is
        s.sendall(b'x' * 4096)
        assert read_to_the_end(s).startswith(b'HTTP/1.0 403 ')
    finally:
        s.close()
    assert [f.name for f in fc.dest.iterdir()] == ['mine.xlsm']
    assert server._saved == {str(fc.dest / 'mine.xlsm')}


# ── the hub page ─────────────────────────────────────────────────────────

def test_the_app_offers_field_capture_and_the_suite_does_not(monkeypatch):
    """The page belongs to OTDR Suite App (Robert 2026-09-28).  The App's
    launcher exports OTDR_SUITE_EDITION; the regular Suite's does not."""
    from conftest import run_streamlit
    monkeypatch.delenv('OTDR_SUITE_EDITION', raising=False)
    at = run_streamlit(default_timeout=180).run()
    tool = next(r for r in at.sidebar.radio if r.label == 'Tool')
    assert 'Field Capture' not in tool.options
    monkeypatch.setenv('OTDR_SUITE_EDITION', 'OTDR Suite App')
    at = run_streamlit(default_timeout=180).run()
    tool = next(r for r in at.sidebar.radio if r.label == 'Tool')
    at = tool.set_value('Field Capture').run()
    assert not at.exception
    assert any('Field Capture' in m.value for m in at.markdown)
    assert server._server is not None                    # the page started its server


# ── the web app and its packaging ────────────────────────────────────────

def test_every_file_the_page_loads_exists():
    html = (WEB / 'index.html').read_text(encoding='utf-8')
    refs = re.findall(r'(?:src|href)="([^"#?]+)"', html)
    assert refs
    for ref in refs:
        assert (WEB / ref).is_file(), ref


def test_the_label_reader_ships_complete():
    """Tesseract picks one of three cores at run time by what the browser
    supports; a missing one breaks label reading on exactly those PCs."""
    core = WEB / 'vendor' / 'tesseract' / 'core'
    for variant in ('lstm', 'simd-lstm', 'relaxedsimd-lstm'):
        assert (core / f'tesseract-core-{variant}.wasm.js').stat().st_size > 1_000_000, variant
    assert (WEB / 'vendor' / 'tesseract' / 'lang' / 'eng.traineddata.gz').stat().st_size > 1_000_000
    assert (WEB / 'vendor' / 'tesseract' / 'worker.min.js').is_file()


def test_both_builds_bundle_the_web_app_with_its_libraries():
    for spec in ('OTDRSuite.spec', 'OTDRSuite-mac.spec'):
        text = (REPO_ROOT / 'desktop' / spec).read_text(encoding='utf-8')
        m = re.search(r'_add_tree\("fieldcapture",\s*\(([^)]*)\)\)', text)
        assert m, spec
        exts = set(re.findall(r'"(\.[a-z]+)"', m.group(1)))
        assert {'.py', '.html', '.js', '.css', '.gz'} <= exts, (spec, exts)


def test_in_the_suite_the_page_uses_the_hub_server_not_a_phone_share_sheet():
    js = (WEB / 'app.js').read_text(encoding='utf-8')
    assert "get('host') === 'suite'" in js
    assert "SUITE ? 'api/blank-form'" in js           # the FQA Builder's template
    assert "fetch('api/save?name='" in js             # saved on the PC ...
    assert "postJson('api/email'" in js               # ... and handed to the mail program
    assert "if (!SUITE && 'serviceWorker' in navigator" in js   # no offline cache on the PC
