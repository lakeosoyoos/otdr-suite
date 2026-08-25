"""A failed swap must not drop a tech to whatever their installer bundled.

WHAT WENT WRONG.  The verified-update swap is:

    cache -> engine.old        (rename)
    staging -> cache           (rename)
    engine.old -> engine.prev  (rename, "rollback reference")

On Windows a directory rename fails with PermissionError while ANY file inside
is held open — antivirus scanning 21 files that were just downloaded is the
ordinary case — so the second rename can fail with the first already done,
leaving NO cache.  The restore was best effort and swallowed by `except: pass`,
and the fallback ladder then checked only `cache/app.py` before going straight
to bundled.  engine.prev, written on every successful swap and described in the
code as a rollback reference, was never read by anything.

A tech hit exactly this: engine "update 226 applied" became "bundled",
dropping 87 engines in one click, while a verified copy sat in engine.old.
Clicking Update again opened the next swap with `rmtree(old)` and destroyed
that copy too — "I went to do it again and that's all it will show and allow
me to do."

THE FIX.  _recover_cache() runs BEFORE the swap's rmtree can eat a survivor,
and again on the failure path; the ladder is cache -> .old -> .prev -> bundled;
and falling all the way to bundled now reports to Slack instead of print()ing
into a log nobody can read off a windowed exe.

Also pinned here: engine files are fetched at the manifest's OWN commit, not at
the branch tip, so a merge landing mid-update cannot invalidate the hashes.
"""
from __future__ import annotations

import importlib.util
import json

import pytest

from conftest import REPO_ROOT

LAUNCHER = REPO_ROOT / "desktop" / "launcher.py"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("otdr_launcher_rec", LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _engine(dirpath, marker="x"):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "app.py").write_text(marker, encoding="utf-8")
    return dirpath


# ─── _recover_cache ──────────────────────────────────────────────────────

def test_recovers_the_cache_from_old(tmp_path):
    L = _load_launcher()
    cache = tmp_path / "engine"
    _engine(cache.with_name("engine.old"), "the 226 engine")
    L._recover_cache(cache)
    assert (cache / "app.py").read_text(encoding="utf-8") == "the 226 engine"


def test_recovers_the_cache_from_prev_when_old_is_gone(tmp_path):
    L = _load_launcher()
    cache = tmp_path / "engine"
    _engine(cache.with_name("engine.prev"), "the rollback copy")
    L._recover_cache(cache)
    assert (cache / "app.py").read_text(encoding="utf-8") == "the rollback copy"


def test_a_healthy_cache_is_left_alone(tmp_path):
    L = _load_launcher()
    cache = _engine(tmp_path / "engine", "current")
    _engine(cache.with_name("engine.old"), "stale")
    assert L._recover_cache(cache) == ""
    assert (cache / "app.py").read_text(encoding="utf-8") == "current"


def test_recovery_prefers_old_over_prev(tmp_path):
    """.old is the copy displaced by the run that just failed; .prev is older."""
    L = _load_launcher()
    cache = tmp_path / "engine"
    _engine(cache.with_name("engine.old"), "newer")
    _engine(cache.with_name("engine.prev"), "older")
    L._recover_cache(cache)
    assert (cache / "app.py").read_text(encoding="utf-8") == "newer"


# ─── the fallback ladder ─────────────────────────────────────────────────

def _prepare_with_failed_update(L, tmp_path, monkeypatch):
    """Drive _prepare_engine with the update failing and no cache in place."""
    monkeypatch.delenv("OTDR_SUITE_NO_UPDATE", raising=False)
    monkeypatch.setattr(L, "update_signing_configured", lambda: True)
    monkeypatch.setattr(L, "_try_auto_update", lambda staging: None)
    monkeypatch.setattr(L, "_cache_dir", lambda: tmp_path / "engine")
    monkeypatch.setattr(L, "bundled_dir", lambda: _engine(tmp_path / "bundled", "BUNDLED"))
    monkeypatch.setattr(L, "_report_update_stuck", lambda reason: None)
    # old exe, newer cache lineage — the state the stuck tech is in
    monkeypatch.setattr(L, "_bundled_build", lambda: 139)
    monkeypatch.setattr(L, "_cached_version", lambda: 226)
    return L._prepare_engine()


def test_a_failed_update_uses_the_survivor_not_bundled(tmp_path, monkeypatch):
    """THE REGRESSION: this is the 87-engine drop."""
    L = _load_launcher()
    _engine((tmp_path / "engine").with_name("engine.old"), "the 226 engine")
    engine_dir, label = _prepare_with_failed_update(L, tmp_path, monkeypatch)
    assert (engine_dir / "app.py").read_text(encoding="utf-8") == "the 226 engine"
    assert "bundled" not in label


def test_bundled_is_still_the_last_resort(tmp_path, monkeypatch):
    """With nothing verified anywhere, bundled is correct — and must say so."""
    L = _load_launcher()
    engine_dir, label = _prepare_with_failed_update(L, tmp_path, monkeypatch)
    assert (engine_dir / "app.py").read_text(encoding="utf-8") == "BUNDLED"
    assert "bundled" in label


