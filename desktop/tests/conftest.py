"""
Shared scaffolding for the OTDR Suite desktop test suite.

The other test modules import the symbols below.  Mirrors the Splice Report
test pattern, adapted for this app's two-engine layout (in-process viewer
trace server + subprocess Secret Sauce runner).

Exports
-------
REPO_ROOT, APP_PATH, VIEWER_DIR, SECRETSAUCE_DIR : pathlib.Path
FIXTURE_DIR, FIXTURE_A_DIR, FIXTURE_B_DIR        : pathlib.Path
    span_A = 4 ELMMIL (A-direction) SOR files; span_B = 4 MILELM (B) files.
    A mixed folder of all 8 clears Secret Sauce's >=2-per-direction-group rule.
run_streamlit(default_timeout=60, **kwargs)      : -> AppTest on app.py
import_trace_server()                            : -> the viewer engine module
                                                    (with VIEWER_DIR on sys.path)
run_secretsauce(folder, out_dir, fmt='xlsx')     : -> (returncode, manifest|None, stderr)
    Invokes secretsauce/run_secretsauce.py exactly as the hub does in dev.

Conventions for the other suites
--------------------------------
* Use these helpers + FIXTURE_* constants; don't rebuild the plumbing.
* The viewer engine (trace_server) and Secret Sauce engine ship DIFFERENT
  sor_reader324802a.py copies — never import both in one process.  Use
  import_trace_server() for the viewer side and run_secretsauce() (a
  subprocess) for the Secret Sauce side.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DESKTOP_DIR = HERE.parent
REPO_ROOT = DESKTOP_DIR.parent

APP_PATH: Path = REPO_ROOT / "app.py"
VIEWER_DIR: Path = REPO_ROOT / "viewer"
SECRETSAUCE_DIR: Path = REPO_ROOT / "secretsauce"
FIXTURE_DIR: Path = HERE / "fixtures"
FIXTURE_A_DIR: Path = FIXTURE_DIR / "span_A"
FIXTURE_B_DIR: Path = FIXTURE_DIR / "span_B"

for p in (REPO_ROOT, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def run_streamlit(default_timeout: float = 60.0, **kwargs):
    """AppTest pointed at the hub app.py."""
    from streamlit.testing.v1 import AppTest
    return AppTest.from_file(str(APP_PATH), default_timeout=default_timeout, **kwargs)


def import_trace_server():
    """Import the viewer engine with VIEWER_DIR on sys.path so it resolves
    the viewer's sor_reader324802a copy (NOT Secret Sauce's)."""
    if str(VIEWER_DIR) not in sys.path:
        sys.path.insert(0, str(VIEWER_DIR))
    import trace_server
    return trace_server


def run_secretsauce(folder, out_dir, fmt: str = "xlsx"):
    """Run the Secret Sauce runner as a subprocess (as the hub does in dev).
    Returns (returncode, manifest_dict_or_None, stderr_str)."""
    runner = SECRETSAUCE_DIR / "run_secretsauce.py"
    cmd = [sys.executable, str(runner),
           "--folder", str(folder), "--out-dir", str(out_dir), "--format", fmt]
    p = subprocess.run(cmd, capture_output=True, text=True)
    manifest = None
    for line in reversed((p.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                manifest = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    return p.returncode, manifest, p.stderr


def mixed_fixture_dir(tmp_path):
    """Copy all 8 fixture SOR files into one flat tmp folder (both directions)
    — the shape Secret Sauce's folder picker sees."""
    import shutil
    d = tmp_path / "mixed"
    d.mkdir()
    for src in list(FIXTURE_A_DIR.glob("*.sor")) + list(FIXTURE_B_DIR.glob("*.sor")):
        shutil.copy(src, d / src.name)
    return d


__all__ = [
    "REPO_ROOT", "APP_PATH", "VIEWER_DIR", "SECRETSAUCE_DIR",
    "FIXTURE_DIR", "FIXTURE_A_DIR", "FIXTURE_B_DIR",
    "run_streamlit", "import_trace_server", "run_secretsauce", "mixed_fixture_dir",
]
