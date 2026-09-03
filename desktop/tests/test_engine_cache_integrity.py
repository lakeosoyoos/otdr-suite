"""An engine cache that lost a file must not be booted into.

WHAT THE TECH SAW.  Build 313, on one machine only:

    ModuleNotFoundError: No module named 'sor_reader324802a'
      File "C:\\Users\\...\\.otdrSuite\\engine\\app.py", line 863
      File "C:\\Users\\...\\.otdrSuite\\engine\\viewer\\trace_server.py", line 40

Both frames are inside the auto-updated cache, and both files are in
ENGINE_FILES: viewer/trace_server.py was there, the viewer/sor_reader324802a.py
it imports was not.  313 ships both — the manifest lists 21 files and the hash
of viewer/sor_reader324802a.py matches the blob on main — and a second tech ran
the same build with no error.  So the file was verified at download and went
missing afterwards on that one box; an antivirus quarantine is the ordinary
cause of a freshly written .py disappearing from a user profile.

WHY THE APP COULD NOT SELF-HEAL.  Every check on an engine directory was
`(d / "app.py").exists()` — one file out of 21.  A half-eaten cache passed it,
so the launcher ran the cache and crashed on import, every boot.  And the cache
still recorded version 313, so anti-rollback ("never swap in an older-or-equal
version") refused to fetch 313 again.  The machine could not get out of it
without someone deleting ~/.otdrSuite by hand.

THE FIX, locked here: _engine_intact() checks the whole ENGINE_FILES set, a
broken cache reports version 0 so the same version re-downloads, recovery
treats incomplete as lost, and the fall-back ladder prefers a complete bundled
engine over a broken cache.  It also reports off-machine, because a machine
that eats our code will do it again.
"""
from __future__ import annotations

import ast
import importlib.util
import json

from conftest import REPO_ROOT

LAUNCHER = REPO_ROOT / "desktop" / "launcher.py"