def test_falling_back_to_bundled_reports(tmp_path, monkeypatch):
    """The silence is the bug: a stuck machine must leave a trace off-machine."""
    L = _load_launcher()
    seen = []
    monkeypatch.delenv("OTDR_SUITE_NO_UPDATE", raising=False)
    monkeypatch.setattr(L, "update_signing_configured", lambda: True)
    monkeypatch.setattr(L, "_try_auto_update", lambda staging: None)
    monkeypatch.setattr(L, "_cache_dir", lambda: tmp_path / "engine")
    monkeypatch.setattr(L, "bundled_dir", lambda: _engine(tmp_path / "bundled", "B"))
    monkeypatch.setattr(L, "_report_update_stuck", lambda reason: seen.append(reason))
    L._prepare_engine()
    assert seen, "falling back to bundled reported nothing"


def test_stuck_report_dedupes_on_reason(tmp_path, monkeypatch):
    L = _load_launcher()
    posts = []
    monkeypatch.setattr(L, "_post_slack", lambda text: posts.append(text))
    monkeypatch.setattr(L.Path, "home", staticmethod(lambda: tmp_path))
    L._report_update_stuck("same reason")
    L._report_update_stuck("same reason")
    assert len(posts) == 1
    L._report_update_stuck("a different reason")
    assert len(posts) == 2


# ─── commit pinning ──────────────────────────────────────────────────────

def test_engine_files_are_fetched_at_the_manifest_commit(monkeypatch):
    """The merge-vs-manifest race: main moves during the ~11 min build, so the
    branch tip stops matching the hashes we are checking against."""
    L = _load_launcher()
    sha = "a" * 40
    manifest = {"version": 999, "commit": sha,
                "files": {rel: "0" * 64 for rel in L.ENGINE_FILES}}
    body = json.dumps(manifest).encode()
    urls = []

    def fake_fetch(url, timeout=15):
        urls.append(url)
        if url.endswith("update_manifest.json"):
            return body
        if url.endswith(".sig"):
            return b"sig"
        return b"content"

    monkeypatch.setattr(L, "_fetch", fake_fetch)
    monkeypatch.setattr(L, "_verify_manifest_signature", lambda m, s: True)
    L._try_auto_update(L.Path("/tmp/nonexistent-staging-xyz"))

    engine_urls = [u for u in urls if u.endswith("app.py")]
    assert engine_urls, "no engine file was fetched"
    assert all(sha in u for u in engine_urls), engine_urls
    assert not any(f"/{L.GH_BRANCH}/app.py" in u for u in engine_urls)


def test_a_manifest_without_a_commit_still_works(monkeypatch):
    """Backwards compatible: an older manifest has no commit field."""
    L = _load_launcher()
    manifest = {"version": 999, "files": {rel: "0" * 64 for rel in L.ENGINE_FILES}}
    body = json.dumps(manifest).encode()
    urls = []

    def fake_fetch(url, timeout=15):
        urls.append(url)
        if url.endswith("update_manifest.json"):
            return body
        if url.endswith(".sig"):
            return b"sig"
        return b"content"

    monkeypatch.setattr(L, "_fetch", fake_fetch)
    monkeypatch.setattr(L, "_verify_manifest_signature", lambda m, s: True)
    L._try_auto_update(L.Path("/tmp/nonexistent-staging-xyz2"))
    assert any(f"/{L.GH_BRANCH}/app.py" in u for u in urls)


# ─── a reinstall must actually move the tech forward ─────────────────────

def test_a_fresh_installer_beats_a_stale_cache(tmp_path, monkeypatch):
    """~/.otdrSuite survives an uninstall, so a rescued machine still has its
    old cache.  Handing a stuck tech a new exe is pointless if the launcher
    then prefers the very engine they reinstalled to escape."""
    L = _load_launcher()
    monkeypatch.delenv("OTDR_SUITE_NO_UPDATE", raising=False)
    monkeypatch.setattr(L, "update_signing_configured", lambda: True)
    monkeypatch.setattr(L, "_try_auto_update", lambda staging: None)
    monkeypatch.setattr(L, "_cache_dir", lambda: _engine(tmp_path / "engine", "OLD 226"))
    monkeypatch.setattr(L, "bundled_dir", lambda: _engine(tmp_path / "bundled", "NEW 266"))
    monkeypatch.setattr(L, "_report_update_stuck", lambda reason: None)
    monkeypatch.setattr(L, "_bundled_build", lambda: 266)     # freshly installed
    monkeypatch.setattr(L, "_cached_version", lambda: 226)    # survived the reinstall

    engine_dir, label = L._prepare_engine()
    assert (engine_dir / "app.py").read_text(encoding="utf-8") == "NEW 266"
    assert "266" not in label or "newer" in label


def test_a_newer_cache_still_beats_an_old_exe(tmp_path, monkeypatch):
    """The other direction must not regress: an old exe whose cache has ridden
    auto-update forward keeps running the cache."""
    L = _load_launcher()
    monkeypatch.delenv("OTDR_SUITE_NO_UPDATE", raising=False)
    monkeypatch.setattr(L, "update_signing_configured", lambda: True)
    monkeypatch.setattr(L, "_try_auto_update", lambda staging: None)
    monkeypatch.setattr(L, "_cache_dir", lambda: _engine(tmp_path / "engine", "CACHED 226"))
    monkeypatch.setattr(L, "bundled_dir", lambda: _engine(tmp_path / "bundled", "BUNDLED 139"))
    monkeypatch.setattr(L, "_report_update_stuck", lambda reason: None)
    monkeypatch.setattr(L, "_bundled_build", lambda: 139)
    monkeypatch.setattr(L, "_cached_version", lambda: 226)

    engine_dir, _ = L._prepare_engine()
    assert (engine_dir / "app.py").read_text(encoding="utf-8") == "CACHED 226"
