"""SharePoint, one folder, through the person's own sign-in (OTDR App).

sharepoint_link.py talks to SharePoint's REST API with the cookies of a
sign-in window.  Here a small HTTP server plays SharePoint over a folder tree
on disk (FakeSharePoint), so the whole path runs without the real site:
browse, the one-folder guard, a sign-in that has run out, download, and the
Quick Analysis Load Traces screen loading a span from it.  pywebview is never
started: the sign-in window's child process is replaced by a stand-in that
saves a session the way the window would."""
from __future__ import annotations

import http.server
import importlib.util
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.cookies import SimpleCookie

import pytest

from conftest import (FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, REPO_ROOT, run_streamlit,
                      go_tab)

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
import sharepoint_link as spl  # noqa: E402

LIB = '/sites/T/Shared Documents'
ROOT = LIB + '/Spans'
SPAN = 'SITEA-SITEB'                 # a span folder's name on SharePoint
LINK = 'https://contoso.sharepoint.com/sites/T/Shared%20Documents/Forms/AllItems.aspx?id=' \
       + urllib.parse.quote(ROOT, safe='')


class FakeSharePoint:
    """GetFolderByServerRelativePath(...)/Folders|Files and
    GetFileByServerRelativePath(...)/$value over <disk>/<server-relative path>.
    A request without the FedAuth cookie gets SharePoint's signed-out answer;
    anything under a folder called Private gets a plain 403."""

    def __init__(self, disk):
        self.disk = disk
        self.paths = []                  # every folder/file path asked for
        self.queue = []                  # (code, headers) to answer next, before anything else
        self.posts = []                  # every change asked for: (what, path)
        self.fail_post = None            # a POST name (StartUpload, ...) to answer 500
        self.uploads = {}                # uploadId -> bytes so far
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake._answer(self)

            def do_POST(self):
                fake._post(self)

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.site = f'http://127.0.0.1:{self.server.server_port}/sites/T'

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def _send(self, h, code, body=b'', ctype='application/json;odata=nometadata', headers=()):
        h.send_response(code)
        h.send_header('Content-Type', ctype)
        h.send_header('Content-Length', str(len(body)))
        for k, v in headers:
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(body)

    def _answer(self, h):
        parts = urllib.parse.urlsplit(h.path)
        if 'FedAuth=good' not in (h.headers.get('Cookie') or ''):
            return self._send(h, 403, b'{}', headers=[(
                'X-MSDAVEXT_Error', '917656; Access denied. Before opening files in this '
                'location, you must first browse to the web site.')])
        if self.queue:
            code, headers = self.queue.pop(0)
            return self._send(h, code, b'', headers=headers)
        a = urllib.parse.parse_qs(parts.query)['@a'][0]
        assert a.startswith("'") and a.endswith("'")
        path = a[1:-1].replace("''", "'")
        self.paths.append(path)
        if '/Private' in path:
            return self._send(h, 403, b'{}')
        local = os.path.join(self.disk, *path.strip('/').split('/'))
        what = urllib.parse.unquote(parts.path).rsplit('/', 1)[-1]
        if what == '$value':
            with open(local, 'rb') as fh:
                return self._send(h, 200, fh.read(), 'application/octet-stream')
        if not os.path.isdir(local):
            return self._send(h, 404, b'{}')

        def stamp(p):
            return datetime.fromtimestamp(os.stat(p).st_mtime, timezone.utc).strftime(
                '%Y-%m-%dT%H:%M:%SZ')
        rows = []
        for name in sorted(os.listdir(local)):
            full = os.path.join(local, name)
            if what == 'Folders' and os.path.isdir(full):
                rows.append({'Name': name, 'ServerRelativeUrl': path + '/' + name,
                             'ItemCount': len(os.listdir(full)), 'TimeLastModified': stamp(full)})
            elif what == 'Files' and os.path.isfile(full):
                rows.append({'Name': name, 'ServerRelativeUrl': path + '/' + name,
                             'Length': str(os.path.getsize(full)),  # SharePoint sends a string
                             'TimeLastModified': stamp(full)})
        self._send(h, 200, json.dumps({'value': rows}).encode('utf-8'),
                   'application/json;odata=nometadata;streaming=true;charset=utf-8')

    @staticmethod
    def _lit(q, k):
        a = q[k][0]
        assert a.startswith("'") and a.endswith("'")
        return a[1:-1].replace("''", "'")

    def _post(self, h):
        """contextinfo, Files/AddUsingPath, Start/Continue/Finish/CancelUpload
        and recycle, the calls the App's Save to SharePoint makes.  A change
        without the digest contextinfo gave is refused, as SharePoint does."""
        parts = urllib.parse.urlsplit(h.path)
        body = h.rfile.read(int(h.headers.get('Content-Length') or 0))
        if 'FedAuth=good' not in (h.headers.get('Cookie') or ''):
            return self._send(h, 403, b'{}', headers=[('X-MSDAVEXT_Error', '917656; denied')])
        call = urllib.parse.unquote(parts.path).rsplit('/', 1)[-1]
        if call == 'contextinfo':
            return self._send(h, 200, json.dumps({'FormDigestValue': 'dig'}).encode())
        if h.headers.get('X-RequestDigest') != 'dig':
            return self._send(h, 403, b'{"error":"The security validation for this page is '
                              b'invalid."}')
        q = urllib.parse.parse_qs(parts.query)
        path = self._lit(q, '@a')
        name = call.split('(', 1)[0]
        self.posts.append((name, path))
        if name == self.fail_post:
            return self._send(h, 500, b'{}')
        if '/Private' in path:
            return self._send(h, 403, b'{}')
        uid = re.search(r"uploadId=guid'([^']+)'", call)
        uid = uid and uid.group(1)
        off = re.search(r'fileOffset=(\d+)', call)
        off = off and int(off.group(1))
        if name == 'AddUsingPath':
            assert 'overwrite=false' in call
            path = path + '/' + self._lit(q, '@n')
            local = os.path.join(self.disk, *path.strip('/').split('/'))
            if os.path.exists(local):
                return self._send(h, 400, b'{"error":"already exists"}')
            os.makedirs(os.path.dirname(local), exist_ok=True)
            with open(local, 'wb') as fh:
                fh.write(body)
            return self._send(h, 200, b'{}')
        local = os.path.join(self.disk, *path.strip('/').split('/'))
        if name == 'StartUpload':
            self.uploads[uid] = body
        elif name in ('ContinueUpload', 'FinishUpload'):
            assert off == len(self.uploads[uid])
            self.uploads[uid] += body
            if name == 'FinishUpload':
                with open(local, 'wb') as fh:
                    fh.write(self.uploads.pop(uid))
        elif name == 'CancelUpload':
            self.uploads.pop(uid, None)
        elif name == 'recycle':
            os.remove(local)
        else:
            return self._send(h, 404, b'{}')
        return self._send(h, 200, b'{}')


