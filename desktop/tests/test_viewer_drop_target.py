"""The Viewer's FILES panel is a drop target: files, a folder, or a zip.

The hub's pages take drag-and-drop through Streamlit's uploader; the Viewer is
its own page and had none (the boss asked for one).  The browser gives bytes,
never paths, so each file is POSTed to the local trace server, staged into a
temp folder, split into A and B by filename prefix with the hub's own rule
(folder_intake.direction_prefix), and the server points itself at the result.
One direction alone becomes A.  A fresh drop also re-seeds the hub sidebar's
folder boxes, so a hub rerun does not put the old paths back.
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


def test_one_prefix_is_a_alone():
    tok = TS.drop_begin()
    TS.drop_file(tok, 'SEANOR001_1550.sor', make_sor(ior=1.47))
    out = TS.drop_end(tok)
    assert out['dir_b'] is None and out['b_count'] == 0
    assert TS.CONFIG['dir_b'] is None


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
