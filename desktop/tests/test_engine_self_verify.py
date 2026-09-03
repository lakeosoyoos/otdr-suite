"""The engine checks itself, and a damaged one says so in English.

#132 stopped the launcher booting into a cache that had LOST a file.  Three
holes were left open on purpose, and this file closes them:

  1. A file that is still there but is no longer what we shipped — truncated,
     half-restored out of quarantine, a bad sector — passed the presence check
     and failed at import exactly like a missing one.  The swap now records the
     manifest hashes in engine.meta.json and boot re-checks them.
  2. The bundled engine in the install directory was never checked at all.  It
     is the last rung of the ladder: if the same thing that ate a file out of
     the cache ate one out of Program Files, preferring bundled handed the tech
     a second unbootable engine, and no update can repair that one because we
     never fetch into the install.
  3. Nothing catches a file removed WHILE the tech is working.  The launcher
     verified at boot and the run had already started.  So the app now says
     what happened in words and offers one button, instead of a red traceback
     with a module name and links to Google and ChatGPT.

The repair button writes a marker; the launcher acts on it at the next boot,
where nothing holds the files open.  That contract is tested from both sides.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import types
from pathlib import Path

from conftest import REPO_ROOT

LAUNCHER = REPO_ROOT / "desktop" / "launcher.py"
APP_SRC = (REPO_ROOT / "app.py").read_text(encoding="utf-8")

LOST = "viewer/sor_reader324802a.py"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("otdr_launcher_sv", LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_app_helper(name, **namespace):
    """Exec one top-level function out of app.py in a bare module — no
    Streamlit, no network (the test_update_nudge.py pattern)."""
    fn = next(n for n in ast.parse(APP_SRC).body
              if isinstance(n, ast.FunctionDef) and n.name == name)
    mod = types.ModuleType("app_helper")
    mod.__dict__.update(namespace)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "app.py", "exec"),
         mod.__dict__)
    return getattr(mod, name)


def _engine(L, dirpath, marker="x"):
    """A complete engine, and the hashes that describe it."""
    hashes = {}
    for rel in L.ENGINE_FILES:
        f = dirpath / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        body = marker if rel == "app.py" else f"# {rel}"
        f.write_text(body, encoding="utf-8")
        hashes[rel] = hashlib.sha256(body.encode()).hexdigest()
    return dirpath, hashes


# ═════════════════════════════════════════════════════════════════════════
#  1. a file that is present but wrong
# ═════════════════════════════════════════════════════════════════════════

def test_an_altered_file_is_caught_when_hashes_are_known(tmp_path):
    L = _load_launcher()
    cache, hashes = _engine(L, tmp_path / "engine")
    assert L._engine_intact(cache, hashes) == ""

    (cache / LOST).write_text("# not what we shipped", encoding="utf-8")
    assert L._engine_intact(cache, hashes) == f"{LOST} altered"


def test_an_altered_file_still_passes_presence_alone(tmp_path):
    """Why the hashes had to be recorded: without them there is nothing to
    compare against, and a wrong file looks exactly like a right one."""
    L = _load_launcher()
    cache, _ = _engine(L, tmp_path / "engine")
    (cache / LOST).write_text("# not what we shipped", encoding="utf-8")
    assert L._engine_intact(cache) == ""


def test_an_engine_with_no_recorded_hashes_is_not_condemned(tmp_path, monkeypatch):
    """An engine whose meta predates this change, or a recovered survivor, must
    fall back to the presence check rather than failing every file."""
    L = _load_launcher()
    cache, _ = _engine(L, tmp_path / "engine")
    monkeypatch.setattr(L, "_cache_dir", lambda: cache)
    cache.with_name("engine.meta.json").write_text(
        json.dumps({"version": 313}), encoding="utf-8")
    assert L._cache_hashes() is None
    assert L._cached_version() == 313


def test_the_swap_records_the_hashes_it_verified(tmp_path, monkeypatch):
    """Boot can only re-check what the swap wrote down."""
    L = _load_launcher()
    cache = tmp_path / "engine"
    staged, hashes = _engine(L, cache.with_name("engine.staging"), "v400")

    def fake_update(staging):
        return {"version": 400, "__version_int": 400, "commit": "c0ffee",
                "files": hashes}

    monkeypatch.delenv("OTDR_SUITE_NO_UPDATE", raising=False)
    monkeypatch.setattr(L, "update_signing_configured", lambda: True)
    monkeypatch.setattr(L, "_cache_dir", lambda: cache)
    monkeypatch.setattr(L, "_try_auto_update", fake_update)
    monkeypatch.setattr(L, "bundled_dir", lambda: _engine(L, tmp_path / "bundled")[0])

    engine_dir, label = L._prepare_engine()

    assert engine_dir == cache and "v400" in label
    recorded = json.loads(cache.with_name("engine.meta.json").read_text(encoding="utf-8"))
    assert recorded["files"] == hashes
    assert L._cache_hashes() == hashes
    assert L._engine_intact(cache, L._cache_hashes()) == ""


def test_recovery_drops_the_meta_it_no_longer_describes(tmp_path, monkeypatch):
    """The trap this avoids: promoting engine.old and then checking it against
    the DISCARDED copy's hashes would condemn a perfectly good engine."""
    L = _load_launcher()
    cache, _ = _engine(L, tmp_path / "engine", "BROKEN")
    (cache / LOST).unlink()
    _, old_hashes = _engine(L, cache.with_name("engine.old"), "COMPLETE 308")
    monkeypatch.setattr(L, "_cache_dir", lambda: cache)
    cache.with_name("engine.meta.json").write_text(
        json.dumps({"version": 313, "files": {r: "0" * 64 for r in L.ENGINE_FILES}}),
        encoding="utf-8")

    L._recover_cache(cache)

    assert not cache.with_name("engine.meta.json").exists()
    assert L._cache_hashes() is None
    assert L._engine_intact(cache, L._cache_hashes()) == ""
    assert L._cached_version() == 0


