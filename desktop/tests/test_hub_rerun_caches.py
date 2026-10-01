"""The hub's staging caches must survive a rerun, and must never serve stale input.

Streamlit re-executes app.py in a fresh module on every rerun, so the three
staging caches used to be empty on every pass.  Every click on a one-folder
tool re-read every trace header (165 MB on a 1,728-file folder), a folder
holding foreign files was copied into a NEW temp folder each time (and a
Unidirectional report vanished, because its manifest named the previous
copy), and a zipped Viewer input was unzipped again (reloading the Viewer).

They now live in an st.cache_resource store, keyed on exactly what each entry
was built from.  These tests run the real functions out of app.py in a bare
module whose cache_resource behaves like Streamlit's (one store per process),
and hub.rerun() re-executes that module the way a rerun does, so a plain
module-level {} would be empty again afterwards.
"""
import ast
import collections
import os
import shutil
import tempfile
import types
import zipfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
FX = os.path.join(HERE, 'fixtures')
import sys
sys.path.insert(0, ROOT)
import folder_intake as fi                                        # noqa: E402

_NAMES = {'_rerun_caches', '_remember', '_files_sig', '_stable_dir', '_settle',
          '_exclude_foreign_files', '_resolve_viewer_dir'}
_ASSIGNS = {'_RERUN_CACHE_KEPT', '_FOREIGN_STAGE_CACHE', '_VIEWER_DIR_CACHE'}


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """The cache functions out of app.py, with a counter on header reads."""
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path / 'temp'))
    os.makedirs(tempfile.tempdir)
    reads = collections.Counter()
    real = fi.sor_header

    def counted(path):
        reads['headers'] += 1
        return real(path)
    monkeypatch.setattr(fi, 'sor_header', counted)

    tree = ast.parse(open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read())
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in _NAMES)
            or (isinstance(n, ast.Assign)
                and any(getattr(t, 'id', '') in _ASSIGNS for t in n.targets))]
    store = {}

    def cache_resource(**_kw):
        def deco(fn):
            def inner():
                if fn.__name__ not in store:
                    store[fn.__name__] = fn()
                return store[fn.__name__]
            return inner
        return deco
    st = types.SimpleNamespace(cache_resource=cache_resource, session_state={},
                               warning=lambda *a, **k: None)
    code = compile(ast.Module(body=body, type_ignores=[]), 'app.py', 'exec')

    def run():
        """One script pass: a fresh module, the process-wide store kept."""
        mod = types.ModuleType('hub_caches')
        mod.os, mod.tempfile, mod.st = os, tempfile, st
        mod.report_error = lambda *a, **k: None
        mod.trace_server = types.SimpleNamespace(
            list_fibers=lambda d: fi.find_otdr_files(d) if os.path.isdir(d) else [])
        exec(code, mod.__dict__)
        mod.reads, mod.rerun, mod.restart = reads, run, restart
        return mod

    def restart():
        """A new hub process: nothing cached, the temp folder still there."""
        store.clear()
        return run()
    return run()


def _mixed_folder(where, trc=False):
    """24 traces of one span plus two strays from another job."""
    os.makedirs(where)
    for f in fi.find_otdr_files(os.path.join(FX, 'splice_A')):
        shutil.copy2(f, where)
    for f in fi.find_otdr_files(os.path.join(FX, 'frspan'))[:2]:
        shutil.copy2(f, where)
    if trc:
        with open(os.path.join(where, 'EXTRA0099.trc'), 'wb') as fh:
            fh.write(b'not a real trc')
    return where


def test_a_rerun_reuses_the_staged_copy_and_reads_no_headers(hub, tmp_path):
    d = _mixed_folder(str(tmp_path / 'mixed'))
    staged, foreign = hub._exclude_foreign_files(d)
    assert len(foreign) == 2 and staged != d
    hub = hub.rerun()                                              # the next click
    hub.reads.clear()
    again, _ = hub._exclude_foreign_files(d)
    assert again == staged
    assert hub.reads['headers'] == 0