def _put(disk, path, data=b'x'):
    full = os.path.join(disk, *path.strip('/').split('/'))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'wb') as fh:
        fh.write(data)
    return full


@pytest.fixture
def settings_dir(tmp_path, monkeypatch):
    d = tmp_path / 'settings'
    d.mkdir()
    monkeypatch.setenv('OTDR_SETTINGS_DIR', str(d))
    monkeypatch.setenv('OTDR_TEST_HOME', '1')
    return d


@pytest.fixture
def sp(tmp_path):
    disk = str(tmp_path / 'sharepoint')
    span = os.path.join(disk, *(ROOT + '/' + SPAN).strip('/').split('/'))
    shutil.copytree(FIXTURE_SPLICE_A_DIR, os.path.join(span, 'A'))
    shutil.copytree(FIXTURE_SPLICE_B_DIR, os.path.join(span, 'B'))
    _put(disk, ROOT + '/' + SPAN + '/notes.pdf', b'%PDF')
    _put(disk, ROOT + "/O'Neil & Sons #2/x.sor", b'sor')
    _put(disk, ROOT + '/Private/y.sor', b'sor')
    _put(disk, LIB + '/Other/secret.sor', b'secret')
    os.makedirs(os.path.join(disk, *(LIB + '/Forms').strip('/').split('/')))
    fake = FakeSharePoint(disk)
    yield fake
    fake.close()


def _session(fake, cookie='good', root=ROOT):
    return {'v': 1, 'link': LINK, 'site_url': fake.site, 'library': LIB, 'root': root,
            'user': 'Test Person', 'login': 'test@contoso.com', 'ua': 'test',
            'cookies': {'FedAuth': cookie, 'rtFa': 'x'}, 'saved': time.time()}


