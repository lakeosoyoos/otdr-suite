"""Report file names say what made them (Robert 2026-10-01).

A Splice Report was saved as <A>_to_<B>_SpliceReport.xlsx and a
Unidirectional report as unidirectional_events.xlsx, whatever the span, the
analysis mode or the gate.  Now:

  <A-end>_to_<B-end>_SpliceReport_<MODE>_<gate>.xlsx
  <from>_to_<to>_Uni_<MODE>_<gate>.xlsx      (the shot's own direction)

MODE is OTDR (OTDR Suite mode) or FR (FastReporter mode); the gate is the
splice gate the report ran at, three decimals.  Site names are the site boxes,
or the names the files store for a box left blank, made safe for a Windows
file name.  A rerun still gets "(2)", "(3)", never a file written over.
"""
from __future__ import annotations

import ast
import copy
import os
import re
import types

import pytest

from conftest import (run_streamlit, finish_engine_run, APP_PATH, go_tab,
                      load_traces, FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR)

A, B = str(FIXTURE_SPLICE_A_DIR), str(FIXTURE_SPLICE_B_DIR)
APP_SRC = APP_PATH.read_text(encoding='utf-8')

_HELPERS = ('_unused_report_path', '_file_name_site', '_report_file_name',
            '_sr_file_sites', '_uni_named_report')
_CONSTANTS = ('REPORT_NAME_MODES', '_FILE_NAME_BAD_CHARS',
              '_OTDR_DISABLE_SENTINEL')


def _helpers(**namespace):
    """The file-name helpers out of app.py, in a bare module."""
    tree = ast.parse(APP_SRC)
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in _HELPERS)
            or (isinstance(n, ast.Assign) and len(n.targets) == 1
                and getattr(n.targets[0], 'id', None) in _CONSTANTS)]
    found = {getattr(n, 'name', None) or n.targets[0].id for n in body}
    assert found == set(_HELPERS) | set(_CONSTANTS), sorted(found)
    mod = types.ModuleType('report_file_names')
    # OTDR Suite App: _uni_named_report also calls the App's project hooks
    # (a project report keeps its run's time and its record follows it; see
    # test_project_screen).  Outside a project they leave the name alone and
    # record nothing, as these stand-ins do.
    app_hooks = {'_project_keep_stamp': lambda out, want: want,
                 '_project_report_moved': lambda old, new: None}
    mod.__dict__.update({'os': os, 're': re, **app_hooks, **namespace})
    exec(compile(ast.Module(body=body, type_ignores=[]), 'app.py', 'exec'),
         mod.__dict__)
    return mod


# ── the name itself ──────────────────────────────────────────────────────

def test_a_splice_report_is_named_by_its_typed_sites_mode_and_gate():
    h = _helpers()
    assert h._sr_file_sites('SITEA', 'SITEB', A, B) == ('SITEA', 'SITEB')
    assert (h._report_file_name('SITEA', 'SITEB', 'SpliceReport', 'suite', 0.16)
            == 'SITEA_to_SITEB_SpliceReport_OTDR_0.160.xlsx')
    assert (h._report_file_name('SITEA', 'SITEB', 'SpliceReport', 'fr', 0.1)
            == 'SITEA_to_SITEB_SpliceReport_FR_0.100.xlsx')


def test_a_blank_site_box_takes_the_name_the_files_store():
    h = _helpers(_site_names_for=lambda da, db: ('SITEA', 'SITEB'))
    assert h._sr_file_sites('', '  ', A, B) == ('SITEA', 'SITEB')
    assert h._sr_file_sites('WEST', '', A, B) == ('WEST', 'SITEB')
    # Nothing stored either: the ends' letters.
    h = _helpers(_site_names_for=lambda da, db: ('', ''))
    assert h._sr_file_sites('', '', A, B) == ('A', 'B')


