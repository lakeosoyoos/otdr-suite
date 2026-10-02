"""A Viewer drop ADDS to what is loaded; nothing loaded is reset by a drop.

Robert 2026-10-02, after the boss dragged a new set of files into the FILES
panel and lost everything he had in there: "we need to be able to drag in as
much as we want into the right panel and have it keep adding and nothing gets
reset until the person does it".  Two more rules came with it:

* a file byte for byte one already loaded is not loaded twice, and the page
  says it is already in there (dragging 10 of the 29 A files in again used to
  load them as the B side);
* a file that is NOT byte for byte one already loaded is added, as a row of
  its own, "even if the directions are the same" -- a second file of a fibre
  is listed under its own id (trace_server.COPY_BASE) and shown as
  "12 (copy 2)".

Real traces from the splice fixtures, which stamp their direction.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'viewer'))
import trace_server as TS                       # noqa: E402
from conftest import FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR   # noqa: E402

JSC = ('/System/Library/Frameworks/JavaScriptCore.framework/'
       'Versions/Current/Helpers/jsc')


@pytest.fixture(autouse=True)
def _own_temp(tmp_path, monkeypatch):
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    import tempfile
    tempfile.tempdir = None
    TS.set_dirs(None, None)
    yield
    tempfile.tempdir = None
    TS.set_dirs(None, None)
    TS.CONFIG.pop('dropped_at', None)


def _files(side):
    d = FIXTURE_SPLICE_A_DIR if side == 'a' else FIXTURE_SPLICE_B_DIR
    return [(p.name, p.read_bytes()) for p in sorted(d.glob('*.sor'))]


def _drop(files, emptied=''):
    tok = TS.drop_begin()
    for name, data in files:
        TS.drop_file(tok, name, data)
    return TS.drop_end(tok, emptied=emptied)


def _ids(d):
    return [n for n, _ in TS.list_fibers(d)]


def test_part_of_a_loaded_folder_again_is_already_in_not_the_b_side():
    """The boss's case: A loaded, some of the same A files dragged in again.
    They used to load as B; now nothing moves and the page is told."""
    a = _drop(_files('a'))
    again = _drop(_files('a')[:10])
    assert again['added'] == '' and again['dir_b'] is None
    assert again['already'] == sorted(n for n, _ in _files('a')[:10])
    assert again['dir_a'] == a['dir_a'] and again['a_count'] == a['a_count']
    assert 'dropped_at' in TS.CONFIG                # from the first drop only
    TS.CONFIG.pop('dropped_at')
    _drop(_files('a')[:3])
    assert 'dropped_at' not in TS.CONFIG            # nothing changed, hub left alone


def test_more_fibres_of_a_loaded_side_join_it():
    """Fibres 1-10 of A, then the rest of A: the rest goes to A, not to the
    empty B side."""
    a_files = _files('a')
    first = _drop(a_files[:10])
    rest = _drop(a_files[10:])
    assert rest['added'] == 'A' and rest['grown'] == 'A'
    assert rest['dir_a'] == first['dir_a'] and rest['dir_b'] is None
    assert rest['a_count'] == len(a_files)
    assert len(rest['new_keys']) == len(a_files) - 10
    b = _drop(_files('b'))
    assert b['added'] == 'B' and b['dir_a'] == first['dir_a']


def test_a_new_set_with_both_sides_loaded_keeps_both():
    a = _drop(_files('a')[:8])
    b = _drop(_files('b')[:8])
    more = _drop(_files('a')[8:16] + _files('b')[8:16])
    assert more['added'] == 'AB' and more['grown'] == 'AB'
    assert (more['dir_a'], more['dir_b']) == (a['dir_a'], b['dir_b'])
    assert more['a_count'] == 16 and more['b_count'] == 16
    assert _ids(more['dir_a']) == sorted(_ids(more['dir_a']))


def test_a_different_file_of_a_loaded_fibre_is_a_row_of_its_own():
    """A reshoot under the same name (different bytes): its own row, the
    loaded file untouched and still fibre 5's own id."""
    a_files = _files('a')
    a = _drop(a_files)
    name, data = a_files[4]
    reshoot = bytearray(data)
    reshoot[-1] ^= 1
    fid = TS.extract_fiber_num(name)
    out = _drop([(name, bytes(reshoot))])
    assert out['added'] == 'A' and out['copies'] == [name]
    assert out['new_keys'] == ['a-%d' % (fid + TS.COPY_BASE)]
    listed = dict(TS.list_fibers(a['dir_a']))
    assert listed[fid] == name
    with open(os.path.join(a['dir_a'], listed[fid + TS.COPY_BASE]), 'rb') as fh:
        assert fh.read() == bytes(reshoot)
    # dropped again, it is already in
    again = _drop([(name, bytes(reshoot))])
    assert again['added'] == '' and again['already'] == [name]