# ── Links and paths ──────────────────────────────────────────────────────
@pytest.mark.parametrize('url, want', [
    (LINK, ROOT),
    ('https://contoso.sharepoint.com/sites/T/Shared%20Documents/Spans', ROOT),
    ('https://contoso.sharepoint.com/sites/T/Shared%20Documents/Forms/AllItems.aspx', LIB),
    ('https://contoso.sharepoint.com/sites/T/Lists/X/AllItems.aspx?RootFolder=%2Fsites%2FT%2FA',
     '/sites/T/A'),
    ('https://contoso.sharepoint.com/:f:/s/T/EabcDEF?e=xyz', None),
    ('https://contoso.sharepoint.com/sites/T/SitePages/Home.aspx', None),
    ('https://contoso.sharepoint.com/', None),
])
def test_the_folder_a_sharepoint_address_shows(url, want):
    assert spl.folder_from_url(url) == want


def test_only_sharepoint_links_are_taken():
    assert spl.is_sharepoint_link(LINK)
    assert not spl.is_sharepoint_link('http://contoso.sharepoint.com/x')      # not https
    assert not spl.is_sharepoint_link('https://contoso.sharepoint.com.evil.test/x')
    assert not spl.is_sharepoint_link('https://example.com/x')
    assert not spl.is_sharepoint_link('')


def test_inside_is_the_folder_and_below_never_beside_or_above():
    assert spl.inside(ROOT, ROOT)
    assert spl.inside(ROOT + '/Span 7/A', ROOT)
    assert spl.inside(ROOT.upper() + '/a', ROOT)                # SharePoint ignores case
    assert not spl.inside(ROOT + ' 2', ROOT)                    # a sibling with a longer name
    assert not spl.inside(LIB, ROOT)
    assert not spl.inside(ROOT + '/../Other', ROOT)
    assert not spl.inside('/', ROOT)


def test_crumbs_run_from_the_top_folder_down():
    assert spl.crumbs(ROOT + '/Span 7/A', ROOT) == [
        ('Spans', ROOT), ('Span 7', ROOT + '/Span 7'), ('A', ROOT + '/Span 7/A')]
    assert spl.crumbs(ROOT, ROOT) == [('Spans', ROOT)]


def test_the_local_copy_is_named_after_the_folder_and_kept_apart_by_path(settings_dir):
    a = spl.local_folder(ROOT + '/Span 7')
    b = spl.local_folder(ROOT + '/Old/Span 7')
    assert os.path.basename(a) == os.path.basename(b) == 'Span 7'
    assert a != b and a.startswith(str(settings_dir))
    assert os.path.basename(spl.local_folder(ROOT + '/a:b')) == 'a_b'


# ── The saved sign-in ────────────────────────────────────────────────────
def test_the_session_round_trips_and_sign_out_forgets_it(settings_dir):
    sess = {'link': LINK, 'site_url': 'https://contoso.sharepoint.com/sites/T', 'root': ROOT,
            'cookies': {'FedAuth': 'SECRETVALUE'}}
    spl.save_session(sess)
    assert spl.load_session() == sess
    os.makedirs(spl.webview_dir())
    spl.forget_signin()
    assert spl.load_session() is None and not os.path.exists(spl.webview_dir())


def test_a_damaged_or_foreign_session_file_reads_as_signed_out(settings_dir):
    with open(spl.session_path(), 'wb') as fh:
        fh.write(b'nonsense')
    assert spl.load_session() is None
    with open(spl.session_path(), 'wb') as fh:
        fh.write(b'PLAIN1\n' + json.dumps({'link': LINK}).encode())   # no cookies
    assert spl.load_session() is None


@pytest.mark.skipif(os.name != 'nt', reason='DPAPI is Windows only')
def test_on_windows_the_cookies_are_encrypted_for_this_user(settings_dir):
    sess = {'link': LINK, 'site_url': 'https://contoso.sharepoint.com/sites/T', 'root': ROOT,
            'cookies': {'FedAuth': 'SECRETVALUE'}}
    spl.save_session(sess)
    with open(spl.session_path(), 'rb') as fh:
        raw = fh.read()
    assert raw.startswith(b'DPAPI1\n') and b'SECRETVALUE' not in raw
    assert spl.load_session() == sess


# ── What the sign-in window keeps ────────────────────────────────────────
def _cookies(**kv):
    out = []
    for k, v in kv.items():
        c = SimpleCookie()
        c[k] = v
        out.append(c)
    return out


def test_the_window_keeps_the_folder_it_landed_on_and_its_cookies():
    ctx = {'web': 'https://contoso.sharepoint.com/sites/T/', 'list': LIB, 'user': 'Test Person',
           'login': 't@contoso.com', 'ua': 'UA', 'href': LINK + '&p=true'}
    sess = spl.session_from('https://contoso.sharepoint.com/:f:/s/T/Eabc', ctx,
                            _cookies(FedAuth='f', rtFa='r'))
    assert sess['root'] == ROOT                       # from where the sharing link led
    assert sess['site_url'] == 'https://contoso.sharepoint.com/sites/T'
    assert sess['cookies'] == {'FedAuth': 'f', 'rtFa': 'r'}
    assert sess['user'] == 'Test Person'


