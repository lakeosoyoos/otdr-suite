"""SharePoint for the OTDR Suite App: ONE folder, through the person's own sign-in.

Robert, 2026-09-29: the App opens one SharePoint folder, and the folders
inside it, for people we know and trust, with no app registration and no
secret in the exe.  The person signs in the normal Microsoft way in a window
of the App's own browser engine (WebView2, with a profile of its own under the
App's folder, so "Stay signed in" carries over).  Once that window reaches the
folder, the App keeps the window's SharePoint session cookies and reads the
folder with SharePoint's REST API, exactly as the SharePoint page itself does:
nobody can do anything here that they could not do in the page.

- The folder is the one the pasted link names.  Every request is checked
  against it (inside()): the App never asks for anything above or beside it.
- The cookies are kept for this Windows user only (DPAPI), never in
  settings.json and never in a log.  Off Windows (a dev Mac) the file is plain
  JSON that only its owner can read.
- A sign-in that has run out answers 403 "917656" (or 401, or a bounce to the
  sign-in page): NeedsSignIn, and the App offers the window again.  With "Stay
  signed in" the window usually goes straight through and closes itself.

Stdlib only, like folder_intake: app.py imports it in the hub process, and the
sign-in window runs in a child process (signin_main, started with
--sharepoint-signin) because pywebview needs a main thread of its own.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SIGNIN_ARG = '--sharepoint-signin'
SIGNIN_TIMEOUT_S = 600          # the window gives up after ten minutes
TRACE_EXTS = ('.sor', '.json', '.bdr', '.zip')
BIG_BYTES = 1024 ** 3           # ask before downloading more than this ...
BIG_FILES = 3000                # ... or this many files
_CHUNK = 1 << 20


class SharePointError(Exception):
    """A plain-English message for the page."""


class NeedsSignIn(SharePointError):
    pass


class NoAccess(SharePointError):
    pass


class OutsideFolder(SharePointError):
    pass


# ── Where things live ─────────────────────────────────────────────────────
def app_dir() -> str:
    """The folder settings.json is in (see app._analysis_settings_path)."""
    return (os.environ.get('OTDR_SETTINGS_DIR') or os.environ.get('OTDR_SUITE_APP_DIR')
            or os.path.join(os.path.expanduser('~'), '.otdrSuite'))


def session_path() -> str:
    return os.path.join(app_dir(), 'sharepoint_session.bin')


def status_path() -> str:
    """The sign-in window's result: ok or a message.  Never a cookie."""
    return os.path.join(app_dir(), 'sharepoint_signin.json')


def webview_dir() -> str:
    return os.path.join(app_dir(), 'sharepoint_web')


def cache_root() -> str:
    return os.path.join(app_dir(), 'SharePoint')


# ── Links and folder paths ────────────────────────────────────────────────
def is_sharepoint_link(link) -> bool:
    parts = urllib.parse.urlsplit((link or '').strip())
    return parts.scheme == 'https' and (parts.hostname or '').lower().endswith('.sharepoint.com')


def norm(path) -> str:
    """'/sites/X/Shared Documents/Span 7' form: one leading slash, no trailing
    one.  A '..' anywhere is refused: it could climb out of the folder."""
    parts = [p for p in str(path or '').replace('\\', '/').split('/') if p]
    if any(p in ('.', '..') for p in parts):
        raise OutsideFolder('That folder path is not allowed.')
    return '/' + '/'.join(parts)


def inside(path, root) -> bool:
    """True when `path` is the folder `root` or a folder under it.  SharePoint
    paths ignore case, so this does too."""
    try:
        p, r = norm(path).lower(), norm(root).lower()
    except OutsideFolder:
        return False
    return p == r or p.startswith(r.rstrip('/') + '/')


