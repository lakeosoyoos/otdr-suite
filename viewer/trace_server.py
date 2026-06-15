#!/usr/bin/env python3
"""Trace server for the OTDR Suite viewer page.

A small HTTP server that parses OTDR SOR/JSON files on demand and serves
trace + event JSON to the canvas viewer (viewer.html).  Designed to run as
a background daemon thread *inside* the Streamlit hub process so the whole
suite ships as one launcher.

The A/B folders are held in a module-level CONFIG dict that the hub writes
when the user picks folders in the sidebar — same process, shared state, no
second config channel needed.

Endpoints:
  GET /                          -> viewer.html
  GET /api/list                  -> {dir_a, dir_b, fibers_a:[...], fibers_b:[...]}
  GET /api/trace?dir=a&fiber=64  -> {dist_km, trace_db, events, ...}

Trace sign convention served to the browser:
  Higher value = stronger signal (descending = loss), FastReporter-style.
  SOR DataPts is ascending-loss, so we negate.  JSON full_trace is already
  descending-signal, served as-is.
"""
from __future__ import annotations

import json
import os
import re
import socket
import threading
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import numpy as np

# These resolve from the viewer/ package dir, which the hub puts on sys.path.
from sor_reader324802a import parse_sor_full, _sor_ior_from_events, _sor_first_pos_m
from json_reader import parse_otdr_json

# Stdlib-only Slack reporting (repo root is on sys.path when the hub imports us).
try:
    from error_report import report_error
except Exception:                                  # standalone/dev — best-effort
    def report_error(*a, **k):
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
VIEWER_HTML = os.path.join(HERE, 'viewer.html')

# Shared, hub-writable configuration.  Pre-seeded with the Long Shots sample
# folders so the viewer shows a trace on first launch; the hub sidebar lets
# the user point it anywhere.
_DEFAULT_A = '/Users/robertcolbert/Downloads/Long Shots/ELMDALE TO MILER TRACES 5-8-2026'
_DEFAULT_B = '/Users/robertcolbert/Downloads/Long Shots/MILLER TO ELMDALE TRACES 5-6-2026'
CONFIG = {
    'dir_a': _DEFAULT_A if os.path.isdir(_DEFAULT_A) else None,
    'dir_b': _DEFAULT_B if os.path.isdir(_DEFAULT_B) else None,
}

_server = None
_thread = None
_started_port = None


# ─── Fiber-number extraction ────────────────────────────────────────────
_FIBER_NUM_RE = re.compile(r'(\d{3,4})_\d{3,4}\b')

def extract_fiber_num(fn):
    """STRROM0064_1550.sor -> 64,  ELMMIL1152_1550.sor -> 1152."""
    m = _FIBER_NUM_RE.search(fn)
    if m:
        return int(m.group(1))
    base = os.path.splitext(fn)[0]
    tail = re.search(r'(\d{3,4})$', base)
    return int(tail.group(1)) if tail else None


def _dir_has_json(d):
    if not d or not os.path.isdir(d):
        return False
    return any(fn.lower().endswith('.json') for fn in os.listdir(d))


def list_fibers(directory):
    """Return sorted [(fiber_num, filename), ...] for a directory."""
    if not directory or not os.path.isdir(directory):
        return []
    ext = '.json' if _dir_has_json(directory) else '.sor'
    out = []
    for fn in os.listdir(directory):
        if fn.startswith('._'):          # AppleDouble files from Mac zips
            continue
        if not fn.lower().endswith(ext):
            continue
        fnum = extract_fiber_num(fn)
        if fnum is not None:
            out.append((fnum, fn))
    out.sort(key=lambda t: t[0])
    return out


# ─── Trace loader (cached on directory+filename) ────────────────────────
@lru_cache(maxsize=256)
def _load_trace_cached(directory, filename):
    path = os.path.join(directory, filename)
    if filename.lower().endswith('.json'):
        r = parse_otdr_json(path)
        if r is None:
            return None
        trace = r['full_trace']
        res_m = float(r.get('_json_resolution_m') or 2.5493)
        first_pos_m = float(r.get('_json_first_pos_m') or 0.0)
        display_trace = trace.astype(np.float64)            # already descending-signal
    else:
        r = parse_sor_full(path, trim=False)
        if r is None:
            return None
        trace = r['trace']
        ior = _sor_ior_from_events(r)
        sp_s = float(r.get('exfo_sampling_period') or 5e-08)
        res_m = 299_792_458.0 * sp_s / 2.0 / ior
        first_pos_m = _sor_first_pos_m(r, res_m)
        display_trace = -trace.astype(np.float64)           # flip to descending-signal

    n = len(display_trace)
    dist_km = (np.arange(n, dtype=np.float64) * res_m + first_pos_m) / 1000.0

    baseline = float(np.median(display_trace[:200])) if n >= 200 else float(display_trace[0])
    display_trace = display_trace - baseline

    events = []
    for e in (r.get('events') or []):
        events.append({
            'number': int(e.get('number') or 0),
            'dist_km': round(float(e.get('dist_km') or 0.0), 4),
            'splice_loss': round(float(e.get('splice_loss') or 0.0), 3),
            'reflection': round(float(e.get('reflection') or 0.0), 2),
            'slope': round(float(e.get('slope') or 0.0), 3),
            'type': str(e.get('type') or ''),
            'is_reflective': bool(e.get('is_reflective')),
            'is_end': bool(e.get('is_end')),
        })

    return {
        'filename': filename,
        'num_points': n,
        'dx_km': res_m / 1000.0,
        'first_pos_km': first_pos_m / 1000.0,
        'dist_km': [round(float(x), 5) for x in dist_km.tolist()],
        'trace_db': [round(float(x), 3) for x in display_trace.tolist()],
        'events': events,
    }


