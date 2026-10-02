"""Files mislabeled for direction still load into the Viewer.

The boss, 2026-09-29: "if we drag in files and they are mislabeled for
direction we still want to be able to load them and then we can fix direction
in the app after they are in viewer".  The fix in the app is the Files panel's
right-click Direction menu; it can only fix a file the drop loaded.  Three
shapes of mislabel used to lose files on main:

  1. One A file named with B's site order split an A-only drop into two
     "directions" (23 files and 1).  Both sides then looked full, so dropping
     the real B folder started a new span and threw every A trace away.
  2. A third spelling of the name was a third group, and was ignored.
  3. A mislabeled file that took the other direction's name collided with the
     real file of that name, and the second to arrive was not loaded.

Real fixture spans (ELMMIL = A, MILELM = B, 24 fibres each, every file
stamped with its direction) wherever the stamp matters.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'viewer'))
import trace_server as TS                       # noqa: E402
from test_sor_writer import make_sor            # noqa: E402
from conftest import FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR   # noqa: E402


@pytest.fixture(autouse=True)
def _own_temp(tmp_path, monkeypatch):
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    import tempfile
    tempfile.tempdir = None
    yield
    tempfile.tempdir = None
    TS.set_dirs(None, None)
    TS.CONFIG.pop('dropped_at', None)


def _real(side, n=24):
    d = FIXTURE_SPLICE_A_DIR if side == 'a' else FIXTURE_SPLICE_B_DIR
    return [(p.name, p.read_bytes()) for p in sorted(d.glob('*.sor'))[:n]]


def _rename(files, fibers, code):
    """Give `fibers` of `files` another span code: the mislabel."""
    return [((code + n[6:]) if int(n[6:10]) in fibers else n, d) for n, d in files]


def _drop(*groups):
    tok = TS.drop_begin()
    for g in groups:
        for name, data in g:
            TS.drop_file(tok, name, data)
    return TS.drop_end(tok)


def _dirs_of(folder):
    """{fiber: stamped direction} of every file on one side."""
    out = {}
    for f in sorted(os.listdir(folder)):
        with open(os.path.join(folder, f), 'rb') as fh:
            out[TS.extract_fiber_num(f)] = TS.read_direction(fh.read())
    return out


# ── 1. a few A files named as B ─────────────────────────────────────────

def test_one_a_file_named_as_b_keeps_the_a_folder_whole():
    a = _drop(_rename(_real('a'), {12}, 'MILELM'))
    assert a['added'] == 'A' and a['a_count'] == 24
    assert a['dir_b'] is None and a['b_count'] == 0
    assert a['kept_whole'] == ['MILELM'] and a['a_prefix'] == 'ELMMIL'
    # and the real B folder after it fills B, with all of A still loaded
    b = _drop(_real('b'))
    assert b['added'] == 'B'
    assert b['dir_a'] == a['dir_a'] and b['a_count'] == 24 and b['b_count'] == 24


def test_a_block_of_a_files_named_as_b_stays_on_one_side():
    out = _drop(_rename(_real('a'), set(range(13, 25)), 'MILELM'))
    assert out['added'] == 'A' and out['a_count'] == 24 and out['b_count'] == 0


def test_the_mislabeled_files_are_still_loaded_as_a():
    """They load where the rest of their folder is.  Their direction is the
    tech's to fix from the Files panel, not the drop's to guess."""
    out = _drop(_rename(_real('a'), {5, 6}, 'MILELM'))
    assert set(_dirs_of(out['dir_a']).values()) == {'a'}
    assert sorted(_dirs_of(out['dir_a'])) == list(range(1, 25))


def test_a_real_bidirectional_drop_still_splits():
    """A span's two directions shoot the same fibres, so they overlap."""
    out = _drop(_real('a'), _real('b'))
    assert out['added'] == 'AB' and out['kept_whole'] == []
    assert out['a_count'] == 24 and out['b_count'] == 24


def test_files_that_say_a_and_b_are_split_even_when_the_fibres_do_not_overlap():
    out = _drop(_real('a')[:12], _real('b')[12:])
    assert out['added'] == 'AB' and out['added_by'] == 'file'
    assert out['kept_whole'] == []


