# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for the OTDR Suite desktop app (Windows, one-folder).
#
# CRITICAL TOOLCHAIN — DO NOT CHANGE WITHOUT READING (same lessons as the
# Splice Report / Secret Sauce builds):
#   * Build with Python 3.11 (NOT 3.12+).  We pin setuptools==65.5.1; that
#     version's pkg_resources uses pkgutil.ImpImporter, removed in 3.12.
#     While the exe bundled pkg_resources it crashed at launch on 3.12 with
#     "module 'pkgutil' has no attribute 'ImpImporter'"; the build is only
#     proven on 3.11.
#   * setuptools must be EXACTLY 65.5.1, installed LAST (build.bat re-pins it
#     after the other deps).  Newer setuptools makes pkg_resources strict and
#     crashes the exe with "InvalidVersion: '.../OTDRSuite'".
#   * Since 2026-10 pkg_resources and setuptools are NOT bundled (nothing the
#     app runs imports them; see below), which removes that crash from the
#     exe itself.  The build venv keeps the pin above all the same.
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
from PyInstaller.utils.hooks import collect_all

# OTDR App (this branch): the exe and its dist folder are OTDRApp, so the
# name a person sees in Task Manager and Explorer is the product's.
APP_NAME  = "OTDRApp"
SPEC_DIR  = os.path.dirname(os.path.abspath(SPEC))
REPO_ROOT = os.path.dirname(SPEC_DIR)

block_cipher = None
datas, binaries, hiddenimports = [], [], []

# ─── Heavy shells fully bundled (needed by hub AND secret-sauce engine) ──
# cryptography + certifi: the launcher's SIGNED auto-update verifies the update
# manifest with Ed25519 (cryptography) and verifies TLS with an explicit CA
# bundle (certifi) — neither is optional; the update path fails closed without
# them.
_to_collect = ["streamlit", "altair", "numpy", "openpyxl", "reportlab", "matplotlib",
               "cryptography", "certifi"]
# webview = the app window (pywebview + its WebView2 loader DLLs, via the
# contrib hook); clr_loader/pythonnet = what it drives WebView2 through.
# Optional: a build without it still runs, in a browser tab.
_optional   = ["pyarrow", "pandas", "scipy", "webview", "clr_loader", "pythonnet",
               "qrcode"]
# Each package's compiled modules and data, but not: its .py sources a second
# time (the compiled copy in the archive is what runs), its test suites, or
# build-only files (headers, Cython/Fortran sources, import libraries, type
# stubs).  Those were over half the installed files.
_SLIM = dict(
    include_py_files=False,
    filter_submodules=lambda mod: ".tests" not in mod and not mod.endswith(".conftest"),
    exclude_datas=["**/tests/**", "**/*.pyi", "**/*.pxd", "**/*.pyx", "**/*.h",
                   "**/*.c", "**/*.f90", "**/*.lib", "**/*.a"],
)
for name in _to_collect + _optional:
    try:
        d, b, h = collect_all(name, **_SLIM)
        datas += d; binaries += b; hiddenimports += h
    except Exception as e:
        print(f"[spec] skip collect_all({name}): {e}")

# ─── tkinter Tcl/Tk runtime — the "Browse for folder" picker needs the Tcl/Tk
#     SCRIPT libraries (tcl8*/tk8*), not just the _tkinter binary.  Without
#     collect_all the frozen Windows build ships _tkinter but no tcl/tk data, so
#     tk.Tk() raises TclError and Browse silently no-ops.  (The UI also falls
#     back to a pasted path, but Browse should work.)  VERIFY on a Windows CI
#     build — Tcl/Tk bundling is environment-sensitive.
try:
    _d, _b, _h = collect_all("tkinter")
    datas += _d; binaries += _b; hiddenimports += _h
except Exception as e:
    print(f"[spec] skip collect_all(tkinter): {e}")

# ─── pkg_resources / setuptools are NOT bundled ──────────────────────────
# Nothing the app runs imports them.  Bundling them only added two runtime
# hooks to every process start (the hub and every report run) and the
# "InvalidVersion: '.../OTDRSuite'" crash class.  packaging stays (streamlit
# uses it).  tzdata: Windows has no system time-zone database.
for name in ("packaging", "tzdata"):
    try:
        d, b, h = collect_all(name, **_SLIM)
        datas += d; binaries += b; hiddenimports += h
    except Exception as e:
        print(f"[spec] skip collect_all({name}): {e}")

# ─── Hidden imports — third-party only.  NEVER our engine modules
#     (sor_reader324802a collides between viewer/ and secretsauce/). ───────
hiddenimports += [
    "tkinter", "tkinter.filedialog",
    "streamlit.web.cli", "streamlit.runtime",
    "streamlit.runtime.scriptrunner.magic_funcs",
    # Custom HTML component for the EXFO-style OTDR settings panel
    # (Splice Report page).  declare_component loads index.html from disk
    # next to __init__.py — see the components/otdr_settings datas below.
    "components.otdr_settings",
    # OTDR Suite App: the owner e-mail (app.py _send_owner_mail) imports
    # smtplib inside a function of an engine file, which PyInstaller never
    # reads; listed here so the exe is sure to carry it
    # (test_engine_stdlib_in_exe).
    "smtplib",
]