def test_a_file_swapped_for_an_older_one_rebuilds_the_copy(hub, tmp_path):
    """Same count, same newest mtime: the old (count, newest mtime) key missed
    this.  An Explorer zip extraction keeps the archive's timestamps."""
    d = _mixed_folder(str(tmp_path / 'mixed'))
    staged, _ = hub._exclude_foreign_files(d)
    ours = {os.path.basename(f)
            for f in fi.find_otdr_files(os.path.join(FX, 'splice_A'))}
    victim = sorted(p for p in fi.find_otdr_files(d)
                    if os.path.basename(p) in ours)[0]
    old = os.stat(victim)
    new = victim + '.new'
    with open(victim, 'rb') as fh:
        data = bytearray(fh.read())
    data[-1] ^= 0xFF                                               # same size
    with open(new, 'wb') as fh:
        fh.write(bytes(data))
    os.utime(new, ns=(old.st_atime_ns, old.st_mtime_ns - 86_400 * 10**9))
    os.replace(new, victim)
    rebuilt, _ = hub.rerun()._exclude_foreign_files(d)
    assert rebuilt != staged
    with open(os.path.join(rebuilt, os.path.basename(victim)), 'rb') as fh:
        assert fh.read() == bytes(data)


def test_unidirectional_and_secret_sauce_keep_their_own_entries(hub, tmp_path):
    """Secret Sauce also reads .trc, so the same folder is a different input
    there.  One entry per folder made the two pages evict each other."""
    d = _mixed_folder(str(tmp_path / 'mixed'), trc=True)
    uni, _ = hub._exclude_foreign_files(d)
    ss, _ = hub._exclude_foreign_files(d, fi.OTDR_EXTS_WITH_TRC)
    hub = hub.rerun()
    hub.reads.clear()
    assert hub._exclude_foreign_files(d)[0] == uni
    assert hub._exclude_foreign_files(d, fi.OTDR_EXTS_WITH_TRC)[0] == ss
    assert hub.reads['headers'] == 0


def test_a_zipped_viewer_input_is_unzipped_once_and_again_when_replaced(hub, tmp_path):
    zp = str(tmp_path / 'span.zip')

    def write_zip(folder):
        with zipfile.ZipFile(zp, 'w') as z:
            for f in fi.find_otdr_files(os.path.join(FX, folder)):
                z.write(f, 'SPAN/' + os.path.basename(f))
    write_zip('splice_A')
    first, note = hub._resolve_viewer_dir(zp)
    assert note == 'viewing from .zip' and os.path.isdir(first)
    hub = hub.rerun()                                              # the next click
    assert hub._resolve_viewer_dir(zp)[0] == first
    write_zip('splice_B')                                          # a new zip, same path
    second, _ = hub.rerun()._resolve_viewer_dir(zp)
    assert second != first
    assert sorted(os.listdir(second)) == sorted(
        os.path.basename(f) for f in fi.find_otdr_files(os.path.join(FX, 'splice_B')))


def test_the_caches_keep_only_the_newest_entries(hub):
    cache = {}
    for i in range(hub._RERUN_CACHE_KEPT + 10):
        hub._remember(cache, i, i)
    assert len(cache) == hub._RERUN_CACHE_KEPT
    assert 0 not in cache and hub._RERUN_CACHE_KEPT + 9 in cache
    hub._remember(cache, 10, 'again')                              # refreshed = newest
    hub._remember(cache, 'x', 'x')
    assert 10 in cache and 11 not in cache


def test_a_restarted_hub_stages_to_the_same_folders(hub, tmp_path):
    """The Uni report and its saved copy are keyed on the folder a run used,
    and a report is saved under the folders it ran on: after a restart the
    same input must stage to the same place, with nothing re-extracted."""
    d = _mixed_folder(str(tmp_path / 'mixed'))
    staged, _ = hub._exclude_foreign_files(d)
    zp = str(tmp_path / 'span.zip')
    with zipfile.ZipFile(zp, 'w') as z:
        for f in fi.find_otdr_files(os.path.join(FX, 'splice_A')):
            z.write(f, 'SPAN/' + os.path.basename(f))
    unzipped, _ = hub._resolve_viewer_dir(zp)
    before = set(os.listdir(tempfile.gettempdir()))

    hub = hub.restart()
    assert hub._exclude_foreign_files(d)[0] == staged
    assert hub._resolve_viewer_dir(zp)[0] == unzipped
    assert set(os.listdir(tempfile.gettempdir())) == before       # nothing new built
