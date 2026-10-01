"""Drag-and-drop staging (_stage_dropped) — helper contract.

Browsers never expose a dropped file's real path, so the hub stages dropped
bytes into a working folder the engines can read.  Locks: loose files land
flat, zips extract (zip-slip-guarded via folder_intake), .trc counts, the
same upload (its upload ids; name and size when a file has none) reuses the
same dir across Streamlit reruns, a new drop of look-alike files does not,
and dot-prefixed junk is not counted.  A dropped PARENT folder arrives flat, so
two subfolders that name their traces alike arrive as one name twice: the
second must not silently overwrite the first.
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
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name == '_remember')
            or (isinstance(n, ast.Assign)
                and getattr(n.targets[0], 'id', '') == '_RERUN_CACHE_KEPT')]
    mod = types.ModuleType('drop')
    mod.os = os
    # app.py holds the cache in an st.cache_resource dict so it survives a
    # rerun; a plain dict stands in for it here.
    mod._rerun_caches = lambda: {'viewer_dir': {}, 'foreign': {}, 'drop': {}}
    exec(compile(ast.Module(body=keep + [cache, fn], type_ignores=[]), 'app.py',
                 'exec'), mod.__dict__)
    return mod


def test_loose_files_stage_flat_and_count():
    mod = _load()
    files = [_Fake('LAMBEY001_1550.sor'), _Fake('LAMBEY002_1550.trc'),
             _Fake('.DS_Store')]
    d, n, dupes = mod._stage_dropped(files)
    assert os.path.isdir(d)
    assert dupes == []
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
    d, n, _dupes = mod._stage_dropped([zip_file])
    assert n == 2


def test_same_signature_reuses_dir():
    mod = _load()
    files = [_Fake('A0001_1550.sor')]
    d1, _n1, _d1 = mod._stage_dropped(files)
    d2, _n2, _d2 = mod._stage_dropped([_Fake('A0001_1550.sor')])
    assert d1 == d2                                 # rerun-stable staging


class _Upload(_Fake):
    """Like Streamlit's UploadedFile: every drop gets new upload ids."""
    def __init__(self, name, file_id, data=b'x'):
        super().__init__(name, data)
        self.file_id = file_id


def test_same_upload_reuses_dir_and_a_new_drop_does_not():
    """The staging must survive a rerun (same upload, same ids), but a second
    drop of different files that happen to share names and sizes is new
    content and must not get the first drop's folder back."""
    mod = _load()
    first = [_Upload('A0001_1550.sor', 'id-1', b'first')]
    d1, _n, _d = mod._stage_dropped(first)
    assert mod._stage_dropped(first)[0] == d1              # rerun
    d2, _n, _d = mod._stage_dropped([_Upload('A0001_1550.sor', 'id-2', b'other')])
    assert d2 != d1
    with open(os.path.join(d2, 'A0001_1550.sor'), 'rb') as fh:
        assert fh.read() == b'other'


def test_the_cache_lives_where_a_rerun_cannot_empty_it():
    """Streamlit re-executes app.py in a fresh module on every rerun, so a
    module-level {} never hit.  The three staging caches must come from the
    st.cache_resource store."""
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    assert "@st.cache_resource(show_spinner=False)\ndef _rerun_caches():" in src
    for name, slot in (('_VIEWER_DIR_CACHE', 'viewer_dir'),
                       ('_FOREIGN_STAGE_CACHE', 'foreign'),
                       ('_DROP_STAGE_CACHE', 'drop')):
        assert f"{name} = _rerun_caches()['{slot}']" in src, name


def test_repeated_name_is_reported_not_overwritten():
    """Dragging a parent folder in hands us both subfolders' files, flat: the
    two directions of Montgomery TX both hold 0001_1550.sor.  The first wins
    (this folder goes straight to an engine that lists it with os.listdir, so
    a nested copy would never be read), and the repeat is REPORTED."""
    mod = _load()
    files = [_Fake('0001_1550.sor', b'A-direction'),
             _Fake('0001_1550.sor', b'B-direction'),
             _Fake('0002_1550.sor', b'A-direction')]
    d, n, dupes = mod._stage_dropped(files)
    assert dupes == ['0001_1550.sor']
    assert n == 2                                   # one of each name on disk
    with open(os.path.join(d, '0001_1550.sor'), 'rb') as fh:
        assert fh.read() == b'A-direction'          # first kept, not clobbered
