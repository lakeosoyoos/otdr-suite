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
temp_home (fixture)                              : -> pathlib.Path
    ~ for this test only (see "A home of the test's own" below).

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
import os
import subprocess
import sys
from pathlib import Path

# The launcher shows the hub in its own window (a second process).  Tests that
# drive launcher.main() must never open real windows: force the browser path,
# which every one of them already stubs.  test_app_window.py clears it.
os.environ["OTDR_SUITE_BROWSER"] = "1"

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _launcher_edition_env_does_not_leak(monkeypatch):
    """launcher.main() exports the edition (app folder, edition name, the
    no-update switch) into os.environ for the hub it starts.  A test that runs
    main() in-process must not hand those to every later test: the update
    banner tests would see no manifest and the error-payload tests an extra
    edition tag.  monkeypatch puts each back as it was after every test."""
    for key in ("OTDR_SUITE_APP_DIR", "OTDR_SUITE_EDITION", "OTDR_SUITE_NO_UPDATE",
                "STREAMLIT_CLIENT_TOOLBAR_MODE", "OTDR_SUITE_MANIFEST_URL",
                "OTDR_SUITE_UPDATE_CHANNEL", "OTDR_SUITE_INSTALLER_URL"):
        monkeypatch.delenv(key, raising=False)

# The hub always opens on its home screen (Quick Analysis / Start New Project /
# Open Recent Project).  Most AppTests drive the trace tools from the first
# run, so run_streamlit() starts them already in Quick Analysis (app_mode
# 'traces'), exactly as clicking that button does.  A test that is about the
# home screen sets OTDR_TEST_HOME=1 (test-only; the app never reads it).

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

# ── A home of the test's own ──────────────────────────────────────────────
# The launcher keeps its engine cache, the cache's meta and its markers under
# Path.home() / ".otdrSuite".  A test that hands _recover_cache a temp engine
# still reads the meta from the REAL home, and when that meta's hashes condemn
# the temp engine it unlinks the real meta: on a Windows dev box, a re-download
# of the same engine at the next boot.  A launcher test module opts in with
# pytestmark = pytest.mark.usefixtures("temp_home").


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    """Point ~ at tmp_path / "home" for this test and return it: Path.home()
    itself, and HOME / USERPROFILE / HOMEDRIVE+HOMEPATH so os.path.expanduser
    agrees on either platform (ntpath ignores HOME).

    For this process only: a Python child handed this HOME loses the user
    site-packages (see test_stale_engine_gate), so a test that spawns one
    builds that child's env itself."""
    import os
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    drive, tail = os.path.splitdrive(str(home))
    monkeypatch.setenv("HOMEDRIVE", drive)
    monkeypatch.setenv("HOMEPATH", tail)
    assert Path.home() == home and Path(os.path.expanduser("~")) == home, (
        "~ still resolves outside the test's own home")
    return home