def test_a_library_page_falls_back_to_the_library_and_no_cookies_is_an_error():
    ctx = {'web': 'https://contoso.sharepoint.com/sites/T', 'list': LIB,
           'href': 'https://contoso.sharepoint.com/sites/T/SitePages/Home.aspx'}
    assert spl.session_from(ctx['href'], ctx, _cookies(FedAuth='f'))['root'] == LIB
    with pytest.raises(spl.SharePointError):
        spl.session_from(ctx['href'], ctx, [])
    with pytest.raises(spl.SharePointError, match='does not open a folder'):
        spl.session_from(ctx['href'], dict(ctx, list=''), _cookies(FedAuth='f'))


def test_signin_refuses_a_link_that_is_not_sharepoint(settings_dir):
    assert spl.signin_main(['--sharepoint-signin', 'https://example.com/x']) == 3
    assert spl.read_status() == {'ok': False, 'message': 'That is not a SharePoint link.'}


# ── Talking to SharePoint ────────────────────────────────────────────────
def test_a_folder_lists_its_folders_and_files(sp):
    c = spl.Client(_session(sp))
    top = c.folder()
    assert [d['name'] for d in top['folders']] == ["O'Neil & Sons #2", 'Private', SPAN]   # by name
    span = c.folder(ROOT + '/' + SPAN)
    assert [d['name'] for d in span['folders']] == ['A', 'B']
    assert [f['name'] for f in span['files']] == ['notes.pdf'] and span['files'][0]['size'] == 4
    odd = c.folder(ROOT + "/O'Neil & Sons #2")               # quotes, & and # survive
    assert [f['name'] for f in odd['files']] == ['x.sor']


def test_the_library_top_hides_its_forms_folder(sp):
    c = spl.Client(_session(sp, root=LIB))
    assert [d['name'] for d in c.folder()['folders']] == ['Other', 'Spans']


def test_nothing_outside_the_folder_is_ever_asked_for(sp):
    c = spl.Client(_session(sp))
    for path in (LIB + '/Other', LIB, ROOT + '/../Other', '/sites/T'):
        with pytest.raises(spl.OutsideFolder):
            c.folder(path)
    with pytest.raises(spl.OutsideFolder):
        c.download({'path': LIB + '/Other/secret.sor', 'name': 'secret.sor'}, 'x')
    assert sp.paths == []


def test_a_run_out_sign_in_says_so(sp):
    with pytest.raises(spl.NeedsSignIn):
        spl.Client(_session(sp, cookie='stale')).folder()


def test_a_sign_in_page_bounce_is_a_run_out_sign_in_too(sp):
    sp.queue.append((302, [('Location', 'https://login.microsoftonline.com/x')]))
    with pytest.raises(spl.NeedsSignIn):
        spl.Client(_session(sp)).folder()


def test_a_folder_this_account_cannot_open_is_not_a_sign_in_problem(sp):
    with pytest.raises(spl.NoAccess):
        spl.Client(_session(sp)).folder(ROOT + '/Private')


def test_busy_sharepoint_is_waited_for(sp):
    waits = []
    sp.queue += [(429, [('Retry-After', '2')]), (503, [])]
    c = spl.Client(_session(sp), sleep=waits.append)
    assert c.folder()['name'] == 'Spans'
    assert waits == [2, 5]


def test_walk_finds_the_traces_all_the_way_down_and_nothing_else(sp):
    files = spl.Client(_session(sp)).walk(ROOT + '/' + SPAN)
    rels = [f['rel'] for f in files]
    assert len(rels) == len(os.listdir(FIXTURE_SPLICE_A_DIR)) + len(os.listdir(FIXTURE_SPLICE_B_DIR))
    assert all(r.startswith(('A/', 'B/')) for r in rels) and 'notes.pdf' not in rels


def test_walk_takes_trc_shots(sp):
    """An EXFO .trc span on SharePoint is a span like any other: its shots
    are walked and copied, not skipped."""
    _put(sp.disk, ROOT + '/' + SPAN + '/C/SITEASITEB0001_155016251310.trc', b'trc')
    rels = [f['rel'] for f in spl.Client(_session(sp)).walk(ROOT + '/' + SPAN)]
    assert 'C/SITEASITEB0001_155016251310.trc' in rels