# ═════════════════════════════════════════════════════════════════════════
#  2. the install directory is not above suspicion
# ═════════════════════════════════════════════════════════════════════════

def _prepare(L, tmp_path, monkeypatch, reasons):
    monkeypatch.delenv("OTDR_SUITE_NO_UPDATE", raising=False)
    monkeypatch.setattr(L, "update_signing_configured", lambda: True)
    monkeypatch.setattr(L, "_try_auto_update", lambda staging: None)
    monkeypatch.setattr(L, "_cache_dir", lambda: tmp_path / "engine")
    monkeypatch.setattr(L, "_report_update_stuck", lambda reason: reasons.append(reason))
    return L._prepare_engine()


def test_a_damaged_bundled_engine_does_not_beat_a_good_cache(tmp_path, monkeypatch):
    """Bundled 400 would normally win over cached 313.  Not when it cannot run:
    an update can repair the cache, and nothing can repair the install."""
    L = _load_launcher()
    _engine(L, tmp_path / "engine", "CACHED 313")
    (tmp_path / "engine").with_name("engine.meta.json").write_text(
        json.dumps({"version": 313}), encoding="utf-8")
    bundled, _ = _engine(L, tmp_path / "bundled", "BUNDLED 400")
    (bundled / LOST).unlink()
    monkeypatch.setattr(L, "bundled_dir", lambda: bundled)
    monkeypatch.setattr(L, "_bundled_build", lambda: 400)
    reasons = []

    engine_dir, _ = _prepare(L, tmp_path, monkeypatch, reasons)

    assert (engine_dir / "app.py").read_text(encoding="utf-8") == "CACHED 313"
    assert any("bundled engine damaged" in r for r in reasons), reasons


def test_an_intact_bundled_engine_still_wins_when_it_is_newer(tmp_path, monkeypatch):
    """The reinstall path must not regress: a fresh exe beats a stale cache."""
    L = _load_launcher()
    _engine(L, tmp_path / "engine", "CACHED 313")
    (tmp_path / "engine").with_name("engine.meta.json").write_text(
        json.dumps({"version": 313}), encoding="utf-8")
    monkeypatch.setattr(L, "bundled_dir", lambda: _engine(L, tmp_path / "bundled", "BUNDLED 400")[0])
    monkeypatch.setattr(L, "_bundled_build", lambda: 400)

    engine_dir, _ = _prepare(L, tmp_path, monkeypatch, [])
    assert (engine_dir / "app.py").read_text(encoding="utf-8") == "BUNDLED 400"


