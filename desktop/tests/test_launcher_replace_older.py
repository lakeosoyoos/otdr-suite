"""A newer exe must replace an older copy still running in the background.

Closing the browser tab leaves the server running, so a freshly downloaded exe
used to find the OLD server on the port and just open a tab to it.  Techs had
to delete the old exe (killing it) before a new download would work.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap
import time

import pytest

from conftest import REPO_ROOT

spec = importlib.util.spec_from_file_location(
    "launcher_replace", REPO_ROOT / "desktop" / "launcher.py")
L = importlib.util.module_from_spec(spec)
spec.loader.exec_module(L)


def _never(_port):
    raise AssertionError("must not look up the port when a record exists")


def test_older_running_build_is_replaced():
    assert L._old_server_to_replace(710, {"pid": 4242, "version": 700}, _never) == 4242


def test_same_or_newer_running_build_is_kept():
    assert L._old_server_to_replace(710, {"pid": 4242, "version": 710}, _never) is None
    assert L._old_server_to_replace(700, {"pid": 4242, "version": 710}, _never) is None


def test_pre_change_build_with_no_record_is_found_by_port():
    assert L._old_server_to_replace(710, {}, lambda port: 5151) == 5151
    assert L._old_server_to_replace(710, {}, lambda port: None) is None


def test_dev_run_never_kills_anything():
    assert L._old_server_to_replace(0, {"pid": 4242, "version": 1}, _never) is None


def test_running_record_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(L.Path, "home", lambda: tmp_path)
    L._write_running(706)
    got = L._read_running()
    assert got["version"] == 706 and got["pid"] == L.os.getpid()


# ── replacing it stops OUR processes, never the tech's browser ───────────
# `taskkill /T` ends the whole tree.  A browser the server opens when none is
# running starts as the server's child, so on a Windows 11 VM (2026-09-27)
# /T on the server ended Edge and every tab in it.
TABLE = {
    100: (1, "otdrsuite.exe"),        # the old server
    101: (100, "otdrsuite.exe"),      # a Splice Report engine subprocess
    102: (101, "otdrsuite.exe"),      #   ...and one it started
    103: (100, "msedge.exe"),         # Edge, opened by the server at boot
    104: (103, "msedge.exe"),         #   ...and its renderers
    105: (100, "conhost.exe"),
    106: (100, "otdrsuite.exe"),      # an Update & restart copy it started
    200: (1, "otdrsuite.exe"),        # the new download (another tree)
}


def test_own_process_tree_is_our_program_only():
    got = L._own_process_tree(100, TABLE)
    assert set(got) == {100, 101, 102, 106}
    assert got[-1] == 100, "children first, the server last"
    assert got.index(102) < got.index(101)


def test_own_process_tree_survives_a_parent_loop():
    table = {1: (2, "otdrsuite.exe"), 2: (1, "otdrsuite.exe")}   # pid reuse
    assert set(L._own_process_tree(1, table)) == {1, 2}


def _stop_on_windows(monkeypatch, table, me):
    """Run _stop_pid(100) as Windows would; return the taskkill argvs."""
    import subprocess
    ran = []
    monkeypatch.setattr(L.os, "name", "nt")
    monkeypatch.setattr(L, "_process_table", table)
    monkeypatch.setattr(L.os, "getpid", lambda: me)
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: ran.append(args))
    L._stop_pid(100)
    return ran


def test_stop_never_uses_a_tree_kill(monkeypatch):
    ran = _stop_on_windows(monkeypatch, lambda: TABLE, me=200)
    assert all("/T" not in a for a in ran)
    stopped = [int(a[a.index("/PID") + 1]) for a in ran]
    assert set(stopped) == {100, 101, 102, 106}, "not Edge, not the new download"
    assert stopped[-1] == 100


def test_stop_skips_the_process_doing_the_stopping(monkeypatch):
    ran = _stop_on_windows(monkeypatch, lambda: TABLE, me=106)   # the restart copy
    stopped = [int(a[a.index("/PID") + 1]) for a in ran]
    assert set(stopped) == {100, 101, 102}


def test_stop_falls_back_to_the_server_alone(monkeypatch):
    def broken():
        raise OSError("no snapshot")
    ran = _stop_on_windows(monkeypatch, broken, me=200)
    assert ran == [["taskkill", "/PID", "100", "/F"]]


# The tests above fake the process table and taskkill.  These two run the
# real ones, so Windows CI proves the Toolhelp32 read and the per-pid stop.
@pytest.mark.skipif(os.name != "nt", reason="Toolhelp32 is Windows only")
def test_process_table_sees_this_process():
    parent, name = L._process_table()[os.getpid()]
    assert parent == os.getppid()
    assert name == os.path.basename(sys.executable).lower()


@pytest.mark.skipif(os.name != "nt", reason="real taskkill, Windows only")
def test_stop_ends_our_children_and_spares_a_foreign_one():
    # A stand-in server: one more copy of this program (an engine run) and a
    # program that is not us (ping, in place of the browser).
    server = subprocess.Popen([sys.executable, "-c", textwrap.dedent("""
        import subprocess, sys, time
        engine = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        other = subprocess.Popen(["ping", "-n", "120", "127.0.0.1"],
                                 stdout=subprocess.DEVNULL)
        print(engine.pid, other.pid, flush=True)
        time.sleep(120)
    """)], stdout=subprocess.PIPE, text=True)

    # Popen holds the server's handle, so its pid cannot be reused while we
    # look for its children by parent pid.
    def children():
        return {p: name for p, (parent, name) in L._process_table().items()
                if parent == server.pid}
    try:
        engine_pid, other_pid = map(int, server.stdout.readline().split())
        assert children().get(engine_pid) == os.path.basename(sys.executable).lower()
        L._stop_pid(server.pid)
        server.wait(timeout=15)
        deadline = time.time() + 10
        while engine_pid in children() and time.time() < deadline:
            time.sleep(0.2)
        left = children()
        assert engine_pid not in left, "the engine subprocess was left running"
        assert left.get(other_pid) == "ping.exe", "the foreign program was stopped"
    finally:
        for p in children():
            subprocess.run(["taskkill", "/PID", str(p), "/F"], capture_output=True)
        server.kill()
        server.wait()
        server.stdout.close()