def folder_from_url(url):
    """The server-relative folder a SharePoint address shows, or None when the
    address alone cannot tell (a sharing link before it has been followed).

    A folder view carries it in ?id= (?RootFolder= on classic pages); a
    library's own view is <library>/Forms/AllItems.aspx; a plain folder
    address is the path itself (SharePoint redirects it to ?id=)."""
    parts = urllib.parse.urlsplit(url or '')
    query = urllib.parse.parse_qs(parts.query)
    for key in ('id', 'RootFolder'):
        if query.get(key):
            return norm(query[key][0])
    path = urllib.parse.unquote(parts.path)
    if '/Forms/' in path:
        return norm(path.split('/Forms/')[0])
    low = path.lower()
    if not path.strip('/') or path.startswith('/:') or low.endswith('.aspx') or '/_layouts/' in low:
        return None
    return norm(path)


def crumbs(path, root):
    """[(name, path)] from the folder `root` down to `path`."""
    root, path = norm(root), norm(path)
    out = [(root.rsplit('/', 1)[-1], root)]
    rest = path[len(root):].strip('/')
    cur = root
    for name in rest.split('/') if rest else []:
        cur = cur + '/' + name
        out.append((name, cur))
    return out


def _alias(path) -> str:
    """An OData string literal for the @a parameter: quotes doubled, then
    URL-encoded (folder names carry '&', '#', spaces, apostrophes)."""
    return urllib.parse.quote("'" + str(path).replace("'", "''") + "'", safe='')


def _when(stamp):
    """SharePoint's '2026-09-03T11:46:41Z' as epoch seconds (0 if unreadable)."""
    try:
        return datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _safe_name(name) -> str:
    """A Windows-legal file or folder name."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(name)).rstrip(' .')
    return s or '_'


def local_folder(path) -> str:
    """Where the App keeps its copy of the SharePoint folder `path`.  A short
    hash keeps two folders with the same name apart, and the folder's own name
    stays last so the Load screen names it."""
    p = norm(path)
    tag = hashlib.sha1(p.lower().encode('utf-8')).hexdigest()[:8]
    return os.path.join(cache_root(), tag, _safe_name(p.rsplit('/', 1)[-1]))


# ── The saved sign-in ─────────────────────────────────────────────────────
_DPAPI, _PLAIN = b'DPAPI1\n', b'PLAIN1\n'


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Encrypt/decrypt `data` for the current Windows user (CryptProtectData)."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = Blob()
    ui_forbidden = 0x1
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(blob_in), 'OTDR Suite App', None, None,
                                      None, ui_forbidden, ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None,
                                        None, ui_forbidden, ctypes.byref(blob_out))
    if not ok:
        raise OSError('DPAPI failed (%d)' % ctypes.GetLastError())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def save_session(sess: dict) -> None:
    data = json.dumps(sess).encode('utf-8')
    blob = _DPAPI + _dpapi(data, True) if os.name == 'nt' else _PLAIN + data
    path = session_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as fh:
        fh.write(blob)
    os.replace(tmp, path)


def load_session():
    """The saved sign-in, or None (none saved, or unreadable here)."""
    try:
        with open(session_path(), 'rb') as fh:
            blob = fh.read()
        if blob.startswith(_DPAPI) and os.name == 'nt':
            data = _dpapi(blob[len(_DPAPI):], False)
        elif blob.startswith(_PLAIN):
            data = blob[len(_PLAIN):]
        else:
            return None
        sess = json.loads(data.decode('utf-8'))
    except (OSError, ValueError):
        return None
    need = ('link', 'site_url', 'root', 'cookies')
    return sess if isinstance(sess, dict) and all(sess.get(k) for k in need) else None


def clear_session() -> None:
    try:
        os.remove(session_path())
    except OSError:
        pass


def forget_signin() -> None:
    """Sign out: the saved cookies AND the sign-in window's own profile, so the
    next sign-in asks for the account again."""
    clear_session()
    shutil.rmtree(webview_dir(), ignore_errors=True)


def write_status(ok, message='') -> None:
    try:
        os.makedirs(app_dir(), exist_ok=True)
        with open(status_path(), 'w', encoding='utf-8') as fh:
            json.dump({'ok': bool(ok), 'message': message}, fh)
    except OSError:
        pass


def read_status():
    try:
        with open(status_path(), encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# ── Talking to SharePoint ─────────────────────────────────────────────────
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect here is SharePoint sending us to sign in: never follow it."""

    def redirect_request(self, *args, **kwargs):
        return None