# The span the trace server held when the running test first opened the hub,
# and the rest of its CONFIG then (see run_streamlit and _no_span_left_loaded).
_HUB = {"opened": False, "before": None, "config": None}


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
    viewer's own tests set them once per module and read them in every test.

    The rest of the server's CONFIG goes back too.  Every hub run writes its
    own into it (engine_argv, settings, analysis_mode, the report's gates),
    and a hub test that plays the installed build (sys.frozen) leaves the
    frozen engine command, [python, '--run-splicereport'], which a plain
    python refuses: the next test that has the server run the report then
    gets no verdicts.  The test order used to hide it, a later hub test
    writing the dev command back; in the CI's parallel parts the order is
    not that."""
    _HUB["opened"], _HUB["before"], _HUB["config"] = False, None, None
    yield
    if _HUB["opened"]:
        _server_dirs(*(_HUB["before"] or (None, None)))
        if _HUB["config"] is not None:
            config, before = _HUB["config"]
            config.clear()
            config.update(before)


def run_streamlit(default_timeout: float = 60.0, **kwargs):
    """AppTest pointed at the hub app.py.  The first hub a test opens finds
    no span loaded; a second one inherits what the first loaded, as a tech's
    new tab does."""
    from streamlit.testing.v1 import AppTest
    if not _HUB["opened"]:
        _HUB["opened"] = True
        _HUB["before"] = _server_dirs()
        tv = sys.modules.get("trace_server")
        if isinstance(getattr(tv, "CONFIG", None), dict):
            _HUB["config"] = (tv.CONFIG, dict(tv.CONFIG))
        _server_dirs(None, None)
    at = AppTest.from_file(str(APP_PATH), default_timeout=default_timeout, **kwargs)
    if os.environ.get("OTDR_TEST_HOME") != "1":
        at.session_state["app_mode"] = "traces"
    return at


# ── The hub's top bar (sandbox/top-tabs-design) ─────────────────────────
# The sidebar is gone: a tab per page across the top (buttons keyed
# nav_tab_<Page>, the page name in session_state['nav_radio']), and the A/B
# Trace Folders boxes, their Browse buttons and Clear Traces on the Traces
# page only.  Tests reach them through these helpers.
TRACE_BOX_KEYS = {'a': 'view_dir_a_input', 'b': 'view_dir_b_input'}


def page_of(at):
    """The page the hub shows (session_state['nav_radio'])."""
    try:
        return at.session_state['nav_radio']
    except Exception:
        return None


def go_tab(at, page, timeout=None):
    """Click the top bar's tab for `page` and run.  Returns `at`."""
    btn = [b for b in at.button if getattr(b, 'key', None) == f'nav_tab_{page}']
    assert btn, f'no tab for {page!r} in the top bar'
    btn[0].click()
    return at.run(timeout=timeout) if timeout else at.run()


def trace_box(at, side):
    """The Traces page's A or B folder box (goes to the Traces tab first)."""
    if page_of(at) != 'Traces':
        go_tab(at, 'Traces')
    key = TRACE_BOX_KEYS[side.lower()]
    return next(t for t in at.text_input if t.key == key)


def trace_box_value(at, side):
    """What the A or B folder box holds, read on any page."""
    key = TRACE_BOX_KEYS[side.lower()]
    return at.session_state[key] if key in at.session_state else ''


def load_traces(at, a=None, b=None):
    """Type folders into the Traces page's boxes (None leaves a box as it
    is) and run.  Stays on the Traces tab.  Returns `at`."""
    for side, v in (('a', a), ('b', b)):
        if v is not None:
            trace_box(at, side).input(str(v))
    return at.run()


def clear_traces(at, allow=True):
    """Press Clear Traces on the Traces tab and answer its pop-up."""
    if page_of(at) != 'Traces':
        go_tab(at, 'Traces')
    at.button(key='side_clear_traces').click().run()
    if allow:
        at.button(key='clear_traces_allow').click().run()
    return at


def finish_engine_run(at, prefix, timeout: float = 300.0):
    """Let the report run a page started finish, and run the page so it takes
    the result.  `prefix` is the page's: 'sr', 'uni' or 'ss'.

    In the hub the run-time panel keeps itself up to date and hands back to
    the page when the engine is done (app._engine_live_panel), and a page
    pass draws the panel once and returns.  AppTest has no browser to keep
    the panel going, so a test waits for the engine itself; counting page
    passes only works on a machine fast enough.  Loops because a Splice
    Report with added spans starts the next span when one finishes.
    Returns `at`."""
    import time
    job_key = f"{prefix}_job"
    deadline = time.monotonic() + timeout
    while job_key in at.session_state:
        left = deadline - time.monotonic()
        if left <= 0:
            raise AssertionError(f"{prefix} run still going after {timeout:.0f} s")
        at.session_state[job_key]["proc"].wait(timeout=left)
        at.run()
    return at


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


def open_in_project(folder, monkeypatch, default_timeout: float = 180.0):
    """AppTest on the hub with the home screen on, `folder` opened as a
    project: where the FQA Builder and Field Capture live (Run Traces is the
    trace tools only, 2026-09-24)."""
    monkeypatch.setenv("OTDR_TEST_HOME", "1")
    monkeypatch.setenv("OTDR_SETTINGS_DIR", str(Path(folder).parent / ".settings"))
    os.makedirs(Path(folder).parent / ".settings", exist_ok=True)
    os.makedirs(folder, exist_ok=True)
    at = run_streamlit(default_timeout=default_timeout).run()
    next(b for b in at.button if b.label == "📂 Open Recent Project").click().run()
    at.text_input(key="home_folder").set_value(str(folder)).run()
    next(b for b in at.button if b.label == "Open This Folder").click().run()
    return at


def goto(at, page):
    """Open a tool page.  In a project there is no tool list in the sidebar
    (2026-09-27): the project screen's buttons set nav_radio, as this does."""
    at.session_state["nav_radio"] = page
    return at.run()


# ── No test writes into the real Documents (2026-09-27) ─────────────────
# With no saved projects_root the hub puts projects in
# <Documents>/OTDR Projects, and a package-import test once left "y",
# "y (2)" ... "y (11)" in the developer's real one.  Every test gets its own
# Documents (OTDR_DOCUMENTS_DIR, an env var because AppTest re-runs app.py
# and would not see a patched module attribute), and the run fails if the
# real folder still gains anything.
_REAL_PROJECTS = Path(os.path.expanduser("~")) / "Documents" / "OTDR Projects"


def _real_projects_listing():
    try:
        return set(os.listdir(_REAL_PROJECTS))
    except OSError:
        return set()


@pytest.fixture(autouse=True)
def _isolated_documents(tmp_path_factory, monkeypatch):
    docs = tmp_path_factory.mktemp("Documents")
    monkeypatch.setenv("OTDR_DOCUMENTS_DIR", str(docs))   # app._documents_folder reads it
    return docs


@pytest.fixture(autouse=True, scope="session")
def _real_documents_untouched():
    before = _real_projects_listing()
    yield
    added = sorted(_real_projects_listing() - before)
    assert not added, f"tests wrote into {_REAL_PROJECTS}: {added}"
