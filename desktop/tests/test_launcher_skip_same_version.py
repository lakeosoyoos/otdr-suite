"""A boot with nothing newer published fetches the manifest and nothing else.

_try_auto_update used to download every engine file (43 of them, about
3.5 MB, one new HTTPS connection each) and only afterwards let
_prepare_engine refuse the version as not newer than the cache
(anti-rollback).  On an up-to-date machine, which is most boots, that was
7 to 13 s of network time thrown away before the first page appeared.  The
same comparison now runs before the download.

Everything that decided the outcome before still decides it: the signature
is checked first, a manifest this exe cannot carry is still reported, a
damaged cache still reports version 0 so a repair re-fetches, and a newer
version is still downloaded and checked file by file.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json

from conftest import REPO_ROOT

LAUNCHER = REPO_ROOT / "desktop" / "launcher.py"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("otdr_launcher_skip", LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _body(rel):
    return f"# {rel}".encode()


def _engine(L, dirpath):
    """A complete engine on disk, and the hashes that describe it."""
    hashes = {}
    for rel in L.ENGINE_FILES:
        f = dirpath / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(_body(rel))
        hashes[rel] = hashlib.sha256(_body(rel)).hexdigest()
    return dirpath, hashes


def _setup(L, tmp_path, monkeypatch, cached_version=500):
    """A machine whose cache holds a verified engine at `cached_version`."""
    cache, hashes = _engine(L, tmp_path / "engine")
    cache.with_name("engine.meta.json").write_text(json.dumps(
        {"version": cached_version, "commit": COMMIT, "files": hashes}),
        encoding="utf-8")
    monkeypatch.setattr(L, "_cache_dir", lambda: cache)
    monkeypatch.setattr(L, "_verify_manifest_signature", lambda m, s: True)
    return cache, hashes


def _github(L, monkeypatch, version, files):
    """A fake GitHub serving a signed manifest for `version` and every engine
    file.  Returns the list of URLs asked for."""
    # OTDR Suite App: its launcher refuses a manifest whose channel is not its
    # own ("app") before anything else, as in test_autoupdate.py.
    body = json.dumps({"version": version, "commit": COMMIT,
                       "files": files, "channel": L.UPDATE_CHANNEL}).encode()
    urls = []

    def fetch(url, timeout=15):
        urls.append(url)
        if url == L.MANIFEST_URL:
            return body
        if url == L.MANIFEST_SIG_URL:
            return b"sig"
        # Longest first: "fqa/app.py" also ends with "app.py".
        for rel in sorted(L.ENGINE_FILES, key=len, reverse=True):
            if url.endswith("/" + rel):
                return _body(rel)
        return None

    monkeypatch.setattr(L, "_fetch", fetch)
    return urls


def test_the_same_version_fetches_only_the_manifest_and_signature(tmp_path, monkeypatch):
    L = _load_launcher()
    _, hashes = _setup(L, tmp_path, monkeypatch, 500)
    urls = _github(L, monkeypatch, 500, hashes)
    staging = tmp_path / "engine.staging"

    assert L._try_auto_update(staging) is None
    assert urls == [L.MANIFEST_URL, L.MANIFEST_SIG_URL]
    assert not staging.exists()


def test_an_older_version_fetches_nothing_either(tmp_path, monkeypatch):
    L = _load_launcher()
    _, hashes = _setup(L, tmp_path, monkeypatch, 500)
    urls = _github(L, monkeypatch, 499, hashes)

    assert L._try_auto_update(tmp_path / "engine.staging") is None
    assert urls == [L.MANIFEST_URL, L.MANIFEST_SIG_URL]


def test_a_newer_version_still_downloads_and_checks_every_file(tmp_path, monkeypatch):
    L = _load_launcher()
    _, hashes = _setup(L, tmp_path, monkeypatch, 500)
    urls = _github(L, monkeypatch, 501, hashes)
    staging = tmp_path / "engine.staging"

    got = L._try_auto_update(staging)
    assert got is not None and got["__version_int"] == 501
    assert len(urls) == 2 + len(L.ENGINE_FILES)
    assert all((staging / rel).read_bytes() == _body(rel) for rel in L.ENGINE_FILES)


def test_a_damaged_cache_re_fetches_its_own_version(tmp_path, monkeypatch):
    """The repair path: a cache that lost a file reports version 0, so the
    same version is downloaded again instead of being skipped."""
    L = _load_launcher()
    cache, hashes = _setup(L, tmp_path, monkeypatch, 500)
    (cache / "viewer" / "sor_reader324802a.py").unlink()
    assert L._cached_version() == 0
    urls = _github(L, monkeypatch, 500, hashes)

    got = L._try_auto_update(tmp_path / "engine.staging")
    assert got is not None and got["__version_int"] == 500
    assert len(urls) == 2 + len(L.ENGINE_FILES)


def test_a_manifest_this_exe_cannot_carry_is_still_reported(tmp_path, monkeypatch):
    """The needs-a-new-installer check runs before the version check, so the
    hub's banner still hears about a build that added engine files."""
    L = _load_launcher()
    _, hashes = _setup(L, tmp_path, monkeypatch, 500)
    reported = []
    monkeypatch.setattr(L, "_report_install_needed", reported.append)
    # setenv (not delenv) so teardown REMOVES what the launcher writes here:
    # a leaked value tells every later hub test that an installer is needed.
    monkeypatch.setenv(L.NEEDS_INSTALL_ENV, "")
    urls = _github(L, monkeypatch, 500, dict(hashes, **{"new/module.py": "0" * 64}))

    assert L._try_auto_update(tmp_path / "engine.staging") is None
    assert reported and "new/module.py" in reported[0]
    assert L.os.environ.get(L.NEEDS_INSTALL_ENV) == reported[0]
    assert urls == [L.MANIFEST_URL, L.MANIFEST_SIG_URL]


def test_an_up_to_date_boot_runs_the_cache_as_before(tmp_path, monkeypatch):
    """End to end: same engine chosen, same label, two requests instead of 45."""
    L = _load_launcher()
    cache, hashes = _setup(L, tmp_path, monkeypatch, 500)
    urls = _github(L, monkeypatch, 500, hashes)
    bundled, _ = _engine(L, tmp_path / "bundled")
    monkeypatch.delenv("OTDR_SUITE_NO_UPDATE", raising=False)
    # _prepare_engine exports these; setenv first so teardown removes them.
    for name in (L.ENGINE_FILES_ENV, L.NEEDS_INSTALL_ENV, L.CACHE_PINNED_ENV):
        monkeypatch.setenv(name, "")
    monkeypatch.setattr(L.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(L, "update_signing_configured", lambda: True)
    monkeypatch.setattr(L, "bundled_dir", lambda: bundled)
    monkeypatch.setattr(L, "_bundled_build", lambda: 400)
    monkeypatch.setattr(L, "_report_update_stuck",
                        lambda reason: (_ for _ in ()).throw(AssertionError(reason)))

    engine_dir, label = L._prepare_engine()

    assert engine_dir == cache
    assert label == "cached (last verified update)"
    assert urls == [L.MANIFEST_URL, L.MANIFEST_SIG_URL]
    assert not cache.with_name("engine.staging").exists()
