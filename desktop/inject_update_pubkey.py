#!/usr/bin/env python3
"""Build-time injection of the Ed25519 update-signing PUBLIC key into launcher.py.

WHY THIS EXISTS (and why we don't just commit the pubkey):
  The committed launcher.py always ships the placeholder
  (UPDATE_PUBLIC_KEY_HEX = UPDATE_PUBLIC_KEY_PLACEHOLDER), which keeps every
  build FAIL-CLOSED by default — a fork, a dev build, or any build without the
  signing secret does NO network code-fetch at all.  test_autoupdate.py and
  test_packaging_contract.py enforce that the SOURCE stays that way.

  An *official* release build turns auto-update ON by running this script AFTER
  the test suite and BEFORE PyInstaller.  It reads the SAME secret the manifest
  signer uses (OTDR_UPDATE_SIGNING_KEY = the 64-hex-char Ed25519 PRIVATE key),
  DERIVES the matching public key, and stamps it into the launcher.py in the
  build workspace only (never committed).  Deriving the pubkey from the private
  key guarantees the shipped .exe trusts EXACTLY the key that signs the manifest
  — there is no way for the two halves to drift.

  No secret in the environment  ->  no-op (prints and exits 0), so the build
  stays fail-closed.  This is the safe default for PRs / forks / local builds.

Usage (from repo root, in CI):
    OTDR_UPDATE_SIGNING_KEY=<priv-hex> python desktop/inject_update_pubkey.py
"""
from __future__ import annotations

import os
import re
import sys

SIGNING_KEY_ENV = "OTDR_UPDATE_SIGNING_KEY"      # 64 hex chars, Ed25519 private
LAUNCHER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "launcher.py")

# The source assignment we replace: the WHOLE line, whatever trailing comment it
# carries.  Anchored to the start of a line (MULTILINE) so we never touch the
# UPDATE_PUBLIC_KEY_PLACEHOLDER *definition* line above it.  If launcher.py ever
# renames this, subn's count check raises loudly rather than silently shipping
# the placeholder and a dead auto-update.
_ASSIGN_RE = re.compile(
    r"^UPDATE_PUBLIC_KEY_HEX = UPDATE_PUBLIC_KEY_PLACEHOLDER.*$", re.MULTILINE)


def derive_public_hex(priv_hex: str) -> str:
    """32-byte Ed25519 public key (hex) for the given 32-byte private key (hex)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv_hex.strip()))
    return priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def inject(launcher_path: str, priv_hex: str) -> str:
    """Rewrite `launcher_path` in place so UPDATE_PUBLIC_KEY_HEX is the derived
    public key.  Returns the public-key hex.  Raises if the placeholder line is
    not found exactly once (so a launcher refactor can't silently disable this)."""
    pub_hex = derive_public_hex(priv_hex)
    if len(bytes.fromhex(pub_hex)) != 32:
        raise ValueError("derived public key is not 32 bytes")

    with open(launcher_path, "r", encoding="utf-8") as f:
        src = f.read()

    new_line = (f'UPDATE_PUBLIC_KEY_HEX = "{pub_hex}"'
                '  # AUTO-INJECTED AT BUILD TIME by inject_update_pubkey.py — NOT committed')
    src, n = _ASSIGN_RE.subn(new_line, src)
    if n != 1:
        raise RuntimeError(
            f"expected exactly one UPDATE_PUBLIC_KEY_HEX assignment in {launcher_path}, "
            f"found {n} — launcher.py changed; update inject_update_pubkey.py")

    with open(launcher_path, "w", encoding="utf-8") as f:
        f.write(src)
    return pub_hex


def main() -> int:
    priv_hex = os.environ.get(SIGNING_KEY_ENV, "").strip()
    if not priv_hex:
        print(f"{SIGNING_KEY_ENV} unset — leaving launcher.py fail-closed "
              "(auto-update stays DISABLED for this build).")
        return 0
    try:
        pub_hex = inject(LAUNCHER, priv_hex)
    except Exception as exc:
        print(f"ERROR: could not inject update pubkey: {exc}", file=sys.stderr)
        return 1
    # Print only the PUBLIC half — safe to log.
    print(f"Injected update-signing public key {pub_hex} into launcher.py "
          "(auto-update ENABLED for this build).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
