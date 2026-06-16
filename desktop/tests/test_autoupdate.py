"""Auto-update guard tests for the launcher (no network).

The launcher fetches engine/UI files from GitHub at boot, but ONLY via a
SIGNED-MANIFEST flow that fails closed.  These assert the security contract
WITHOUT hitting the network and WITHOUT needing the cryptography lib at runtime
(the verify path is exercised structurally / with monkeypatched fetch):

  • ENGINE_FILES lists exactly the tracked engine files,
  • an Ed25519 PUBLIC-KEY constant + a manifest-verify function exist,
  • with the placeholder key the launcher FAILS CLOSED (no fetch, bundled),
  • the update path refuses to promote unverified / hash-mismatched files,
  • the update points at the correct repo + a signed manifest URL.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from conftest import REPO_ROOT

LAUNCHER = REPO_ROOT / "desktop" / "launcher.py"

# cryptography is a bundled dep but may be absent in the local Python 3.9 test
# env; the crypto-dependent assertions skip cleanly when it is.
try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except Exception:
    HAVE_CRYPTO = False


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


def test_update_targets_this_repo_and_signed_manifest():
    L = _load_launcher()
    assert L.GH_OWNER == "lakeosoyoos" and L.GH_REPO == "otdr-suite"
    assert "raw.githubusercontent.com" in L.RAW_URL_FMT
    # The signed-manifest URLs must point at the same branch as the engine files.
    assert L.MANIFEST_URL.endswith("update_manifest.json")
    assert L.MANIFEST_SIG_URL.endswith("update_manifest.json.sig")


def test_pubkey_constant_and_verify_fn_exist():
    """The Ed25519 PUBLIC-KEY constant + a manifest-verify function must exist —
    this is the only trust gate for the update."""
    L = _load_launcher()
    assert hasattr(L, "UPDATE_PUBLIC_KEY_HEX")
    assert hasattr(L, "update_signing_configured")
    assert callable(L._verify_manifest_signature)


def test_fails_closed_with_placeholder_pubkey(monkeypatch, tmp_path):
    """SECURITY: while the pubkey is the unset placeholder, auto-update is
    DISABLED — no engine code is fetched and the bundled engine is used.  The
    moment this lands the RCE vector is closed until a real key is provisioned."""
    L = _load_launcher()
    assert L.UPDATE_PUBLIC_KEY_HEX == L.UPDATE_PUBLIC_KEY_PLACEHOLDER, (
        "the committed pubkey MUST be the placeholder — a real key is a human step"
    )
    assert L.update_signing_configured() is False

    # _fetch must never be called when fail-closed; trip a sentinel if it is.
    def _boom(*a, **k):
        raise AssertionError("fail-closed launcher must NOT fetch any update")
    monkeypatch.setattr(L, "_fetch", _boom)
    monkeypatch.delenv("OTDR_SUITE_NO_UPDATE", raising=False)
    monkeypatch.setattr(L.Path, "home", staticmethod(lambda: tmp_path))

    engine_dir, label = L._prepare_engine()
    assert engine_dir == L.bundled_dir()
    assert "disabled" in label.lower()


def test_verify_rejects_when_not_configured():
    """With the placeholder key, signature verification returns False even if a
    perfectly-formed signature is supplied (no key == no trust)."""
    L = _load_launcher()
    assert L._verify_manifest_signature(b"any-manifest", b"\x00" * 64) is False


def test_no_update_flag_pins_bundled(monkeypatch):
    """OTDR_SUITE_NO_UPDATE skips the network fetch and runs the bundled build
    (air-gapped/offline pinning)."""
    L = _load_launcher()
    monkeypatch.setenv("OTDR_SUITE_NO_UPDATE", "1")
    engine_dir, label = L._prepare_engine()
    assert engine_dir == L.bundled_dir()
    assert "disabled" in label.lower()


# ── Crypto-path tests — exercise the REAL Ed25519 verify with an ephemeral
#    keypair.  Skipped when cryptography isn't installed in the local env. ──
@pytest.mark.skipif(not HAVE_CRYPTO, reason="cryptography not installed locally")
def test_signed_update_accepts_good_and_rejects_tampered(monkeypatch, tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    L = _load_launcher()
    priv = Ed25519PrivateKey.generate()
    raw_pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(L, "UPDATE_PUBLIC_KEY_HEX", raw_pub.hex())
    assert L.update_signing_configured() is True

    files = {rel: hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest()
             for rel in L.ENGINE_FILES}
    manifest = {"version": 9, "commit": "abc", "files": files}
    mbytes = json.dumps(manifest).encode()
    sig = priv.sign(mbytes)

    assert L._verify_manifest_signature(mbytes, sig) is True
    assert L._verify_manifest_signature(mbytes + b" ", sig) is False   # tampered
    assert L._verify_manifest_signature(mbytes, b"\x00" * 64) is False  # bad sig

    # Full fetch path: a valid signed manifest + real files populates staging.
    def good_fetch(url, timeout=15):
        if url == L.MANIFEST_URL:
            return mbytes
        if url == L.MANIFEST_SIG_URL:
            return sig
        for rel in L.ENGINE_FILES:
            if url.endswith(rel):
                return (REPO_ROOT / rel).read_bytes()
        return None

    staging = tmp_path / "staging"
    monkeypatch.setattr(L, "_fetch", good_fetch)
    got = L._try_auto_update(staging)
    assert got is not None and got["__version_int"] == 9
    assert (staging / "app.py").exists()


@pytest.mark.skipif(not HAVE_CRYPTO, reason="cryptography not installed locally")
def test_signed_update_rejects_hash_mismatch(monkeypatch, tmp_path):
    """A file whose bytes don't match the signed SHA-256 is rejected and never
    promoted — this is the core anti-RCE guarantee."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    L = _load_launcher()
    priv = Ed25519PrivateKey.generate()
    raw_pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(L, "UPDATE_PUBLIC_KEY_HEX", raw_pub.hex())

    files = {rel: hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest()
             for rel in L.ENGINE_FILES}
    manifest = {"version": 9, "commit": "abc", "files": files}
    mbytes = json.dumps(manifest).encode()
    sig = priv.sign(mbytes)

    def poisoned_fetch(url, timeout=15):
        if url == L.MANIFEST_URL:
            return mbytes
        if url == L.MANIFEST_SIG_URL:
            return sig
        if url.endswith("app.py"):
            return b"def evil():\n    pass  # poisoned post-signing\n"
        for rel in L.ENGINE_FILES:
            if url.endswith(rel):
                return (REPO_ROOT / rel).read_bytes()
        return None

    staging = tmp_path / "staging"
    monkeypatch.setattr(L, "_fetch", poisoned_fetch)
    assert L._try_auto_update(staging) is None     # rejected on SHA mismatch
