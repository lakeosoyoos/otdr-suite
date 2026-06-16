"""
Generate + sign the OTDR Suite auto-update manifest (CI-only).
==============================================================
The launcher's SIGNED auto-update fetches `update_manifest.json` (each engine
file -> its SHA-256, plus a monotonic `version` and the source `commit`) and a
detached Ed25519 signature `update_manifest.json.sig`, verifies the signature
with the PUBLIC key baked into launcher.py, then checks every downloaded file's
SHA-256 against the manifest.  This script is the producer half: CI runs it
after a green build to (re)generate + sign those two files before publishing.

Usage (from repo root, in CI):
    python desktop/make_update_manifest.py --version <N> --commit <SHA> --out-dir .

The Ed25519 PRIVATE key is read from env OTDR_UPDATE_SIGNING_KEY (64 hex chars,
the repo secret).  If it is absent, signing is SKIPPED gracefully (exit 0, no
files written) — a build without the secret still succeeds; auto-update simply
stays unprovisioned (the launcher fails closed).  The file list MUST match
launcher.ENGINE_FILES exactly (verified here, and by the test suite).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Import ENGINE_FILES from the launcher so the manifest can NEVER drift from the
# files the app actually runs (a partial manifest is rejected by the launcher).
sys.path.insert(0, str(REPO_ROOT / "desktop"))
from launcher import ENGINE_FILES  # noqa: E402

SIGNING_KEY_ENV = "OTDR_UPDATE_SIGNING_KEY"
MANIFEST_NAME = "update_manifest.json"
SIG_NAME = "update_manifest.json.sig"


def build_manifest(version: int, commit: str) -> bytes:
    """Return the canonical manifest bytes (sha-256 of every ENGINE_FILE)."""
    files = {}
    for rel in ENGINE_FILES:
        p = REPO_ROOT / rel
        files[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    manifest = {"version": int(version), "commit": commit, "files": files}
    # sort_keys + compact separators → byte-stable output we can sign + re-verify.
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign(manifest_bytes: bytes, key_hex: str) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(key_hex.strip()))
    return priv.sign(manifest_bytes)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True, type=int)
    ap.add_argument("--commit", required=True)
    ap.add_argument("--out-dir", default=str(REPO_ROOT))
    args = ap.parse_args()

    key_hex = os.environ.get(SIGNING_KEY_ENV, "").strip()
    if not key_hex:
        # No signing key provisioned — skip gracefully so the build still passes.
        print(f"{SIGNING_KEY_ENV} unset — skipping manifest signing "
              "(auto-update stays unprovisioned; launcher fails closed).")
        return 0

    manifest_bytes = build_manifest(args.version, args.commit)
    sig = sign(manifest_bytes, key_hex)

    out = Path(args.out_dir)
    (out / MANIFEST_NAME).write_bytes(manifest_bytes)
    (out / SIG_NAME).write_bytes(sig)
    print(f"wrote {MANIFEST_NAME} (v{args.version}, {len(ENGINE_FILES)} files) "
          f"+ {SIG_NAME} ({len(sig)} bytes) to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