def test_a_unidirectional_report_is_named_in_its_own_mode_and_gate():
    h = _helpers()
    assert (h._report_file_name('SITEA', 'SITEB', 'Uni', 'suite', 0.25)
            == 'SITEA_to_SITEB_Uni_OTDR_0.250.xlsx')
    assert (h._report_file_name('SITEB', 'SITEA', 'Uni', 'fr', 0.3)
            == 'SITEB_to_SITEA_Uni_FR_0.300.xlsx')


def test_characters_windows_refuses_are_taken_out_of_the_site_names():
    h = _helpers()
    name = h._report_file_name('SI/TE\\A:*?', ' "SITE<B>|. ', 'SpliceReport',
                               'suite', 0.16)
    assert name == 'SITEA_to_SITEB_SpliceReport_OTDR_0.160.xlsx'
    assert h._file_name_site('SITEA...', 'A') == 'SITEA'
    assert h._file_name_site('SITE\tA', 'A') == 'SITEA'
    assert h._file_name_site('/:*', 'A') == 'A'           # nothing left
    assert h._file_name_site('NET-XX-SITEB-0001', 'B') == 'NET-XX-SITEB-0001'


def test_a_gate_switched_off_reads_off():
    h = _helpers()
    assert h._report_file_name('SITEA', 'SITEB', 'SpliceReport', 'suite',
                               1.0e9).endswith('_OTDR_off.xlsx')


# ── the Unidirectional workbook, renamed once the run says the direction ─

def _uni_manifest(out, side, origin, far, mode='suite', gate=0.25):
    return {'ok': True, 'out': str(out), 'analysis_mode': mode,
            'thresholds': {'UNI_BEND_THRESHOLD': gate},
            'uni': {'shot_side': side, 'site_a': origin, 'site_b': far}}


def test_a_b_direction_unidirectional_run_is_named_from_b_to_a(tmp_path):
    h = _helpers()
    out = tmp_path / 'SITEA_to_SITEB_Uni_OTDR_0.250.xlsx'
    out.write_bytes(b'the report')
    got = h._uni_named_report(_uni_manifest(out, 'B', 'SITEB', 'SITEA'))
    assert got == str(tmp_path / 'SITEB_to_SITEA_Uni_OTDR_0.250.xlsx')
    assert not out.exists()
    with open(got, 'rb') as fh:
        assert fh.read() == b'the report'


def test_an_a_direction_run_keeps_its_name_and_its_rerun_number(tmp_path):
    h = _helpers()
    out = tmp_path / 'SITEA_to_SITEB_Uni_OTDR_0.250 (2).xlsx'
    out.write_bytes(b'the rerun')
    (tmp_path / 'SITEA_to_SITEB_Uni_OTDR_0.250.xlsx').write_bytes(b'the first')
    assert (h._uni_named_report(_uni_manifest(out, 'A', 'SITEA', 'SITEB'))
            == str(out))


def test_a_renamed_report_never_writes_over_another(tmp_path):
    h = _helpers()
    first = tmp_path / 'SITEB_to_SITEA_Uni_FR_0.300.xlsx'
    first.write_bytes(b'the report before')
    out = tmp_path / 'SITEA_to_SITEB_Uni_FR_0.300.xlsx'
    out.write_bytes(b'the new report')
    got = h._uni_named_report(_uni_manifest(out, 'B', 'SITEB', 'SITEA',
                                            mode='fr', gate=0.3))
    assert got == str(tmp_path / 'SITEB_to_SITEA_Uni_FR_0.300 (2).xlsx')
    assert first.read_bytes() == b'the report before'


def test_rerun_numbering_is_kept(tmp_path):
    h = _helpers()
    p = str(tmp_path / 'SITEA_to_SITEB_SpliceReport_OTDR_0.160.xlsx')
    assert h._unused_report_path(p) == p
    open(p, 'wb').close()
    assert h._unused_report_path(p).endswith('_OTDR_0.160 (2).xlsx')
    assert h._unused_report_path(p, [p[:-5] + ' (2).xlsx']).endswith(' (3).xlsx')


