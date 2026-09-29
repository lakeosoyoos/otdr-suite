"""The suite gives the same answer in any file order.

The three engines ship different sor_reader324802a.py copies under one module
name (and two of them a json_reader.py), and Python caches a module by name.
Whichever copy a test process imports first answers every later import of
that name.  Only the Splice Report engine's copy has the names the engine
asks for, so an engine test collected after a file that had cached another
copy failed on

    ImportError: cannot import name 'measure_fr_exact_loss'
                 from 'sor_reader324802a' (.../viewer/sor_reader324802a.py)

CI never saw it because files are collected alphabetically and an engine
test happens to sort first.  A new file that sorts ahead of it, a shuffled
run or a hand-picked list of files turned the build red for a reason that
had nothing to do with the change under test.

conftest.py now loads the engine's copies before any test module is
imported.  These tests run the orders that used to fail, in a pytest of
their own, and check the cached copies from inside this one.

The viewer is the other half.  trace_server asks for the readers by the
same two names, so in the alphabetical run it was handed the engine's
copies and the viewer tests never ran on the reader the viewer ships with.
conftest.py imports it on the viewer's own copies, and the same runs check
that it stays on them in either order.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from conftest import HERE, SPLICEREPORT_DIR, VIEWER_DIR

DESKTOP_DIR = HERE.parent

# The module names more than one engine ships a copy of.
SHARED_READER_NAMES = ("sor_reader324802a", "json_reader")

# An engine test that imports the engine by name at module level.
ENGINE_TEST = "tests/test_dead_direction.py"

# Files that cache another copy of the reader when they are imported: the
# viewer's (through trace_server) and Secret Sauce's (through report_sor).
OTHER_READER_FIRST = [
    "tests/test_viewer_drop_target.py",
    "tests/test_event_table_fallback.py",
]


# Run at the end of each order: what the engine and the viewer are bound to.
BOUND = [
    "tests/test_reader_import_order.py::test_the_engine_is_bound_to_its_own_reader",
    "tests/test_reader_import_order.py::test_the_viewer_is_bound_to_its_own_readers",
]


def _pytest(*args):
    """A pytest run of its own, from desktop/ as CI does."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=str(DESKTOP_DIR), env=env, capture_output=True,
        text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _tail(out, n=40):
    return "\n".join(out.splitlines()[-n:])


# ── the two orders ────────────────────────────────────────────────────────

@pytest.mark.parametrize("other", OTHER_READER_FIRST)
def test_an_engine_test_passes_after_a_file_that_cached_another_reader(other):
    """The order that failed at collection."""
    code, out = _pytest(other, ENGINE_TEST, *BOUND)
    assert code == 0, _tail(out)
    assert "ImportError" not in out, _tail(out)


@pytest.mark.parametrize("other", OTHER_READER_FIRST)
def test_an_engine_test_passes_before_it(other):
    """The order CI has always run."""
    code, out = _pytest(ENGINE_TEST, other, *BOUND)
    assert code == 0, _tail(out)


def test_every_test_file_is_importable_in_reverse_order():
    """Every file, not the one pair: the whole suite is collected last file
    first, so each file is imported after the ones that follow it in CI."""
    files = sorted(("tests/" + p.name for p in HERE.glob("test_*.py")),
                   reverse=True)
    # Windows caps a command line at 32,767 characters.
    assert sum(len(f) + 1 for f in files) < 30000, (
        "too many file names for one command line: hand pytest the list in "
        "an @file instead")
    code, out = _pytest("--collect-only", *files)
    assert code == 0, _tail(out)


# ── the cached copies, from inside this process ───────────────────────────

def _is_the_engines(mod, name):
    return Path(mod.__file__).resolve() == (SPLICEREPORT_DIR / (name + ".py")).resolve()


def _file_of(func):
    return Path(func.__code__.co_filename).resolve()


def test_a_reader_left_swapped_by_a_test():
    """Leaves a stand-in behind on purpose, for the next test to find gone."""
    for name in SHARED_READER_NAMES:
        sys.modules[name] = types.ModuleType(name)


@pytest.mark.parametrize("name", SHARED_READER_NAMES)
def test_the_reader_imported_by_name_is_the_engines(name):
    assert _is_the_engines(importlib.import_module(name), name)


def test_the_engine_is_bound_to_its_own_reader():
    sys.path.insert(0, str(SPLICEREPORT_DIR))
    try:
        engine = importlib.import_module("splicereportmatchexfo")
    finally:
        sys.path.remove(str(SPLICEREPORT_DIR))
    assert _file_of(engine.measure_fr_exact_loss) == (
        SPLICEREPORT_DIR / "sor_reader324802a.py").resolve()


def test_the_viewer_is_bound_to_its_own_readers():
    """Imported by name, as the viewer tests and the hub do."""
    viewer = importlib.import_module("trace_server")
    sor = (VIEWER_DIR / "sor_reader324802a.py").resolve()
    assert Path(viewer.__file__).resolve() == (VIEWER_DIR / "trace_server.py").resolve()
    assert _file_of(viewer.parse_sor_full) == sor
    assert _file_of(viewer.parse_genparams) == sor
    assert _file_of(viewer.parse_otdr_json) == (VIEWER_DIR / "json_reader.py").resolve()


def test_every_way_in_is_handed_the_same_viewer():
    from conftest import import_trace_server
    assert import_trace_server() is importlib.import_module("trace_server")


def test_a_viewer_left_swapped_by_a_test():
    """Leaves a stand-in behind on purpose, for the next test to find gone."""
    sys.modules["trace_server"] = types.ModuleType("trace_server")


def test_the_viewer_is_put_back():
    test_the_viewer_is_bound_to_its_own_readers()
