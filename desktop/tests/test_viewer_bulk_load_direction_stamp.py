"""Viewer: a file's own direction stamp counts at any load size.

Found 2026-10-01 in an audit of where the Viewer treats A and B differently.
A .sor file carries its own direction (FastReporter's LocationsDirection), so
a copy saved the other way is drawn that way when it is opened again.  The
single-trace endpoint /api/trace sent that stamp and a detail load (48 files
or fewer) honoured it; the bulk endpoint /api/traces, used past 48 files,
sent nothing.  So the same mislabeled file was drawn as itself in a small
load and as its folder in a big one, and the FILES list's direction column
and the A/B pairing changed with it.

The bulk endpoint now sends the same stamp, under the same rule (a folder
whose stamps mostly say the other side is not believed), from one shared
helper, and the bulk loader records it as the detail loader does.

Reading the stamp walked every record of EXFO's proprietary block, ~40 ms a
file, which would have added ~45 s to a 1152-file load.  The record is now
found by its name and proven by the same descriptor test, at the same offset
as the walk on every file checked (3637 real files, both directions), in a
few microseconds.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import threading
from http.server import HTTPServer
from pathlib import Path
from urllib.request import urlopen

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'viewer'))
import trace_server as T  # noqa: E402

FIX = os.path.join(ROOT, 'desktop', 'tests', 'fixtures')
HTML = open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8').read()
JSC = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/"
           "Current/Helpers/jsc")


def _raw(p):
    with open(p, 'rb') as f:
        return f.read()


# ─── the lookup: same answer as the full walk, without the walk ──────────

def _walk_locdir(stream):
    """The lookup as it was: every record, then the first that matches."""
    for r in T._prop_records(stream):
        if r['name'] == 'LocationsDirection' and r['tc'] == 1 and r['size'] == 4:
            return r['pay']
    return None


def _streams():
    for p in sorted(glob.glob(os.path.join(FIX, '**', '*.sor'), recursive=True)):
        try:
            _mv, bl = T.split(_raw(p))
        except Exception:                                  # noqa: BLE001
            continue
        for b in bl:
            if b.name.startswith(b'ExfoNewProprietaryBlock'):
                _hdr, chunks, _tail = T._prop_chunks(b.body)
                yield p, b''.join(d for _, d in chunks)
                break


def test_lookup_lands_where_the_walk_does_on_every_fixture():
    seen = 0
    for p, stream in _streams():
        assert T._prop_locdir(stream) == _walk_locdir(stream), p
        seen += 1
    assert seen >= 20, 'too few proprietary blocks to say anything'


def test_lookup_needs_the_descriptor_not_just_the_name():
    for _p, stream in _streams():
        off = T._prop_locdir(stream)
        if off is None:
            continue
        # break the descriptor's self-pointer: the name alone must not count
        name_at = off - len(b'LocationsDirection\x00')
        bad = bytearray(stream)
        bad[name_at - 16:name_at - 12] = (name_at + 1).to_bytes(4, 'little')
        assert T._prop_locdir(bytes(bad)) is None
        return
    pytest.skip('no fixture carries LocationsDirection')


def test_a_saved_direction_still_reads_back():
    a = os.path.join(FIX, 'span_A', 'ELMMIL0001_1550.sor')
    assert T.read_direction(_raw(a)) == 'a'
    assert T.read_direction(T.set_direction(_raw(a), 'b')) == 'b'


# ─── the wire: the bulk load sends what the single load sends ────────────

def _b_folder(tmp_path, stamp_all, flip_one=None):
    d = tmp_path / 'B'
    d.mkdir()
    for fn in sorted(os.listdir(os.path.join(FIX, 'span_B'))):
        raw = _raw(os.path.join(FIX, 'span_B', fn))
        want = stamp_all if fn != flip_one else ('a' if stamp_all == 'b' else 'b')
        (d / fn).write_bytes(T.set_direction(raw, want))
    return d


def _served(dir_b):
    """{fiber: stored_dir} from /api/trace (each fibre) and /api/traces (bulk)."""
    T._stamp_cache.clear()
    T.set_dirs(os.path.join(FIX, 'span_A'), str(dir_b))
    srv = HTTPServer(('127.0.0.1', 0), T.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{srv.server_port}'
    try:
        fibers = [f for f, _fn in T.list_fibers(str(dir_b))]
        single = {}
        for f in fibers:
            with urlopen(f'{base}/api/trace?dir=b&fiber={f}', timeout=60) as r:
                single[f] = json.loads(r.read().decode('utf-8'))['stored_dir']
        spec = ','.join(str(f) for f in fibers)
        with urlopen(f'{base}/api/traces?dir=b&fibers={spec}&maxpts=200', timeout=120) as r:
            bulk = {t['fiber']: t.get('stored_dir', 'MISSING')
                    for t in json.loads(r.read().decode('utf-8'))['traces']}
        return single, bulk
    finally:
        srv.shutdown()
        srv.server_close()
        T.set_dirs(None, None)


def test_bulk_sends_a_saved_copys_direction_like_the_single_load(tmp_path):
    d = _b_folder(tmp_path, 'b', flip_one='MILELM0002_1550.sor')
    single, bulk = _served(d)
    assert bulk == single
    assert bulk[2] == 'a'                                  # the saved copy
    assert all(v == 'b' for f, v in bulk.items() if f != 2)


def test_bulk_ignores_a_folder_stamped_all_the_other_way(tmp_path):
    single, bulk = _served(_b_folder(tmp_path, 'a'))
    assert bulk == single
    assert set(bulk.values()) == {None}


# ─── the Viewer's bulk loader records the stamp ──────────────────────────

def _fn(name):
    i = HTML.index('async function ' + name + '(')
    return HTML[i:HTML.index('\n}\n', i) + 3]


def test_bulk_loader_reads_the_stamp_as_the_detail_loader_does():
    line = "if (data.stored_dir && data.stored_dir !== dir) gStoredDir[key] = data.stored_dir;"
    assert line in _fn('loadOverview')
    assert line in _fn('loadOne')


needs_jsc = pytest.mark.skipif(not JSC.exists(), reason="no JavaScriptCore shell here")


@needs_jsc
def test_bulk_loaded_file_is_drawn_as_its_stamp(tmp_path):
    i = HTML.index('function effDir(')
    eff = HTML[i:HTML.index('\n}\n', i) + 3]
    prog = r"""
