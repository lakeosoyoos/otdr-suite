"""The Splice Report one-folder box, exercised on dropped files.

_resolve_bidir_from_single is the hub function behind "One folder / zip (both
directions)".  It is lifted out of app.py with ast (like test_drop_stage) and
run against a stub Streamlit, so the drop path is covered without a browser:
dragging a span folder in now hands it loose .sor files, not just a .zip.
"""
import ast
import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

APP = (REPO_ROOT / 'app.py').read_text(encoding='utf-8')


class _St:
    """Just enough Streamlit: session_state plus the message calls."""
    def __init__(self):
        self.session_state = {}
        self.msgs = []

    def __getattr__(self, name):
        if name in ('error', 'info', 'warning', 'caption', 'success'):
            return lambda m, *a, **k: self.msgs.append((name, m))
        raise AttributeError(name)

    def said(self, kind):
        return [m for k, m in self.msgs if k == kind]


class _Upload:
    """A Streamlit UploadedFile: bytes and a name, never a path."""
    def __init__(self, name, data=b'trace'):
        self.name, self._data, self.size = name, data, len(data)

    def getbuffer(self):
        return self._data


def _sor_bytes(loc_a, loc_b):
    return (b'\x00\x02Map\x00GenParams\x00GenParams\x00EN'
            b'CABLE\x00FIBER\x00' + b'\x00' * 4
            + loc_a.encode() + b'\x00' + loc_b.encode() + b'\x00' + b'\xff' * 64)


def _resolver():
    fn = next(n for n in ast.parse(APP).body
              if isinstance(n, ast.FunctionDef)
              and n.name == '_resolve_bidir_from_single')
    mod = types.ModuleType('res')
    mod.os, mod.tempfile = os, tempfile
    mod.st = _St()
    mod.report_error = lambda *a, **k: None
    exec(compile(ast.Module(body=[fn], type_ignores=[]), 'app.py', 'exec'),
         mod.__dict__)
    return mod


def _montgomery_uploads(reversed_headers=True):
    ups = []
    for i in range(1, 7):
        ups.append(_Upload(f'MTG4-{i:04d}_1550.sor', _sor_bytes('MTG4', 'MTG5')))
        ups.append(_Upload(f'MTG5-{i:04d}_1550.sor',
                           _sor_bytes(*(('MTG5', 'MTG4') if reversed_headers
                                        else ('MTG4', 'MTG5')))))
    return ups


def test_dropped_traces_split_into_two_directions():
    mod = _resolver()
    da, db = mod._resolve_bidir_from_single('', _montgomery_uploads())
    assert os.path.isdir(da) and os.path.isdir(db)
    assert len(os.listdir(da)) == 6 and len(os.listdir(db)) == 6
    assert 'MTG4' in mod.st.said('caption')[0]
    assert not mod.st.said('error')


def test_dropped_traces_split_by_name_when_headers_agree():
    """Most OTDRs stamp the same location pair both ways — the site code in
    the filename still separates the two directions."""
    mod = _resolver()
    da, db = mod._resolve_bidir_from_single(
        '', _montgomery_uploads(reversed_headers=False))
    assert len(os.listdir(da)) == 6 and len(os.listdir(db)) == 6
    assert 'by site code' in mod.st.said('caption')[0]


def test_same_source_is_cached_across_reruns():
    """Streamlit reruns on every widget touch; the drop must not re-stage."""
    mod = _resolver()
    ups = _montgomery_uploads()
    first = mod._resolve_bidir_from_single('', ups)
    second = mod._resolve_bidir_from_single('', ups)
    assert first == second


def test_repeated_names_survive_and_are_reported():
    """Both direction folders dropped at once: each names its traces alike.
    Every file is kept, the headers decide the direction, and the tech is
    told the names repeated."""
    mod = _resolver()
    ups = [_Upload('0001_1550.sor', _sor_bytes('MTG4', 'MTG5')),
           _Upload('0001_1550.sor', _sor_bytes('MTG5', 'MTG4')),
           _Upload('0002_1550.sor', _sor_bytes('MTG4', 'MTG5')),
           _Upload('0002_1550.sor', _sor_bytes('MTG5', 'MTG4'))]
    da, db = mod._resolve_bidir_from_single('', ups)
    assert len(os.listdir(da)) == 2 and len(os.listdir(db)) == 2
    assert mod.st.said('warning') and 'share a name' in mod.st.said('warning')[0]


def test_bdr_and_traces_together_are_refused():
    mod = _resolver()
    ups = [_Upload('SPAN_1550_1550.bdr', b'x'),
           _Upload('MTG4-0001_1550.sor', _sor_bytes('MTG4', 'MTG5'))]
    assert mod._resolve_bidir_from_single('', ups) == ('', '')
    assert mod.st.said('error')


def test_nothing_dropped_prompts_for_a_folder():
    mod = _resolver()
    assert mod._resolve_bidir_from_single('', []) == ('', '')
    assert mod.st.said('info')
