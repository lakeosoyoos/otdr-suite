"""Drag-and-drop staging (_stage_dropped) — helper contract.

Browsers never expose a dropped file's real path, so the hub stages dropped
bytes into a working folder the engines can read.  Locks: loose files land
flat, zips extract (zip-slip-guarded via folder_intake), .trc counts, the
(name, size) signature reuses the same dir across Streamlit reruns, and
dot-prefixed junk is not counted.
"""
import ast
import io
import os
import types
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
import sys
sys.path.insert(0, ROOT)


class _Fake:
    def __init__(self, name, data=b'x'):
        self.name = name
        self._data = data
        self.size = len(data)
    def getbuffer(self):
        return self._data
    # file-like for zipfile.ZipFile
    def read(self, *a):
        return self._data if not a else self._data[:a[0]]
    def seek(self, *a):
        self._pos = a[0] if a else 0
        return self._pos
    def tell(self):
        return getattr(self, '_pos', 0)


def _load():
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == '_stage_dropped')
    cache = next(n for n in tree.body if isinstance(n, ast.Assign)
                 and getattr(n.targets[0], 'id', '') == '_DROP_STAGE_CACHE')
    mod = types.ModuleType('drop')
    mod.os = os
    exec(compile(ast.Module(body=[cache, fn], type_ignores=[]), 'app.py',
                 'exec'), mod.__dict__)
    return mod


def test_loose_files_stage_flat_and_count():
    mod = _load()
    files = [_Fake('LAMBEY001_1550.sor'), _Fake('LAMBEY002_1550.trc'),
             _Fake('.DS_Store')]
    d, n = mod._stage_dropped(files)
    assert os.path.isdir(d)
    assert n == 2                                   # trc counts, dotfile doesn't
    assert os.path.exists(os.path.join(d, 'LAMBEY001_1550.sor'))


def test_zip_extracts():
    mod = _load()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('SPAN/F001_1550.sor', b'data')
        zf.writestr('SPAN/F002_1550.sor', b'data')
    zip_file = io.BytesIO(buf.getvalue())
    zip_file.name = 'span.zip'
    zip_file.size = len(buf.getvalue())
    d, n = mod._stage_dropped([zip_file])
    assert n == 2


def test_same_signature_reuses_dir():
    mod = _load()
    files = [_Fake('A0001_1550.sor')]
    d1, _ = mod._stage_dropped(files)
    d2, _ = mod._stage_dropped([_Fake('A0001_1550.sor')])
    assert d1 == d2                                 # rerun-stable staging
