"""A newer exe must replace an older copy still running in the background.

Closing the browser tab leaves the server running, so a freshly downloaded exe
used to find the OLD server on the port and just open a tab to it.  Techs had
to delete the old exe (killing it) before a new download would work.
"""
from __future__ import annotations

import importlib.util

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