def test_fetch_downloads_once_then_keeps_what_is_unchanged(sp, settings_dir):
    c = spl.Client(_session(sp))
    files = c.walk(ROOT + '/' + SPAN)
    dest = spl.local_folder(ROOT + '/' + SPAN)
    seen = []
    got, kept = spl.fetch(c, files, dest, lambda done, total, name: seen.append((done, total)))
    assert (got, kept) == (len(files), 0)
    assert seen[-1][0] == seen[-1][1] == sum(f['size'] for f in files)
    one = os.path.join(dest, 'A', os.listdir(FIXTURE_SPLICE_A_DIR)[0])
    with open(one, 'rb') as fh, open(FIXTURE_SPLICE_A_DIR / os.path.basename(one), 'rb') as ref:
        assert fh.read() == ref.read()
    assert not [p for p in os.listdir(os.path.join(dest, 'A')) if p.endswith('.part')]
    assert spl.fetch(c, files, dest) == (0, len(files))


# ── The launcher's sign-in role ──────────────────────────────────────────
def test_the_launcher_runs_the_sign_in_window_before_anything_else(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    monkeypatch.delenv('OTDR_SUITE_HOME', raising=False)
    spec = importlib.util.spec_from_file_location('launcher_sp', str(REPO_ROOT / 'desktop' / 'launcher.py'))
    L = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(L)
    calls = []
    monkeypatch.setattr(L, '_redirect_output_to_log', lambda: None)
    monkeypatch.setattr(L, '_maybe_run_window', lambda: pytest.fail('window role ran'))
    monkeypatch.setattr(spl, 'signin_main', lambda argv: calls.append(argv) or 0)

    def exit_(code):
        raise SystemExit(code)
    monkeypatch.setattr(L.os, '_exit', exit_)
    monkeypatch.setattr(sys, 'argv', ['OTDRSuite.exe', L.SHAREPOINT_SIGNIN_ARG, LINK])
    with pytest.raises(SystemExit) as out:
        L.main()
    assert out.value.code == 0
    assert calls == [[L.SHAREPOINT_SIGNIN_ARG, LINK]]
    assert L.SHAREPOINT_SIGNIN_ARG == spl.SIGNIN_ARG


def test_both_specs_bundle_the_module_and_updates_carry_it(tmp_path, monkeypatch):
    for spec in ('OTDRSuite.spec', 'OTDRSuite-mac.spec'):
        text = (REPO_ROOT / 'desktop' / spec).read_text(encoding='utf-8')
        assert '"sharepoint_link.py"' in text, spec
    # app.py imports it, so an update that ships app.py must ship it too.
    monkeypatch.setenv('HOME', str(tmp_path))
    spec = importlib.util.spec_from_file_location('launcher_files', str(REPO_ROOT / 'desktop' / 'launcher.py'))
    L = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(L)
    assert 'sharepoint_link.py' in L.ENGINE_FILES


def test_a_sign_in_popup_stays_in_the_app_not_the_system_browser(monkeypatch):
    import types
    sent = []

    class EdgeChrome:
        def on_new_window_request(self, sender, args):
            sent.append(str(args.get_Uri()))          # pywebview: off to the system browser

    platforms = types.ModuleType('webview.platforms')
    platforms.edgechromium = types.SimpleNamespace(EdgeChrome=EdgeChrome)
    monkeypatch.setitem(sys.modules, 'webview', types.ModuleType('webview'))
    monkeypatch.setitem(sys.modules, 'webview.platforms', platforms)
    spl._keep_popups_in_window()

    class Args:
        def __init__(self, uri):
            self.uri = uri

        def get_Uri(self):
            return self.uri
    EdgeChrome().on_new_window_request(None, Args('https://login.microsoftonline.com/common/x'))
    EdgeChrome().on_new_window_request(None, Args('mailto:help@contoso.com'))
    assert sent == ['mailto:help@contoso.com']


# ── Quick Analysis: From SharePoint ──────────────────────────────────────
def _load_screen():
    """Quick Analysis: From SharePoint sits under the A and B boxes
    (2026-09-30), on the Traces tab (the left panel until 2026-10-01)."""
    at = run_streamlit().run()
    at.button(key='home_traces').click().run()
    assert not at.exception, list(at.exception)
    go_tab(at, 'Traces')
    assert not at.exception, list(at.exception)
    assert any(e.label == '☁️ From SharePoint' for e in at.main.expander)
    return at


def _set_link(settings_dir, link=LINK):
    path = settings_dir / 'settings.json'
    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    data['sharepoint_link'] = link
    path.write_text(json.dumps(data), encoding='utf-8')


def _button(at, label):
    hits = [b for b in at.button if b.label == label]
    assert hits, [b.label for b in at.button]
    return hits[0]


def test_the_folder_link_is_asked_for_once_and_must_be_sharepoint(settings_dir):
    at = _load_screen()
    at.text_input(key='sp_link_input').set_value('https://example.com/x').run()
    at.button(key='sp_link_save').click().run()
    assert any('not a SharePoint link' in e.value for e in at.error)
    at.text_input(key='sp_link_input').set_value(LINK).run()
    at.button(key='sp_link_save').click().run()
    assert not at.exception, list(at.exception)
    saved = json.loads((settings_dir / 'settings.json').read_text(encoding='utf-8'))
    assert saved['sharepoint_link'] == LINK
    assert 'sp_signin' in {b.key for b in at.button}


def test_sign_in_opens_the_window_and_then_the_folder_shows(settings_dir, sp, monkeypatch):
    import subprocess
    _set_link(settings_dir)
    started = []

    class Window:                      # the sign-in child process, as the window leaves it
        def __init__(self, cmd, **kw):
            started.append(cmd)
            spl.save_session(_session(sp))
            spl.write_status(True)

        def wait(self, timeout=None):
            return 0
    monkeypatch.setattr(subprocess, 'Popen', Window)
    at = _load_screen()
    at.button(key='sp_signin').click().run()
    assert not at.exception, list(at.exception)
    assert started and started[0][-2:] == [spl.SIGNIN_ARG, LINK]
    assert any('Signed in as Test Person' in s.value for s in at.success)
    assert {'📁 ' + SPAN, "📁 O'Neil & Sons #2"} <= {b.label for b in at.button}


def test_a_run_out_sign_in_goes_back_to_the_sign_in_button(settings_dir, sp):
    _set_link(settings_dir)
    spl.save_session(_session(sp, cookie='stale'))
    at = _load_screen()
    assert not at.exception, list(at.exception)
    assert any('run out' in w.value for w in at.warning)
    assert 'sp_signin' in {b.key for b in at.button} and spl.load_session() is None


def test_a_span_loads_straight_from_the_sharepoint_folder(settings_dir, sp):
    _set_link(settings_dir)
    spl.save_session(_session(sp))
    at = _load_screen()
    _button(at, '📁 ' + SPAN).click().run()
    assert at.session_state['sp_path'] == ROOT + '/' + SPAN
    assert {'📁 A', '📁 B'} <= {b.label for b in at.button}
    at.button(key='sp_load').click().run()
    assert not at.exception, list(at.exception)
    # The Traces tab's A and B boxes take the copied folders.
    ends = at.session_state['span_loaded']
    assert at.session_state['view_dir_a_input'] == ends['dir_a']
    assert at.session_state['view_dir_b_input'] == ends['dir_b']
    # The panel's heading names both ends, as the traces carry them.
    assert ends['ila_a'] not in ('', 'A') and ends['ila_b'] not in ('', 'B')
    assert any(ends['ila_a'] in m.value and ends['ila_b'] in m.value for m in at.markdown)
    copy = spl.local_folder(ROOT + '/' + SPAN)
    assert sorted(os.listdir(copy)) == ['A', 'B']                  # the pdf stayed on SharePoint
    assert not [p for p in sp.paths if not spl.inside(p, ROOT)]


def test_up_goes_back_but_never_above_the_folder(settings_dir, sp):
    _set_link(settings_dir)
    spl.save_session(_session(sp))
    at = _load_screen()
    assert at.button(key='sp_up').disabled                      # already at the top
    _button(at, '📁 ' + SPAN).click().run()
    at.button(key='sp_up').click().run()
    assert at.session_state['sp_path'] == ROOT
    at.session_state['sp_path'] = LIB + '/Other'                # a stale or doctored path
    at.run()
    assert not at.exception, list(at.exception)
    assert '📁 ' + SPAN in {b.label for b in at.button}
    assert not [p for p in sp.paths if not spl.inside(p, ROOT)]


def test_sign_out_forgets_the_sign_in(settings_dir, sp):
    _set_link(settings_dir)
    spl.save_session(_session(sp))
    at = _load_screen()
    at.button(key='sp_signout').click().run()
    assert spl.load_session() is None
    assert 'sp_signin' in {b.key for b in at.button}


# ── New Project: the same folder, remembered by the project ─────────────
def _setup_screen():
    at = run_streamlit().run()
    at.button(key='home_new').click().run()
    assert not at.exception, list(at.exception)
    assert any(e.label == '☁️ From SharePoint' for e in at.expander)
    return at


def test_new_project_takes_its_traces_from_sharepoint_and_keeps_the_folder(
        settings_dir, sp, tmp_path):
    _set_link(settings_dir)
    spl.save_session(_session(sp))
    at = _setup_screen()
    _button(at, '📁 ' + SPAN).click().run()
    at.button(key='sp_load').click().run()
    assert not at.exception, list(at.exception)
    # The download is the page's One folder box, both directions read from it.
    assert at.session_state['setup_tr_one'] == spl.local_folder(ROOT + '/' + SPAN)
    assert any('A ' in s.value and 'fibers' in s.value for s in at.success)
    at.text_input(key='setup_parent').set_value(str(tmp_path / 'projects')).run()
    at.selectbox(key='setup_customer').select_index(0).run()
    at.button(key='setup_create').click().run()
    assert not at.exception, list(at.exception)
    pfile = at.session_state['project_path']
    data = json.loads(open(pfile, encoding='utf-8').read())
    assert data['sharepoint'] == {'link': LINK, 'path': ROOT + '/' + SPAN}


def test_an_existing_project_opens_on_its_folder_and_only_asks_to_sign_in(
        settings_dir, sp, tmp_path):
    _set_link(settings_dir)
    spl.save_session(_session(sp))
    at = _setup_screen()
    _button(at, '📁 ' + SPAN).click().run()
    at.button(key='sp_load').click().run()
    at.text_input(key='setup_parent').set_value(str(tmp_path / 'projects')).run()
    at.selectbox(key='setup_customer').select_index(0).run()
    at.button(key='setup_create').click().run()
    pfile = at.session_state['project_path']
    # Another PC: no saved link, no sign-in.  The project brings its folder.
    (settings_dir / 'settings.json').write_text('{}', encoding='utf-8')
    spl.forget_signin()
    at = run_streamlit().run()
    at.session_state['_setup_open'] = os.path.dirname(pfile)
    at.run()
    at.session_state['nav_radio'] = 'Project Status'
    at.run()
    assert not at.exception, list(at.exception)
    keys = {b.key for b in at.button}
    assert 'sp_signin' in keys and 'sp_link_save' not in keys
    spl.save_session(_session(sp))                              # the Microsoft sign-in
    at.run()
    assert at.session_state['sp_path'] == ROOT + '/' + SPAN
    assert {'📁 A', '📁 B'} <= {b.label for b in at.button}
    # A load there fills the new shoot's One folder box.
    at.button(key='sp_load').click().run()
    assert not at.exception, list(at.exception)
    assert at.session_state['ps_tr_one'] == spl.local_folder(ROOT + '/' + SPAN)
    # A load there fills the new shoot's One folder box.
    at.button(key='sp_load').click().run()
    assert not at.exception, list(at.exception)
    assert at.session_state['ps_tr_one'] == spl.local_folder(ROOT + '/' + SPAN)


# ── Saving to SharePoint, through the same sign-in ───────────────────────
def _disk(fake, path):
    return os.path.join(fake.disk, *path.strip('/').split('/'))


def test_a_file_goes_up_into_the_folder_with_the_digest(sp, tmp_path):
    c = spl.Client(_session(sp))
    src = tmp_path / 'P (2026-09-30).zdb'
    src.write_bytes(b'zdb' * 100)
    got = c.upload(str(src), ROOT + '/' + SPAN)
    assert got == ROOT + '/' + SPAN + '/P (2026-09-30).zdb'
    assert open(_disk(sp, got), 'rb').read() == b'zdb' * 100
    # A name already there is never replaced: free_name picks the next one.
    assert c.free_name(ROOT + '/' + SPAN, src.name) == 'P (2026-09-30) 2.zdb'
    with pytest.raises(spl.SharePointError):
        c.upload(str(src), ROOT + '/' + SPAN)
    assert open(_disk(sp, got), 'rb').read() == b'zdb' * 100


def test_a_big_file_goes_up_in_pieces(sp, tmp_path, monkeypatch):
    monkeypatch.setattr(spl, 'UPLOAD_CHUNK', 1000)
    data = os.urandom(3500)
    src = tmp_path / 'big.zdb'
    src.write_bytes(data)
    seen = []
    got = spl.Client(_session(sp)).upload(str(src), ROOT, progress=seen.append)
    assert open(_disk(sp, got), 'rb').read() == data
    assert [n for n, _p in sp.posts] == ['AddUsingPath', 'StartUpload', 'ContinueUpload',
                                         'ContinueUpload', 'FinishUpload']
    assert sum(seen) == 3500


def test_a_failed_upload_leaves_no_half_file(sp, tmp_path, monkeypatch):
    monkeypatch.setattr(spl, 'UPLOAD_CHUNK', 1000)
    src = tmp_path / 'big.zdb'
    src.write_bytes(os.urandom(3500))
    sp.fail_post = 'ContinueUpload'
    with pytest.raises(spl.SharePointError):
        spl.Client(_session(sp)).upload(str(src), ROOT)
    assert not os.path.exists(_disk(sp, ROOT + '/big.zdb'))
    assert [n for n, _p in sp.posts][-2:] == ['CancelUpload', 'recycle']


def test_saving_stays_inside_the_folder_and_says_when_it_may_not(sp, tmp_path):
    c = spl.Client(_session(sp))
    src = tmp_path / 'x.zdb'
    src.write_bytes(b'x')
    with pytest.raises(spl.OutsideFolder):
        c.upload(str(src), LIB + '/Other')
    with pytest.raises(spl.NoAccess, match='cannot save'):
        c.upload(str(src), ROOT + '/Private')
    with pytest.raises(spl.NeedsSignIn):
        spl.Client(_session(sp, cookie='stale')).upload(str(src), ROOT)
    assert not [p for _n, p in sp.posts if not spl.inside(p, ROOT)]


def _project_on_sharepoint(settings_dir, sp, tmp_path):
    """A project made from the SharePoint span (as the New Project test)."""
    _set_link(settings_dir)
    spl.save_session(_session(sp))
    at = _setup_screen()
    _button(at, '📁 ' + SPAN).click().run()
    at.button(key='sp_load').click().run()
    at.text_input(key='setup_parent').set_value(str(tmp_path / 'projects')).run()
    at.selectbox(key='setup_customer').select_index(0).run()
    at.button(key='setup_create').click().run()
    assert not at.exception, list(at.exception)
    pfile = at.session_state['project_path']
    at = run_streamlit().run()
    at.session_state['_setup_open'] = os.path.dirname(pfile)
    at.run()
    at.session_state['nav_radio'] = 'Project Status'
    at.run()
    assert not at.exception, list(at.exception)
    return at


def test_a_project_saves_to_sharepoint_and_opens_from_it_on_another_pc(
        settings_dir, sp, tmp_path):
    at = _project_on_sharepoint(settings_dir, sp, tmp_path)
    pfile = at.session_state['project_path']
    name = os.path.basename(os.path.dirname(pfile))
    # Save to SharePoint starts in the project's own SharePoint folder.
    assert at.session_state['spx_path'] == ROOT + '/' + SPAN
    _button(at, '⬆ Up')                                        # the picker is drawn
    at.button(key='spx_up').click().run()
    assert at.session_state['spx_path'] == ROOT
    at.button(key='spx_save').click().run()
    assert not at.exception, list(at.exception)
    ups = [f for f in os.listdir(_disk(sp, ROOT)) if f.endswith('.zdb')]
    assert len(ups) == 1 and ups[0].startswith(name + ' (')
    assert any('Saved' in s.value and ups[0] in s.value for s in at.success)
    data = json.loads(open(pfile, encoding='utf-8').read())
    assert data['sharepoint']['save'] == ROOT
    assert data['sharepoint']['path'] == ROOT + '/' + SPAN
    # Twice: a second file, the first untouched.
    at.button(key='spx_save').click().run()
    assert len([f for f in os.listdir(_disk(sp, ROOT)) if f.endswith('.zdb')]) == 2
    assert not [p for _n, p in sp.posts if not spl.inside(p, ROOT)]
    # Another PC: Open a Project, From SharePoint, the .zdb.
    shutil.rmtree(os.path.dirname(pfile))
    at = run_streamlit().run()
    at.button(key='home_open_recent').click().run()
    assert not at.exception, list(at.exception)
    hits = [b for b in at.button if b.label.startswith('📦 ' + ups[0] + ' · ')]
    assert len(hits) == 1, [b.label for b in at.button]
    hits[0].click().run()
    assert not at.exception, list(at.exception)
    opened = at.session_state['project_path']
    assert os.path.basename(os.path.dirname(opened)).startswith(name)
    assert os.path.isfile(opened)


def test_save_to_sharepoint_asks_to_sign_in_first(settings_dir, sp, tmp_path):
    at = _project_on_sharepoint(settings_dir, sp, tmp_path)
    spl.forget_signin()
    at.run()
    assert not at.exception, list(at.exception)
    assert 'spx_signin' in {b.key for b in at.button}
    assert 'spx_save' not in {b.key for b in at.button}
