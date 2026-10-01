"""An update downloads its engine files several at a time.

One after another, each on its own new HTTPS connection, the 43 files took
7 to 13 s of the boot that applies an update.  They are now fetched in
parallel.  What must not change: every file is checked against the signed
manifest, the first failure rejects the whole update, and nothing reaches
the staging folder unless every file passed.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import threading
import time

from conftest import REPO_ROOT

LAUNCHER = REPO_ROOT / "desktop" / "launcher.py"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("otdr_launcher_par", LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _body(rel):
    return f"# {rel}".encode()


def _github(L, monkeypatch, *, delay=0.0, broken=None, poisoned=None):
    """A fake GitHub serving a newer signed manifest and every engine file.
    `broken` fails one file's fetch, `poisoned` serves wrong bytes for one."""
    files = {rel: hashlib.sha256(_body(rel)).hexdigest() for rel in L.ENGINE_FILES}
    # OTDR Suite App: the launcher takes only a manifest for its own channel.
    body = json.dumps({"version": 9_999_999, "commit": COMMIT, "files": files,
                       "channel": L.UPDATE_CHANNEL}).encode()
    state = {"urls": [], "now": 0, "peak": 0}
    lock = threading.Lock()

    def fetch(url, timeout=15):
        if url == L.MANIFEST_URL:
            return body
        if url == L.MANIFEST_SIG_URL:
            return b"sig"
        with lock:
            state["urls"].append(url)
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        try:
            time.sleep(delay)
            for rel in sorted(L.ENGINE_FILES, key=len, reverse=True):
                if url.endswith("/" + rel):
                    if rel == broken:
                        return None
                    return b"not what was signed" if rel == poisoned else _body(rel)
            return None
        finally:
            with lock:
                state["now"] -= 1

    monkeypatch.setattr(L, "_fetch", fetch)
    monkeypatch.setattr(L, "_verify_manifest_signature", lambda m, s: True)
    monkeypatch.setattr(L, "_cached_version", lambda: 0)
    return state


def test_the_files_come_down_several_at_a_time(tmp_path, monkeypatch):
    L = _load_launcher()
    state = _github(L, monkeypatch, delay=0.05)
    staging = tmp_path / "engine.staging"

    got = L._try_auto_update(staging)
    assert got is not None and got["__version_int"] == 9_999_999
    assert state["peak"] > 1, "files were fetched one at a time"
    assert len(state["urls"]) == len(L.ENGINE_FILES)
    assert all((staging / rel).read_bytes() == _body(rel) for rel in L.ENGINE_FILES)


def test_a_failed_file_rejects_the_update_and_writes_nothing(tmp_path, monkeypatch):
    L = _load_launcher()
    _github(L, monkeypatch, broken="viewer/trace_server.py")
    staging = tmp_path / "engine.staging"

    assert L._try_auto_update(staging) is None
    assert not [p for p in staging.rglob("*") if p.is_file()]


def test_a_file_that_does_not_match_the_signed_hash_rejects_the_update(tmp_path, monkeypatch):
    L = _load_launcher()
    _github(L, monkeypatch, poisoned="app.py")
    staging = tmp_path / "engine.staging"

    assert L._try_auto_update(staging) is None
    assert not [p for p in staging.rglob("*") if p.is_file()]


def test_the_tls_context_is_made_once(monkeypatch):
    """Building one reads the whole CA bundle; an update fetches 45 URLs."""
    L = _load_launcher()
    made = []
    real = L.ssl.create_default_context
    monkeypatch.setattr(L.ssl, "create_default_context",
                        lambda *a, **k: made.append(1) or real(*a, **k))
    first = L._tls_context()
    assert all(L._tls_context() is first for _ in range(5))
    assert len(made) == 1


def test_a_failure_does_not_wait_for_downloads_in_flight(tmp_path, monkeypatch):
    """The first failure returns at once; the files still downloading are
    left to finish on their own and thrown away."""
    L = _load_launcher()
    files = {rel: hashlib.sha256(_body(rel)).hexdigest() for rel in L.ENGINE_FILES}
    # OTDR Suite App: the launcher takes only a manifest for its own channel.
    body = json.dumps({"version": 9_999_999, "commit": COMMIT, "files": files,
                       "channel": L.UPDATE_CHANNEL}).encode()
    first = L.ENGINE_FILES[0]                 # submitted first, so it starts at once

    def fetch(url, timeout=15):
        if url == L.MANIFEST_URL:
            return body
        if url == L.MANIFEST_SIG_URL:
            return b"sig"
        if url.endswith("/" + first):
            return None                       # fails at once
        time.sleep(1.5)                       # everything else is slow
        return b"late"

    monkeypatch.setattr(L, "_fetch", fetch)
    monkeypatch.setattr(L, "_verify_manifest_signature", lambda m, s: True)
    monkeypatch.setattr(L, "_cached_version", lambda: 0)
    t = time.perf_counter()
    assert L._try_auto_update(tmp_path / "engine.staging") is None
    assert time.perf_counter() - t < 1.0


def test_a_download_cut_off_part_way_is_a_failed_fetch(monkeypatch):
    """http.client.IncompleteRead from resp.read() used to escape _fetch and
    stop the launcher before the hub started."""
    import http.client
    L = _load_launcher()

    class Cut:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            raise http.client.IncompleteRead(b"partial", 100)

    monkeypatch.setattr(L.urllib.request, "urlopen", lambda *a, **k: Cut())
    assert L._fetch("https://example.invalid/app.py") is None
