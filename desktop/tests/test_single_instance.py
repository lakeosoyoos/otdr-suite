"""Only one instance may boot at a time.

THE BUG.  A tech launched the app two or three times within seconds, every
time (his log: 08:21:23, :28, :30, and again five minutes later).  Nothing
stopped the second one.  `_health_ok()` is asked BEFORE the update runs, and
the update fetches 21 files at up to 15 s each, so the first instance does not
claim the port for 10-30 s; every launch inside that window sails past the
guard and starts its own `_prepare_engine`.  Two of those rename the same
directory at once and the tech boots into "No module named 'trace_server'"
out of a cache that every later boot reports as perfectly intact.  It was read
as antivirus eating our files for three days.

These tests drive the real lock through real processes, because a lock that
only holds inside one interpreter would have passed a mock and shipped the
bug.  POSIX flock is per-process; a second `flock` from the SAME process
succeeds.  Only a second PROCESS proves anything.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from conftest import REPO_ROOT
from test_engine_self_verify import _load_launcher

LAUNCHER = REPO_ROOT / "desktop" / "launcher.py"

CHILD = textwrap.dedent("""
    import importlib.util, sys, time
    spec = importlib.util.spec_from_file_location("L", sys.argv[1])
    L = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(L)
    got = L._take_boot_lock()
    print("GOT" if got is not None else "BLOCKED", flush=True)
    time.sleep(float(sys.argv[2]))
""")


def _child(tmp_path, hold_s, script_path):
    """A separate PROCESS that takes the boot lock and holds it."""
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)              # Path.home() on POSIX
    env["USERPROFILE"] = str(tmp_path)       # ...and on Windows
    drive, tail = os.path.splitdrive(str(tmp_path))
    env["HOMEDRIVE"], env["HOMEPATH"] = drive, tail
    return subprocess.Popen(
        [sys.executable, str(script_path), str(LAUNCHER), str(hold_s)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)


def _first_line(proc, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        line = proc.stdout.readline()
        if line:
            return line.strip()
    raise AssertionError("child printed nothing")


@pytest.fixture
def child_script(tmp_path):
    p = tmp_path / "child.py"
    p.write_text(CHILD, encoding="utf-8")
    return p


def test_a_second_process_cannot_take_the_boot_lock(tmp_path, child_script):
    """THE REGRESSION.  Two launches seconds apart must not both boot."""
    a = _child(tmp_path, 8, child_script)
    try:
        assert _first_line(a) == "GOT", "the first instance must get the lock"

        b = _child(tmp_path, 0, child_script)
        try:
            assert _first_line(b) == "BLOCKED", "two instances booted at once"
        finally:
            b.kill(), b.wait()
    finally:
        a.kill(), a.wait()


def test_the_lock_dies_with_the_process_that_held_it(tmp_path, child_script):
    """No stale-lock class of bug: the OS releases it, so a crashed or killed
    instance can never wedge the app shut."""
    a = _child(tmp_path, 30, child_script)
    assert _first_line(a) == "GOT"
    a.kill()
    a.wait()

    b = _child(tmp_path, 0, child_script)
    try:
        assert _first_line(b) == "GOT", "a dead holder still blocks the app"
    finally:
        b.kill(), b.wait()


def test_the_lock_file_lives_beside_the_shared_cache(tmp_path, monkeypatch):
    """The tech had TWO installs (one on his OneDrive Desktop) sharing one
    ~/.otdrSuite.  They serialise only if the lock lives there too."""
    L = _load_launcher()
    monkeypatch.setattr(L.Path, "home", staticmethod(lambda: tmp_path))
    assert L._lock_path() == tmp_path / ".otdrSuite" / "boot.lock"


def test_a_machine_we_cannot_lock_still_boots(tmp_path, monkeypatch):
    """A lock we cannot take must never be the reason a tech cannot work."""
    L = _load_launcher()
    monkeypatch.setattr(L.Path, "home", staticmethod(lambda: tmp_path / "nope"))
    monkeypatch.setattr(L, "open", lambda *a, **k: (_ for _ in ()).throw(
        OSError("read-only")), raising=False)

    def _no_mkdir(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(L.Path, "mkdir", _no_mkdir)
    assert L._take_boot_lock() is not None, "an unlockable machine must boot"


# ─── waiting for the other instance ──────────────────────────────────────

def test_waiting_ends_when_the_other_instance_serves(monkeypatch):
    L = _load_launcher()
    calls = []
    monkeypatch.setattr(L, "_health_ok", lambda: calls.append(1) or len(calls) > 2)
    monkeypatch.setattr(L, "_take_boot_lock", lambda: None)
    monkeypatch.setattr(L.time, "sleep", lambda s: None)
    assert L._wait_for_the_other_boot(deadline_s=5) is True


def test_waiting_ends_when_the_other_instance_dies(monkeypatch):
    """It crashed mid-update.  We take the lock it dropped and boot."""
    L = _load_launcher()
    monkeypatch.setattr(L, "_health_ok", lambda: False)
    seen = []
    monkeypatch.setattr(L, "_take_boot_lock",
                        lambda: seen.append(1) or (object() if len(seen) > 1 else None))
    monkeypatch.setattr(L.time, "sleep", lambda s: None)
    assert L._wait_for_the_other_boot(deadline_s=5) is False


def test_waiting_gives_up_rather_than_hanging(monkeypatch):
    """A tech must never be left staring at nothing for ever."""
    L = _load_launcher()
    monkeypatch.setattr(L, "_health_ok", lambda: False)
    monkeypatch.setattr(L, "_take_boot_lock", lambda: None)
    monkeypatch.setattr(L.time, "sleep", lambda s: None)
    assert L._wait_for_the_other_boot(deadline_s=0.2) is False


# ─── where the guard sits in main() ──────────────────────────────────────

def _main_source():
    src = LAUNCHER.read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "main")
    return ast.get_source_segment(src, fn)


def test_the_lock_is_taken_before_the_health_check():
    """The window this closes is exactly the one the health check cannot see:
    the port is not bound yet.  Taking the lock afterwards would fix nothing."""
    src = _main_source()
    assert src.index("_take_boot_lock") < src.index("_health_ok"), \
        "the boot lock must be taken before the health check"


def test_the_lock_is_taken_before_the_update_runs():
    src = _main_source()
    assert src.index("_take_boot_lock") < src.index("_prepare_engine"), \
        "the update is the critical section; lock first"


def test_an_engine_subprocess_never_takes_the_lock():
    """Engine subprocesses run under the same exe and would deadlock the app
    against itself."""
    src = _main_source()
    assert src.index("_maybe_run_engine") < src.index("_take_boot_lock")


def test_a_blocked_launch_still_opens_a_tab():
    """The tech clicked the icon: something must appear, or he clicks again —
    which is how this started."""
    src = _main_source()
    after = src[src.index("_take_boot_lock"):]
    assert "webbrowser.open(APP_URL)" in after
