"""Every dropped file goes on the side its own Direction stamp names, in the
span its sites or name say (Robert and the boss, 2026-10-02).

  * "if we load 1 and then we drag in 2 through 12 it makes it B side".
  * "they will always have an A or B label": the label is the stamp, and it
    wins over the file's name; a file stamped wrong is fixed by hand
    (right-click > Direction).  A real B folder held 4 re-shots stamped A:
    they load on A, as copy rows of A's own fibres, and are named on the
    readout.
  * A different span dropped beside a loaded one is added as a span of its
    own ('a2' / 'b2'), never into the loaded span's sides.
A drop only adds, and a file byte for byte one loaded is not loaded again:
test_viewer_drops_keep_adding.py.
"""
from __future__ import annotations

import os

import pytest

from conftest import import_trace_server, FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR
from test_sor_writer import make_sor

TS = import_trace_server()


@pytest.fixture(autouse=True)
def _own_temp(tmp_path, monkeypatch):
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    import tempfile
    tempfile.tempdir = None
    TS.set_dirs(None, None)
    yield
    tempfile.tempdir = None
    TS.set_dirs(None, None)


def _drop_bytes(*groups, emptied=''):
    tok = TS.drop_begin()
    for g in groups:
        for name, data in g:
            TS.drop_file(tok, name, data)
    return TS.drop_end(tok, emptied=emptied)


def _drop(names, ior=1.47, sites=None):
    data = make_sor(ior=ior)
    if sites:
        data = TS.set_identifiers(data, loc_a=sites[0], loc_b=sites[1])
    return _drop_bytes([(n, data) for n in names])


def _real(side, fibers, rename=None):
    """Real fixture files (they carry EXFO's Direction stamp) of `fibers`."""
    d = FIXTURE_SPLICE_A_DIR if side == 'a' else FIXTURE_SPLICE_B_DIR
    out = []
    for n in fibers:
        p = next(d.glob(f'*{n:04d}_1550.sor'))
        out.append((rename(p.name) if rename else p.name, p.read_bytes()))
    return out


def _ids(src):
    return [n for n, _ in TS.list_fibers(TS.src_dir(src))]


# ── the report: fiber 1, then 2-12 ─────────────────────────────────────

@pytest.mark.parametrize('name', ['ELMMIL{:03d}_1550.sor', '{:04d}_1550.sor'])
def test_fiber_1_then_2_to_12_all_load_on_a(name):
    out = _drop([name.format(1)])
    assert out['added'] == 'A' and out['a_count'] == 1
    out = _drop([name.format(n) for n in range(2, 13)], ior=1.4682)
    assert out['added'] == 'A' and out['grown'] == 'A'
    assert out['a_count'] == 12 and out['dir_b'] is None
    assert _ids('a') == list(range(1, 13))


def test_real_stamped_files_fiber_1_then_2_to_12_load_on_a():
    _drop_bytes(_real('a', [1]))
    out = _drop_bytes(_real('a', range(2, 13)))
    assert out['added'] == 'A' and out['added_by'] == 'file'
    assert out['a_count'] == 12 and out['dir_b'] is None


# ── the stamp wins over the name ───────────────────────────────────────

def test_a_b_folder_holding_an_a_stamped_reshot_loads_it_on_a():
    """A dropped, then a B folder with one re-shot stamped A.  Every
    A trace stays, the 23 B files go on B, and the re-shot goes on A as a
    copy row of A's own fibre 5, named on the readout for the tech."""
    a = _drop_bytes(_real('a', range(1, 25)))
    reshot = _real('a', [5], rename=lambda n: 'MILELM' + n[6:])     # A bytes, B name
    b = _drop_bytes([f for f in _real('b', range(1, 25)) if '0005' not in f[0]], reshot)
    assert b['dir_a'] == a['dir_a'] and b['a_count'] == 25        # A untouched, + the copy
    assert b['b_count'] == 23 and 5 not in _ids('b')
    assert 5 + TS.COPY_BASE in _ids('a')                          # "5 (copy 2)"
    assert b['copies'] == ['MILELM0005_1550.sor']
    assert b['against_name'] == ['MILELM0005_1550.sor']


def test_two_directions_both_stamped_a_load_on_a():
    """A span whose two directions both stamp A: the stamp rule stands, both
    load on A (the second as copy rows of the same fibres), and the tech
    fixes one."""
    first = _drop_bytes(_real('a', (1, 2, 3), rename=lambda n: 'ABCDEF' + n[6:]))
    out = _drop_bytes(_real('a', (1, 2, 3), rename=lambda n: 'DEFABC' + n[6:]))
    assert out['added'] == 'A' and out['dir_a'] == first['dir_a'] and out['dir_b'] is None
    assert len(out['copies']) == 3


def test_unstamped_files_still_go_by_their_names():
    """A file with no stamp (.json, .trc, another make) is placed as before:
    the side named like it, then its fibres."""
    _drop([f'ELMMIL{n:03d}_1550.sor' for n in (1, 2, 3)])
    out = _drop([f'MILELM{n:03d}_1550.sor' for n in (1, 2, 3)])
    assert out['added'] == 'B' and out['a_count'] == 3 and out['b_count'] == 3
    out = _drop([f'ELMMIL{n:03d}_1550.sor' for n in (4, 5)])
    assert out['added'] == 'A' and out['a_count'] == 5


# ── another span is added, as a span of its own ───────────────────────

def test_another_span_is_added_beside_the_loaded_one():
    a = _drop([f'ELMMIL{n:03d}_1550.sor' for n in (1, 2, 3)], sites=('ELM', 'MIL'))
    b = _drop([f'MILELM{n:03d}_1550.sor' for n in (1, 2, 3)], sites=('ELM', 'MIL'))
    out = _drop([f'ROMTUC{n:03d}_1550.sor' for n in (1, 2, 3)], sites=('ROM', 'TUC'))
    assert out['new_spans'] == [2] and out['touched'] == ['a2'] and out['added'] == ''
    assert out['dir_a'] == a['dir_a'] and out['dir_b'] == b['dir_b']
    assert out['a_count'] == 3 and out['b_count'] == 3          # span 1 as it was
    assert _ids('a2') == [1, 2, 3]
    assert [s['span'] for s in TS._list_spans()] == [1, 2]
    # its other direction joins it, not span 1
    out = _drop([f'TUCROM{n:03d}_1550.sor' for n in (1, 2, 3)], sites=('ROM', 'TUC'))
    assert out['touched'] == ['b2'] and out['new_spans'] == []
    assert _ids('b2') == [1, 2, 3]


def test_a_site_typo_on_one_direction_is_still_the_same_span():
    """The real fixture's A and B files spell one site two ways."""
    _drop_bytes(_real('a', (1, 2, 3)))
    out = _drop_bytes(_real('b', (1, 2, 3)))
    assert out['added'] == 'B' and out['new_spans'] == []


def test_new_rows_of_another_span_are_keyed_by_its_source():
    _drop([f'ELMMIL{n:03d}_1550.sor' for n in (1, 2)], sites=('ELM', 'MIL'))
    out = _drop([f'ROMTUC{n:03d}_1550.sor' for n in (1, 2)], sites=('ROM', 'TUC'))
    assert out['new_keys'] == ['a2-1', 'a2-2']


def test_the_readout_names_what_a_drop_did():
    h = open(os.path.join(os.path.dirname(TS.__file__), 'viewer.html'), encoding='utf-8').read()
    assert 'j.against_name' in h and 'j.new_spans' in h