def _tls_context():
    """Verifying TLS; certifi's bundle when it is there (the frozen build has
    no system store to lean on), else the OS store.  Never unverified."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _refusal(exc) -> SharePointError:
    code, hdr = exc.code, exc.headers
    if 300 <= code < 400 or code == 401:
        return NeedsSignIn('Your SharePoint sign-in has run out. Sign in again.')
    if code == 403:
        if (hdr.get('X-MSDAVEXT_Error') or '').startswith('917656'):
            return NeedsSignIn('Your SharePoint sign-in has run out. Sign in again.')
        return NoAccess('SharePoint says this account cannot open that folder.')
    if code == 404:
        return SharePointError('That folder is not on SharePoint any more (moved or renamed?).')
    return SharePointError(f'SharePoint answered {code} ({exc.reason}).')


class Client:
    """REST calls for ONE folder with a saved sign-in."""

    def __init__(self, sess: dict, timeout=60, sleep=time.sleep):
        self.site = sess['site_url'].rstrip('/')
        self.root = norm(sess['root'])
        self.library = norm(sess['library']) if sess.get('library') else None
        self._cookie = '; '.join(f'{k}={v}' for k, v in sess['cookies'].items())
        self._ua = sess.get('ua') or 'OTDR Suite App'
        self._timeout = timeout
        self._sleep = sleep
        https = urllib.request.HTTPSHandler(context=_tls_context())
        self._opener = urllib.request.build_opener(_NoRedirect, https)

    def _guard(self, path) -> str:
        if not inside(path, self.root):
            raise OutsideFolder('The App only opens the SharePoint folder it was given.')
        return norm(path)

    def _open(self, url, accept):
        for attempt in range(4):
            req = urllib.request.Request(url, headers={
                'Cookie': self._cookie, 'User-Agent': self._ua, 'Accept': accept})
            try:
                return self._opener.open(req, timeout=self._timeout)
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 503) and attempt < 3:
                    try:
                        wait = int(exc.headers.get('Retry-After') or 5)
                    except ValueError:
                        wait = 5
                    self._sleep(min(30, max(1, wait)))
                    continue
                raise _refusal(exc) from None
            except (urllib.error.URLError, socket.timeout, ConnectionError) as exc:
                reason = getattr(exc, 'reason', exc)
                raise SharePointError(f'Could not reach SharePoint ({reason}).') from None
        raise SharePointError('SharePoint is busy. Try again in a minute.')

    def _json(self, url):
        with self._open(url, 'application/json;odata=nometadata') as resp:
            if 'json' not in (resp.headers.get('Content-Type') or ''):
                raise NeedsSignIn('Your SharePoint sign-in has run out. Sign in again.')
            return json.loads(resp.read().decode('utf-8'))

    def _folder_url(self, path, what, select):
        return (f"{self.site}/_api/web/GetFolderByServerRelativePath(decodedurl=@a)/{what}"
                f"?@a={_alias(path)}&$select={select}")

    def folder(self, path=None) -> dict:
        """The folders and files directly in `path` (default: the top)."""
        path = self._guard(path or self.root)
        folders = self._json(self._folder_url(
            path, 'Folders', 'Name,ServerRelativeUrl,ItemCount,TimeLastModified')).get('value') or []
        files = self._json(self._folder_url(
            path, 'Files', 'Name,ServerRelativeUrl,Length,TimeLastModified')).get('value') or []
        at_library = self.library is not None and path.lower() == self.library.lower()
        subs = [{'name': f['Name'], 'path': norm(f['ServerRelativeUrl']),
                 'count': int(f.get('ItemCount') or 0)}
                for f in folders
                if not (at_library and f.get('Name') == 'Forms')
                and inside(f.get('ServerRelativeUrl') or '', path)]
        docs = [{'name': f['Name'], 'path': norm(f['ServerRelativeUrl']),
                 'size': int(f.get('Length') or 0), 'modified': _when(f.get('TimeLastModified'))}
                for f in files if inside(f.get('ServerRelativeUrl') or '', path)]
        return {'path': path, 'name': path.rsplit('/', 1)[-1],
                'folders': sorted(subs, key=lambda d: d['name'].lower()),
                'files': sorted(docs, key=lambda d: d['name'].lower())}

    def walk(self, path=None, exts=TRACE_EXTS) -> list:
        """Every file with one of `exts` in `path` and the folders under it,
        each with 'rel', its path below `path`."""
        top = self._guard(path or self.root)
        out, todo = [], [top]
        while todo:
            listing = self.folder(todo.pop())
            todo.extend(d['path'] for d in listing['folders'])
            for f in listing['files']:
                if f['name'].lower().endswith(exts):
                    out.append(dict(f, rel=f['path'][len(top):].strip('/')))
        return sorted(out, key=lambda f: f['rel'].lower())

    def download(self, f, dest, progress=None) -> None:
        """One file to `dest` (whole, or not at all), dated as on SharePoint."""
        path = self._guard(f['path'])
        url = (f"{self.site}/_api/web/GetFileByServerRelativePath(decodedurl=@a)/$value"
               f"?@a={_alias(path)}")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        part = dest + '.part'
        with self._open(url, '*/*') as resp, open(part, 'wb') as out:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                if progress:
                    progress(len(chunk))
        os.replace(part, dest)
        if f.get('modified'):
            os.utime(dest, (f['modified'], f['modified']))


def fetch(client, files, dest, progress=None):
    """Bring `files` (from Client.walk) into the folder `dest`.  A file already
    there with the same size and date is kept, not downloaded again.
    progress(done_bytes, total_bytes, name).  Returns (downloaded, kept)."""
    total = sum(f['size'] for f in files)
    done = [0]
    got = kept = 0
    for f in files:
        local = os.path.join(dest, *[_safe_name(p) for p in f['rel'].split('/')])
        try:
            st = os.stat(local)
            same = st.st_size == f['size'] and abs(st.st_mtime - f['modified']) < 2
        except OSError:
            same = False
        if same:
            kept += 1
            done[0] += f['size']
        else:
            def step(n, name=f['name']):
                done[0] += n
                if progress:
                    progress(done[0], total, name)
            client.download(f, local, step)
            got += 1
        if progress:
            progress(done[0], total, f['name'])
    return got, kept


# ── The sign-in window (child process) ────────────────────────────────────
_CONTEXT_JS = (
    "(function(){var c=window._spPageContextInfo;if(!c||!c.webAbsoluteUrl)return null;"
    "return {web:c.webAbsoluteUrl,list:c.listUrl||'',user:c.userDisplayName||'',"
    "login:c.userLoginName||'',ua:navigator.userAgent,href:location.href};})()")


def _cookie_jar(cookies) -> dict:
    """pywebview's cookies (http.cookies.SimpleCookie objects) as name -> value."""
    jar = {}
    for c in cookies or []:
        for name, morsel in getattr(c, 'items', lambda: [])():
            jar[name] = morsel.value
    return jar


