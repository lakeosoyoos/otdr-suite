"""Build-time update-pubkey injection (desktop/inject_update_pubkey.py).

The committed launcher.py is FAIL CLOSED (placeholder pubkey); an official
release build turns auto-update ON by DERIVING the public key from the
OTDR_UPDATE_SIGNING_KEY secret and stamping it into launcher.py at build time.
These tests prove that chain end-to-end WITHOUT touching the committed source:

  • no secret in env            -> injector is a no-op (build stays fail-closed),
  • a secret in env             -> injector derives the MATCHING pubkey and the
                                   resulting launcher is 'configured',
  • the injected launcher VERIFIES a manifest signed by the SAME key (via the
    real make_update_manifest signer) and REJECTS a tampered one,
  • the committed source launcher.py stays fail-closed no matter what.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys

import pytest

from conftest import REPO_ROOT

DESKTOP = REPO_ROOT / "desktop"
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except Exception:
    HAVE_CRYPTO = False

pytestmark = pytest.mark.skipif(not HAVE_CRYPTO, reason="cryptography not installed locally")


def _load(path):
    spec = importlib.util.spec_from_file_location("otdr_launcher_under_test", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ephemeral_priv_hex():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
    priv = Ed25519PrivateKey.generate()
    return priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()


def test_source_launcher_is_fail_closed():
    """Guard: the COMMITTED launcher.py never carries a real key."""
    L = _load(DESKTOP / "launcher.py")
    assert L.UPDATE_PUBLIC_KEY_HEX == L.UPDATE_PUBLIC_KEY_PLACEHOLDER
    assert L.update_signing_configured() is False


def test_injector_is_a_noop_without_the_secret(tmp_path, monkeypatch):
    import inject_update_pubkey as INJ
    monkeypatch.delenv(INJ.SIGNING_KEY_ENV, raising=False)
    tmp_launcher = tmp_path / "launcher.py"
    shutil.copy(DESKTOP / "launcher.py", tmp_launcher)
    before = tmp_launcher.read_text(encoding="utf-8")

    monkeypatch.setattr(INJ, "LAUNCHER", str(tmp_launcher))
    assert INJ.main() == 0                      # graceful no-op, exit 0
    assert tmp_launcher.read_text(encoding="utf-8") == before   # untouched
    assert _load(tmp_launcher).update_signing_configured() is False


def test_injector_main_with_secret_enables(tmp_path, monkeypatch):
    """CI invokes main() (not inject() directly) WITH the secret set.  Guard that
    exact path: a regression that made main() silently no-op when the secret IS
    present (wrong env-var name, a stray early return) would ship a fail-closed
    exe while every other test stayed green — the boss would think auto-update is
    on when it isn't.  This test fails if main() doesn't actually enable it."""
    import inject_update_pubkey as INJ
    priv_hex = _ephemeral_priv_hex()
    tmp_launcher = tmp_path / "launcher.py"
    shutil.copy(DESKTOP / "launcher.py", tmp_launcher)

    monkeypatch.setenv(INJ.SIGNING_KEY_ENV, priv_hex)
    monkeypatch.setattr(INJ, "LAUNCHER", str(tmp_launcher))
    assert INJ.main() == 0                                   # CI entry point, secret set
    L = _load(tmp_launcher)
    assert L.update_signing_configured() is True            # <- would catch a silent no-op
    assert L.UPDATE_PUBLIC_KEY_HEX == INJ.derive_public_hex(priv_hex)


def test_inject_derives_matching_key_and_enables(tmp_path):
    import inject_update_pubkey as INJ
    priv_hex = _ephemeral_priv_hex()
    tmp_launcher = tmp_path / "launcher.py"
    shutil.copy(DESKTOP / "launcher.py", tmp_launcher)

    pub_hex = INJ.inject(str(tmp_launcher), priv_hex)
    assert pub_hex == INJ.derive_public_hex(priv_hex)
    assert len(bytes.fromhex(pub_hex)) == 32

    L = _load(tmp_launcher)
    assert L.UPDATE_PUBLIC_KEY_HEX == pub_hex
    assert L.update_signing_configured() is True
    # The placeholder DEFINITION must survive (only the assignment was rewritten).
    assert L.UPDATE_PUBLIC_KEY_PLACEHOLDER == "REPLACE_WITH_ED25519_PUBLIC_KEY_HEX"


def test_injected_launcher_trusts_the_same_keys_signed_manifest(tmp_path):
    """The whole point: the exe (pubkey injected) verifies a manifest signed by
    make_update_manifest with the SAME private key, and rejects a tamper."""
    import inject_update_pubkey as INJ
    import make_update_manifest as MUM

    priv_hex = _ephemeral_priv_hex()
    tmp_launcher = tmp_path / "launcher.py"
    shutil.copy(DESKTOP / "launcher.py", tmp_launcher)
    INJ.inject(str(tmp_launcher), priv_hex)
    L = _load(tmp_launcher)

    manifest = {"version": 1, "commit": "x", "files": {"app.py": "0" * 64}}
    mbytes = json.dumps(manifest).encode()
    sig = MUM.sign(mbytes, priv_hex)

    assert L._verify_manifest_signature(mbytes, sig) is True       # good sig accepted
    assert L._verify_manifest_signature(mbytes + b" ", sig) is False  # tamper rejected
    assert L._verify_manifest_signature(mbytes, b"\x00" * 64) is False  # garbage rejected


def test_inject_raises_if_launcher_assignment_missing(tmp_path):
    """If launcher.py is refactored so the placeholder assignment is gone, the
    injector must FAIL LOUDLY rather than silently ship a dead auto-update."""
    import inject_update_pubkey as INJ
    bad = tmp_path / "launcher.py"
    bad.write_text("UPDATE_PUBLIC_KEY_HEX = 'nope'\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        INJ.inject(str(bad), _ephemeral_priv_hex())
