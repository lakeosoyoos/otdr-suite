"""The Viewer's FILES panel is a drop target: files, a folder, or a zip.

The hub's pages take drag-and-drop through Streamlit's uploader; the Viewer is
its own page and had none (the boss asked for one).  The browser gives bytes,
never paths, so each file is POSTed to the local trace server, staged into a
temp folder, split into A and B by filename prefix with the hub's own rule
(folder_intake.direction_prefix), and the server points itself at the result.
A fresh drop also re-seeds the hub sidebar's folder boxes, so a hub rerun does
not put the old paths back.

A drop holding ONE direction fills whichever side is EMPTY.  2026-09-18, the
field: "if we drag in our a side into files in viewer and then we try to drag
in our b side, it seems to override the a side that were in there" — both
drops became A and set_dirs overwrote both folders, so the second drop threw
the A traces away and loaded the B folder as the A side.
"""
from __future__ import annotations

import io
import os
import sys
import zipfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'viewer'))
import trace_server as TS                       # noqa: E402
from test_sor_writer import make_sor            # noqa: E402


@pytest.fixture(autouse=True)
def _own_temp(tmp_path, monkeypatch):
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    import tempfile
    tempfile.tempdir = None
    yield
    tempfile.tempdir = None
    TS.set_dirs(None, None)
    TS.CONFIG.pop('dropped_at', None)


def test_two_prefixes_split_into_a_and_b_and_the_server_points_at_them():
    tok = TS.drop_begin()
    for name in ('ROMTUC001_1550.sor', 'ROMTUC002_1550.sor', 'TUCROM001_1550.sor'):
        assert TS.drop_file(tok, name, make_sor(ior=1.47))['files'] == 1
    out = TS.drop_end(tok)
    assert out['a_prefix'] == 'ROMTUC' and out['a_count'] == 2
    assert out['b_prefix'] == 'TUCROM' and out['b_count'] == 1
    assert TS.CONFIG['dir_a'] == out['dir_a'] and TS.CONFIG['dir_b'] == out['dir_b']
    assert [n for n, _ in TS.list_fibers(out['dir_a'])] == [1, 2]
    assert [n for n, _ in TS.list_fibers(out['dir_b'])] == [1]
    assert TS.CONFIG['dropped_at'] > 0


def test_one_prefix_with_nothing_loaded_is_a_alone():
    tok = TS.drop_begin()
    TS.drop_file(tok, 'SEANOR001_1550.sor', make_sor(ior=1.47))
    out = TS.drop_end(tok)
    assert out['added'] == 'A'
    assert out['dir_b'] is None and out['b_count'] == 0
    assert TS.CONFIG['dir_b'] is None


def _drop(*names):
    tok = TS.drop_begin()
    for n in names:
        TS.drop_file(tok, n, make_sor(ior=1.47))
    return TS.drop_end(tok)


def test_the_a_side_then_the_b_side_loads_both():
    a = _drop('ROMTUC001_1550.sor', 'ROMTUC002_1550.sor')
    assert a['added'] == 'A' and a['dir_b'] is None
    b = _drop('TUCROM001_1550.sor', 'TUCROM002_1550.sor')
    assert b['added'] == 'B'                       # the empty side, not A again
    assert b['dir_a'] == a['dir_a']                # the A folder is untouched
    assert b['a_prefix'] == 'ROMTUC' and b['a_count'] == 2
    assert b['b_prefix'] == 'TUCROM' and b['b_count'] == 2
    assert TS.CONFIG['dir_a'] == a['dir_a'] and TS.CONFIG['dir_b'] == b['dir_b']
    assert [n for n, _ in TS.list_fibers(TS.CONFIG['dir_a'])] == [1, 2]
    assert [n for n, _ in TS.list_fibers(TS.CONFIG['dir_b'])] == [1, 2]


def test_the_b_side_is_staged_in_a_folder_named_b():
    """/api/list labels each side with its folder's basename, so a B-side drop
    staged into an 'A' folder would read 'B: A' on the page."""
    _drop('ROMTUC001_1550.sor', 'ROMTUC002_1550.sor')
    b = _drop('TUCROM001_1550.sor', 'TUCROM002_1550.sor')
    assert os.path.basename(b['dir_b']) == 'B'
    assert os.path.basename(b['dir_a']) == 'A'


def test_the_same_folder_again_refreshes_its_own_side():
    """Re-dropping a folder is a refresh of the side it is already on — it
    must not turn into the other direction and give the span two A sides."""
    a = _drop('ROMTUC001_1550.sor', 'ROMTUC002_1550.sor')
    b = _drop('TUCROM001_1550.sor', 'TUCROM002_1550.sor')
    again = _drop('ROMTUC001_1550.sor', 'ROMTUC002_1550.sor')
    assert again['added'] == 'A'
    assert again['dir_a'] not in (a['dir_a'], None)      # the fresh staging
    assert again['dir_b'] == b['dir_b']                  # B kept as it was
    assert again['a_prefix'] == 'ROMTUC' and again['b_prefix'] == 'TUCROM'
    once_more = _drop('TUCROM001_1550.sor', 'TUCROM002_1550.sor')
    assert once_more['added'] == 'B'
    assert once_more['dir_a'] == again['dir_a']


