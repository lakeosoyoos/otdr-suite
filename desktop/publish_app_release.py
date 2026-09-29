"""
Publish OTDR Suite App's release: installer + signed update feed (CI-only).
===========================================================================
The App does not read main's manifest: main's code would replace the App's
screens.  Its launcher reads the signed manifest + signature from the assets
of one GitHub release, tag `app-build` (launcher.UPDATE_FEED_TAG), and takes
only a manifest marked for its channel ("app").  The same release holds the
App's installer, so its download link never changes:

    https://github.com/lakeosoyoos/otdr-suite/releases/download/app-build/OTDRSuiteApp-Setup.exe

CI publishes this release only from the app-release branch, which moves only
when Robert says to ship the App, so a checkpoint push to sandbox/app-window
never reaches anyone.  The release is a pre-release and never "Latest", so
the repository's front page keeps pointing at the regular OTDR Suite.

This script runs after the installer is built and signed and after
make_update_manifest.py (--channel app), and decides:

  publish     the release is empty or older than this run: upload the
              installer, then the signature, then the manifest (an App that
              sees the new manifest finds the new installer already there),
              then download the feed back and compare bytes.
  superseded  the release already carries this run's version or a newer one
              (a later app-release build finished first): upload nothing,
              exit 0.  The launcher would refuse the older manifest anyway
              (anti-rollback); standing down keeps the installer and the feed
              on the same, newest build.

Fails loud (exit 1): no manifest was written (the signing secret is missing,
so this build would ship an App that can never update), no installer, a
manifest for another version, commit or channel, or uploads that keep failing.

Usage (CI, from the repo root, with GH_TOKEN set):
    python desktop/publish_app_release.py --version <N> --built <SHA> \
        --installer desktop/dist/OTDRSuiteApp-Setup.exe
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "desktop"))
from launcher import UPDATE_CHANNEL, UPDATE_FEED_TAG  # noqa: E402
from make_update_manifest import MANIFEST_NAME, SIG_NAME  # noqa: E402

INSTALLER_NAME = "OTDRSuiteApp-Setup.exe"
RELEASE_TITLE = "OTDR Suite App"
RELEASE_NOTES = ("Permanent download link for OTDR Suite App. "
                 f"{INSTALLER_NAME} is the installer; once installed, the App "
                 "updates itself from the signed manifest beside it. Rewritten "
                 "by CI from the app-release branch.")
RETRY_DELAYS = (15, 30, 60, 120)


class PublishError(Exception):
    pass


def _gh(args, check=True):
    """Run the GitHub CLI.  Returns (exit code, stdout)."""
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise PublishError(f"gh {' '.join(args)} failed ({proc.returncode}): "
                           f"{proc.stderr.strip()}")
    return proc.returncode, proc.stdout


def check_manifest(manifest_bytes: bytes, version: int, built: str) -> dict:
    """The manifest this run signed must be this run's, for the App."""
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except ValueError as exc:
        raise PublishError(f"{MANIFEST_NAME} is not JSON ({exc})")
    if not isinstance(manifest, dict):
        raise PublishError(f"{MANIFEST_NAME} is not a manifest")
    try:
        got_version = int(manifest.get("version", -1))
    except (TypeError, ValueError):
        got_version = -1
    if got_version != version:
        raise PublishError(f"{MANIFEST_NAME} is version {manifest.get('version')}, "
                           f"this run is {version}")
    if manifest.get("commit") != built:
        raise PublishError(f"{MANIFEST_NAME} is for commit {manifest.get('commit')}, "
                           f"this run built {built}")
    if manifest.get("channel") != UPDATE_CHANNEL:
        raise PublishError(f"{MANIFEST_NAME} is for channel "
                           f"{manifest.get('channel')!r}, the App takes "
                           f"{UPDATE_CHANNEL!r} only")
    return manifest


