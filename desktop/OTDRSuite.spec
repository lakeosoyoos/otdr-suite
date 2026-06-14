# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for the OTDR Suite desktop app (Windows, one-folder).
#
# CRITICAL TOOLCHAIN — DO NOT CHANGE WITHOUT READING (same lessons as the
# Splice Report / Secret Sauce builds):
#   * Build with Python 3.11 (NOT 3.12+).  We pin setuptools==65.5.1; that
#     version's pkg_resources uses pkgutil.ImpImporter, removed in 3.12, so
#     the exe crashes at launch on 3.12 with
#     "module 'pkgutil' has no attribute 'ImpImporter'".
#   * setuptools must be EXACTLY 65.5.1, installed LAST (build.bat re-pins it
#     after the other deps).  Newer setuptools makes pkg_resources strict and
#     crashes the exe with "InvalidVersion: '.../OTDRSuite'".
#   * pkg_resources' vendored jaraco/packaging/platformdirs/etc. are bundled
#     three ways (collect_submodules + real top-level installs + collect_all).
#
# OTDR-SUITE-SPECIFIC NOTE — the sor_reader collision:
#   viewer/ and secretsauce/ each ship a DIFFERENT sor_reader324802a.py.
#   Two same-named modules cannot coexist in one frozen archive, so we do
#   NOT list any of our engine modules in hiddenimports.  Instead every
#   engine .py is bundled as ON-DISK DATA under viewer/ and secretsauce/,
#   and loaded at runtime via sys.path:
#     - the hub process adds <bundle>/viewer  → imports the viewer's copy
#     - the `--run-secretsauce` subprocess adds <bundle>/secretsauce
#   Their third-party deps (numpy/openpyxl/reportlab/matplotlib) are pulled
#   in by the collect_all() calls below, independent of our engine analysis.
#
# A green PyInstaller build proves NOTHING about whether the exe boots.
# The only proof is the boot self-test (build.bat step 6 / CI).  Treat a
# green build with a missing/failing boot test as broken.

import os
from PyInstaller.utils.hooks import (
    collect_all, collect_submodules, collect_data_files,
)

APP_NAME  = "OTDRSuite"
SPEC_DIR  = os.path.dirname(os.path.abspath(SPEC))
REPO_ROOT = os.path.dirname(SPEC_DIR)

block_cipher = None
datas, binaries, hiddenimports = [], [], []

# ─── Heavy shells fully bundled (needed by hub AND secret-sauce engine) ──
_to_collect = ["streamlit", "altair", "numpy", "openpyxl", "reportlab", "matplotlib"]
_optional   = ["pyarrow", "pandas", "scipy"]
for name in _to_collect + _optional:
    try:
        d, b, h = collect_all(name)
        datas += d; binaries += b; hiddenimports += h
    except Exception as e:
        print(f"[spec] skip collect_all({name}): {e}")

# ─── pkg_resources + setuptools (vendored deps) ──────────────────────────
hiddenimports += collect_submodules("pkg_resources")
hiddenimports += collect_submodules("setuptools")
datas += collect_data_files("pkg_resources")
for name in ("jaraco.text", "jaraco.functools", "jaraco.context",
             "more_itertools", "packaging", "platformdirs", "appdirs",
             "ordered_set"):
    try:
        d, b, h = collect_all(name)
        datas += d; binaries += b; hiddenimports += h
    except Exception as e:
        print(f"[spec] skip collect_all({name}): {e}")

# ─── Hidden imports — third-party only.  NEVER our engine modules
#     (sor_reader324802a collides between viewer/ and secretsauce/). ───────
hiddenimports += [
    "tkinter", "tkinter.filedialog",
    "streamlit.web.cli", "streamlit.runtime",
    "streamlit.runtime.scriptrunner.magic_funcs",
]

# ─── Our code, bundled as ON-DISK DATA (loaded via sys.path at runtime) ──
datas += [(os.path.join(REPO_ROOT, "app.py"), ".")]

def _add_dir(subdir):
    src = os.path.join(REPO_ROOT, subdir)
    for fn in os.listdir(src):
        if fn.endswith((".py", ".html", ".png")) and not fn.startswith("."):
            datas.append((os.path.join(src, fn), subdir))

_add_dir("viewer")        # viewer.html, trace_server.py, sor_reader324802a.py, json_reader.py
_add_dir("secretsauce")   # run_secretsauce.py, report*.py, trc_parser.py, sor_reader324802a.py, zerodblogo.png

excludes = ["weasyprint", "cairocffi", "pango", "gobject",
            "PyQt5", "PyQt6", "PySide2", "PySide6"]

a = Analysis(
    [os.path.join(SPEC_DIR, "launcher.py")],
    pathex=[REPO_ROOT, SPEC_DIR],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX corrupts some PyInstaller bootloaders on Windows.
    console=False,       # windowed — no console flashes for the tech.
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, upx_exclude=[],
    name=APP_NAME,
)