def load_trace(direction, fiber):
    d = CONFIG['dir_a'] if direction == 'a' else CONFIG['dir_b']
    fmap = {n: fn for n, fn in list_fibers(d)}
    fn = fmap.get(fiber)
    if fn is None:
        return None
    return _load_trace_cached(d, fn)


# ─── HTTP handler ───────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype='text/html; charset=utf-8'):
        try:
            with open(path, 'rb') as f:
                body = f.read()
        except OSError as e:
            self.send_error(404, str(e))
            return
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ('/', '/index.html', '/viewer.html'):
            self._send_file(VIEWER_HTML)
            return
        if u.path == '/api/list':
            fa = list_fibers(CONFIG['dir_a'])
            fb = list_fibers(CONFIG['dir_b'])
            self._send_json({
                'dir_a': CONFIG['dir_a'] or '',
                'dir_b': CONFIG['dir_b'] or '',
                'dir_a_name': os.path.basename((CONFIG['dir_a'] or '').rstrip('/')) or '(none)',
                'dir_b_name': os.path.basename((CONFIG['dir_b'] or '').rstrip('/')) or '(none)',
                'fibers_a': [n for n, _ in fa],
                'fibers_b': [n for n, _ in fb],
            })
            return
        if u.path == '/api/trace':
            q = parse_qs(u.query)
            direction = (q.get('dir') or [''])[0].lower()
            try:
                fiber = int((q.get('fiber') or [''])[0])
            except ValueError:
                self._send_json({'error': 'invalid fiber'}, status=400)
                return
            if direction not in ('a', 'b'):
                self._send_json({'error': 'dir must be a or b'}, status=400)
                return
            try:
                t = load_trace(direction, fiber)
            except Exception as exc:                       # surface parse errors as JSON
                report_error("viewer trace load", exc,
                             {"direction": direction, "fiber": fiber})
                self._send_json({'error': f'parse failed: {exc}'}, status=500)
                return
            if t is None:
                self._send_json({'error': f'fiber {fiber} not found in dir {direction}'}, status=404)
                return
            self._send_json({'direction': direction.upper(), 'fiber': fiber, **t})
            return
        self.send_error(404, 'unknown route')

    def do_POST(self):
        # Browser JS errors from viewer.html POST here → Slack via report_error.
        u = urlparse(self.path)
        if u.path == '/api/jserror':
            try:
                n = int(self.headers.get('Content-Length', 0) or 0)
                data = json.loads((self.rfile.read(n) if n else b'{}').decode('utf-8') or '{}')
            except Exception:
                data = {}
            msg = str(data.get('message') or 'unknown JS error')[:300]
            stack = str(data.get('stack') or '')[:800]
            page = str(data.get('page') or '')[:200]
            try:
                raise RuntimeError(msg)               # give report_error an exc + a frame
            except Exception as exc:
                report_error("viewer (browser JS)", exc, {"js_stack": stack, "url": page})
            self._send_json({'ok': True})
            return
        self.send_error(404, 'unknown route')


# ─── Bootstrap helpers ──────────────────────────────────────────────────
def set_dirs(dir_a, dir_b):
    """Hub calls this when the user picks folders.  Returns True if either
    directory changed.  (Only _load_trace_cached is memoized, and it keys on
    directory+filename, so a folder swap can't serve stale traces.)"""
    changed = (CONFIG['dir_a'] != (dir_a or None)) or (CONFIG['dir_b'] != (dir_b or None))
    CONFIG['dir_a'] = dir_a or None
    CONFIG['dir_b'] = dir_b or None
    return changed


def is_running():
    return _server is not None


def get_port():
    return _started_port


def find_free_port(start):
    for port in range(start, start + 50):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('127.0.0.1', port))
                return port
        except OSError:
            continue
    raise RuntimeError(f'no free port in {start}-{start + 49}')


def start_in_thread(port=8771):
    """Start the server once per process (idempotent).  Returns the port."""
    global _server, _thread, _started_port
    if _server is not None:
        return _started_port
    actual = find_free_port(port)
    _server = HTTPServer(('127.0.0.1', actual), Handler)
    _thread = threading.Thread(target=_server.serve_forever, daemon=True)
    _thread.start()
    _started_port = actual
    return actual


# ─── CLI (standalone dev) ───────────────────────────────────────────────
def _main():
    import argparse
    import time
    import webbrowser
    ap = argparse.ArgumentParser(description='OTDR Suite trace server (standalone)')
    ap.add_argument('--dir-a', default=None)
    ap.add_argument('--dir-b', default=None)
    ap.add_argument('--port', type=int, default=8771)
    ap.add_argument('--no-browser', action='store_true')
    args = ap.parse_args()
    set_dirs(args.dir_a, args.dir_b)
    port = start_in_thread(args.port)
    url = f'http://127.0.0.1:{port}/'
    print(f'Trace server at {url}')
    if not args.no_browser:
        time.sleep(0.3)
        webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print('\nShutting down.')
        _server.shutdown()


if __name__ == '__main__':
    _main()
