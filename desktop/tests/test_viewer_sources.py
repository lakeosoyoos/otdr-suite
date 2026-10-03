"""Every folder the Viewer holds is a source: span 1's 'a' / 'b' (the hub's
boxes), another span's 'a2' / 'b2' (Robert 2026-10-02: a different span is
added, never swapped in).  A second file of a fibre is a copy row inside
its source (COPY_BASE), not a source."""
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
    TS.reset_viewer_state()
    yield
    tempfile.tempdir = None
    TS.set_dirs(None, None)
    TS.reset_viewer_state()


def _folder(tmp_path, name, fibers, sites=('ROM', 'TUC')):
    d = tmp_path / name
    d.mkdir()
    for n in fibers:
        data = TS.set_identifiers(make_sor(ior=1.47), loc_a=sites[0], loc_b=sites[1])
        (d / f'{name}{n:03d}_1550.sor').write_bytes(data)
    return str(d)


def test_source_ids():
    assert TS.parse_src('a') == ('a', 1) and TS.parse_src('b2') == ('b', 2)
    assert TS.parse_src('b31') == ('b', 31)
    for bad in ('', 'c', 'a1', 'ar2', 'a0', 'A', 'a-1', '../a'):
        assert TS.parse_src(bad) is None, bad
    assert [TS.src_id(*TS.parse_src(s)) for s in ('a', 'b2', 'a31')] == ['a', 'b2', 'a31']


def test_the_hub_sets_span_1_and_the_other_spans_stay(tmp_path):
    a, b = _folder(tmp_path, 'ROMTUC', (1, 2)), _folder(tmp_path, 'TUCROM', (1, 2))
    s2 = _folder(tmp_path, 'AAABBB', (1, 2), sites=('AAA', 'BBB'))
    TS.set_dirs(a, b)
    TS.set_sources({'a2': s2})
    assert TS.sources() == [('a', a), ('b', b), ('a2', s2)]
    TS.set_dirs(a, b)                       # every hub rerun: nothing moves
    assert TS.src_dir('a2') == s2
    TS.set_dirs(None, None)                 # Clear Traces: everything goes
    assert TS.sources() == []


def test_a_new_span_1_folder_leaves_the_other_spans(tmp_path):
    a, b = _folder(tmp_path, 'ROMTUC', (1,)), _folder(tmp_path, 'TUCROM', (1,))
    s2 = _folder(tmp_path, 'AAABBB', (1,), sites=('AAA', 'BBB'))
    TS.set_dirs(a, b)
    TS.set_sources({'a2': s2})
    TS.set_dirs(_folder(tmp_path, 'OTHER', (1,)), b)   # the hub's A box moved
    assert TS.src_dir('a2') == s2


def test_unloading_one_source_keeps_the_rest(tmp_path):
    a, b = _folder(tmp_path, 'ROMTUC', (1,)), _folder(tmp_path, 'TUCROM', (1,))
    s2 = _folder(tmp_path, 'AAABBB', (1,), sites=('AAA', 'BBB'))
    TS.set_dirs(a, b)
    TS.set_sources({'a2': s2})
    TS.unload_sides('a2')
    assert TS.sources() == [('a', a), ('b', b)]
    TS.set_sources({'a2': s2})
    TS.unload_sides('ab')                   # span 1's two sides, as the page sends them
    assert TS.sources() == [('a2', s2)]


def test_list_reports_every_source_and_span(tmp_path):
    a, b = _folder(tmp_path, 'ROMTUC', (1, 2)), _folder(tmp_path, 'TUCROM', (1, 2))
    s2 = _folder(tmp_path, 'AAABBB', (3,), sites=('AAA', 'BBB'))
    TS.set_dirs(a, b)
    TS.set_sources({'a2': s2})
    rows = TS._list_sources()
    assert [(r['src'], r['span'], r['side'], r['fibers'], r['copies']) for r in rows] == [
        ('a', 1, 'a', [1, 2], {}), ('b', 1, 'b', [1, 2], {}), ('a2', 2, 'a', [3], {})]
    assert [s['span'] for s in TS._list_spans()] == [1, 2]
    assert TS._list_spans()[1]['name_a'] == 'AAABBB' and TS._list_spans()[1]['name_b'] is None
    sig = TS.sources_sig()
    TS.unload_sides('a2')
    assert TS.sources_sig() != sig          # an open page sees it on its next poll


def test_removed_files_hold_per_span(tmp_path):
    a, b = _folder(tmp_path, 'ROMTUC', (1, 2)), _folder(tmp_path, 'TUCROM', (1, 2))
    s2 = _folder(tmp_path, 'AAABBB', (1, 2), sites=('AAA', 'BBB'))
    TS.set_dirs(a, b)
    TS.set_sources({'a2': s2})
    TS.set_viewer_state({'keys': [], 'removed': ['a-1', 'a2-2']})
    assert TS.removed_keys() == {'a-1'}                      # the hub reads span 1's
    assert TS.removed_names(s2) == ['AAABBB002_1550.sor']
    # span 2's folder replaced: its removals no longer apply, span 1's do
    TS.set_sources({'a2': _folder(tmp_path, 'BBBAAA', (1, 2), sites=('AAA', 'BBB'))})
    assert TS.removed_keys() == {'a-1'} and TS.removed_names(s2) == []


