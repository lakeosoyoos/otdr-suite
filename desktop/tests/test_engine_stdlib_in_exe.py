"""Engine files may import only standard modules the exe is known to carry.

The engine files (launcher.ENGINE_FILES) ship in the exe as on-disk data and
arrive later by hot update, so PyInstaller never reads their imports: a
standard module that nothing else in the exe pulls in is simply not there.
viewer/trace_server.py imported filecmp, and every Rename of dropped files
died with ModuleNotFoundError in the App's VM test (2026-10-01).

A new standard import here needs a check that the frozen exe has it (the App
log, or `import <name>` from the installed exe), then a line in KNOWN_IN_EXE.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
import sys
import sysconfig

from conftest import REPO_ROOT, import_trace_server

# Every standard module the engine files imported on 2026-10-01, all in use
# in the field.  filecmp is NOT here: the exe does not have it.
KNOWN_IN_EXE = {
    '__future__', 'argparse', 'base64', 'calendar', 'collections', 'contextlib',
    'copy', 'dataclasses', 'datetime', 'decimal', 'email', 'functools',
    'getpass', 'glob', 'hashlib', 'heapq', 'html', 'http', 'importlib', 'io',
    'itertools', 'json', 'math', 'os', 'pathlib', 'platform', 're', 'secrets',
    'shutil', 'socket', 'ssl', 'struct', 'subprocess', 'sys', 'tempfile',
    'threading', 'time', 'tkinter', 'traceback', 'typing', 'urllib', 'uuid',
    'webbrowser', 'xml', 'zipfile', 'zlib', 'zoneinfo',
    # OTDR Suite App's own engine code (2026-10-02), each with its reason:
    'ctypes',     # desktop/launcher.py imports it: PyInstaller bundles it
    'msvcrt',     # desktop/launcher.py imports it; built into Python on Windows
    'inspect',    # Streamlit (collect_all) imports it
    'posixpath',  # pathlib imports it
    'pwd',        # POSIX only, behind os.name != 'nt' (never on Windows)
    'fcntl',      # POSIX only, behind os.name != 'nt' (never on Windows)
    'smtplib',    # owner e-mail: in the App spec's hiddenimports
}


def _engine_py_files():
    src = (REPO_ROOT / 'desktop' / 'launcher.py').read_text(encoding='utf-8')
    block = src[src.index('ENGINE_FILES = ['):]
    block = block[:block.index('\n]')]
    return [f for f in re.findall(r'"([^"]+\.py)"', block)]


def _is_stdlib(name):
    names = getattr(sys, 'stdlib_module_names', None)
    if names is not None:
        return name in names
    if name in sys.builtin_module_names:
        return True
    spec = importlib.util.find_spec(name)
    origin = (spec and (spec.origin or (spec.submodule_search_locations or [''])[0])) or ''
    std = os.path.normcase(sysconfig.get_paths()['stdlib'])
    return os.path.normcase(origin).startswith(std) and 'site-packages' not in origin


def _imports(path):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            yield from (a.name.split('.')[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            yield n.module.split('.')[0]


def test_engine_files_import_only_standard_modules_the_exe_carries():
    unknown = {}
    for rel in _engine_py_files():
        path = REPO_ROOT / rel
        if not path.exists():
            continue
        for m in _imports(path):
            if m not in KNOWN_IN_EXE and _is_stdlib(m):
                unknown.setdefault(m, set()).add(rel)
    assert not unknown, (
        'engine files import standard modules the exe may not carry '
        '(see this file\'s docstring): %r' % {k: sorted(v) for k, v in unknown.items()})


def test_filecmp_is_not_known_to_the_exe():
    assert 'filecmp' not in KNOWN_IN_EXE


def test_same_bytes_compares_without_filecmp(tmp_path, monkeypatch):
    TS = import_trace_server()
    monkeypatch.setitem(sys.modules, 'filecmp', None)     # as in the exe
    a, b, c, d = (tmp_path / n for n in 'abcd')
    a.write_bytes(b'x' * 200_000)
    b.write_bytes(b'x' * 200_000)
    c.write_bytes(b'x' * 199_999 + b'y')                  # same size, last byte
    d.write_bytes(b'x' * 10)
    assert TS._same_bytes(str(a), str(b)) is True
    assert TS._same_bytes(str(a), str(c)) is False
    assert TS._same_bytes(str(a), str(d)) is False
    assert TS._same_bytes(str(a), str(tmp_path / 'gone')) is False
