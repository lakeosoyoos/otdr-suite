"""Secret Sauce's report survives the pair-click round trip in EVERY mode.

Clicking a mating pair navigates to the Viewer through a URL query param, which
starts a fresh Streamlit session and drops session_state.  The in-app pairs mode
has always cached its manifest to disk and rebuilt from it on return.  The
Excel/PDF mode never did — and the pair links render in every output mode
(_render_mating_top), so a tech who ran Excel, clicked a pair, and came back was
shown "choose a folder" and had to re-run the whole analysis.

Streamlit page, so: the pure helpers are exercised directly, the wiring is
pinned in the source.
"""
from __future__ import annotations

import ast
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SRC = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()


def _load():
    """Lift the cache helpers out of app.py without importing Streamlit."""
    tree = ast.parse(SRC)
    wanted = {'_hub_cache_path', '_ss_cache_write', '_ss_cache_read',
              'SS_CACHE_NAMES'}
    mod = ast.Module(body=[n for n in tree.body
                           if (isinstance(n, ast.FunctionDef) and n.name in wanted)
                           or (isinstance(n, ast.Assign) and any(
                               isinstance(t, ast.Name) and t.id in wanted
                               for t in n.targets))],
                     type_ignores=[])
    ns = {'os': os}
    exec(compile(mod, 'app.py', 'exec'), ns)
    return ns


def _manifest(folder, mode):
    return {'ok': True, 'mode': mode, '_folder': folder, 'counts': {'sor': 3}}


def test_excel_result_round_trips_through_disk(tmp_path, monkeypatch):
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    ns = _load()
    folder = str(tmp_path / 'traces')
    m = _manifest(folder, 'xlsx')
    ns['_ss_cache_write']('ss_result_cache.json', folder, m)
    assert ns['_ss_cache_read']('ss_result_cache.json', folder) == m


def test_pairs_result_still_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    ns = _load()
    folder = str(tmp_path / 'traces')
    m = _manifest(folder, 'pairs')
    ns['_ss_cache_write']('pairs_cache.json', folder, m)
    assert ns['_ss_cache_read']('pairs_cache.json', folder, mode='pairs') == m


def test_last_run_wins_so_a_stale_pairs_report_cannot_mask_a_later_excel_run(
        tmp_path, monkeypatch):
    """The pairs restore runs FIRST and returns.  Without eviction, a folder
    that once produced an in-app report would keep showing it forever."""
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    ns = _load()
    folder = str(tmp_path / 'traces')
    ns['_ss_cache_write']('pairs_cache.json', folder, _manifest(folder, 'pairs'))
    ns['_ss_cache_write']('ss_result_cache.json', folder, _manifest(folder, 'xlsx'))
    assert ns['_ss_cache_read']('pairs_cache.json', folder, mode='pairs') is None
    assert ns['_ss_cache_read']('ss_result_cache.json', folder) is not None


def test_another_folders_report_is_never_shown(tmp_path, monkeypatch):
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    ns = _load()
    mine, theirs = str(tmp_path / 'mine'), str(tmp_path / 'theirs')
    ns['_ss_cache_write']('ss_result_cache.json', mine, _manifest(mine, 'xlsx'))
    assert ns['_ss_cache_read']('ss_result_cache.json', theirs) is None


def test_a_pairs_manifest_never_reaches_the_download_renderer(tmp_path, monkeypatch):
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    ns = _load()
    folder = str(tmp_path / 'traces')
    ns['_ss_cache_write']('pairs_cache.json', folder, _manifest(folder, 'pairs'))
    assert ns['_ss_cache_read']('pairs_cache.json', folder, mode='xlsx') is None


def test_an_unwritable_cache_dir_never_raises(tmp_path, monkeypatch):
    """A cache that cannot be written costs a re-run, not the run."""
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    ns = _load()
    folder = str(tmp_path / 'traces')

    class Unserialisable:
        pass

    ns['_ss_cache_write']('ss_result_cache.json', folder,
                          {'ok': True, '_folder': folder, 'x': Unserialisable()})
    assert ns['_ss_cache_read']('ss_result_cache.json', folder) is None


def test_corrupt_cache_reads_as_absent(tmp_path, monkeypatch):
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / 'cache'))
    ns = _load()
    folder = str(tmp_path / 'traces')
    path = ns['_hub_cache_path']('ss_result_cache.json', folder)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('{not json')
    assert ns['_ss_cache_read']('ss_result_cache.json', folder) is None


def test_the_page_actually_wires_both_modes():
    """Pin the wiring: the Excel/PDF branch caches, and the renderer restores."""
    page = SRC.split('def page_duplicate_check', 1)[1]
    assert "_ss_cache_write('pairs_cache.json', folder, manifest)" in page
    assert "_ss_cache_write('ss_result_cache.json', folder, manifest)" in page
    assert "_ss_cache_read('ss_result_cache.json', folder)" in page
    assert "_ss_cache_read('pairs_cache.json', folder, mode='pairs')" in page


def test_both_cache_names_are_registered_for_eviction():
    ns = _load()
    assert set(ns['SS_CACHE_NAMES']) == {'pairs_cache.json', 'ss_result_cache.json'}
