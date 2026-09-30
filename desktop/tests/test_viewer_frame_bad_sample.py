"""One sampled file that will not parse must not fail its whole folder.

frame_facts measures a folder's reel frame from up to REEL_SAMPLE fibers
spread across it, and load_trace frames EVERY fiber through it.  Its sample
loop skipped a file it could not read (OSError) but not one it read and could
not parse: a .sor cut short in a copy raises ValueError out of numpy ("buffer
size must be a multiple of element size").  That escaped frame_facts, so:

  * every fiber in that direction failed to load, not just the bad one,
  * the bulk overview called all of them missing,
  * /api/list came back with no fibers at all (it reads the frame too),
  * nothing was cached on the way out, so every load parsed the bad file
    again and failed again.

On a 1152-fiber cable only every 96th file is sampled, so it is rare, but
when it happens the whole direction goes dark.  A bad sample is now skipped
the way an unreadable one always was: it is one vote, and the rest decide.
"""
import json
import os
import shutil
import threading
from http.server import HTTPServer
from urllib.request import urlopen

import pytest

from conftest import FIXTURE_SPLICE_A_DIR, import_trace_server

T = import_trace_server()


def _folder(root, name, fibers=None):
    """A copy of the 24-fiber splice_A fixture, or of just `fibers`."""
    d = os.path.join(str(root), name)
    os.mkdir(d)
    for fn in sorted(os.listdir(FIXTURE_SPLICE_A_DIR)):
        if fibers is None or T.extract_fiber_num(fn) in fibers:
            shutil.copy2(os.path.join(FIXTURE_SPLICE_A_DIR, fn), d)
    return d


def _cut(d, fiber):
    """Truncate one fiber's file to a fifth of its bytes, as a copy that
    stopped part way leaves it.  Returns its path."""
    path = os.path.join(d, dict(T.list_fibers(d))[fiber])
    with open(path, 'rb') as fh:
        data = fh.read()
    with open(path, 'wb') as fh:
        fh.write(data[:len(data) // 5])
    return path


def _sampled(d):
    """The fibers frame_facts reads, by the same spread rule it uses."""
    fibers = T.list_fibers(d)
    step = max(1, len(fibers) // T.REEL_SAMPLE)
    return [n for n, _fn in fibers[::step][:T.REEL_SAMPLE]]


@pytest.fixture
def on_a(monkeypatch):
    """Point the viewer's A side at a folder, B at nothing; put both back."""
    monkeypatch.setitem(T.CONFIG, 'dir_b', None)

    def point(d):
        monkeypatch.setitem(T.CONFIG, 'dir_a', d)
    return point


# F7 is in the 24-fiber sample and its length is not one the span estimate
# leans on, so the other eleven settle exactly what all twelve did.
BAD = 7


def test_every_other_fiber_still_loads_exactly_as_before(tmp_path, on_a):
    intact = _folder(tmp_path, 'intact')
    broken = _folder(tmp_path, 'broken')
    assert BAD in _sampled(broken), 'the bad file has to be one frame_facts reads'
    _cut(broken, BAD)

    on_a(intact)
    want = {n: T.load_trace('a', n) for n, _fn in T.list_fibers(intact)}
    on_a(broken)
    for n, _fn in T.list_fibers(broken):
        if n == BAD:
            continue
        got = T.load_trace('a', n)       # raised the bad file's ValueError
        assert got == want[n], f'F{n} loads differently beside a bad file'


def test_the_bad_file_itself_still_fails(tmp_path, on_a):
    """Skipping it as a vote does not make it load: the tech is still told
    that one file is broken (the route turns this into 'parse failed')."""
    broken = _folder(tmp_path, 'broken')
    _cut(broken, BAD)
    on_a(broken)
    with pytest.raises(Exception):
        T.load_trace('a', BAD)


def test_the_frame_is_what_the_good_files_say(tmp_path):
    """The bad file is simply left out of the vote.  Twelve files are all
    sampled, so the folder without the bad file reads the same eleven."""
    keep = set(range(1, 13))
    broken = _folder(tmp_path, 'broken', keep)
    without = _folder(tmp_path, 'without', keep - {5})
    assert _sampled(broken) == sorted(keep)
    _cut(broken, 5)
    assert T.frame_facts(broken) == T.frame_facts(without)

    # And on the full cable the eleven agree with all twelve.
    full = _folder(tmp_path, 'full')
    full_broken = _folder(tmp_path, 'full_broken')
    _cut(full_broken, BAD)
    assert T.frame_facts(full_broken) == T.frame_facts(full)


def test_the_bad_file_is_parsed_once_not_on_every_load(tmp_path, on_a, monkeypatch):
    broken = _folder(tmp_path, 'broken')
    bad_path = _cut(broken, BAD)
    reads = []
    real = T.parse_sor_full

    def counting(path, *a, **k):
        if os.path.samefile(path, bad_path):
            reads.append(path)
        return real(path, *a, **k)
    monkeypatch.setattr(T, 'parse_sor_full', counting)
    on_a(broken)
    for n in (1, 2, 3, 4, 5, 6):
        assert T.load_trace('a', n) is not None
    assert len(reads) == 1, f'the bad file was parsed {len(reads)} times'


@pytest.fixture(scope='module')
def viewer():
    srv = HTTPServer(('127.0.0.1', 0), T.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_port
    finally:
        srv.shutdown()
        srv.server_close()


def _get(port, path):
    with urlopen(f'http://127.0.0.1:{port}{path}', timeout=60) as r:
        return r.status, json.loads(r.read())


def test_the_list_and_the_overview_keep_the_good_fibers(tmp_path, on_a, viewer):
    intact = _folder(tmp_path, 'intact')
    broken = _folder(tmp_path, 'broken')
    _cut(broken, BAD)
    on_a(broken)

    _status, listing = _get(viewer, '/api/list')
    assert 'error' not in listing, listing.get('error')
    assert listing['fibers_a'] == list(range(1, 25))    # was [] on main
    assert listing['launch_a_km'] == T.frame_facts(intact)['launch_km']

    good = [n for n in range(1, 25) if n != BAD]
    spec = ','.join(str(n) for n in good)
    status, bulk = _get(viewer, f'/api/traces?dir=a&fibers={spec}')
    assert status == 200
    assert bulk['missing'] == []                        # was all 23
    assert [t['fiber'] for t in bulk['traces']] == good

    status, one = _get(viewer, '/api/trace?dir=a&fiber=2')
    assert status == 200 and one['fiber'] == 2
