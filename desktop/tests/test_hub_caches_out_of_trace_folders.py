"""The hub never writes into a traces folder.

Four back-from-Viewer caches used to be written beside the traces:
.uni_result_cache.json, .sr_grid_cache.json, .srfr_grid_cache.json in the
folder itself, and SecretSauce_reports/pairs_cache.json under it.  The boss
asked for the trace folders to stay untouched.  They now live under
~/.otdrSuite/cache, keyed by the folder(s) they describe, and the three pages
delete any that an earlier build left behind when they take a folder.

Streamlit page, so: the helper is exercised directly (it is pure), the
wiring is pinned in the source.
"""
from __future__ import annotations

import ast
import os
import re
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SRC = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()


def _load(name):
    """Lift one top-level function (and the constants it needs) out of app.py
    without importing Streamlit."""
    tree = ast.parse(SRC)
    wanted = {name, '_LEGACY_CACHE_NAMES'}
    mod = ast.Module(body=[n for n in tree.body
                           if (isinstance(n, ast.FunctionDef) and n.name in wanted)
                           or (isinstance(n, ast.Assign) and any(
                               isinstance(t, ast.Name) and t.id in wanted for t in n.targets))],
                     type_ignores=[])
    ns = {'os': os}
    exec(compile(mod, 'app.py', 'exec'), ns)
    return ns[name]


def test_cache_path_is_under_the_app_state_dir_and_keyed_by_folder(tmp_path, monkeypatch):
    monkeypatch.setenv('OTDR_CACHE_DIR', str(tmp_path / '.otdrSuite' / 'cache'))
    f = _load('_hub_cache_path')
    p1 = f('uni_result_cache.json', '/spans/A')
    p2 = f('uni_result_cache.json', '/spans/B')
    p3 = f('.sr_grid_cache.json', '/spans/A')
    assert p1.startswith(os.path.join(str(tmp_path), '.otdrSuite', 'cache'))
    assert p1 != p2 and p1 != p3
    assert not os.path.basename(p3).startswith('.') and p3.endswith('_sr_grid_cache.json')
    assert '/spans' not in p1


def test_legacy_caches_are_removed_and_nothing_else_is(tmp_path):
    f = _load('_remove_legacy_caches')
    (tmp_path / '.uni_result_cache.json').write_text('{}', encoding='utf-8')
    (tmp_path / '.sr_grid_cache.json').write_text('{}', encoding='utf-8')
    (tmp_path / 'SecretSauce_reports').mkdir()
    (tmp_path / 'SecretSauce_reports' / 'pairs_cache.json').write_text('{}', encoding='utf-8')
    (tmp_path / 'SecretSauce_reports' / 'report.xlsx').write_bytes(b'x')
    (tmp_path / 'F0001.sor').write_bytes(b'x')
    (tmp_path / '.hidden_not_ours.json').write_text('{}', encoding='utf-8')
    f(str(tmp_path))
    left = sorted(os.path.relpath(os.path.join(r, x), tmp_path)
                  for r, _d, fs in os.walk(tmp_path) for x in fs)
    assert left == ['.hidden_not_ours.json', 'F0001.sor',
                    os.path.join('SecretSauce_reports', 'report.xlsx')]


def test_no_page_writes_a_cache_into_a_traces_folder():
    assert "os.path.join(folder, '.uni_result_cache.json')" not in SRC
    assert "os.path.join(out_dir, 'pairs_cache.json')" not in SRC
    # The one remaining mention of the old in-folder path is the cleanup that
    # deletes it.
    assert SRC.count("os.path.join(folder, 'SecretSauce_reports', 'pairs_cache.json')") == 1
    assert "os.path.join(_sd[0], _cache_name)" not in SRC
    assert "os.path.join(_cand[0], _cache_name)" not in SRC
    assert SRC.count('_hub_cache_path(') >= 7   # def + 6 call sites
    assert SRC.count('_remove_legacy_caches(') >= 5  # def + SS + Uni + SR a/b