# The file the tech's machine lost.
LOST = "viewer/sor_reader324802a.py"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("otdr_launcher_int", LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _engine(L, dirpath, marker="x"):
    for rel in L.ENGINE_FILES:
        f = dirpath / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(marker if rel == "app.py" else f"# {rel}", encoding="utf-8")
    return dirpath


# ═════════════════════════════════════════════════════════════════════════
#  1. _engine_intact — the check that did not exist
# ═════════════════════════════════════════════════════════════════════════

def test_a_complete_engine_is_intact(tmp_path):
    L = _load_launcher()
    assert L._engine_intact(_engine(L, tmp_path / "engine")) == ""


def test_the_techs_cache_is_reported_broken(tmp_path):
    """THE REGRESSION: app.py present, one import target gone."""
    L = _load_launcher()
    cache = _engine(L, tmp_path / "engine")
    (cache / LOST).unlink()
    assert (cache / "app.py").exists(), "the old check would still pass"
    assert L._engine_intact(cache) == f"{LOST} missing"


def test_a_zero_byte_file_counts_as_broken(tmp_path):
    """A truncated write is not a usable engine either."""
    L = _load_launcher()
    cache = _engine(L, tmp_path / "engine")
    (cache / LOST).write_text("", encoding="utf-8")
    assert L._engine_intact(cache) == f"{LOST} empty"


def test_a_missing_directory_is_broken(tmp_path):
    L = _load_launcher()
    assert L._engine_intact(tmp_path / "nothing-here")


# ═════════════════════════════════════════════════════════════════════════
#  2. anti-rollback must not pin a machine to a broken cache
# ═════════════════════════════════════════════════════════════════════════

def test_a_broken_cache_has_no_version(tmp_path, monkeypatch):
    """The trap: meta says 313, so `313 <= 313` refused to re-fetch 313 and the
    only file that could fix the crash was never downloaded again."""
    L = _load_launcher()
    cache = _engine(L, tmp_path / "engine")
    cache.with_name("engine.meta.json").write_text(
        json.dumps({"version": 313, "commit": "abc"}), encoding="utf-8")
    monkeypatch.setattr(L, "_cache_dir", lambda: cache)
    assert L._cached_version() == 313

    (cache / LOST).unlink()
    assert L._cached_version() == 0, "a broken cache must not claim a version"


# ═════════════════════════════════════════════════════════════════════════
#  3. recovery + the fallback ladder
# ═════════════════════════════════════════════════════════════════════════

def test_recovery_replaces_a_half_eaten_cache_with_a_complete_survivor(tmp_path):
    L = _load_launcher()
    cache = _engine(L, tmp_path / "engine", "BROKEN 313")
    (cache / LOST).unlink()
    _engine(L, cache.with_name("engine.old"), "COMPLETE 308")

    L._recover_cache(cache)

    assert L._engine_intact(cache) == ""
    assert (cache / "app.py").read_text(encoding="utf-8") == "COMPLETE 308"


def test_a_broken_survivor_is_not_promoted(tmp_path):
    """Do not swap one unbootable engine for another."""
    L = _load_launcher()
    cache = _engine(L, tmp_path / "engine", "BROKEN 313")
    (cache / LOST).unlink()
    old = _engine(L, cache.with_name("engine.old"), "ALSO BROKEN")
    (old / LOST).unlink()

    L._recover_cache(cache)

    assert (cache / "app.py").read_text(encoding="utf-8") == "BROKEN 313"


def _prepare(L, tmp_path, monkeypatch, reasons=None):
    """_prepare_engine with the network fetch failing (the tech is offline, or
    the manifest is the same 313 that is already cached)."""
    monkeypatch.delenv("OTDR_SUITE_NO_UPDATE", raising=False)
    monkeypatch.setattr(L, "update_signing_configured", lambda: True)
    monkeypatch.setattr(L, "_try_auto_update", lambda staging: None)
    monkeypatch.setattr(L, "_cache_dir", lambda: tmp_path / "engine")
    monkeypatch.setattr(L, "_bundled_build", lambda: 300)
    monkeypatch.setattr(L, "_report_update_stuck",
                        lambda reason: (reasons if reasons is not None else []).append(reason))
    return L._prepare_engine()


def test_a_complete_bundled_engine_beats_a_broken_cache(tmp_path, monkeypatch):
    """Even though the cache is NEWER (313 > bundled 300), it cannot run."""
    L = _load_launcher()
    cache = _engine(L, tmp_path / "engine", "BROKEN 313")
    (cache / LOST).unlink()
    cache.with_name("engine.meta.json").write_text(
        json.dumps({"version": 313}), encoding="utf-8")
    monkeypatch.setattr(L, "bundled_dir",
                        lambda: _engine(L, tmp_path / "bundled", "BUNDLED 300"))

    engine_dir, label = _prepare(L, tmp_path, monkeypatch)

    assert (engine_dir / "app.py").read_text(encoding="utf-8") == "BUNDLED 300"
    assert L._engine_intact(engine_dir) == ""


def test_an_intact_cache_still_wins_over_an_older_exe(tmp_path, monkeypatch):
    """The other direction must not regress: a good cache keeps running."""
    L = _load_launcher()
    _engine(L, tmp_path / "engine", "CACHED 313")
    cache = tmp_path / "engine"
    cache.with_name("engine.meta.json").write_text(
        json.dumps({"version": 313}), encoding="utf-8")
    monkeypatch.setattr(L, "bundled_dir",
                        lambda: _engine(L, tmp_path / "bundled", "BUNDLED 300"))
    monkeypatch.setattr(L, "_bundled_build", lambda: 139)

    engine_dir, _ = _prepare(L, tmp_path, monkeypatch)
    assert (engine_dir / "app.py").read_text(encoding="utf-8") == "CACHED 313"


def test_a_broken_cache_is_reported_off_machine(tmp_path, monkeypatch):
    """A machine that deletes our code will do it again — say so in Slack, with
    the file named, instead of only in a log nobody can read off a windowed exe."""
    L = _load_launcher()
    cache = _engine(L, tmp_path / "engine")
    (cache / LOST).unlink()
    monkeypatch.setattr(L, "bundled_dir", lambda: _engine(L, tmp_path / "bundled"))
    reasons = []

    _prepare(L, tmp_path, monkeypatch, reasons)

    assert any(LOST in r for r in reasons), reasons


# ═════════════════════════════════════════════════════════════════════════
#  4. the import that crashed must stay covered by ENGINE_FILES
# ═════════════════════════════════════════════════════════════════════════

def test_every_module_the_viewer_imports_is_shipped():
    """trace_server.py imports its siblings by bare name off sys.path.  If one
    of them ever leaves ENGINE_FILES, the update ships a viewer that cannot
    import — the same crash, but for the whole fleet instead of one box."""
    L = _load_launcher()
    src = (REPO_ROOT / "viewer" / "trace_server.py").read_text(encoding="utf-8")
    siblings = {p.stem for p in (REPO_ROOT / "viewer").glob("*.py")}
    imported = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)

    for mod in sorted(imported & siblings):
        assert f"viewer/{mod}.py" in L.ENGINE_FILES, (
            f"viewer/{mod}.py is imported by trace_server.py but is not in "
            "ENGINE_FILES — an update would ship a viewer that cannot import")