# ─── Our code, bundled as ON-DISK DATA (loaded via sys.path at runtime) ──
datas += [(os.path.join(REPO_ROOT, "app.py"), ".")]
datas += [(os.path.join(REPO_ROOT, "error_report.py"), ".")]   # stdlib-only Slack reporter
datas += [(os.path.join(REPO_ROOT, "folder_intake.py"), ".")]  # stdlib-only folder/zip intake
datas += [(os.path.join(REPO_ROOT, "sharepoint_link.py"), ".")]  # stdlib-only SharePoint folder (App)

# ─── Custom Streamlit component (EXFO OTDR settings panel) ────────────────
# declare_component resolves index.html next to __init__.py, so both files
# must ship under components/otdr_settings/ (mirrors the standalone
# SpliceReport.spec).  The hub imports it in-process on the Splice Report page.
datas += [(os.path.join(REPO_ROOT, "components", "otdr_settings", "__init__.py"),
           "components/otdr_settings")]
datas += [(os.path.join(REPO_ROOT, "components", "otdr_settings", "index.html"),
           "components/otdr_settings")]

def _add_dir(subdir):
    src = os.path.join(REPO_ROOT, subdir)
    for fn in os.listdir(src):
        if fn.endswith((".py", ".html", ".png")) and not fn.startswith("."):
            datas.append((os.path.join(src, fn), subdir))

_add_dir("viewer")        # viewer.html, trace_server.py, sor_reader324802a.py, json_reader.py
_add_dir("secretsauce")   # run_secretsauce.py, report*.py, trc_parser.py, sor_reader324802a.py, zerodblogo.png
_add_dir("splicereport")  # run_splicereport.py, splicereportmatchexfo.py, sor_reader324802a.py, json_reader.py, acquisition_audit.py, reburn_summary.py

# fqa/ — the FQA Builder.  Unlike the three engine tools it ships no
# sor_reader and parses no traces, so it runs in-process as a hub page and
# needs no isolation.  It DOES ship a binary: the blank Lumen form its
# writer patches, which _add_dir's .py/.html/.png filter would drop.
def _add_tree(subdir, exts):
    root = os.path.join(REPO_ROOT, subdir)
    for dirpath, _dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, REPO_ROOT)
        if "private_photos" in rel.split(os.sep):     # local-only job photos: never shipped
            continue
        for fn in filenames:
            if fn.endswith(exts) and not fn.startswith("."):
                datas.append((os.path.join(dirpath, fn), rel))

_add_tree("fqa", (".py", ".xlsm"))
# demo/ -- the made-up span behind Home -> View Demonstrative Span: traces, a
# production sheet and a Field Capture package (its build script stays out).
_add_tree("demo", (".sor", ".xlsx", ".zfc", ".jpg", ".json"))
# fieldcapture/ — the Field Capture page: a web app the hub serves in-process.
# Everything under web/ ships, including the label reader's cores and language
# data (.wasm.js, .gz), which are not engine files and so live only here.
_add_tree("fieldcapture", (".py", ".html", ".js", ".css", ".gz", ".png", ".webmanifest"))


# Error-report webhook — bundled ONLY if CI wrote it from the SLACK_ERROR_WEBHOOK
# secret (see build-windows.yml).  Absent in dev / when the secret is unset →
# error reporting ships OFF.  Never committed (the repo is public).
_webhook = os.path.join(SPEC_DIR, "_webhook.cfg")
if os.path.exists(_webhook):
    datas += [(_webhook, ".")]

# Project Owner emails' sender mailbox — bundled ONLY if CI wrote it from the
# OWNER_MAIL_SENDER secret (JSON: host, port, user, password, from).  Absent →
# owner emails ship OFF.  Never committed (the repo is public).
_mail_sender = os.path.join(SPEC_DIR, "_mail_sender.cfg")
if os.path.exists(_mail_sender):
    datas += [(_mail_sender, ".")]

# Build stamp — CI writes version.json at the repo root BEFORE the bundle step
# (build-windows.yml "Write version.json"): build number + UTC date + short
# commit.  Bundled so the app sidebar + error reports can identify the build.
# Same conditional pattern as _webhook.cfg: absent in a dev checkout → skipped
# and the app shows "dev".
_version = os.path.join(REPO_ROOT, "version.json")
if os.path.exists(_version):
    datas += [(_version, ".")]

excludes = ["weasyprint", "cairocffi", "pango", "gobject",
            "PyQt5", "PyQt6", "PySide2", "PySide6",
            # build and test tools, never run by the app
            "pkg_resources", "setuptools", "_distutils_hack",
            "pytest", "_pytest", "pluggy", "PyInstaller"]

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
