"""Auto-update guard tests for the launcher (no network).

The launcher fetches every engine/UI file from GitHub at boot and runs the
validated copy.  These assert the contract WITHOUT hitting the network:
  • ENGINE_FILES lists exactly the tracked engine files (so a new engine file
    can't be silently left out of updates),
  • the all-or-nothing validator accepts good code and rejects broken/empty,
  • the update points at the correct repo.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

from conftest import REPO_ROOT

LAUNCHER = REPO_ROOT / "desktop" / "launcher.py"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("otdr_launcher", LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_engine_files_cover_all_tracked_engine_files():
    """ENGINE_FILES must list every tracked .py/.html outside desktop/ — else
    an update would ship a partial app (old file + new file mix)."""
    L = _load_launcher()
    tracked = subprocess.run(["git", "ls-files"], cwd=str(REPO_ROOT),
                             capture_output=True, text=True).stdout.split()
    engine = {f for f in tracked
              if f.endswith((".py", ".html")) and not f.startswith("desktop/")}
    listed = set(L.ENGINE_FILES)
    assert engine == listed, (
        f"ENGINE_FILES out of sync — missing {engine - listed}, extra {listed - engine}"
    )


def test_validator_all_or_nothing():
    L = _load_launcher()
    assert L._validate(b"def f():\n    return 1\n", "a.py") is True
    assert L._validate(b"def f(:\n", "a.py") is False       # syntax error
    assert L._validate(b"", "a.py") is False                # empty
    assert L._validate(b"x = 1\n", "a.py") is False         # no 'def ' marker
    assert L._validate(b"<html>x</html>", "v.html") is True
    assert L._validate(b"", "v.html") is False


def test_update_targets_this_repo():
    L = _load_launcher()
    assert L.GH_OWNER == "lakeosoyoos" and L.GH_REPO == "otdr-suite"
    assert "raw.githubusercontent.com" in L.RAW_URL_FMT


def test_no_update_flag_pins_bundled(monkeypatch):
    """OTDR_SUITE_NO_UPDATE skips the network fetch and runs the bundled build
    (air-gapped/offline pinning)."""
    L = _load_launcher()
    monkeypatch.setenv("OTDR_SUITE_NO_UPDATE", "1")
    engine_dir, label = L._prepare_engine()
    assert engine_dir == L.bundled_dir()
    assert "disabled" in label.lower()