def test_a_report_is_never_saved_in_any_source_folder(tmp_path):
    a = _folder(tmp_path, 'ROMTUC', (1,))
    s2 = _folder(tmp_path, 'AAABBB', (1,), sites=('AAA', 'BBB'))
    TS.set_dirs(a, None)
    TS.set_sources({'a2': s2})
    with pytest.raises(ValueError):
        TS.report_dest(s2)


def test_the_per_fiber_table_of_a_named_pair(tmp_path, monkeypatch):
    """/api/fr_table?pairs=1:a2:b2 builds span 2's fiber 1 from ITS files,
    keyed by the pair, never span 1's of the same number."""
    a, b = _folder(tmp_path, 'ROMTUC', (1,)), _folder(tmp_path, 'TUCROM', (1,))
    a2, b2 = str(FIXTURE_SPLICE_A_DIR), str(FIXTURE_SPLICE_B_DIR)
    TS.set_dirs(a, b)
    TS.set_sources({'a2': a2, 'b2': b2})
    seen = []

    def fake_run(cmd, **kw):
        import json as _j
        spec = cmd[cmd.index('--fr-table-file') + 1]
        jobs = _j.load(open(spec, encoding='utf-8'))
        seen.extend(jobs)

        class P:
            stdout = _j.dumps({'ok': True, 'tables': {str(j[0]): [{'row': 1}] for j in jobs}})
            stderr = ''
        return P()
    monkeypatch.setattr(TS.subprocess, 'run', fake_run)
    res = TS.fr_tables([], pairs=[(1, 'a2', 'b2')])
    assert list(res['tables']) == ['1:a2:b2'] and res['error'] is None
    assert seen[0][0] == '1:a2:b2'
    assert os.path.dirname(seen[0][1]) == a2 and os.path.dirname(seen[0][2]) == b2


def test_a_pair_of_two_ids_reads_each_file_and_span_1s_pair_keeps_its_key(monkeypatch):
    """A file the tech set to its right direction pairs with its partner
    under another id ('3:b:b:100003': both files on the B side, the second
    listed as "3 (copy 2)").  Span 1's own A and B of one id, asked in the
    same query as 'f:a:b', is keyed by the fibre alone, as the page reads it."""
    TS.set_dirs('/A', '/B')
    monkeypatch.setattr(TS, '_fiber_path', lambda d, f: f'{d}/{f}.sor')
    monkeypatch.setattr(TS.os.path, 'getmtime', lambda p: 1.0)
    TS._FR_TABLE_CACHE.clear()
    seen = []

    def fake_run(cmd, **kw):
        import json as _j
        jobs = _j.load(open(cmd[cmd.index('--fr-table-file') + 1], encoding='utf-8'))
        seen.extend(jobs)

        class P:
            stdout = _j.dumps({'ok': True, 'tables': {str(j[0]): [{'row': 1}] for j in jobs}})
            stderr = ''
        return P()
    monkeypatch.setattr(TS.subprocess, 'run', fake_run)
    res = TS.fr_tables([], pairs=[(1, 'a', 'b'), (3, 'b', 'b', 100003)])
    assert sorted(res['tables']) == ['1', '3:b:b:100003'] and res['error'] is None
    by = {j[0]: j[1:] for j in seen}
    assert by['1'] == ['/A/1.sor', '/B/1.sor']
    assert by['3:b:b:100003'] == ['/B/3.sor', '/B/100003.sor']
    TS._FR_TABLE_CACHE.clear()


def test_each_trace_carries_the_ids_it_states(tmp_path):
    """/api/trace carries the file's Cable ID and Fiber ID: FastReporter's
    pairing key (the page pairs on them, pairTraces / idsMatch)."""
    d = tmp_path / 'ids'
    d.mkdir()
    (d / 'AAABBB003_1550.sor').write_bytes(
        TS.set_identifiers(make_sor(), cable_id='CAB1', fiber_id='0003'))
    assert TS._file_ids(str(d), 'AAABBB003_1550.sor') == ('CAB1', '0003')
    (d / 'x.json').write_text('{}', encoding='utf-8')
    assert TS._file_ids(str(d), 'x.json') == (None, None)


def _drop_at(sites, names, ior=1.47):
    tok = TS.drop_begin()
    for n in names:
        TS.drop_file(tok, n, TS.set_identifiers(make_sor(ior=ior), loc_a=sites[0], loc_b=sites[1]))
    return TS.drop_end(tok)


def test_the_hub_hears_only_of_a_drop_that_changed_span_1():
    """The hub's boxes and reports follow span 1 (app.py: a newer
    `dropped_at` resets its report grids).  A drop that only added span 2 or
    a copy left span 1 as it was, and used to throw the hub's reports away."""
    TS.CONFIG.pop('dropped_at', None)
    _drop_at(('ROM', 'TUC'), ['ROMTUC001_1550.sor'])          # span 1's A: the hub hears
    first = TS.CONFIG.get('dropped_at')
    assert first
    _drop_at(('AAA', 'BBB'), ['AAABBB001_1550.sor'])          # span 2 added
    assert TS.CONFIG.get('dropped_at') == first
    _drop_at(('AAA', 'BBB'), ['AAABBB001_1550.sor'], ior=1.4682)   # a copy row in span 2
    assert TS.CONFIG.get('dropped_at') == first
    TS.unload_sides('a2')                                     # span 2 let go of
    assert TS.CONFIG.get('dropped_at') == first
    _drop_at(('ROM', 'TUC'), ['TUCROM001_1550.sor'])          # span 1's B: the hub hears
    assert TS.CONFIG.get('dropped_at') > first