def live_version(gh=_gh) -> int:
    """Version the release carries now; 0 when the release or asset is missing."""
    with tempfile.TemporaryDirectory() as tmp:
        code, _ = gh(["release", "download", UPDATE_FEED_TAG, "-p", MANIFEST_NAME,
                      "-D", tmp, "--clobber"], check=False)
        path = Path(tmp) / MANIFEST_NAME
        if code != 0 or not path.exists():
            return 0
        try:
            return int(json.loads(path.read_text(encoding="utf-8"))["version"])
        except (ValueError, KeyError, TypeError):
            return 0          # a garbled feed is replaced, never trusted


def ensure_release(gh=_gh):
    code, _ = gh(["release", "view", UPDATE_FEED_TAG], check=False)
    if code == 0:
        return
    gh(["release", "create", UPDATE_FEED_TAG, "--prerelease", "--latest=false",
        "--title", RELEASE_TITLE, "--notes", RELEASE_NOTES])


def upload(path: Path, gh=_gh, sleep=time.sleep):
    """uploads.github.com answers 5xx now and then; --clobber makes a retry
    safe (see the windows-build step in build-windows.yml)."""
    for i in range(len(RETRY_DELAYS) + 1):
        code, _ = gh(["release", "upload", UPDATE_FEED_TAG, str(path), "--clobber"],
                     check=False)
        if code == 0:
            return
        if i == len(RETRY_DELAYS):
            raise PublishError(f"upload of {path.name} failed after {i + 1} attempts")
        print(f"upload of {path.name} failed (exit {code}); "
              f"retrying in {RETRY_DELAYS[i]}s")
        sleep(RETRY_DELAYS[i])


def verify_published(expected: dict, gh=_gh):
    """Download the feed back: what the Apps will fetch must be what we signed."""
    with tempfile.TemporaryDirectory() as tmp:
        for name, want in expected.items():
            gh(["release", "download", UPDATE_FEED_TAG, "-p", name, "-D", tmp,
                "--clobber"])
            got = (Path(tmp) / name).read_bytes()
            if got != want:
                raise PublishError(f"published {name} does not match what this "
                                   "run signed")


def publish(root: Path, version: int, built: str, installer: Path,
            gh=_gh, sleep=time.sleep) -> str:
    manifest_path, sig_path = root / MANIFEST_NAME, root / SIG_NAME
    if not manifest_path.exists() or not sig_path.exists():
        raise PublishError(
            f"no signed {MANIFEST_NAME}: OTDR_UPDATE_SIGNING_KEY is not set for "
            "this run, so this App build could never update.  Not publishing.")
    if installer.name != INSTALLER_NAME or not installer.is_file():
        raise PublishError(f"no installer at {installer}")
    manifest_bytes = manifest_path.read_bytes()
    check_manifest(manifest_bytes, version, built)
    ensure_release(gh)
    live = live_version(gh)
    if live >= version:
        print(f"superseded: the release already carries version {live} "
              f"(this run is {version}); nothing uploaded")
        return "superseded"
    # Installer first: an App told "a fresh install is needed" by the new
    # manifest must find the new installer at the link.  Then the signature,
    # then the manifest: an App that fetches between those two sees the old
    # manifest with the new signature, fails verification, and simply keeps
    # its engine until its next start.
    upload(installer, gh, sleep)
    upload(sig_path, gh, sleep)
    upload(manifest_path, gh, sleep)
    verify_published({MANIFEST_NAME: manifest_bytes,
                      SIG_NAME: sig_path.read_bytes()}, gh)
    print(f"published: {UPDATE_FEED_TAG} now carries build {version} "
          f"({built[:7]}), was {live or 'empty'}")
    return "published"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True, type=int)
    ap.add_argument("--built", required=True)
    ap.add_argument("--installer", required=True)
    ap.add_argument("--root", default=str(REPO_ROOT))
    args = ap.parse_args()
    try:
        publish(Path(args.root), args.version, args.built, Path(args.installer))
    except PublishError as exc:
        print(f"::error::{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