var OVERVIEW_PTS = 2000, gLoadFailures = [], gTraces = [], gRemovedFiles = new Set();
var gStoredDir = {'b-3': 'a'}, gDirOverride = {}, gAutoFit = false;
function nextColor() { return '#000'; }
function traceColor() { return '#000'; }
function setReadout() {} function renderChips() {} function draw() {}
function dataBounds() { return null; }
var console = { warn: function () {} };
function setTimeout(fn) { Promise.resolve().then(fn); }
function fetch() {
  // B folder: fibre 2 saved as A->B, fibre 3's stamp now agrees with B
  var body = { traces: [{fiber: 1, stored_dir: 'b'}, {fiber: 2, stored_dir: 'a'},
                         {fiber: 3, stored_dir: 'b'}], missing: [], failed: [] };
  return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(body); } });
}
%s
%s
loadOverview([{d: 'b', f: 1}, {d: 'b', f: 2}, {d: 'b', f: 3}]).then(function () {
  print(JSON.stringify({ dirs: gTraces.map(function (t) { return [t.key, t.dir]; }), stored: gStoredDir }));
}, function (e) { print('THREW ' + e); });
""" % (eff, _fn('loadOverview'))
    p = tmp_path / 'overview.js'
    p.write_text(prog, encoding='utf-8')
    res = subprocess.run([str(JSC), str(p)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr + res.stdout
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out['dirs'] == [['b-1', 'b'], ['b-2', 'a'], ['b-3', 'b']]   # F2 was drawn as B
    assert out['stored'] == {'b-2': 'a'}                                # a stale stamp is dropped