# ═════════════════════════════════════════════════════════════════════════
#  3. the repair button, from both ends
# ═════════════════════════════════════════════════════════════════════════

def test_the_app_and_the_launcher_agree_on_the_marker_path():
    """Two files, one path, no import between them.  If either side moves it,
    the button becomes a restart that changes nothing."""
    L = _load_launcher()
    import os
    app_path = _load_app_helper("_repair_marker_path", os=os)()
    assert Path(app_path) == L._repair_marker()
    assert L._repair_marker() == Path.home() / ".otdrSuite" / "repair_requested"


def test_a_requested_repair_discards_the_cache(tmp_path, monkeypatch):
    """The point of the button: the next boot must NOT decide the cache it just
    crashed on is the newest thing it has."""
    L = _load_launcher()
    cache, hashes = _engine(L, tmp_path / "engine", "THE BROKEN ONE")
    monkeypatch.setattr(L, "_cache_dir", lambda: cache)
    monkeypatch.setattr(L.Path, "home", staticmethod(lambda: tmp_path))
    cache.with_name("engine.meta.json").write_text(
        json.dumps({"version": 313, "files": hashes}), encoding="utf-8")
    marker = tmp_path / ".otdrSuite" / "repair_requested"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("repair", encoding="utf-8")

    assert L._honour_repair_request(cache) is True

    assert not cache.exists(), "the cache must be gone"
    assert not cache.with_name("engine.meta.json").exists()
    assert not marker.exists(), "a repair runs once, not every boot"
    assert L._cached_version() == 0, "nothing left to block the re-download"


def test_repair_keeps_the_survivors(tmp_path, monkeypatch):
    """A tech who clicks Repair with no signal still has to get a working app."""
    L = _load_launcher()
    cache, _ = _engine(L, tmp_path / "engine", "BROKEN")
    _engine(L, cache.with_name("engine.old"), "THE FALLBACK")
    monkeypatch.setattr(L, "_cache_dir", lambda: cache)
    monkeypatch.setattr(L.Path, "home", staticmethod(lambda: tmp_path))
    marker = tmp_path / ".otdrSuite" / "repair_requested"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("repair", encoding="utf-8")

    L._honour_repair_request(cache)
    L._recover_cache(cache)

    assert (cache / "app.py").read_text(encoding="utf-8") == "THE FALLBACK"


def test_no_marker_is_not_a_repair(tmp_path, monkeypatch):
    L = _load_launcher()
    cache, _ = _engine(L, tmp_path / "engine", "KEEP ME")
    monkeypatch.setattr(L, "_cache_dir", lambda: cache)
    monkeypatch.setattr(L.Path, "home", staticmethod(lambda: tmp_path))
    assert L._honour_repair_request(cache) is False
    assert (cache / "app.py").read_text(encoding="utf-8") == "KEEP ME"


def test_the_boot_import_is_guarded(tmp_path):
    """The traceback the tech photographed came from an unguarded import."""
    tree = ast.parse(APP_SRC)
    guarded = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        imports = [n for n in node.body
                   if isinstance(n, ast.Import)
                   and any(a.name == "trace_server" for a in n.names)]
        if imports and node.handlers:
            guarded = True
    assert guarded, "import trace_server must not be able to reach the tech raw"


def test_the_repair_page_says_what_to_do_without_jargon():
    """It is read by a tech on a phone in a truck, not by us."""
    fn = next(n for n in ast.parse(APP_SRC).body
              if isinstance(n, ast.FunctionDef)
              and n.name == "_engine_file_missing_page")
    # Only what is HANDED TO STREAMLIT — the docstring explains the bug to us
    # and names the module, which is exactly what the tech must not be shown.
    shown = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and getattr(node.func.value, "id", None) == "st"):
            shown += [a.value for a in node.args
                      if isinstance(a, ast.Constant) and isinstance(a.value, str)]
    text = " ".join(shown)
    assert "missing from this computer" in text
    assert "Repair and restart" in text
    for jargon in ("ModuleNotFoundError", "sys.path", "sor_reader", "traceback"):
        assert jargon not in text, f"{jargon} is not for a tech to read"
