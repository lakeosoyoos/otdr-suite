"""Auto-update manifest integrity guards (two silent fleet-killers).

Both bugs are INVISIBLE to the normal suite on macOS/Linux and to the CI boot
self-test (which runs with OTDR_SUITE_NO_UPDATE=1); they only bite the real
Windows fleet after a manifest is published.  These tests make them fail LOUD in
CI instead:

  1. CRLF poisoning — make_update_manifest hashes the working-tree bytes; the
     launcher verifies against the LF blobs raw.githubusercontent serves.  A
     CRLF checkout (Windows autocrlf) would mismatch every hash → auto-update
     silently dead.  `.gitattributes * -text` prevents it; this test proves no
     ENGINE_FILE contains CRLF as checked out (so it FAILS on a Windows runner
     if the .gitattributes protection is ever removed).

  2. Path-filter coverage — the build only runs when on.push.paths matches; a
     push touching an ENGINE_FILE outside those globs ships no Release and, once
     auto-update is live, strands the fleet on a stale manifest.  This asserts
     every ENGINE_FILE is covered by a build path.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from conftest import REPO_ROOT

LAUNCHER = REPO_ROOT / "desktop" / "launcher.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-windows.yml"


def _engine_files():
    spec = importlib.util.spec_from_file_location("otdr_launcher_efi", LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return list(mod.ENGINE_FILES)


def test_no_engine_file_is_checked_out_with_crlf():
    """The manifest hashes these bytes; the launcher fetches LF blobs.  Any CRLF
    here (a Windows autocrlf checkout without `.gitattributes * -text`) would
    poison every SHA-256 and silently kill auto-update fleet-wide."""
    offenders = []
    for rel in _engine_files():
        p = REPO_ROOT / rel
        if p.exists() and b"\r\n" in p.read_bytes():
            offenders.append(rel)
    assert not offenders, (
        "CRLF line endings in engine files would break auto-update hash "
        f"verification on the fleet: {offenders}. Ensure `.gitattributes` has "
        "`* -text` so the working tree matches the committed LF blobs."
    )


def test_gitattributes_pins_line_endings():
    """The one-line protection behind the CRLF guard must stay present."""
    ga = REPO_ROOT / ".gitattributes"
    assert ga.exists(), ".gitattributes missing — CRLF auto-update poisoning unguarded"
    assert "-text" in ga.read_text(encoding="utf-8"), (
        ".gitattributes must disable EOL conversion (`* -text`)"
    )


def _push_paths():
    # Dependency-free parse (PyYAML isn't a CI test dependency): collect the
    # `- "glob"` entries under the single `paths:` key in the workflow.
    paths, in_paths = [], False
    for ln in WORKFLOW.read_text(encoding="utf-8").splitlines():
        s = ln.strip()
        if s == "paths:":
            in_paths = True
            continue
        if in_paths:
            if s.startswith("- "):
                paths.append(s[2:].strip().strip('"').strip("'"))
            elif s and not s.startswith("#"):
                break   # first non-list, non-comment line ends the paths block
    return paths


def _covered(rel, globs):
    for g in globs:
        if g == rel:
            return True
        if g.endswith("/**") and (rel + "/").startswith(g[:-2]):  # "viewer/**" ⊇ "viewer/x.py"
            return True
    return False


def test_every_engine_file_is_covered_by_a_build_path():
    """A push touching an ENGINE_FILE outside on.push.paths builds nothing → no
    Release, and (auto-update live) a stale manifest the fleet then rejects."""
    globs = _push_paths()
    assert globs, "could not read on.push.paths from the workflow"
    uncovered = [rel for rel in _engine_files() if not _covered(rel, globs)]
    assert not uncovered, (
        f"these ENGINE_FILES are not covered by build-windows.yml on.push.paths "
        f"(a push touching only them would ship no build): {uncovered}. "
        f"Add their path/glob to the workflow. Current paths: {globs}"
    )