# ── the hub, end to end on the fixture span ──────────────────────────────

@pytest.fixture
def dest(tmp_path, monkeypatch):
    """Where the runs below save: never the tech's Downloads folder.  The
    settings folder too, so the runs start in OTDR Suite mode whatever this
    machine's hub was last switched to."""
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setenv('OTDR_SETTINGS_DIR', str(tmp_path / 'settings'))
    out = tmp_path / 'saved reports'
    out.mkdir()
    return str(out)


def _hub(a='', b=''):
    at = run_streamlit(default_timeout=180).run()
    for side, v in (('a', a), ('b', b)):
        if v:
            load_traces(at, **{side: v})
    assert not at.exception, at.exception
    return at


def _open(at, page):
    go_tab(at, page)
    assert not at.exception, at.exception
    return at


def _box(at, label):
    return next(t for t in at.main.text_input if t.label == label)


def _saved(dest):
    return sorted(f for f in os.listdir(dest) if f.endswith('.xlsx'))


def _splice_report(at, dest):
    at.session_state['sr_report_dest'] = dest
    at.run()
    next(b for b in at.main.button if b.label.startswith('Generate')).click().run()
    finish_engine_run(at, 'sr')
    assert not at.exception, at.exception
    return at.session_state['sr_result']


def test_hub_splice_report_from_typed_sites_at_the_tuned_gate(dest):
    at = _open(_hub(A, B), 'Splice Report')
    _box(at, 'A-Direction ILA / Site').input('SITE/A').run()
    _box(at, 'B-Direction ILA / Site').input('SITEB').run()
    tuned = copy.deepcopy(at.session_state['otdr_settings'])
    tuned['bidir_splice_loss']['fail'] = 0.12
    at.session_state['otdr_settings'] = tuned
    res = _splice_report(at, dest)
    want = 'SITEA_to_SITEB_SpliceReport_OTDR_0.120.xlsx'
    assert _saved(dest) == [want]
    assert res['xlsx'] == os.path.join(dest, want)
    assert res['thresholds']['REBURN_THRESHOLD'] == 0.12   # the gate it ran at


def test_hub_splice_report_in_fr_mode_from_the_stored_sites(dest):
    at = _hub(A, B)
    at.session_state['analysis_mode'] = 'fr'
    _open(at, 'Splice Report')
    stored = (_box(at, 'A-Direction ILA / Site').value,
              _box(at, 'B-Direction ILA / Site').value)
    assert all(stored) and stored != ('A', 'B')
    _box(at, 'A-Direction ILA / Site').input('').run()
    _box(at, 'B-Direction ILA / Site').input('').run()
    res = _splice_report(at, dest)
    want = f'{stored[0]}_to_{stored[1]}_SpliceReport_FR_0.160.xlsx'
    assert _saved(dest) == [want]
    assert res['xlsx'] == os.path.join(dest, want)


def test_hub_unidirectional_b_run_is_named_in_the_shots_direction(dest):
    at = _open(_hub(B), 'Unidirectional')
    a_end, b_end = _box(at, 'A-End Site').value, _box(at, 'B-End Site').value
    at.session_state['uni_report_dest'] = dest
    at.run()
    next(b for b in at.main.button
         if b.label == 'Run Unidirectional Report').click().run()
    finish_engine_run(at, 'uni')
    assert not at.exception, at.exception
    res = at.session_state['uni_result']
    u = res['uni']
    assert u['shot_side'] == 'B'
    # Shot from the B end: the far end's name comes second.
    assert (u['site_a'], u['site_b']) == (b_end, a_end)
    want = f'{b_end}_to_{a_end}_Uni_OTDR_0.250.xlsx'
    assert _saved(dest) == [want]                 # nothing left at the box order
    assert res['out'] == os.path.join(dest, want)
    assert at.session_state['uni_out_xlsx'] == res['out']
    dl = next(d for d in at.get('download_button') if d.proto.label.startswith('⬇'))
    assert dl.proto.label == f'⬇ {want}'
