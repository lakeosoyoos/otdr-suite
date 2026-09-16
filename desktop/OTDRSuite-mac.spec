# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for the OTDR Suite desktop app (macOS .app).
# IDENTICAL to OTDRSuite.spec (Windows) except for the trailing BUNDLE()
# step that turns the COLLECT dir into a double-clickable .app.  Read the
# top-of-file comment in OTDRSuite.spec for the toolchain pins and the
# sor_reader-collision strategy and why each matters.
#
# This Mac build is for LOCAL DE-RISKING — it flushes OS-independent
# packaging bugs (the sor_reader isolation, the --run-secretsauce dispatch,
# the Streamlit first-run hang) before burning a Windows CI cycle.  A green
# Mac build does NOT prove the Windows app launches; the Windows CI boot
# self-test in build-windows.yml is the authoritative check.

import os
from PyInstaller.utils.hooks import (
    collect_all, collect_submodules, collect_data_files,
)

APP_NAME  = "OTDRSuite"
SPEC_DIR  = os.path.dirname(os.path.abspath(SPEC))
REPO_ROOT = os.path.dirname(SPEC_DIR)

block_cipher = None
datas, binaries, hiddenimports = [], [], []

# cryptography + certifi: needed by the launcher's SIGNED auto-update (Ed25519
# manifest verification + explicit-CA TLS).  Not optional — the update path
# fails closed without them.
_to_collect = ["streamlit", "altair", "numpy", "openpyxl", "reportlab", "matplotlib",
               "cryptography", "certifi"]
_optional   = ["pyarrow", "pandas", "scipy"]
for name in _to_collect + _optional:
    try:
        d, b, h = collect_all(name)
        datas += d; binaries += b; hiddenimports += h
    except Exception as e:
        print(f"[spec] skip collect_all({name}): {e}")

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

# Third-party only — NEVER our engine modules (sor_reader324802a collides).
hiddenimports += [
    "tkinter", "tkinter.filedialog",
    "streamlit.web.cli", "streamlit.runtime",
    "streamlit.runtime.scriptrunner.magic_funcs",
    # Custom HTML component for the EXFO-style OTDR settings panel
    # (Splice Report page) — index.html loaded from disk, see datas below.
    "components.otdr_settings",
]

# Our code as ON-DISK DATA (loaded via sys.path at runtime).
datas += [(os.path.join(REPO_ROOT, "app.py"), ".")]
datas += [(os.path.join(REPO_ROOT, "error_report.py"), ".")]   # stdlib-only Slack reporter
datas += [(os.path.join(REPO_ROOT, "folder_intake.py"), ".")]  # stdlib-only folder/zip intake

# Custom Streamlit component (EXFO OTDR settings panel) — declare_component
# resolves index.html next to __init__.py, so both ship under
# components/otdr_settings/ (mirrors the standalone SpliceReport-mac.spec).
datas += [(os.path.join(REPO_ROOT, "components", "otdr_settings", "__init__.py"),
           "components/otdr_settings")]
datas += [(os.path.join(REPO_ROOT, "components", "otdr_settings", "index.html"),
           "components/otdr_settings")]

def _add_dir(subdir):
    src = os.path.join(REPO_ROOT, subdir)
    for fn in os.listdir(src):
        if fn.endswith((".py", ".html", ".png")) and not fn.startswith("."):
            datas.append((os.path.join(src, fn), subdir))

_add_dir("viewer")
_add_dir("secretsauce")
_add_dir("splicereport")

# Error-report webhook — bundled only if present (CI writes it from the secret).
_webhook = os.path.join(SPEC_DIR, "_webhook.cfg")
if os.path.exists(_webhook):
    datas += [(_webhook, ".")]

# Build stamp — CI writes version.json at the repo root before the bundle step
# (see build-windows.yml + OTDRSuite.spec).  Conditional like _webhook.cfg:
# absent in a dev checkout → skipped and the app shows "dev".
_version = os.path.join(REPO_ROOT, "version.json")
if os.path.exists(_version):
    datas += [(_version, ".")]

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
    upx=False,
    console=False,
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

app = BUNDLE(
    coll,
    name=f"{APP_NAME}.app",
    icon=None,
    bundle_identifier="com.lakeosoyoos.otdrsuite",
    info_plist={
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": "OTDR Suite",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
        "NSHighResolutionCapable": True,
        "LSUIElement": False,
    },
)
