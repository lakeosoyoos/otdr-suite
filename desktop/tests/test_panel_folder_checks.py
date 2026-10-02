"""The left panel's Trace Folders boxes, checked the same way on every page
(click-through audit, main 5806a3f, a 24/240/1152-fiber bidirectional span,
2026-10-02).

1. A folder whose traces are one folder down (unzipping SITE.zip
   leaves SITE/SITE/*.sor): the Viewer said "A: 0 fibers · B: 0
   fibers" with nothing more, and the Splice Report said the folders were
   loaded and failed only on Generate ("Loaded A=0 B=0 fibers").  The .zip
   itself in the box worked.  A folder with no trace files at all said
   nothing either.
"""
from __future__ import annotations

import os
import shutil

from conftest import (run_streamlit, import_trace_server,
                      FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR)

A, B = str(FIXTURE_SPLICE_A_DIR), str(FIXTURE_SPLICE_B_DIR)


def _sor_names(folder):
    return sorted(f for f in os.listdir(folder) if f.lower().endswith('.sor'))


N_A, N_B = len(_sor_names(A)), len(_sor_names(B))


# ── helpers ───────────────────────────────────────────────────────────────

def _box(at, label):
    return next(t for t in at.sidebar.text_input if t.label == label)


def _hub(a='', b=''):
    at = run_streamlit(default_timeout=180).run()
    if a:
        _box(at, 'A Folder').input(a).run()
    if b:
        _box(at, 'B Folder').input(b).run()
    assert not at.exception, at.exception
    return at


def _open(at, page):
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, at.exception
    return at


def _texts(els):
    return ' '.join(str(e.value) for e in els)


def _sites(at):
    return {t.label: t.value for t in at.main.text_input if 'ILA' in t.label}


def _generate(at):
    return next((b for b in at.main.button if b.label.startswith('Generate')), None)


def _server():
    tv = import_trace_server()
    return tv.CONFIG.get('dir_a'), tv.CONFIG.get('dir_b')


def _copy(src, dest):
    """`src`'s traces into `dest` (made), as a tech's copy of the folder."""
    os.makedirs(dest, exist_ok=True)
    for f in _sor_names(src):
        shutil.copy2(os.path.join(src, f), os.path.join(dest, f))
    return str(dest)


# ── 1. traces one folder down; a folder with none ─────────────────────────

def test_a_folder_whose_traces_are_one_folder_down_loads(tmp_path):
    """SITE.zip unzipped: SITE/SITE/*.sor, the outer folder
    in each box."""
    outer_a = tmp_path / 'ELMMIL'
    outer_b = tmp_path / 'MILELM'
    _copy(A, outer_a / 'ELMMIL')
    _copy(B, outer_b / 'MILELM')
    at = _open(_hub(str(outer_a), str(outer_b)), 'Viewer')
    assert f'A: {N_A} fibers · B: {N_B} fibers' in _texts(at.sidebar.caption)
    assert not at.sidebar.warning
    dir_a, dir_b = _server()
    assert _sor_names(dir_a) == _sor_names(A) and _sor_names(dir_b) == _sor_names(B)
    # flattened once: the next pass reads the same folders, built nowhere new
    at.run()
    assert _server() == (dir_a, dir_b)
    # the Splice Report runs on them, names both sites and says where the
    # traces came from
    _open(at, 'Splice Report')
    assert _generate(at) is not None and not _generate(at).disabled
    assert 'Pick **both**' not in _texts(at.main.info)
    sites = set(_sites(at).values())
    assert len(sites) == 2 and not sites & {'A', 'B'}
    assert 'A: traces read from its subfolders' in _texts(at.main.caption)
    assert 'B: traces read from its subfolders' in _texts(at.main.caption)


def test_a_folder_with_traces_of_its_own_keeps_its_subfolders_unread(tmp_path):
    """Today's fast path: traces at the top are the folder's traces, a
    subfolder beside them is not read (here it holds the other direction)."""
    own = _copy(A, tmp_path / 'ELMMIL')
    _copy(B, tmp_path / 'ELMMIL' / 'other way')
    empty_b = tmp_path / 'not yet'
    (empty_b / 'notes').mkdir(parents=True)
    at = _open(_hub(own, str(empty_b)), 'Viewer')
    assert f'A: {N_A} fibers · B: 0 fibers' in _texts(at.sidebar.caption)
    assert _server()[0] == own
    # the B box named a folder with no trace files in it or below it
    assert ('B: no trace files (.sor, .trc, .json) in that folder'
            in _texts(at.sidebar.warning))


def test_a_span_folder_with_a_and_b_subfolders_is_split(tmp_path):
    """The two directions in subfolders of one folder, in the A box: read
    from the subfolders, then split as a folder holding both directions is."""
    span = tmp_path / 'span'
    _copy(A, span / 'A')
    _copy(B, span / 'B')
    at = _open(_hub(str(span)), 'Viewer')
    assert f'A: {N_A} fibers · B: {N_B} fibers' in _texts(at.sidebar.caption)
    assert 'holds both directions' in _texts(at.sidebar.caption)
    dir_a, dir_b = _server()
    assert _sor_names(dir_a) == _sor_names(A) and _sor_names(dir_b) == _sor_names(B)
    _open(at, 'Splice Report')
    assert _generate(at) is not None and not _generate(at).disabled


def test_the_same_name_in_two_subfolders_is_said(tmp_path):
    """Flattening keeps one file per name: two copies of the A folder in one
    folder load once, and the page says the other copies were left out."""
    outer = tmp_path / 'ELMMIL'
    _copy(A, outer / 'first copy')
    _copy(A, outer / 'second copy')
    at = _open(_hub(str(outer)), 'Viewer')
    assert f'A: {N_A} fibers · B: 0 fibers' in _texts(at.sidebar.caption)
    warned = _texts(at.sidebar.warning)
    assert f'{N_A} file names are in more than one subfolder' in warned
    assert 'only one copy of each was loaded' in warned


def test_a_folder_with_no_trace_files_says_so_on_every_page(tmp_path):
    """Nothing to load: every page that reads the boxes says why, and the
    Splice Report does not take the folder as loaded."""
    nothing = tmp_path / 'nothing here'
    (nothing / 'photos').mkdir(parents=True)
    (nothing / 'photos' / 'pole.jpg').write_bytes(b'jpg')
    (nothing / '.sr_grid_cache.json').write_text('{}', encoding='utf-8')  # the hub's own file
    want = 'B: no trace files (.sor, .trc, .json) in that folder'
    at = _open(_hub(A, str(nothing)), 'Viewer')
    assert want in _texts(at.sidebar.warning)
    # the Viewer is still pointed at it (0 fibers), so its frame reloads
    assert _server() == (A, str(nothing))
    _open(at, 'Splice Report')
    assert want in _texts(at.main.warning)
    assert _generate(at) is None
    _open(at, 'Unidirectional')
    assert want in _texts(at.main.warning)
    _open(at, 'Secret Sauce')
    assert want in _texts(at.main.warning)