def session_from(link, ctx, cookies) -> dict:
    """What to keep from the page the sign-in window reached."""
    root = folder_from_url(ctx.get('href')) or folder_from_url(link) or (
        norm(ctx['list']) if ctx.get('list') else None)
    if not root:
        raise SharePointError('That link does not open a folder. Open the folder in '
                              'SharePoint and copy the address bar.')
    jar = _cookie_jar(cookies)
    if not jar:
        raise SharePointError('The sign-in finished but SharePoint gave the App nothing to '
                              'keep. Try again.')
    return {'v': 1, 'link': link, 'site_url': ctx['web'].rstrip('/'),
            'library': ctx.get('list') or '', 'root': root,
            'user': ctx.get('user') or '', 'login': ctx.get('login') or '',
            'ua': ctx.get('ua') or '', 'cookies': jar, 'saved': time.time()}


def _keep_popups_in_window() -> None:
    """Windows: pywebview sends every window.open to the system browser.  A
    sign-in page that opens a popup (some MFA and "Stay signed in" steps do)
    would then finish in the wrong browser and never reach this window.  An
    https popup is left to WebView2, which opens it as its own window on the
    same profile (the App window does the same for its own pages, see
    launcher._let_the_viewer_pop_out).  Off Windows the import fails and the
    default stands."""
    try:
        from webview.platforms import edgechromium
    except Exception:
        return
    original = edgechromium.EdgeChrome.on_new_window_request

    def on_new_window_request(self, sender, args):
        if str(args.get_Uri()).lower().startswith('https://'):
            return                           # unhandled: WebView2 makes the popup
        return original(self, sender, args)

    edgechromium.EdgeChrome.on_new_window_request = on_new_window_request