def test_the_listing_says_which_ids_are_copies(tmp_path):
    d = tmp_path / 'multi'
    d.mkdir()
    src = sorted(FIXTURE_SPLICE_A_DIR.glob('*.sor'))
    for i, p in enumerate(src[:2]):
        (d / ('SPAN001_%d.sor' % (1310 + i * 240))).write_bytes(p.read_bytes())
    (d / 'SPAN002_1550.sor').write_bytes(src[2].read_bytes())
    # the LAST of a fibre's files keeps the fibre's number, as {n: fn} maps
    # last-win did, so a report cell opens the same file it always did
    assert TS.list_fibers(str(d)) == [(1, 'SPAN001_1550.sor'),
                                      (1 + TS.COPY_BASE, 'SPAN001_1310.sor'),
                                      (2, 'SPAN002_1550.sor')]
    assert TS.copy_of(1 + TS.COPY_BASE) == (1, 2) and TS.copy_of(2) == (2, 1)


def test_a_hub_folder_is_never_written_to(tmp_path):
    """A side picked in the hub that a drop grows is copied first: the tech's
    own folder keeps exactly what it had, and the Viewer keeps its rows."""
    own = tmp_path / 'job A'
    own.mkdir()
    for p in sorted(FIXTURE_SPLICE_A_DIR.glob('*.sor'))[:6]:
        (own / p.name).write_bytes(p.read_bytes())
    before = sorted(os.listdir(own))
    TS.set_dirs(str(own), None)
    TS.set_viewer_state({'keys': ['a-1'], 'removed': ['a-2']})
    name, data = _files('a')[6]
    out = _drop([(name, data)])
    assert out['added'] == 'A' and out['grown'] == 'A'
    assert out['dir_a'] != str(own) and sorted(os.listdir(own)) == before
    assert out['a_count'] == 7 and out['a_name'] == 'job A'
    assert TS.removed_keys() == {'a-2'}             # still removed on the copy


def test_a_side_the_tech_emptied_is_still_replaced():
    """Removing every file of a side is the tech resetting it: a drop there
    starts that side afresh."""
    a = _drop(_files('a')[:5])
    out = _drop(_files('a')[5:10], emptied='a')
    assert out['added'] == 'A' and out['grown'] == ''
    assert out['dir_a'] != a['dir_a'] and out['a_count'] == 5


def test_the_page_labels_a_copy_by_its_fibre():
    if not os.path.exists(JSC):
        pytest.skip('JavaScriptCore shell (macOS) not present')
    src = open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8').read()
    start = src.index('const COPY_BASE = 100000;')
    end = src.index('\n}', src.index('function fiberLabel(id)')) + 2
    js = src[start:end] + r"""
gCopies = { '100012': [12, 2], '200012': [12, 3] };
print(JSON.stringify([fiberLabel(12), fiberLabel(100012), fiberLabel(200012),
                      realFiber(100012), realFiber(7)]));
"""
    r = subprocess.run([JSC, '-e', js], capture_output=True, text=True, timeout=30)
    assert r.stdout.strip() == '["12","12 (copy 2)","12 (copy 3)",12,7]', r.stdout + r.stderr
