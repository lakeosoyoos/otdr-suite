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
* By NAME, sor_reader324802a and json_reader are the Splice Report engine's
  copies in every test process, in any file order (see "One reader per
  name" below).  A test about the viewer's or Secret Sauce's own reader
  loads that file by path under a name of its own
  (importlib.util.spec_from_file_location), or runs a subprocess.
* trace_server runs on the VIEWER's own two readers, as it does in the hub
  a tech runs, whoever imports it and in any file order (see "The viewer on
  its own readers" below).
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
# Larger spans (24 fibers/dir) — the splice pipeline needs >= MIN_POP_SPLICE (20).
FIXTURE_SPLICE_A_DIR: Path = FIXTURE_DIR / "splice_A"
FIXTURE_SPLICE_B_DIR: Path = FIXTURE_DIR / "splice_B"
SPLICEREPORT_DIR: Path = REPO_ROOT / "splicereport"

for p in (REPO_ROOT, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import importlib.util

import pytest

# ── One reader per name, the same in every test order ─────────────────────
# Python caches a module by NAME, so the first sor_reader324802a.py (or
# json_reader.py) a process imports answers every later import of that name,
# whatever sys.path says by then.  In the alphabetical run the Splice Report
# engine's copies happen to land first and every test has been written
# against that.  Put the viewer's copy first instead (a viewer test file that
# sorts ahead, a shuffled run, a hand-picked list of files) and
# `import splicereportmatchexfo` fails on the first name only the engine's
# reader has.  So the engine's copies are loaded here, by path, before any
# test module is imported, and put back before every test module and every
# test.  A test that needs the viewer's or Secret Sauce's reader loads it by
# path under a name of its own, or runs a subprocess.
SHARED_READER_NAMES = ("sor_reader324802a", "json_reader")


def _load_by_path(name, path):
    """The module in the file `path`, cached under `name`."""
    have = sys.modules.get(name)
    have_file = getattr(have, "__file__", None)
    if have_file and Path(have_file).resolve() == path.resolve():
        return have
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        if have is not None:
            sys.modules[name] = have
        raise
    return mod


_ENGINE_READERS = {name: _load_by_path(name, SPLICEREPORT_DIR / (name + ".py"))
                   for name in SHARED_READER_NAMES}

# ── The viewer on its own readers ─────────────────────────────────────────
# trace_server asks for the two readers by name, once, when it is imported.
# In the hub a tech runs only viewer/ is on the path at that moment, so it
# gets the viewer's copies.  In this process the engine's copies hold the
# names, and the viewer tests ran on the engine's readers: not what ships.
# So the viewer is imported here, with the viewer's copies standing in for
# the two names for the length of that one import.  Every later
# `import trace_server` (a viewer test, the hub, import_trace_server) is
# handed this module.  The viewer's copies stay cached under names of their
# own.
VIEWER_READER_PREFIX = "viewer_"

_VIEWER_READERS = {name: _load_by_path(VIEWER_READER_PREFIX + name,
                                       VIEWER_DIR / (name + ".py"))
                   for name in SHARED_READER_NAMES}


def _load_viewer():
    """trace_server, imported while the two names mean the viewer's copies."""
    sys.modules.update(_VIEWER_READERS)
    sys.modules.pop("trace_server", None)
    try:
        return _load_by_path("trace_server", VIEWER_DIR / "trace_server.py")
    finally:
        sys.modules.update(_ENGINE_READERS)


_CACHED = dict(_ENGINE_READERS, trace_server=_load_viewer())


def _put_cached_back():
    for name, mod in _CACHED.items():
        if sys.modules.get(name) is not mod:
            sys.modules[name] = mod


def pytest_collectstart(collector):
    """Runs before each test module is imported."""
    _put_cached_back()


@pytest.fixture(autouse=True)
def _cached_modules_put_back():
    """A test that swapped a reader or the viewer in sys.modules does not
    pass it on."""
    _put_cached_back()
    yield
    _put_cached_back()

# The span the trace server held when the running test first opened the hub
# (see run_streamlit and _no_span_left_loaded).
_HUB = {"opened": False, "before": None}


def _server_dirs(*put):
    """Read, or with two arguments set, the trace server's folders, on the
    module every test and the hub share (imported above)."""
    tv = sys.modules.get("trace_server")
    if tv is None or not hasattr(tv, "set_dirs"):
        return None
    if put:
        tv.set_dirs(*put)
    return tv.CONFIG.get("dir_a"), tv.CONFIG.get("dir_b")


@pytest.fixture(autouse=True)
def _no_span_left_loaded():
    """A test that opens the hub starts with no span loaded and leaves none.

    The trace server's folders are process-wide and seed the left panel of
    every new hub session, as they do for a tech who reopens the tab.  With
    the panel loaded the tools run on it and draw no loader of their own, so
    a span one test left behind would load the panel of the next, and a test
    that hands a tool its own folder would find the tool running on the
    other test's span.

    Only hub tests are touched, and the folders are put back afterwards: the
    viewer's own tests set them once per module and read them in every test."""
    _HUB["opened"], _HUB["before"] = False, None
    yield
    if _HUB["opened"]:
        _server_dirs(*(_HUB["before"] or (None, None)))


def run_streamlit(default_timeout: float = 60.0, **kwargs):
    """AppTest pointed at the hub app.py.  The first hub a test opens finds
    no span loaded; a second one inherits what the first loaded, as a tech's
    new tab does."""
    from streamlit.testing.v1 import AppTest
    if not _HUB["opened"]:
        _HUB["opened"] = True
        _HUB["before"] = _server_dirs()
        _server_dirs(None, None)
    return AppTest.from_file(str(APP_PATH), default_timeout=default_timeout, **kwargs)


def import_trace_server():
    """The viewer engine, running on the viewer's own two readers (see "The
    viewer on its own readers" above).  VIEWER_DIR goes on sys.path as it
    always has."""
    if str(VIEWER_DIR) not in sys.path:
        sys.path.insert(0, str(VIEWER_DIR))
    return _CACHED["trace_server"]


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


def run_splicereport(dir_a, dir_b, out_xlsx, site_a="A", site_b="B", overrides=None):
    """Run the Splice Report runner as a subprocess (its own sor_reader copy).
    `overrides` is an optional engine-global threshold dict (e.g.
    {"REBURN_THRESHOLD": 0.05}) forwarded as --overrides JSON, exactly as the
    hub does when the OTDR settings panel has active overrides.
    Returns (returncode, manifest_dict_or_None, stderr_str)."""
    runner = SPLICEREPORT_DIR / "run_splicereport.py"
    cmd = [sys.executable, str(runner), "--dir-a", str(dir_a), "--dir-b", str(dir_b),
           "--out", str(out_xlsx), "--site-a", site_a, "--site-b", site_b]
    if overrides:
        cmd += ["--overrides", json.dumps(overrides)]
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


def single_dir_fixture(tmp_path):
    """Copy only the 4 A-direction SOR files into one flat tmp folder.
    Fibers 1–4 are distinct, so the "Stay in app" pairs are all viewable
    (one direction group, no fiber-number collisions)."""
    import shutil
    d = tmp_path / "one_dir"
    d.mkdir()
    for src in FIXTURE_A_DIR.glob("*.sor"):
        shutil.copy(src, d / src.name)
    return d


__all__ = [
    "REPO_ROOT", "APP_PATH", "VIEWER_DIR", "SECRETSAUCE_DIR", "SPLICEREPORT_DIR",
    "SHARED_READER_NAMES",
    "FIXTURE_DIR", "FIXTURE_A_DIR", "FIXTURE_B_DIR",
    "FIXTURE_SPLICE_A_DIR", "FIXTURE_SPLICE_B_DIR",
    "run_streamlit", "import_trace_server", "run_secretsauce", "run_splicereport",
    "mixed_fixture_dir", "single_dir_fixture",
]
