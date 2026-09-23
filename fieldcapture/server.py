"""
Field Capture page server
=========================

The Field Capture page is a web app (fieldcapture/web): the same code that
will run on the tech's iPhone later.  In OTDR Suite it is shown the way the
Viewer is: a small HTTP server runs as a daemon thread inside the hub process
and the hub page embeds it in an iframe.

Routes (loopback only):

  GET  /                  web/index.html
  GET  /<file>            a file of the web app
  GET  /api/blank-form    the blank Lumen FQA form the FQA Builder ships
  POST /api/save?name=    write the request body into the "Save to" folder
  POST /api/email         JSON {path, to, subject, body}: write an email draft
                          with the saved file attached, and open it
  POST /api/reveal        JSON {path}: show a saved file in Explorer / Finder
  POST /api/jserror       browser errors to Slack, as the Viewer does

Where the web files come from.  The app's own code (index.html, app.js, ...)
is in the launcher's ENGINE_FILES, so an update replaces it in the engine
cache next to this module.  The libraries (Tesseract's 15 MB of reader cores
and language data, ExcelJS, JSZip) never change and are NOT in ENGINE_FILES;
they are only in the install.  So files are looked up next to this module
first and in the installed bundle second.

Robert, 2026-09-23.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
ENGINE_ROOT = HERE.parent
TEMPLATE = ENGINE_ROOT / 'fqa' / 'templates' / 'FQA_Site_Survey_v1_1.xlsm'
PORT_BASE = 8781          # clear of the hub (8510) and the trace server (8771+)
MAX_UPLOAD = 300 * 1024 * 1024

# Shared with the hub page: it sets the folder the tech chose.
CONFIG = {'dest_dir': None}

_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': 'application/json',
    '.webmanifest': 'application/manifest+json',
    '.png': 'image/png',
    '.svg': 'image/svg+xml',
    '.wasm': 'application/wasm',
    # The reader's language data is fetched and un-gzipped by the reader
    # itself, so it must NOT be sent with Content-Encoding: gzip.
    '.gz': 'application/octet-stream',
    '.xlsm': 'application/vnd.ms-excel.sheet.macroEnabled.12',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
}

# Files this server wrote.  /api/email and /api/reveal act only on these, so a
# page cannot ask the server to mail or open an arbitrary file on the PC.
_saved: set[str] = set()


def _bundled_web() -> Path:
    if getattr(sys, 'frozen', False):
        base = Path(getattr(sys, '_MEIPASS', os.path.dirname(sys.executable)))
        return base / 'fieldcapture' / 'web'
    return HERE / 'web'


def web_roots() -> list[Path]:
    roots = [HERE / 'web']
    b = _bundled_web()
    if b.resolve() != roots[0].resolve():
        roots.append(b)
    return roots


def resolve_web_file(rel: str) -> Path | None:
    """The file for a URL path, or None.  Never leaves a web root."""
    rel = unquote(rel).lstrip('/') or 'index.html'
    if '\\' in rel or any(part in ('', '.', '..') for part in rel.split('/')):
        return None
    for root in web_roots():
        base = root.resolve()
        p = (base / rel).resolve()
        if base not in p.parents:
            continue
        if p.is_file():
            return p
    return None


def default_dest() -> Path:
    d = CONFIG.get('dest_dir')
    if d:
        return Path(d)
    home = Path.home()
    for cand in (home / 'Downloads', home / 'Desktop', home):
        if cand.is_dir():
            return cand
    return Path.cwd()


def safe_name(name: str) -> str:
    """A plain file name for a saved workbook: no folders, no characters
    Windows refuses, and an Excel extension."""
    name = os.path.basename(str(name or '').replace('\\', '/'))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', ' ', name)
    name = ' '.join(name.split()).strip(' .') or 'Field Capture.xlsm'
    if not re.search(r'\.(xlsm|xlsx)$', name, re.I):
        name += '.xlsm'
    return name[:180]


def unique_path(folder: Path, name: str) -> Path:
    """`name` in `folder`, numbered '(2)', '(3)' if it is already taken, so a
    second send never overwrites the first."""
    p = folder / name
    stem, ext = p.stem, p.suffix
    n = 2
    while p.exists():
        p = folder / f'{stem} ({n}){ext}'
        n += 1
    return p


class Handler(BaseHTTPRequestHandler):
    server_version = 'FieldCapture/1'

    def log_message(self, fmt, *args):          # keep the hub's console quiet
        pass

    # ── helpers ──
    def _send(self, code, body: bytes, ctype: str, cache: str = 'no-store'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode('utf-8'), 'application/json')

    def _origin_is_local(self) -> bool:
        """Only the page itself may POST.  A cross-site POST always carries an
        Origin naming the other site; the page sends a loopback one (or none)."""
        origin = self.headers.get('Origin')
        if not origin or origin == 'null':
            return True
        try:
            host = urlparse(origin).hostname
        except Exception:
            return False
        return host in ('127.0.0.1', 'localhost', '::1')

    def _body(self) -> bytes:
        n = int(self.headers.get('Content-Length', 0) or 0)
        if n > MAX_UPLOAD:
            raise ValueError('file too large')
        return self.rfile.read(n) if n else b''

    # ── GET ──
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/api/blank-form':
            if not TEMPLATE.is_file():
                self._json({'error': 'The blank FQA form is not installed.'}, 404)
                return
            self._send(200, TEMPLATE.read_bytes(), _TYPES['.xlsm'])
            return
        if u.path == '/api/config':
            self._json({'dest_dir': str(default_dest())})
            return
        f = resolve_web_file(u.path)
        if f is None:
            self.send_error(404)
            return
        ext = ''.join(f.suffixes[-1:]).lower()
        ctype = _TYPES.get(ext, 'application/octet-stream')
        cache = 'public, max-age=604800' if '/vendor/' in f.as_posix() else 'no-store'
        self._send(200, f.read_bytes(), ctype, cache)

    # ── POST ──
    def do_POST(self):
        u = urlparse(self.path)
        if not self._origin_is_local():
            self.send_error(403, 'cross-origin POST rejected')
            return
        try:
            if u.path == '/api/save':
                name = safe_name((parse_qs(u.query).get('name') or [''])[0])
                folder = default_dest()
                folder.mkdir(parents=True, exist_ok=True)
                path = unique_path(folder, name)
                path.write_bytes(self._body())
                _saved.add(str(path))
                self._json({'path': str(path)})
                return
            if u.path in ('/api/email', '/api/reveal'):
                data = json.loads(self._body().decode('utf-8') or '{}')
                path = str(data.get('path') or '')
                if path not in _saved or not os.path.isfile(path):
                    self._json({'error': 'Only a file this page saved can be sent or shown.'}, 400)
                    return
                from fieldcapture import email_draft
                if u.path == '/api/reveal':
                    ok, err = email_draft.reveal(path)
                    self._json({'shown': ok, 'error': err})
                    return
                eml = email_draft.write_draft(path, data.get('to') or '',
                                              data.get('subject') or '', data.get('body') or '')
                ok, err = email_draft.open_with_default_app(eml)
                self._json({'eml': str(eml), 'opened': ok, 'error': err})
                return
            if u.path == '/api/jserror':
                data = json.loads(self._body().decode('utf-8') or '{}')
                try:
                    from error_report import report_error
                    try:
                        raise RuntimeError(str(data.get('message') or 'unknown JS error')[:300])
                    except Exception as exc:
                        report_error('field capture (browser JS)', exc,
                                     {'js_stack': str(data.get('stack') or '')[:800]})
                except Exception:
                    pass
                self._json({'ok': True})
                return
        except Exception as exc:
            self._json({'error': str(exc)}, 500)
            return
        self.send_error(404)


class _Server(ThreadingHTTPServer):
    daemon_threads = True


_server = None
_port = None
_lock = threading.Lock()


def find_free_port(start=PORT_BASE, count=50):
    """Bind to the first port in [start, start+count) that takes it.  The
    server itself binds inside the loop (not a probe socket first): on Windows
    a probe and the real bind can disagree, which is how the trace server once
    died with WinError 10013."""
    last = None
    for port in range(start, start + count):
        try:
            return _Server(('127.0.0.1', port), Handler), port
        except OSError as exc:
            last = exc
    raise RuntimeError(f'no free port in {start}-{start + count - 1}: {last}')


def start_in_thread(port=PORT_BASE) -> int:
    """Start the server once per process.  Returns its port."""
    global _server, _port
    with _lock:
        if _server is None:
            _server, _port = find_free_port(port)
            threading.Thread(target=_server.serve_forever, daemon=True).start()
        return _port


if __name__ == '__main__':                     # standalone dev run
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--port', type=int, default=PORT_BASE)
    ap.add_argument('--dest', default=None)
    ap.add_argument('--no-open', action='store_true',
                    help='write email drafts but do not open them (testing)')
    a = ap.parse_args()
    sys.path.insert(0, str(ENGINE_ROOT))
    CONFIG['dest_dir'] = a.dest
    if a.no_open:
        from fieldcapture import email_draft
        email_draft.open_with_default_app = lambda p: (False, 'opening is off (--no-open)')
        email_draft.reveal = lambda p: (False, 'opening is off (--no-open)')
    srv, port = find_free_port(a.port, 1)
    print(f'Field Capture on http://127.0.0.1:{port}/?host=suite')
    srv.serve_forever()