def signin_main(argv=None) -> int:
    """Open the sign-in window on the folder link; save the session once the
    folder's page is showing, then close.  0 = signed in, 2 = the window was
    closed first, 3 = could not start, 4 = signed in but not to a folder."""
    import threading

    argv = list(sys.argv[1:] if argv is None else argv)
    link = next((a for a in argv if is_sharepoint_link(a)), '')
    write_status(False, 'The sign-in window was closed before it finished.')
    if not link:
        write_status(False, 'That is not a SharePoint link.')
        return 3
    try:
        import webview
    except Exception as exc:
        write_status(False, f'The sign-in window cannot open on this PC ({exc}).')
        return 3
    _keep_popups_in_window()
    host = urllib.parse.urlsplit(link).hostname.lower()
    closed = threading.Event()
    result = {}
    # on_top: the hub (not the window the person clicked in) starts this
    # process, so Windows would let the window open BEHIND the App.  Only for
    # a moment (watch): a sign-in popup must be able to come above it.
    window = webview.create_window('Sign In to SharePoint - OTDR Suite App', link,
                                   width=1100, height=820, text_select=True, on_top=True)
    window.events.closed += closed.set

    def watch(win):
        start = time.time()
        end = start + SIGNIN_TIMEOUT_S
        while not closed.is_set() and time.time() < end:
            time.sleep(1)
            if win.on_top and time.time() - start > 2:
                try:
                    win.on_top = False               # in front now; popups can pass it
                except Exception:
                    pass
            try:
                if (urllib.parse.urlsplit(win.get_current_url() or '').hostname or '').lower() != host:
                    continue                         # still on the Microsoft sign-in pages
                ctx = win.evaluate_js(_CONTEXT_JS)
                if not ctx:
                    continue                         # page still loading
                sess = session_from(link, ctx, win.get_cookies())
            except SharePointError as exc:
                result['error'] = str(exc)
                break
            except Exception:
                continue                             # mid-navigation; look again
            save_session(sess)
            result['ok'] = True
            print(f"sharepoint: signed in as {sess['user'] or sess['login']}")
            break
        try:
            win.destroy()
        except Exception:
            pass

    os.makedirs(webview_dir(), exist_ok=True)
    webview.start(watch, (window,), gui='edgechromium' if os.name == 'nt' else None,
                  private_mode=False, storage_path=webview_dir())
    if result.get('ok'):
        write_status(True)
        return 0
    if result.get('error'):
        write_status(False, result['error'])
        return 4
    return 2


if __name__ == '__main__' and SIGNIN_ARG in sys.argv:
    # Dev runs (not frozen): app.py starts this file directly.  os._exit, not
    # return: pywebview's start() thread is not a daemon.
    _code = signin_main(sys.argv[1:])
    sys.stdout.flush()
    os._exit(_code)