def test_fibres_that_do_not_make_one_run_are_not_one_folder():
    """Fibre 1 of one span and fibre 9 of another fill no hole."""
    tok = TS.drop_begin()
    TS.drop_file(tok, 'ROMTUC001_1550.sor', make_sor(ior=1.47))
    TS.drop_file(tok, 'TUCROM009_1550.sor', make_sor(ior=1.47))
    out = TS.drop_end(tok)
    assert out['added'] == 'AB' and out['kept_whole'] == []


# ── 2. a third spelling ─────────────────────────────────────────────────

def test_a_third_spelling_joins_the_side_its_fibres_are_missing_from():
    a = _rename(_real('a'), {22, 23, 24}, 'ELMLIM')     # a typo, not a variant
    out = _drop(a, _real('b'))
    assert out['ignored'] == []
    assert out['folded'] == [{'key': 'ELMLIM', 'into': 'ELMMIL'}]
    assert out['a_count'] == 24 and out['b_count'] == 24
    assert set(_dirs_of(out['dir_a']).values()) == {'a'}


def test_another_span_in_the_same_drop_is_still_ignored():
    """A third group whose fibres both sides already hold is not a mislabel."""
    other = [('KNOTOO' + n[6:], d) for n, d in _real('a', 6)]
    out = _drop(_real('a'), _real('b'), other)
    assert out['ignored'] == ['KNOTOO'] and out['folded'] == []
    assert out['a_count'] == 24 and out['b_count'] == 24


# ── 3. a mislabeled name that collides ──────────────────────────────────

def test_an_a_file_under_bs_name_is_loaded_on_the_other_side():
    """A12 carries B's name, so it and the real B12 arrive under one name.
    Whichever arrives second used to be lost.  Now it goes on the side its
    twin is not on, and the tech flips the two in the Viewer."""
    a = _rename(_real('a'), {12}, 'MILELM')
    out = _drop(a, _real('b'))
    assert out['repeated'] == [] and out['repeats_placed'] == ['MILELM0012_1550.sor']
    assert out['a_count'] == 24 and out['b_count'] == 24
    got = {**{('A', k): v for k, v in _dirs_of(out['dir_a']).items()},
           **{('B', k): v for k, v in _dirs_of(out['dir_b']).items()}}
    # both fibre-12 files loaded, one on each side
    assert sorted([got[('A', 12)], got[('B', 12)]]) == ['a', 'b']


def test_a_retest_of_the_same_direction_is_not_moved_across():
    """Same name, same stamp, different bytes: a re-shot fibre, not the
    other direction.  It stays reported, as before."""
    a = _real('a', 6)
    retest = [(a[2][0], a[3][1])]                  # another A shot under F3's name
    out = _drop(a, retest)
    assert out['repeats_placed'] == [] and out['repeated'] == [a[2][0]]
    assert out['dir_b'] is None


def test_an_identical_copy_is_not_moved_across():
    a = _real('a', 6)
    out = _drop(a, a[:2])
    assert out['repeats_placed'] == []
    assert out['dir_b'] is None and out['a_count'] == 6


def test_a_named_repeat_with_no_stamp_is_not_moved_across():
    """A synthetic .sor has no stamp; its name says the same side as its twin."""
    tok = TS.drop_begin()
    TS.drop_file(tok, 'ROMTUC001_1550.sor', make_sor(raw_payload=b'\xa1' * 40))
    TS.drop_file(tok, 'ROMTUC001_1550.sor', make_sor(raw_payload=b'\xb2' * 90))
    out = TS.drop_end(tok)
    assert out['repeats_placed'] == [] and out['repeated'] == ['ROMTUC001_1550.sor']


# ── the page says what the drop did ─────────────────────────────────────