def test_a_single_drop_with_both_sides_full_starts_a_new_span():
    """Both sides loaded and an unrelated folder arrives: it is a different
    span, not a third direction — and this is the way back to a clean slate."""
    _drop('ROMTUC001_1550.sor', 'ROMTUC002_1550.sor')
    _drop('TUCROM001_1550.sor', 'TUCROM002_1550.sor')
    new = _drop('SEANOR001_1550.sor', 'SEANOR002_1550.sor')
    assert new['added'] == 'A'
    assert new['a_prefix'] == 'SEANOR'
    assert new['dir_b'] is None and new['b_prefix'] is None and new['b_count'] == 0
    assert TS.CONFIG['dir_b'] is None


def test_both_directions_in_one_drop_still_replace_both_sides():
    _drop('SEANOR001_1550.sor', 'SEANOR002_1550.sor')
    out = _drop('ROMTUC001_1550.sor', 'TUCROM001_1550.sor')
    assert out['added'] == 'AB'
    assert out['a_prefix'] == 'ROMTUC' and out['b_prefix'] == 'TUCROM'
    assert TS.CONFIG['dir_a'] == out['dir_a'] and TS.CONFIG['dir_b'] == out['dir_b']


def test_a_side_whose_folder_has_gone_is_not_treated_as_loaded(tmp_path):
    """A path left in CONFIG for a folder that has since moved is not a loaded
    side: the drop replaces it instead of filling B and leaving the dead one
    on screen."""
    gone = tmp_path / 'moved_away'
    gone.mkdir()
    TS.set_dirs(str(gone), None)
    gone.rmdir()
    out = _drop('ROMTUC001_1550.sor', 'ROMTUC002_1550.sor')
    assert out['added'] == 'A'
    assert out['dir_a'] != str(gone) and out['dir_b'] is None


def test_a_drop_fills_an_empty_a_side_under_a_hub_picked_b():
    TS.set_dirs(None, None)
    b_only = _drop('TUCROM001_1550.sor', 'TUCROM002_1550.sor')
    TS.set_dirs(None, b_only['dir_a'])              # hub picked B and no A
    out = _drop('ROMTUC001_1550.sor', 'ROMTUC002_1550.sor')
    assert out['added'] == 'A'
    assert out['dir_b'] == b_only['dir_a']          # the B pick survives


def test_a_zip_is_unpacked_flat_and_guarded():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('span/ROMTUC001_1550.sor', make_sor(ior=1.47))
        zf.writestr('span/notes.txt', 'ignored')
        zf.writestr('../../evil/TUCROM009_1550.sor', make_sor(ior=1.47))   # zip-slip name
        zf.writestr('__MACOSX/._ROMTUC001_1550.sor', b'junk')
    tok = TS.drop_begin()
    r = TS.drop_file(tok, 'span.zip', buf.getvalue())
    assert r['files'] == 2
    out = TS.drop_end(tok)
    assert out['a_prefix'] == 'ROMTUC' and out['b_prefix'] == 'TUCROM'
    assert sorted(os.listdir(out['dir_b'])) == ['TUCROM009_1550.sor']   # flattened, inside


def test_names_are_sanitised_and_non_traces_are_skipped():
    tok = TS.drop_begin()
    r = TS.drop_file(tok, '../../etc/ROMTUC001_1550.sor', make_sor(ior=1.47))
    assert r['name'] == 'ROMTUC001_1550.sor'
    assert TS.drop_file(tok, 'photo.jpg', b'x')['skipped'] == 'not a trace file'
    with pytest.raises(ValueError):
        TS.drop_file(tok, '.hidden.sor', b'x')
    out = TS.drop_end(tok)
    assert out['a_count'] == 1


def test_a_drop_with_nothing_usable_is_refused_and_the_token_dies():
    tok = TS.drop_begin()
    TS.drop_file(tok, 'photo.jpg', b'x')
    with pytest.raises(ValueError, match='nothing dropped'):
        TS.drop_end(tok)
    with pytest.raises(ValueError, match='unknown'):
        TS.drop_end(tok)


def test_the_page_forgets_only_the_side_the_drop_replaced():
    h = open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8').read()
    fn = h.split('async function handleFilesDrop(dt) {', 1)[1].split('\n}', 1)[0]
    assert "const before = { a: (gInfo && gInfo.dir_a) || '', b: (gInfo && gInfo.dir_b) || '' };" in fn
    assert "if ((j['dir_' + d] || '') !== before[d]) forgetSide(d);" in fn
    assert fn.index('const before =') < fn.index('/api/drop_end')
    fs = h.split('function forgetSide(dir) {', 1)[1].split('\n}', 1)[0]
    assert "gTraces = gTraces.filter(t => t.src !== dir);" in fs
    # and the hint names the side the next drop fills
    panel = h.split('function renderFilesPanel() {', 1)[1].split('\n}\n', 1)[0]
    assert "'drop the B side here — A stays loaded'" in panel
    assert "'drop the A side here — B stays loaded'" in panel


def test_the_page_and_hub_are_wired():
    h = open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8').read()
    assert "panel.addEventListener('drop'" in h
    assert "/api/drop_begin" in h and "/api/drop_file?token=" in h and "/api/drop_end?token=" in h
    assert 'webkitGetAsEntry' in h                      # folders, not just files
    assert 'id="files-drop-hint"' in h
    s = open(os.path.join(ROOT, 'viewer', 'trace_server.py'), encoding='utf-8').read()
    body = s.split("u.path in ('/api/drop_begin', '/api/drop_file', '/api/drop_end')", 1)[1].split('return', 1)[0]
    assert '_origin_is_local' in body
    a = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    assert "trace_server.CONFIG.get('dropped_at')" in a