def test_the_readout_names_what_was_kept_folded_and_placed():
    """Robert 2026-09-30: a file moved to the other side, or kept on one side
    against its label, is a bold orange warning; a file not loaded is a bold
    red failure; plain notes are not shown."""
    h = open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8').read()
    fn = h.split('async function handleFilesDrop(dt, pathsP) {', 1)[1].split('\n}', 1)[0]
    for key in ('j.kept_whole && j.kept_whole.length', 'for (const f of (j.folded || []))',
                'j.repeats_placed && j.repeats_placed.length', 'if (j.sites_swapped) {'):
        block = fn.split(key, 1)[1].split('\n    }', 1)[0]
        assert 'warn.push(' in block and 'msg +=' not in block, key
    ign = fn.split('if (j.ignored && j.ignored.length) {', 1)[1].split('\n    }', 1)[0]
    assert 'fail.push(`not loaded: ' in ign
    assert "if (moved) warn.push('to change a file\\'s direction, right-click it > Direction');" in fn
    assert fn.index('j.kept_whole') < fn.index('j.ignored') < fn.index(
        "setReadout(msg, { warn: warn.join(' · '), fail: fail.join(' · ') });")


def test_hovering_the_readout_shows_the_whole_message():
    """The readout is one line and a drop's warning runs past the window edge,
    taking the right-click hint with it.  Its tooltip carries the full text."""
    h = open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8').read()
    fn = h.split('function setReadout(s, extra = {}) {', 1)[1].split('\n}', 1)[0]
    assert "el.textContent = shown.map(([t]) => t).join('   ');" in fn
    assert 'el.title = el.textContent;' in fn
    assert fn.index('el.title = el.textContent;') < fn.index('if (!shown.length) return;')


# ── the batch route (/api/drop_files) and the page's retry ──────────────

def _pack(items):
    """drop_files' body: per file a 4-byte name length, the name, a 4-byte
    length and the bytes (viewer.html _packDropBatch)."""
    out = b''
    for name, data in items:
        n = name.encode('utf-8')
        out += len(n).to_bytes(4, 'big') + n + len(data).to_bytes(4, 'big') + data
    return out


def test_a_batched_drop_places_the_mislabeled_file_too():
    a = _rename(_real('a'), {12}, 'MILELM')
    tok = TS.drop_begin()
    TS.drop_files(tok, _pack(a))
    TS.drop_files(tok, _pack(_real('b')))
    out = TS.drop_end(tok)
    assert out['repeats_placed'] == ['MILELM0012_1550.sor'] and out['repeated'] == []
    assert out['a_count'] == 24 and out['b_count'] == 24


def test_a_batched_a_folder_with_one_b_name_stays_whole():
    tok = TS.drop_begin()
    TS.drop_files(tok, _pack(_rename(_real('a'), {12}, 'MILELM')))
    out = TS.drop_end(tok)
    assert out['added'] == 'A' and out['a_count'] == 24 and out['kept_whole'] == ['MILELM']


def test_a_retried_batch_is_not_a_repeat():
    """The page retries a batch whose answer was lost: every file is already
    in, byte for byte, so nothing is a repeat and nothing is placed."""
    a = _real('a', 6)
    tok = TS.drop_begin()
    TS.drop_files(tok, _pack(a))
    TS.drop_files(tok, _pack(a), retry=True)
    out = TS.drop_end(tok)
    assert out['repeated'] == [] and out['repeats_placed'] == []
    assert out['a_count'] == 6 and out['dir_b'] is None


def test_a_retry_of_the_other_directions_same_name_file_is_kept():
    """A parent folder of two site-less subfolders: the A batch arrives, then
    the B batch's first try is lost before it is staged and the page retries.
    B's files carry A's names, so a retry skipped on the name alone would
    lose all of B.  Only an identical staged copy counts as already in."""
    un = lambda files: [('%04d_1550.sor' % (i + 1), d) for i, (_n, d) in enumerate(files)]
    tok = TS.drop_begin()
    TS.drop_files(tok, _pack(un(_real('a', 3))))
    TS.drop_files(tok, _pack(un(_real('b', 3))), retry=True)
    out = TS.drop_end(tok)
    assert out['added'] == 'AB' and out['a_count'] == 3 and out['b_count'] == 3
    assert len(out['repeats_placed']) == 3 and out['repeated'] == []
