"""
OTDR Suite — PyInstaller launcher (Windows .exe entry point)
============================================================
This is the entry point of the frozen OTDRSuite.exe.  It does two jobs,
selected by argv:

  • NORMAL launch (double-click) — boot the Streamlit hub (app.py) on a
    fixed port, poll /_stcore/health, then open the tech's browser.

  • SECRET-SAUCE SUBPROCESS (`--run-secretsauce ...`) — the hub shells out
    to run the Secret Sauce engine in a clean process (its sor_reader copy
    can't share the hub's namespace).  In a frozen build `sys.executable`
    IS this exe, so the hub re-invokes the exe with this sentinel and we
    dispatch to the bundled secretsauce/run_secretsauce.py here.

Why this shape:
  - A frozen windowed app has sys.stdout/err == None; any print() would
    crash it, so we redirect to a log file first.
  - Streamlit's first-run e-mail prompt blocks on stdin in a hidden
    process, so we pre-seed credentials + headless env vars.
  - Cold launches can take 20-40 s while PyInstaller unpacks; opening the
    browser too early shows "connection refused", so we poll health first.

Engine files (viewer/* and secretsauce/*) ship as on-disk data next to the
exe and are imported via sys.path at runtime — NOT as PyInstaller modules —
because viewer/ and secretsauce/ each carry a DIFFERENT sor_reader324802a.py
and two same-named modules can't coexist in one frozen archive.
"""
from __future__ import annotations

import os
import re
import sys
import ssl
import time
import json
import socket
import hashlib
import threading
import urllib.request
import urllib.error
import webbrowser
from pathlib import Path

# ── Edition: OTDR Suite App ─────────────────────────────────────────────
# This branch builds "OTDR Suite App", the app-window edition, so it installs
# and runs BESIDE the regular OTDR Suite on one PC.  Nothing is shared: its own
# app folder (settings, cache, locks, log), its own port, its own installer
# identity (OTDRSuite.iss), and its own updates.  main's manifest carries
# main's code, which would overwrite this edition's, so the App never reads
# it: it reads the App's release (UPDATE_FEED_TAG below: the installer plus
# the signed manifest), which CI publishes only from the app-release branch,
# and takes only a manifest marked for the App (UPDATE_CHANNEL).  The regular
# edition is APP_NAME "OTDRSuite", ".otdrSuite", PORT 8510, main's manifest,
# no channel.
EDITION      = "OTDR Suite App"
APP_NAME     = "OTDRSuiteApp"
APP_DIR_NAME = ".otdrSuiteApp"
HOST         = "127.0.0.1"
PORT         = 8520                       # see project-desktop-ports-registry
AUTO_UPDATE  = True
UPDATE_CHANNEL  = "app"
UPDATE_FEED_TAG = "app-build"
# Streamlit's own toolbar, top right: "minimal" drops its "Deploy" button and
# its three-dot menu (Rerun / Settings / Print / About -- Streamlit's, not
# ours; the hub adds no menu items, so the menu disappears).  An app has no
# use for them.  The regular edition leaves Streamlit's default.
TOOLBAR_MODE = "minimal"
HEALTH_URL   = f"http://{HOST}:{PORT}/_stcore/health"
APP_URL      = f"http://{HOST}:{PORT}"

# ── Auto-update: pull the latest engine + UI from GitHub at boot ─────────
# SIGNED-MANIFEST update, FAIL CLOSED.  The flow is:
#   1. fetch manifest.json (lists each ENGINE_FILE -> its SHA-256, plus a
#      monotonic `version` and the source `commit`),
#   2. fetch manifest.sig (a detached Ed25519 signature over the EXACT
#      manifest bytes),
#   3. VERIFY that signature against UPDATE_PUBLIC_KEY_HEX (baked below),
#   4. fetch each ENGINE_FILE and check its SHA-256 against the manifest,
#   5. refuse the swap unless manifest.version > the cached version
#      (anti-rollback), then atomically swap into ~/.otdrSuite/engine.
# ANY mismatch (bad signature, hash miss, stale version, fetch failure)
# discards the staging dir and keeps the current engine — we NEVER write an
# unverified file into the run path.
#
# The OLD behaviour (fetch raw .py and trust "non-empty + compiles") was a
# fleet-wide RCE: anyone who could write main, poison a branch, leak a CI/PAT
# token, or MITM the fetch ran arbitrary code on every tech's machine.  That
# unverified fetch path has been REMOVED — there is no fallback to it.
#
# FAIL CLOSED: until a real Ed25519 public key is provisioned (see
# UPDATE_PUBLIC_KEY_HEX below), auto-update is DISABLED and the app runs the
# bundled engine.  This means the RCE vector is closed the moment this lands;
# auto-update stays off until Robert pastes the key.
#
# This can only ship .py/.html changes — launcher.py / the .spec / Python
# itself still require a fresh download (the bootstrap can't update its own
# bootstrap).
GH_OWNER    = "lakeosoyoos"
GH_REPO     = "otdr-suite"
GH_BRANCH   = "app-release"   # App edition: the branch its feed is built from
RAW_URL_FMT = ("https://raw.githubusercontent.com/"
               f"{GH_OWNER}/{GH_REPO}/{GH_BRANCH}/{{path}}")
# The signed manifest + detached signature live next to the engine files on
# the same branch, written by CI (see build-windows.yml).
# Engine files are fetched at the manifest's OWN commit, not at the branch tip.
# CI publishes a manifest ~11 min after the merge that produced it, so between
# any merge and its build main's files are newer than the live manifest hashes
# them: measured over 60 HEAD states on main, 29 of them would have failed every
# launcher's SHA-256 check.  Pinning to manifest["commit"] removes that race
# outright and is what lets the manifest stop tracking the branch tip at all.
# Falls back to the branch when a manifest predates the commit field.
RAW_REF_URL_FMT = ("https://raw.githubusercontent.com/"
                   f"{GH_OWNER}/{GH_REPO}/{{ref}}/{{path}}")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

MANIFEST_PATH     = "update_manifest.json"
MANIFEST_SIG_PATH = "update_manifest.json.sig"
# The App's updates come from a release (tag UPDATE_FEED_TAG), not a branch:
# CI uploads the installer, the signed manifest and its signature there from
# app-release builds and never commits to any branch.  The engine files are
# still fetched from raw.githubusercontent.com at the manifest's own commit.
FEED_URL_FMT = ("https://github.com/"
                f"{GH_OWNER}/{GH_REPO}/releases/download/{UPDATE_FEED_TAG}/{{path}}")
MANIFEST_URL      = FEED_URL_FMT.format(path=MANIFEST_PATH)
MANIFEST_SIG_URL  = FEED_URL_FMT.format(path=MANIFEST_SIG_PATH)
# The permanent installer link, beside the manifest; the hub's "fresh install
# needed" notices point here.
INSTALLER_URL     = FEED_URL_FMT.format(path="OTDRSuiteApp-Setup.exe")

# ── Ed25519 update-signing PUBLIC key ────────────────────────────────────
# The committed source ALWAYS keeps the placeholder below, so every build is
# FAIL CLOSED by default (auto-update DISABLED, bundled engine only, no network
# code-fetch at all) — enforced by test_autoupdate.py + test_packaging_contract.py.
# An OFFICIAL release build turns auto-update ON WITHOUT editing source: the CI
# step "Inject update-signing public key" runs desktop/inject_update_pubkey.py,
# which DERIVES this public key from the OTDR_UPDATE_SIGNING_KEY repo secret (the
# private half — which also signs the manifest) and stamps it in at build time.
# The shipped exe therefore trusts exactly the key that signs.  See README_BUILD.txt.
UPDATE_PUBLIC_KEY_PLACEHOLDER = "REPLACE_WITH_ED25519_PUBLIC_KEY_HEX"
UPDATE_PUBLIC_KEY_HEX = UPDATE_PUBLIC_KEY_PLACEHOLDER  # build-time injected; see inject_update_pubkey.py


def update_signing_configured() -> bool:
    """True only once a real Ed25519 public key has been baked in.  While this
    is False the launcher FAILS CLOSED — no engine code is fetched at all."""
    key = (UPDATE_PUBLIC_KEY_HEX or "").strip()
    if not key or key == UPDATE_PUBLIC_KEY_PLACEHOLDER:
        return False
    try:
        return len(bytes.fromhex(key)) == 32   # Ed25519 public keys are 32 bytes
    except ValueError:
        return False
# Every engine/UI file the running app imports or serves.  Keep in sync with
# what the spec bundles — test_autoupdate.py asserts this covers them all.
ENGINE_FILES = [
    "app.py",
    "error_report.py",
    "folder_intake.py",
    "sharepoint_link.py",
    "viewer/trace_server.py",
    "viewer/sor_reader324802a.py",
    "viewer/json_reader.py",
    "viewer/viewer.html",
    "secretsauce/run_secretsauce.py",
    "secretsauce/report.py",
    "secretsauce/report_sor.py",
    "secretsauce/sor_reader324802a.py",
    "secretsauce/trc_parser.py",
    "secretsauce/exfo_proprietary_decoder.py",
    "splicereport/run_splicereport.py",
    "splicereport/splicereportmatchexfo.py",
    "splicereport/sor_reader324802a.py",
    "splicereport/json_reader.py",
    "splicereport/acquisition_audit.py",
    "splicereport/reburn_summary.py",
    "components/otdr_settings/__init__.py",
    "components/otdr_settings/index.html",
    # FQA Builder.  The template is a binary .xlsm; the updater fetches and
    # hashes bytes throughout, so it travels like any other engine file.
    "fqa/__init__.py",
    "fqa/ui.py",
    "fqa/app.py",
    "fqa/run_fqa.py",
    "fqa/writer.py",
    "fqa/completeness.py",
    "fqa/production_sheet.py",
    "fqa/event_chain.py",
    "fqa/fat.py",
    "fqa/job_facts.py",
    "fqa/xlsx_patch.py",
    "fqa/make_template.py",
    "fqa/templates/FQA_Site_Survey_v1_1.xlsm",
    # Field Capture.  The web app's own code travels with updates; its
    # libraries (fieldcapture/web/vendor: 15 MB of label reader, ExcelJS,
    # JSZip) never change and ship only in the install, where
    # fieldcapture/server.py falls back to them.
    "fieldcapture/__init__.py",
    "fieldcapture/server.py",
    "fieldcapture/email_draft.py",
    "fieldcapture/web/index.html",
    "fieldcapture/web/app.js",
    "fieldcapture/web/fqa.js",
    "fieldcapture/web/labels.js",
    "fieldcapture/web/app.css",
    "fieldcapture/web/sw.js",
]


# ── Where the bundled files live ────────────────────────────────────────
def bundled_dir() -> Path:
    if getattr(sys, "frozen", False):
        # one-folder build → files sit next to the exe (or _MEIPASS for onefile)
        return Path(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
    return Path(__file__).resolve().parent.parent   # repo root in dev


def _cache_dir() -> Path:
    return Path.home() / APP_DIR_NAME / "engine"


# ── Auto-update helpers ──────────────────────────────────────────────────
def _tls_context():
    """An explicit verifying TLS context.  Prefer certifi's CA bundle (bundled
    with the exe — the frozen build has no system trust store on Windows), and
    fall back to the OS default if certifi is unavailable (dev).  We NEVER
    disable verification."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        # certifi missing (dev) — still verify, just with the OS store.
        return ssl.create_default_context()


def _fetch(url: str, timeout: int = 15):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
        with urllib.request.urlopen(req, timeout=timeout, context=_tls_context()) as resp:
            if resp.status != 200:
                return None
            return resp.read()
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError):
        return None


def _verify_manifest_signature(manifest_bytes: bytes, sig: bytes) -> bool:
    """Verify the detached Ed25519 signature `sig` over the EXACT manifest bytes
    against the baked public key.  Returns False on ANY problem (bad signature,
    missing crypto lib, malformed key) — fail closed, never raise."""
    if not update_signing_configured():
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
        from cryptography.exceptions import InvalidSignature
    except Exception as exc:
        # No crypto lib bundled → we cannot verify → refuse the update.
        print(f"auto-update: cryptography unavailable, cannot verify ({exc})")
        return False
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(UPDATE_PUBLIC_KEY_HEX))
        pub.verify(sig, manifest_bytes)        # raises InvalidSignature on mismatch
        return True
    except InvalidSignature:
        print("auto-update: manifest signature INVALID — rejecting update")
        return False
    except Exception as exc:
        print(f"auto-update: signature check errored ({exc}) — rejecting")
        return False


def _engine_intact(d: Path, hashes=None) -> str:
    """'' when `d` holds a COMPLETE engine, else a short note on what is wrong.

    THE BUG THIS EXISTS FOR.  Every test of an engine directory used to be
    `(d / "app.py").exists()` — one file out of 21.  A tech on build 313 booted
    straight into `ModuleNotFoundError: No module named 'sor_reader324802a'`
    because his ~/.otdrSuite/engine held viewer/trace_server.py but not the
    viewer/sor_reader324802a.py that file imports.  Both are in ENGINE_FILES and
    both hashed clean at download, so the file went missing AFTER the swap (an
    antivirus quarantine is the ordinary cause).  The launcher could not tell
    that cache from a good one, so it ran it every boot; a second tech on the
    same build was fine.  Worse, the cache still carried version 313, so
    anti-rollback refused to fetch 313 again and nothing self-healed.

    With `hashes` (the manifest's {rel: sha256} as recorded in engine.meta.json)
    each file is hashed too, which catches the file that is still THERE but no
    longer what we shipped — a truncated write, a half-restored quarantine, a
    disk that lost a sector.  Deletion is what the field hit; corruption fails
    at import just as hard and used to look identical to a healthy engine.
    1.6 MB over 21 files, so this costs single-digit milliseconds at boot.

    Without `hashes` (a survivor directory, an engine whose meta predates this)
    it falls back to present-and-not-empty."""
    for rel in ENGINE_FILES:
        f = d / rel
        want = (hashes or {}).get(rel)
        try:
            if not f.is_file():
                return f"{rel} missing"
            if not want:
                if f.stat().st_size == 0:
                    return f"{rel} empty"
                continue
            data = f.read_bytes()
            if not data:
                return f"{rel} empty"
            if hashlib.sha256(data).hexdigest() != want:
                return f"{rel} altered"
        except OSError as exc:
            return f"{rel} unreadable ({exc})"
    return ""


def _meta_path() -> Path:
    return _cache_dir().with_name(_cache_dir().name + ".meta.json")


def _cache_meta() -> dict:
    """What we recorded about the cache at the swap that put it there: its
    version, its commit, and the manifest hashes we verified it against.  {} if
    absent or unreadable — a cache we know nothing about is checked by presence
    alone rather than being condemned."""
    try:
        meta = json.loads(_meta_path().read_text(encoding="utf-8"))
        return meta if isinstance(meta, dict) else {}
    except Exception:
        return {}


def _cache_hashes():
    """The manifest hashes for the CURRENT cache, or None.

    Only meaningful while the meta still describes what is on disk.  Recovery
    promotes engine.old into place and clears the meta for exactly this reason:
    checking a recovered engine against the discarded copy's hashes would
    condemn a perfectly good engine on every file."""
    files = _cache_meta().get("files")
    return files if isinstance(files, dict) and files else None


def _cached_version() -> int:
    """The version currently in the cache (0 if no cache / unreadable) — the
    floor for anti-rollback.  We persist it next to the cached engine."""
    # A cache that is missing a file it needs has no usable version: report 0
    # so anti-rollback lets the SAME manifest version be fetched again, and so
    # the ladder below prefers a complete bundled engine over a broken cache.
    if _engine_intact(_cache_dir(), _cache_hashes()):
        return 0
    try:
        return int(_cache_meta().get("version", 0))
    except (TypeError, ValueError):
        return 0


def _repair_marker() -> Path:
    """Written by the hub's "Repair and restart" button, read here.

    A tech whose engine has lost a file sees a page saying so, clicks one
    button, and the app restarts.  This is the half that makes the restart
    mean something: without it the launcher would find the same cache, decide
    it was the newest thing it had, and boot into the same failure."""
    return Path.home() / APP_DIR_NAME / "repair_requested"


def _honour_repair_request(cache: Path) -> bool:
    """Discard the cached engine when a repair was asked for.  The survivors
    are left alone on purpose: they are the offline fallback, and a tech who
    clicks Repair in a truck with no signal still has to get a working app."""
    marker = _repair_marker()
    if not marker.exists():
        return False
    try:
        marker.unlink()                  # once, not every boot from now on
    except OSError:
        pass
    _discard_cache(cache)
    print("auto-update: repair requested — cached engine discarded")
    return True


def _discard_cache(cache: Path) -> None:
    """Throw the cached engine and its meta away.  The survivors (.old/.prev)
    are left where they are: they are the offline fallback."""
    import shutil
    shutil.rmtree(cache, ignore_errors=True)
    try:
        _meta_path().unlink()            # no version left to block a re-fetch
    except OSError:
        pass


# ── A machine that keeps losing engine files ────────────────────────────
# One tech (sscot) lost a different engine .py out of ~/.otdrSuite/engine
# twice in three days, each time after a hash-verified download, while the
# same files in his install directory were never touched.  Something on that
# machine removes what this exe writes into the profile; nobody has been able
# to say what.  The Repair button re-downloads, the file goes again, and the
# tech is back on the same page: a loop with no exit that only IT could end.
#
# So the launcher keeps a short memory of losses.  A loss is the cache being
# found damaged at boot when it was intact the boot before, or the app
# reporting a file missing from an engine the launcher had just verified.
# The second loss inside CACHE_LOSS_WINDOW_DAYS pins this machine to the
# bundled engine: no fetch, no cache, the copy the installer put in place,
# which is the one thing that has survived on every such machine.  The pin
# is recorded against the exe build it was set under, so installing a newer
# build (the documented way out) clears it and the machine gets to try the
# cache again.  While pinned the hub shows a notice saying updates are not
# kept on this computer and how to get them (CACHE_PINNED_ENV carries it).
CACHE_LOSS_WINDOW_DAYS = 7
CACHE_LOSS_PIN_AFTER = 2
CACHE_PINNED_ENV = "OTDR_SUITE_CACHE_PINNED"     # read by app.py; keep in sync

# A signed manifest that names engine files this exe does not carry (a build
# that added modules) can never be applied here: ENGINE_FILES is frozen into
# the launcher, and no update replaces the launcher.  Only a fresh installer
# moves such a machine.  The launcher says so (NEEDS_INSTALL_ENV carries the
# reason) and publishes its own file list (ENGINE_FILES_ENV) so the hub can
# tell BEFORE it offers a restart that a restart would change nothing.
NEEDS_INSTALL_ENV = "OTDR_SUITE_NEEDS_INSTALL"  # read by app.py; keep in sync
ENGINE_FILES_ENV = "OTDR_SUITE_ENGINE_FILES"    # read by app.py; keep in sync


def _cache_health_path() -> Path:
    """Beside engine.meta.json, so it lives and dies with the cache dir."""
    return _cache_dir().with_name("cache_health.json")


def _read_cache_health() -> dict:
    try:
        d = json.loads(_cache_health_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_cache_health(health: dict) -> None:
    try:
        p = _cache_health_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(health), encoding="utf-8")
    except OSError:
        pass


def _mark_cache_ok(ok: bool) -> None:
    """Remember whether THIS boot ran from an intact cache.  A loss only
    counts against a cache that was known good, so one damaged cache that
    sits there boot after boot (no signal to re-fetch) is counted once."""
    health = _read_cache_health()
    health["cache_ok"] = bool(ok)
    _write_cache_health(health)


def _cache_ok_last_boot() -> bool:
    return bool(_read_cache_health().get("cache_ok"))


def _note_cache_loss(reason: str, now: float = None) -> int:
    """Record one loss; return how many fall inside the window."""
    now = time.time() if now is None else now
    health = _read_cache_health()
    window = CACHE_LOSS_WINDOW_DAYS * 86400
    losses = [t for t in health.get("losses", [])
              if isinstance(t, (int, float)) and 0 <= now - t < window]
    losses.append(now)
    health["losses"] = losses
    health["last_loss"] = reason
    health["cache_ok"] = False           # this damage is now accounted for
    _write_cache_health(health)
    return len(losses)


def _pin_cache(reason: str, now: float = None) -> None:
    health = _read_cache_health()
    health["pinned"] = {
        "since": time.time() if now is None else now,
        "bundled_build": _bundled_build(),
        "reason": reason,
    }
    health["cache_ok"] = False
    _write_cache_health(health)


def _cache_pin() -> str:
    """'' when the cache is trusted on this machine, else why it is not.

    A pin belongs to the exe build it was set under.  A different bundled
    build means the tech installed a new version, which is the only thing we
    ask of them, so the pin and the loss history are cleared and the cache
    gets another chance under the new exe."""
    health = _read_cache_health()
    pin = health.get("pinned")
    if not isinstance(pin, dict):
        return ""
    if pin.get("bundled_build") != _bundled_build():
        health.pop("pinned", None)
        health["losses"] = []
        _write_cache_health(health)
        print("auto-update: cache pin cleared (a different build was installed)")
        return ""
    return str(pin.get("reason") or "engine files keep disappearing from the cache")


def _try_auto_update(staging: Path):
    """Fetch + VERIFY a signed update into `staging`.  Returns the manifest dict
    on full success (signature ok, every file's SHA-256 matches), else None — in
    which case the caller discards `staging` and keeps the current engine.  This
    function NEVER writes an unverified file into the run path: files land in the
    throwaway staging dir and are only promoted by the verified swap upstream."""
    import shutil
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)

    # 1. manifest + detached signature
    manifest_bytes = _fetch(MANIFEST_URL)
    if manifest_bytes is None:
        print("auto-update: manifest fetch failed")
        return None
    sig = _fetch(MANIFEST_SIG_URL)
    if sig is None:
        print("auto-update: signature fetch failed")
        return None

    # 2. verify signature over the EXACT manifest bytes BEFORE trusting anything
    if not _verify_manifest_signature(manifest_bytes, sig):
        return None
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        files = manifest["files"]              # {rel_path: sha256_hex}
        version = int(manifest["version"])
    except (ValueError, KeyError, TypeError) as exc:
        print(f"auto-update: manifest malformed ({exc}) — rejecting")
        return None
    # A signed manifest for ANOTHER edition is not for this exe: main's and
    # the App's are signed with the same key, and either one would replace
    # this edition's screens with the other's.  The feed URL keeps them apart;
    # this keeps them apart if a URL is ever wrong.
    channel = manifest.get("channel", "") if isinstance(manifest, dict) else ""
    if channel != UPDATE_CHANNEL:
        print(f"auto-update: manifest is for channel {channel or 'main'!r}, "
              f"this is {UPDATE_CHANNEL!r} — rejecting")
        return None

    # 3. the signed manifest must cover EXACTLY the files this exe runs.  The
    #    signature has already passed, so a different set is not tampering:
    #    it is a build that added (or dropped) engine files, which this
    #    launcher cannot carry.  Say so where it can be seen.  Build 476 sat
    #    under a "needs a restart" banner for manifest 658 and its 13 new
    #    files; the only record was one line in a log nobody reads, and
    #    every restart the tech tried changed nothing.
    reason = _install_needed(version, files)
    if reason:
        print(f"auto-update: {reason} — rejecting")
        os.environ[NEEDS_INSTALL_ENV] = reason
        _report_install_needed(reason)
        return None

    # 4. fetch each file into staging and check its SHA-256 against the manifest
    staging.mkdir(parents=True, exist_ok=True)
    # Pin to the manifest's own commit so a merge landing mid-update cannot
    # invalidate the hashes we are checking against (see RAW_REF_URL_FMT).
    commit = str(manifest.get("commit") or "")
    ref = commit if _SHA_RE.match(commit) else GH_BRANCH
    if ref == GH_BRANCH:
        print("auto-update: manifest carries no commit — fetching at branch tip")
    for rel in ENGINE_FILES:
        data = _fetch(RAW_REF_URL_FMT.format(ref=ref, path=rel))
        if data is None:
            print(f"auto-update: fetch failed for {rel}")
            return None
        digest = hashlib.sha256(data).hexdigest()
        if digest != files[rel]:
            print(f"auto-update: SHA-256 mismatch for {rel} — rejecting update")
            return None
        target = staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    manifest["__version_int"] = version
    return manifest


def _install_needed(version, manifest_files) -> str:
    """'' when this exe can carry the manifest, else why only an installer
    can: the manifest names engine files ENGINE_FILES does not (a build that
    added modules), or drops ones this exe runs."""
    added = sorted(set(manifest_files) - set(ENGINE_FILES))
    dropped = sorted(set(ENGINE_FILES) - set(manifest_files))
    if not added and not dropped:
        return ""
    what = []
    if added:
        shown = ", ".join(added[:3]) + (", ..." if len(added) > 3 else "")
        what.append(f"adds {len(added)} engine file(s) this build does not "
                    f"carry ({shown})")
    if dropped:
        what.append(f"drops {len(dropped)} this build runs")
    return (f"update {version} needs a fresh install (app build "
            f"{_bundled_build()}): it {' and '.join(what)}")


def _report_install_needed(reason: str):
    """Tell the shared channel this machine can only move by installer.

    Not the error header: the Slack->issues bridge files everything that
    starts ':rotating_light: *OTDR Suite error*' as an issue, and one post per
    tech per build is not an issue, it is the rollout list.  Deduped on the
    exe build via a marker: a machine reports once however many builds go by
    until it is reinstalled, then once more if it happens again.  Never
    raises; no webhook -> silent."""
    try:
        build = _bundled_build()
        marker = Path.home() / APP_DIR_NAME / "update_install_needed.json"
        try:
            last = json.loads(marker.read_text(encoding="utf-8")).get("build")
        except Exception:
            last = None
        if last == build:
            return
        try:
            who = "%s / %s" % (socket.gethostname(), __import__("getpass").getuser())
        except Exception:
            who = "?"
        _post_slack(
            ":arrow_down: *OTDR Suite needs a fresh install* — %s\n"
            "%s\n"
            "Update & restart cannot apply this one; the tech needs the "
            "installer." % (who, reason))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"build": build}), encoding="utf-8")
    except Exception:
        pass


def _report_update_stuck(reason: str):
    """Tell the shared Slack channel when auto-update could not land.

    Until now every failure here was a bare print() into a log nobody reads
    from a windowed exe.  That is how a tech ran engine 139 against a fleet on
    264 for three days: the app knew, said so locally, and nothing left the
    machine.  The rollout ping already reports the good case, so the bad case
    arriving in the same channel is what makes a stuck machine visible.

    Deduped on the reason via a marker so a machine that is stuck for a week
    reports once, not once per boot.  Never raises; no webhook -> silent."""
    try:
        marker = Path.home() / APP_DIR_NAME / "update_stuck.json"
        try:
            last = json.loads(marker.read_text(encoding="utf-8")).get("reason")
        except Exception:
            last = None
        if last == reason:
            return
        try:
            who = "%s / %s" % (socket.gethostname(), __import__("getpass").getuser())
        except Exception:
            who = "?"
        _post_slack(
            ":rotating_light: *OTDR Suite error* — auto-update stuck\n"
            "*%s*\ntech: `%s`  |  app: %s" % (reason, who, _bundled_build()))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"reason": reason}), encoding="utf-8")
    except Exception:
        pass


def _bundled_build() -> int:
    """CI run number of the engine frozen into THIS exe (0 when unknown).
    Comparable with a manifest version: a build-N exe bundles engine N."""
    try:
        return int(json.loads((bundled_dir() / "version.json")
                              .read_text(encoding="utf-8"))["build"])
    except Exception:
        return 0


def _recover_cache(cache: Path) -> str:
    """Put a lost engine cache back before anything else touches it.

    THE BUG THIS EXISTS FOR.  The swap renames cache -> engine.old, then
    staging -> cache.  On Windows a directory rename fails with PermissionError
    while ANY file inside is held open — antivirus scanning 21 files that were
    just downloaded is the ordinary case — so the second rename can fail with
    the first already done, leaving no cache at all.  The restore was best
    effort and swallowed by `except: pass`, and the fallback ladder then went
    straight to bundled.

    A tech hit exactly this: engine "update 226 applied" became "bundled",
    dropping 87 engines in one click, even though a verified copy was sitting
    in engine.old.  Clicking Update again opened the next swap with
    `rmtree(old)`, destroying that copy too, and engine.prev — written on every
    successful swap and labelled a "rollback reference" — was never read by
    anything at all.

    So: before any update attempt, if the cache is missing and either survivor
    is present, move it back.  Returns a short note for the log, '' when the
    cache was already fine.

    INCOMPLETE COUNTS AS LOST (see _engine_intact).  A cache that kept app.py
    but lost a file app.py imports used to pass every check here and get run
    anyway, which is how one tech booted into ModuleNotFoundError on a build
    the rest of the fleet ran fine.  A complete survivor now replaces a
    half-eaten cache the same way it replaces a missing one.
    """
    import shutil
    problem = _engine_intact(cache, _cache_hashes())
    if not problem:
        return ""
    print(f"auto-update: engine cache unusable ({problem})")
    for name in (".old", ".prev"):
        survivor = cache.with_name(cache.name + name)
        if _engine_intact(survivor):
            continue
        try:
            if cache.exists():
                shutil.rmtree(cache, ignore_errors=True)
            survivor.rename(cache)
            # The meta describes the copy we just discarded, not this one: its
            # version would block a re-fetch and its hashes would condemn every
            # file here.  Drop it — an engine of unknown version reads as 0,
            # which is the honest answer and lets any update land.
            try:
                _meta_path().unlink()
            except OSError:
                pass
            print(f"auto-update: recovered engine cache from {survivor.name}")
            return f"recovered cache from {survivor.name}"
        except Exception as exc:
            # Still locked.  Run FROM the survivor rather than falling all the
            # way back to bundled — it is verified code, just not in place.
            print(f"auto-update: cache recovery from {survivor.name} failed ({exc})")
    return ""


def _prepare_engine():
    """Decide which engine source to run.  Returns (engine_dir, source_label).
    verified-latest → cached (last good) → bundled.  FAILS CLOSED to bundled
    when no signing key is provisioned (no unverified fetch ever runs)."""
    import shutil
    # What this exe can carry, for the hub's update banner.  A fact about the
    # launcher, not about the engine chosen below, so every path exports it.
    os.environ[ENGINE_FILES_ENV] = json.dumps(ENGINE_FILES)
    # Escape hatch: OTDR_SUITE_NO_UPDATE pins the bundled build (air-gapped /
    # offline sites, or to run exactly what shipped without a network fetch).
    if os.environ.get("OTDR_SUITE_NO_UPDATE"):
        print("auto-update: disabled via OTDR_SUITE_NO_UPDATE — using bundled")
        return bundled_dir(), "bundled (auto-update disabled)"

    # FAIL CLOSED: with no real signing key baked in we do NOT fetch any code.
    # Use the last verified cache if one exists from a prior signed build,
    # otherwise the bundled engine.  We never fall back to an unverified fetch.
    if not update_signing_configured():
        print("auto-update: no update-signing key provisioned — DISABLED (fail closed)")
        cache = _cache_dir()
        if not _engine_intact(cache, _cache_hashes()):
            return cache, "cached (last verified update; auto-update disabled)"
        return bundled_dir(), "bundled (auto-update disabled — no signing key)"

    cache = _cache_dir()
    staging = cache.with_name(cache.name + ".staging")
    meta = cache.with_name(cache.name + ".meta.json")
    pinned = _cache_pin()
    if pinned:
        bundled_problem = _engine_intact(bundled_dir())
        if not bundled_problem:
            _honour_repair_request(cache)    # a stale Repair click must not outlive the pin
            os.environ[CACHE_PINNED_ENV] = pinned
            print(f"auto-update: cache pinned to bundled on this machine ({pinned})")
            return bundled_dir(), f"bundled (cache pinned: {pinned})"
        # A pin cannot be honoured on a damaged install.  The ladder below is
        # still the best this machine has, so fall through to it.
        print(f"auto-update: cache pinned but bundled engine damaged "
              f"({bundled_problem}); using the normal ladder")
    repairing = _honour_repair_request(cache)
    broken = _engine_intact(cache, _cache_hashes()) if cache.exists() else ""
    _recover_cache(cache)            # BEFORE the swap's rmtree(old) eats it
    if broken and not repairing:
        # A cache that LOST a file after a hash-verified download means
        # something on that machine is deleting our code — antivirus, normally.
        # The ladder below heals it, but silence is what turned the last one
        # into a field call: the app knew, and only the local log said so.
        _report_update_stuck(f"engine cache incomplete: {broken}")
    lost = ""
    if _cache_ok_last_boot():
        if repairing:
            lost = "the app found an engine file missing after a verified download"
        elif broken:
            lost = f"engine cache damaged since the last boot ({broken})"
    if lost:
        n = _note_cache_loss(lost)
        if n >= CACHE_LOSS_PIN_AFTER and not _engine_intact(bundled_dir()):
            reason = (f"engine files disappeared from the cache {n} times in "
                      f"{CACHE_LOSS_WINDOW_DAYS} days")
            _pin_cache(reason)
            _report_update_stuck(f"cache pinned to bundled: {reason}. "
                                 "Install the newest version to get updates.")
            os.environ[CACHE_PINNED_ENV] = reason
            print(f"auto-update: {reason}; this machine now runs bundled")
            return bundled_dir(), f"bundled (cache pinned: {reason})"
    _mark_cache_ok(False)            # set back to True below only if the cache runs
    print(f"auto-update: fetching signed update {GH_OWNER}/{GH_REPO} "
          f"feed {UPDATE_FEED_TAG} ...")
    manifest = _try_auto_update(staging)
    if manifest is not None:
        new_version = manifest["__version_int"]
        cur_version = _cached_version()
        # 5. ANTI-ROLLBACK: never swap in an older-or-equal version.  Blocks a
        #    replayed/poisoned older signed manifest from downgrading the fleet.
        if new_version <= cur_version:
            print(f"auto-update: version {new_version} <= cached {cur_version} "
                  "— refusing (anti-rollback)")
            shutil.rmtree(staging, ignore_errors=True)
        else:
            # ATOMIC swap with anti-rollback safety: keep the prior cache as
            # engine.prev, move staging into place by rename, only delete the
            # prior copy on success, and restore it if the rename fails.
            prev = cache.with_name(cache.name + ".prev")
            old  = cache.with_name(cache.name + ".old")
            try:
                shutil.rmtree(old, ignore_errors=True)
                if cache.exists():
                    cache.rename(old)              # cache -> cache.old
                staging.rename(cache)              # staging -> cache  (atomic)
                # Promote the displaced copy to engine.prev (rollback reference).
                shutil.rmtree(prev, ignore_errors=True)
                if old.exists():
                    old.rename(prev)               # cache.old -> cache.prev
                meta.write_text(json.dumps({
                    "version": new_version,
                    "commit": manifest.get("commit", ""),
                    # The hashes this engine was verified against at download.
                    # Boot re-checks them, which is how a file that is changed
                    # rather than deleted stops looking like a healthy engine.
                    "files": manifest["files"],
                }), encoding="utf-8")
                print(f"auto-update: ok — verified v{new_version} → using {cache}")
                _mark_cache_ok(True)
                return cache, f"latest (verified update v{new_version})"
            except Exception as exc:
                # Swap failed mid-flight — put the prior cache back.  Shared
                # with the boot path so both routes recover the same way.
                print(f"auto-update: swap failed ({exc}); restoring previous cache")
                shutil.rmtree(staging, ignore_errors=True)
                _recover_cache(cache)
                _report_update_stuck(f"swap failed: {type(exc).__name__}: {exc}")
    # Verification/fetch failed or version not newer — use the last verified
    # cache if present, else a surviving copy of one, else bundled.  Never an
    # unverified fetch.
    _recover_cache(cache)
    # A FRESH INSTALLER SHIPS A NEWER ENGINE THAN A STALE CACHE.  ~/.otdrSuite
    # survives an uninstall — the .iss touches only the program files — so after
    # a reinstall the cache is usually OLDER than what the new exe bundles.
    # Preferring the cache unconditionally would silently keep a rescued machine
    # on the very engine the reinstall was meant to escape, which is the whole
    # point of handing a stuck tech a new installer.  Both numbers are CI run
    # numbers (a build-N exe bundles engine N), so they compare directly.
    bundled_v, cached_v = _bundled_build(), _cached_version()
    # The last rung was the one thing never checked.  If whatever ate a file
    # out of the cache also ate one out of the install directory, preferring
    # bundled here would hand the tech a second unbootable engine — and this
    # one no update can repair, because we do not fetch into the install.
    bundled_problem = _engine_intact(bundled_dir())
    if bundled_problem:
        _report_update_stuck(f"bundled engine damaged: {bundled_problem}")
        print(f"auto-update: bundled engine damaged ({bundled_problem})")
    if bundled_v and bundled_v >= cached_v and not bundled_problem:
        print(f"auto-update: bundled engine {bundled_v} >= cached {cached_v} "
              "— using bundled")
        return bundled_dir(), f"bundled (newer than cached {cached_v})"
    if not _engine_intact(cache, _cache_hashes()):
        print(f"auto-update: keeping verified cache {cache}")
        _mark_cache_ok(True)
        return cache, "cached (last verified update)"
    # The cache could not be put back (still locked), but a verified copy
    # survives.  Run FROM it: it is signed code that passed every hash check,
    # and the alternative is dropping the tech to whatever their installer
    # bundled — which cost one tech 87 engines.
    for name in (".old", ".prev"):
        survivor = cache.with_name(cache.name + name)
        if not _engine_intact(survivor):
            print(f"auto-update: cache unavailable — running from {survivor.name}")
            return survivor, "cached (previous verified update)"
    # Nothing verified anywhere.  This is the state that let a tech run 87
    # engines behind for days with no signal, so it does NOT stay a print().
    _report_update_stuck("no verified engine — fell back to bundled")
    print("auto-update: no cache — using bundled copies")
    return bundled_dir(), "bundled (offline)"


# ── Error-report webhook (build-time only; never committed) ──────────────
def _load_webhook():
    """Read the bundled _webhook.cfg (written by CI from the SLACK_ERROR_WEBHOOK
    secret) into env SS_ERROR_WEBHOOK so error_report can post.  Also tags the
    build source.  No-op if absent (dev / not configured).  Never raises."""
    try:
        os.environ.setdefault("OTDR_SUITE_SOURCE",
                              "bundled .exe" if getattr(sys, "frozen", False) else "dev")
        p = bundled_dir() / "_webhook.cfg"
        if p.exists():
            url = p.read_text(encoding="utf-8").strip()
            if url:
                os.environ["SS_ERROR_WEBHOOK"] = url
                return url
    except Exception:
        pass
    return None


def _post_slack(text):
    """Fire-and-forget Slack post for LAUNCHER-side (won't-boot) failures — the
    silent class the engine's report_error never gets to handle.  Never raises."""
    url = os.environ.get("SS_ERROR_WEBHOOK")
    if not url:
        return
    try:
        import json as _json
        import urllib.request
        req = urllib.request.Request(
            url, data=_json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=4, context=_tls_context())
    except Exception:
        pass


def _export_edition() -> None:
    """Hand the edition to the hub and the engine subprocesses: the app folder
    they keep settings/cache/markers in, the update feed the hub's banner and
    Check for Updates read (the same one this launcher applies), and
    (AUTO_UPDATE False) the switch that pins the bundled engine and hides the
    update banner."""
    os.environ["OTDR_SUITE_APP_DIR"] = str(Path.home() / APP_DIR_NAME)
    os.environ["OTDR_SUITE_EDITION"] = EDITION
    os.environ["OTDR_SUITE_MANIFEST_URL"] = MANIFEST_URL
    os.environ["OTDR_SUITE_UPDATE_CHANNEL"] = UPDATE_CHANNEL
    os.environ["OTDR_SUITE_INSTALLER_URL"] = INSTALLER_URL
    if TOOLBAR_MODE:
        os.environ["STREAMLIT_CLIENT_TOOLBAR_MODE"] = TOOLBAR_MODE
    if not AUTO_UPDATE:
        os.environ["OTDR_SUITE_NO_UPDATE"] = "1"


# ── Engine subprocess dispatch (must run BEFORE anything Streamlit) ───────
def _maybe_run_engine() -> bool:
    """If invoked with --run-secretsauce / --run-splicereport, dispatch to that
    engine's runner in this clean process (its own sor_reader copy) and exit."""
    specs = [("--run-secretsauce", "secretsauce", "run_secretsauce"),
             ("--run-splicereport", "splicereport", "run_splicereport")]
    for sentinel, subdir, module in specs:
        if sentinel not in sys.argv:
            continue
        # Use the SAME engine source the parent hub chose (it exported
        # OTDR_SUITE_HOME = the validated update dir, or the bundle).  Put it +
        # the engine subdir on path so the runner imports the matching code and
        # error_report; load the webhook so subprocess errors can report.
        root = Path(os.environ.get("OTDR_SUITE_HOME") or bundled_dir())
        sys.path.insert(0, str(root))
        sys.path.insert(0, str(root / subdir))
        _load_webhook()
        sys.argv = [a for a in sys.argv if a != sentinel]
        runner = __import__(module)
        runner.main()
        return True
    return False


# ── stdout/stderr → log file ────────────────────────────────────────────
def _redirect_output_to_log() -> Path:
    log_dir = Path.home() / APP_DIR_NAME
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{APP_NAME.lower()}.log"
    fh = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
    sys.stdout = fh
    sys.stderr = fh
    print(f"\n=== {APP_NAME} launch {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    print(f"frozen={getattr(sys, 'frozen', False)}  exe={sys.executable}")
    return log_path


# ── Silence Streamlit first-run prompt + headless env ───────────────────
def _silence_first_run_prompt() -> None:
    cred_dir = Path.home() / ".streamlit"
    cred_dir.mkdir(parents=True, exist_ok=True)
    cred_path = cred_dir / "credentials.toml"
    if not cred_path.exists():
        cred_path.write_text('[general]\nemail = ""\n', encoding="utf-8")
    os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
    os.environ.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    os.environ.setdefault("STREAMLIT_GLOBAL_DEVELOPMENT_MODE", "false")
    os.environ.setdefault("STREAMLIT_SERVER_ADDRESS", HOST)
    os.environ.setdefault("STREAMLIT_SERVER_PORT", str(PORT))
    # No developer toolbar ("Deploy" button) in the hub's header.
    os.environ.setdefault("STREAMLIT_CLIENT_TOOLBAR_MODE", "viewer")
    # Light theme to match the viewer (per-process so it doesn't touch the
    # tech's other Streamlit apps via a global config).
    os.environ.setdefault("STREAMLIT_THEME_BASE", "light")
    os.environ.setdefault("STREAMLIT_THEME_PRIMARY_COLOR", "#2c5b8a")
    os.environ.setdefault("STREAMLIT_THEME_BACKGROUND_COLOR", "#ffffff")
    os.environ.setdefault("STREAMLIT_THEME_SECONDARY_BACKGROUND_COLOR", "#eef3f8")
    # Black lettering, as .streamlit/config.toml has had since 2026-09-22
    # ("the grey-blue read badly"); the exe reads this, not that file.
    os.environ.setdefault("STREAMLIT_THEME_TEXT_COLOR", "#000000")
    # Windows' own font (2026-09-24), matching .streamlit/config.toml.
    os.environ.setdefault("STREAMLIT_THEME_FONT", "Segoe UI, sans-serif")
    # NOTE: OTDR_SUITE_HOME is set in main() AFTER _prepare_engine() chooses the
    # engine source (updated cache vs bundled), so the hub + subprocess load the
    # same code.


# ── Health poll + browser opener ────────────────────────────────────────
# ── Update & restart: the exe relaunches ITSELF ──────────────────────────
# The hub's "Update & restart now" button starts a second copy of this exe
# with RESTART_ENV set to the running hub's pid, then exits.  The new copy
# lands here, and its first job is to WAIT for the old server to stop
# answering on the port.  That wait is the whole point: the already-serving
# guard in main() runs BEFORE the signed-update path, so a new instance that
# health-checks while the dying one still answers prints "Another instance is
# already serving", opens a tab and quits — the click looks like it worked
# and the update never applies.
#
# This used to be done by a detached PowerShell (Windows) / sh+curl (POSIX)
# helper that polled the port and started the exe once it went quiet.  The
# Windows helper was never seen to work on a tech's machine: the boss reported
# that Update did not restart the app.  A hidden PowerShell started with no
# console from inside a windowed exe has several ways to die quietly (host
# output with no console, Application Control, an AV that dislikes hidden
# PowerShell) and none of them leave a trace we can read.  The exe itself has
# none of those problems: it is the signed binary IT already trusts, and it
# is the process that is starting anyway.
RESTART_ENV = "OTDR_SUITE_RESTART_FROM"      # set by app.py; keep in sync
RESTART_DRAIN_S = 30                         # the old server exits within ~1 s
RESTART_POLL_S = 0.4


def _restart_blocked_marker() -> Path:
    """Left behind when the old instance never let go — app.py turns it into
    a visible 'close it completely (or reboot)' message at the next boot."""
    return Path.home() / APP_DIR_NAME / "update_restart_blocked"


def _drain_old_instance(health_ok=None, deadline_s=RESTART_DRAIN_S,
                        clock=time.time, sleep=time.sleep) -> bool:
    """Wait for the instance that launched us to stop serving.  Returns True
    the moment the health endpoint stops answering (the normal case, ~1 s);
    False, with the blocked marker written, if it is still answering at the
    deadline — the caller then boots normally, finds the server alive, and
    opens a tab, which is honest: nothing was updated and the hub says so."""
    health_ok = health_ok or _health_ok
    end = clock() + deadline_s
    while True:
        if not health_ok():
            print("restart: the previous instance has stopped serving")
            return True
        if clock() >= end:
            break
        sleep(RESTART_POLL_S)
    print(f"restart: the previous instance still answers after {deadline_s}s "
          "— leaving the blocked marker")
    try:
        marker = _restart_blocked_marker()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("blocked", encoding="utf-8")
    except OSError as exc:
        print(f"restart: could not write the blocked marker ({exc})")
    return False


def _health_ok() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2) as resp:
            return resp.status == 200 and resp.read().strip() == b"ok"
    except (urllib.error.URLError, socket.timeout, ConnectionError):
        return False


def _open_browser_when_ready() -> None:
    deadline = time.time() + 90
    while time.time() < deadline:
        if _health_ok():
            _show_app()
            return
        time.sleep(0.5)
    print("browser opener: server never returned ok within 90s")


# ── The app window: the hub in its own window, not a browser tab ─────────
# The server is unchanged: it still runs in the main process on PORT exactly
# as before.  What changed is what the tech SEES.  Instead of a tab in their
# browser, the launcher starts a second copy of this exe with WINDOW_ARG, and
# that copy shows APP_URL in a native window (pywebview over Edge WebView2 on
# Windows) with no address bar and no tabs, so the app looks like an app.
#
# The window lives in its own process on purpose.  pywebview has to own the
# main thread, and so does Streamlit's server; splitting them keeps every
# server-side rule above (boot lock, update, restart drain, replace-older)
# byte for byte the same.  It also means the window can fail without taking
# the server down: when it cannot open (no WebView2 runtime, pywebview missing
# from a dev install, a crash at start) the tech gets the browser tab they
# always had.  BROWSER_ENV=1 forces the tab, for tests and as a field escape.
#
# ONE window.  A second launch while a window is up raises that window instead
# of opening another (a file the window watches), and the Update & restart
# relaunch reuses it too: Streamlit reconnects the open page to the new server
# on the same port by itself.  Closing the window QUITS the app: the server
# (and any report still running under it) is stopped.  In the browser-tab
# fallback, closing the tab still leaves the server running, as it always did.
WINDOW_ARG = "--app-window"
BROWSER_ENV = "OTDR_SUITE_BROWSER"
WINDOW_TITLE = EDITION
WINDOW_START_S = 20            # a cold WebView2 start on a slow laptop is ~5 s
WINDOW_RAISE_POLL_S = 0.5


def _window_lock_path() -> Path:
    return Path.home() / APP_DIR_NAME / "window.lock"


def _window_ready_path() -> Path:
    return Path.home() / APP_DIR_NAME / "window.ready"


def _window_raise_path() -> Path:
    return Path.home() / APP_DIR_NAME / "window.raise"


def _window_command() -> list:
    if getattr(sys, "frozen", False):
        return [sys.executable, WINDOW_ARG]
    return [sys.executable, str(Path(__file__).resolve()), WINDOW_ARG]


def _let_the_window_take_focus() -> None:
    """Windows gives the foreground only to the process the tech is working
    in: this launch, which they just double-clicked.  Hand that right on, so
    the window can take the keyboard and not only the top of the pile."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(-1)    # ASFW_ANY
    except Exception as exc:
        print(f"app window: could not pass on the foreground ({exc})")


def _ask_open_window_to_raise() -> bool:
    """True when a window is already open (and has been asked to come
    forward).  Its lock is held for the window's whole life."""
    fh = _lock_file(_window_lock_path())
    if fh is None:
        _let_the_window_take_focus()
        try:
            _window_raise_path().write_text(str(time.time()), encoding="utf-8")
        except OSError as exc:
            print(f"app window: could not ask it to come forward ({exc})")
        return True
    if fh is not True:
        fh.close()               # nobody had it: release, the child takes it
    return False


def _spawn_window() -> bool:
    """Start the window process and wait for it to show the page.  False
    means it could not, and the caller opens a browser tab instead."""
    import subprocess
    ready = _window_ready_path()
    try:
        ready.unlink()
    except OSError:
        pass
    try:
        proc = subprocess.Popen(_window_command(), close_fds=True)
    except Exception as exc:
        print(f"app window: could not start ({exc})")
        return False
    end = time.time() + WINDOW_START_S
    while time.time() < end:
        code = proc.poll()
        if code is not None:
            # 0 = another window won the race and was raised instead.
            print(f"app window: process exited with {code}")
            return code == 0
        try:
            if ready.read_text(encoding="utf-8").strip() == str(proc.pid):
                return True
        except OSError:
            pass
        time.sleep(0.25)
    print(f"app window: not shown after {WINDOW_START_S}s, still running")
    return True


def _show_app() -> None:
    """Put the hub in front of the tech: its own window, or a browser tab
    when the window cannot open."""
    if os.environ.get(BROWSER_ENV) != "1":
        if _ask_open_window_to_raise() or _spawn_window():
            return
        print("app window: falling back to a browser tab")
    try:
        webbrowser.open(APP_URL)
    except Exception as exc:
        print(f"webbrowser.open failed: {exc}")


def _is_own_url(url: str) -> bool:
    """A page this app serves: the hub on PORT, and just as much the Viewer's
    trace server and Field Capture's, which pick their own ports (8771 and
    up, 8781 and up).  The first cut allowed PORT only, and the Viewer
    pop-out (http://127.0.0.1:8771/) opened in Edge on the VM test of
    2026-09-27.  So: anything on this PC's loopback address.  about:blank is
    window.open('', name) looking for one of our windows by name."""
    if url == "about:blank":
        return True
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and host in ("127.0.0.1", "localhost", "::1")


def _let_the_viewer_pop_out() -> None:
    """The Viewer's pop-out and its "Back to report" are window.open() calls
    that keep a handle to the other window (named windows + postMessage drive
    the live-updating Viewer).  pywebview answers EVERY window.open by sending
    it to the system browser, which would put the Viewer in a browser and cut
    the handle.  For our own pages, leave the request unhandled so WebView2
    opens its own popup window with the opener kept; anything else still goes
    to the browser.  Pinned to the pywebview version in requirements.
    (Off Windows the import fails and the default stands: dev box only.)"""
    try:
        from webview.platforms import edgechromium
    except Exception as exc:
        print(f"app window: pop-out hook unavailable ({exc})")
        return
    original = edgechromium.EdgeChrome.on_new_window_request

    def on_new_window_request(self, sender, args):
        if _is_own_url(str(args.get_Uri())):
            return                       # unhandled: WebView2 makes the popup
        return original(self, sender, args)

    edgechromium.EdgeChrome.on_new_window_request = on_new_window_request


# Win32, for _bring_forward (ctypes releases the GIL; every call is async).
SW_RESTORE = 9
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW, SWP_ASYNCWINDOWPOS = 0x1, 0x2, 0x40, 0x4000


def _top_windows_of(pid: int, title: str = None) -> list:
    """The visible top-level windows of process `pid`, only those called
    `title` when one is given.  InternalGetWindowText, not GetWindowText: it
    reads the title without sending the window a message."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            if title is None:
                found.append(hwnd)
            else:
                buf = ctypes.create_unicode_buffer(256)
                user32.InternalGetWindowText(hwnd, buf, 256)
                if buf.value == title:
                    found.append(hwnd)
        return True

    user32.EnumWindows(each, 0)
    return found


def _own_top_window(title: str):
    """This process's visible top-level window called `title` (the form
    pywebview made), or None."""
    found = _top_windows_of(os.getpid(), title)
    return found[0] if found else None


# ── The App's WebView2 goes with it ─────────────────────────────────────
# The Viewer pop-outs are WebView2's OWN popup windows (see
# _let_the_viewer_pop_out): they belong to the WebView2 browser process that
# this window process started, not to this process.  Closing the App left
# that browser process running with both pop-outs still open, pointed at a
# server that had just been stopped (VM test, 2026-09-28).  So the close
# path closes them the way their X does, and ends whatever of that WebView2
# tree is still there after a moment.  Only msedgewebview2.exe processes
# started by this process: the tech's Edge (msedge.exe) and other apps'
# WebView2 (Windows Search, Copilot, ...) are never touched.
WEBVIEW2_EXE = "msedgewebview2.exe"
WM_CLOSE = 0x0010
WEBVIEW2_CLOSE_WAIT_S = 3.0


def _post_close(hwnd) -> None:
    import ctypes
    ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def _close_own_webview() -> None:
    if os.name != "nt":
        return
    import subprocess
    try:
        table = _process_table()
    except Exception as exc:
        print(f"app window: could not list processes ({exc})")
        return
    me = os.getpid()
    roots = [p for p, (parent, name) in table.items()
             if parent == me and name == WEBVIEW2_EXE]
    if not roots:
        return
    closed = 0
    for root in roots:
        for hwnd in _top_windows_of(root):
            _post_close(hwnd)
            closed += 1
    print(f"app window: closing {closed} pop-out window(s)")
    end = time.time() + WEBVIEW2_CLOSE_WAIT_S
    while time.time() < end:
        time.sleep(0.2)
        try:
            table = _process_table()
        except Exception:
            break
        if not any(table.get(r, (None, ""))[1] == WEBVIEW2_EXE for r in roots):
            return
    for root in roots:
        if table.get(root, (None, ""))[1] != WEBVIEW2_EXE:
            continue
        for p in _own_process_tree(root, table):
            subprocess.run(["taskkill", "/PID", str(p), "/F"],
                           capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print("app window: ended the rest of its WebView2")


def _bring_forward(window) -> None:
    """Bring the window in front the way its taskbar button does.  On Windows
    with plain Win32 calls, not pywebview's: its restore() sets the form to
    Normal, which UN-MAXIMIZED the maximized window on every raise (VM test,
    2026-09-27).  SW_RESTORE only on a minimized window, which puts it back
    the way it was, maximized included; any other window keeps its size.
    Then a topmost on/off pulse, posted asynchronously so this thread never
    waits on the window's, and the foreground (the launch that asked for the
    raise allowed it, see _ask_open_window_to_raise)."""
    if os.name != "nt":
        window.restore()                  # dev box only
        window.show()
        return
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    hwnd = _own_top_window(WINDOW_TITLE)
    if not hwnd:
        print("app window: raise: window not found")
        return
    if user32.IsIconic(hwnd):
        user32.ShowWindowAsync(hwnd, SW_RESTORE)
    set_pos = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT)(("SetWindowPos", user32))
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW | SWP_ASYNCWINDOWPOS
    set_pos(hwnd, wintypes.HWND(HWND_TOPMOST), 0, 0, 0, 0, flags)
    set_pos(hwnd, wintypes.HWND(HWND_NOTOPMOST), 0, 0, 0, 0, flags)
    user32.SetForegroundWindow(hwnd)


def _reload_page(window) -> None:
    """A fresh run of the hub page: what claims a double-clicked .zfc/.zdb.
    NOT load_url(APP_URL): pywebview sets WebView2's Source, and the URL it
    already shows is no navigation at all, so on the VM test the request sat
    unclaimed.  The reload is deferred a tick so evaluate_js has its answer
    before the page goes away."""
    window.evaluate_js("setTimeout(function () { window.location.reload(); }, 50); 0")


# Set when the window closes.  pywebview runs _watch_for_raise on an ordinary
# (non-daemon) thread, and Python waits for those before a process can exit:
# without this the window process outlived its own window forever.  The old
# `taskkill /T` hid that by killing the window along with the server (CI run
# 36380438454 caught it once _stop_pid stopped killing whole trees).
_WINDOW_CLOSED = threading.Event()


def _watch_for_raise(window) -> None:
    path = _window_raise_path()
    try:
        seen = path.stat().st_mtime
    except OSError:
        seen = 0.0
    while not _WINDOW_CLOSED.is_set():
        time.sleep(WINDOW_RAISE_POLL_S)
        if _WINDOW_CLOSED.is_set():
            return
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        if stamp == seen:
            continue
        seen = stamp
        waiting = _open_request_path().exists()
        print(f"app window: raise{' + reload for a double-clicked file' if waiting else ''}")
        if waiting:
            try:
                _reload_page(window)
            except Exception as exc:
                print(f"app window: reload failed ({exc})")
        try:
            _bring_forward(window)
        except Exception as exc:
            print(f"app window: raise failed ({exc})")


def _run_window() -> int:
    import webview

    webview.settings["ALLOW_DOWNLOADS"] = True    # Save As, starts in Downloads
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    _let_the_viewer_pop_out()

    window = webview.create_window(
        WINDOW_TITLE, APP_URL, width=1400, height=900, min_size=(900, 600),
        maximized=True,
        text_select=True,        # pywebview blocks selection by default
        zoomable=True)

    def _loaded():
        try:
            _window_ready_path().write_text(str(os.getpid()), encoding="utf-8")
        except OSError as exc:
            print(f"app window: could not write ready ({exc})")
    window.events.loaded += _loaded

    storage = Path.home() / APP_DIR_NAME / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    # gui pinned on Windows: without it pywebview silently falls back to the
    # old IE engine when WebView2 is missing, which cannot run the hub.  With
    # it, a missing runtime raises and the launcher opens a browser tab.
    webview.start(_watch_for_raise, (window,),
                  gui="edgechromium" if os.name == "nt" else None,
                  private_mode=False,  # keep the Viewer's localStorage
                  storage_path=str(storage))
    _WINDOW_CLOSED.set()                  # let the raise watcher end
    _close_own_webview()                  # and the Viewer pop-outs with it
    # The tech closed the window: that quits the app, like any other app.
    # (A browser tab never did; the window is what makes this possible.)
    _quit_server()
    return 0


def _server_pid():
    """The pid serving the hub now, or None when nothing is serving.  The
    port's owner wins over running.json: after an Update & restart the window
    is still open against the NEW server, and a recorded pid can be stale."""
    if not _health_ok():
        return None
    return _pid_listening_on(PORT) or _read_running().get("pid")


def _quit_server() -> None:
    pid = _server_pid()
    if not pid or pid == os.getpid():
        print("app window: closed, no server to stop")
        return
    print(f"app window: closed, stopping the server (pid {pid})")
    sys.stdout.flush()
    # The engine subprocesses of a running report go with it (_stop_pid:
    # our own processes only, never a browser we happened to start).
    _stop_pid(pid)


def _maybe_run_window():
    """Window role: show the hub in a native window.  Returns the exit code,
    or None when this process is not the window."""
    if WINDOW_ARG not in sys.argv:
        return None
    _redirect_output_to_log()
    print("app window: starting")
    lock = _lock_file(_window_lock_path())
    if lock is None:
        print("app window: one is already open, asking it to come forward")
        _ask_open_window_to_raise()
        return 0
    try:
        return _run_window()
    except Exception:
        import traceback
        traceback.print_exc()
        return 3


# ── SharePoint sign-in window (Robert, 2026-09-29) ───────────────────────
# The hub starts this exe with --sharepoint-signin <folder link> to open the
# SharePoint sign-in window: pywebview wants a main thread, and the hub's is
# Streamlit's.  The window itself is sharepoint_link.signin_main, loaded from
# the same code the hub runs (like the engine dispatch above).
SHAREPOINT_SIGNIN_ARG = "--sharepoint-signin"


def _maybe_run_sharepoint_signin():
    """Sign-in role.  Exits the process when this is it; otherwise returns."""
    if SHAREPOINT_SIGNIN_ARG not in sys.argv:
        return
    _redirect_output_to_log()
    print("sharepoint: sign-in window")
    root = Path(os.environ.get("OTDR_SUITE_HOME") or bundled_dir())
    sys.path.insert(0, str(root))
    try:
        import sharepoint_link
        code = sharepoint_link.signin_main(sys.argv[1:])
    except Exception:
        import traceback
        traceback.print_exc()
        code = 3
    sys.stdout.flush()
    os._exit(code)          # pywebview's start() thread is not a daemon


# ── Double-clicked file hand-off (.zfc / .zdb / .otdrproject) ────────────
# The installer associates these extensions with OTDRSuite.exe "%1".  The
# path never goes in the URL: it is written to <APP_DIR_NAME>/open_request.json
# and the hub (a fresh page load = a fresh script run) consumes it and calls
# app.open_share_file(path).  Works the same whether this launch boots the
# server or finds one already serving: with the app window already open, the
# window reloads its page when asked to come forward with a request waiting
# (_watch_for_raise), which is the fresh run a new browser tab used to be.
OPEN_EXTS = (".zfc", ".zdb", ".otdrproject")


def _open_request_path() -> Path:
    return Path.home() / APP_DIR_NAME / "open_request.json"


def _file_arg(argv) -> str:
    """The first argv entry that is an associated file, else ''."""
    for a in list(argv)[1:]:
        a = (a or "").strip().strip('"')
        if a.lower().endswith(OPEN_EXTS):
            return os.path.abspath(a)
    return ""


def _write_open_request(path: str, target: Path = None) -> bool:
    """Atomically record `path` for the hub to open.  Never raises."""
    if not path:
        return False
    target = target or _open_request_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps({"path": path, "ts": time.time()}),
                       encoding="utf-8")
        os.replace(tmp, target)
        return True
    except Exception as exc:
        print(f"open-request: could not write hand-off: {exc}")
        return False


# ── One boot at a time ───────────────────────────────────────────────────
# THE BUG THIS EXISTS FOR.  A tech launched the app two or three times within
# seconds, every time (his log: 08:21:23, :28, :30).  Nothing stopped the
# second one.  `_health_ok()` is asked BEFORE the update runs, and the update
# fetches 21 files at up to 15 s each, so the first instance does not claim
# the port for 10-30 s; every launch inside that window sails past the guard
# and starts its own _prepare_engine.  Two of those rename the SAME directory
# at the same time — the swap moves the cache aside and the new engine in,
# while the other process's _recover_cache can move the old one back — and the
# tech boots into "No module named 'trace_server'" out of a cache that every
# later boot then reports as perfectly intact.  It was read as antivirus
# eating our files for three days.  It was us, twice over: he also had a
# second copy of the app on his OneDrive Desktop sharing the same ~/.otdrSuite.
#
# So the boot is serialised on an OS-level exclusive lock — held by the kernel
# against the open handle, not a marker file, so it CANNOT go stale: a crashed
# or killed instance releases it the moment the process dies.  Two different
# installs on one machine still serialise, because the lock lives beside the
# cache they share.
_LOCK_FH = None                       # module-global: keep the handle alive


def _lock_path() -> Path:
    return Path.home() / APP_DIR_NAME / "boot.lock"


def _take_boot_lock():
    """Return an open, EXCLUSIVELY LOCKED file, or None if another instance
    holds it.  Never blocks.  Returns a handle on any platform we cannot lock
    on, so a machine we cannot protect still boots exactly as it does today."""
    global _LOCK_FH
    fh = _lock_file(_lock_path())
    if fh is not None and fh is not True:
        _LOCK_FH = fh                 # released by the OS when we exit
    return fh


def _lock_file(path: Path):
    """_take_boot_lock's lock, on any file: an open locked handle, None when
    another process holds it, True when this machine cannot lock at all."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+b")
        if not path.stat().st_size:   # msvcrt locks a byte RANGE: give it one
            fh.write(b"\0")
            fh.flush()
    except OSError as exc:
        print(f"single-instance: cannot open the lock file ({exc}) — continuing")
        return True                   # truthy sentinel: boot, do not serialise
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()                    # somebody else is booting
        return None
    except Exception as exc:          # no msvcrt/fcntl — do not block the app
        print(f"single-instance: locking unavailable ({exc}) — continuing")
        return True
    return fh


# How long a second launch waits for the first one to finish booting before it
# gives up and opens a tab anyway.  A boot is a whole signed update (21 files),
# so this is generous on purpose; it exists to stop an infinite wait, not to
# bound a normal start.
BOOT_WAIT_S = 120


def _wait_for_the_other_boot(deadline_s=BOOT_WAIT_S) -> bool:
    """Another instance is booting.  Wait for it to serve, then let the caller
    open a tab.  Returns True when it came up.

    If it DIES instead (crash, killed, a failed update), its lock is released
    and we take it — the app must still start for the tech, so we return False
    and the caller boots normally."""
    end = time.time() + deadline_s
    while time.time() < end:
        if _health_ok():
            return True
        if _take_boot_lock() is not None:
            print("single-instance: the other instance went away — booting")
            return False
        time.sleep(0.5)
    print(f"single-instance: no server after {deadline_s}s — booting anyway")
    return False


# ── A newer exe replaces an older one that is still running ──────────────
# THE BUG THIS EXISTS FOR.  Closing the browser tab does not close the app:
# the server keeps running in the background on PORT.  A tech downloads a new
# exe, double-clicks it, and the already-serving guard in main() sees the OLD
# server answering and just opens a tab to it -- so the new download "does not
# work" until they delete the old one (which kills it along the way) or reboot.
#
# So every boot records who is serving (pid + the engine build it runs) in
# running.json, and a launch whose own bundled build is NEWER than that stops
# the old one and boots itself.  An equal or newer server is kept -- a second
# double-click, or an old copy opened by mistake, still just opens a tab.
# A server with no record is a build from before this change: we look up the
# pid listening on PORT and replace it, which is what makes the very first new
# download work.
def _running_path() -> Path:
    return Path.home() / APP_DIR_NAME / "running.json"


def _write_running(version: int) -> None:
    try:
        p = _running_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"pid": os.getpid(), "version": int(version),
                                 "exe": sys.executable}), encoding="utf-8")
    except OSError as exc:
        print(f"replace-old: could not record the running build ({exc})")


def _read_running() -> dict:
    try:
        d = json.loads(_running_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _pid_listening_on(port: int):
    """The pid holding PORT on Windows (netstat -ano), else None."""
    if os.name != "nt":
        return None
    import subprocess
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                             ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if (len(parts) >= 5 and parts[1].endswith(f":{port}")
                and parts[3].upper() == "LISTENING" and parts[4].isdigit()):
            return int(parts[4])
    return None


def _old_server_to_replace(my_build: int, running: dict, find_pid=None):
    """The pid of a running server older than `my_build`, else None."""
    if not my_build:
        return None                     # dev / unknown build: never kill
    if running:
        try:
            if int(running.get("version", 0)) >= my_build:
                return None
            pid = int(running.get("pid", 0))
        except (TypeError, ValueError):
            return None
        return pid if pid and pid != os.getpid() else None
    pid = (find_pid or _pid_listening_on)(PORT)
    return pid if pid and pid != os.getpid() else None


def _process_table() -> dict:
    """{pid: (parent pid, exe name lower-cased)} for every process.  Windows
    only (Toolhelp32; no PowerShell, see RESTART_ENV for why)."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)   # private: no global restype
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)            # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    table = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            table[entry.th32ProcessID] = (entry.th32ParentProcessID,
                                          entry.szExeFile.lower())
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return table


def _own_process_tree(pid: int, table: dict) -> list:
    """`pid` and every process under it that is this same program (the
    engine subprocesses of a running report), children before parents.
    Anything else under it is left out: a browser that the server opened
    when none was running (_open_browser_when_ready) becomes its child, and
    it is the tech's browser, not ours."""
    exe = table.get(pid, (0, ""))[1]
    order, seen, todo = [], set(), [pid]
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        order.append(p)
        todo += [c for c, (parent, name) in table.items()
                 if parent == p and name == exe and c != p]
    return order[::-1]


def _stop_pid(pid: int) -> None:
    """End the old app and its engine subprocesses, and nothing else.

    Not `taskkill /T`, which ends the WHOLE tree.  The server opens the
    tech's browser itself, and when no browser was running yet that browser
    starts as the server's child, so /T ended the browser and every tab in
    it (seen on a Windows 11 VM on 2026-09-27: the msedge process count went
    to 0).  Each of our own processes is ended by its pid instead.  The
    process doing the stopping is skipped in case it is in that tree itself
    (an Update & restart copy is a child of the server that started it;
    in OTDR Suite App so is the app window, which stops the server when
    the tech closes it)."""
    import subprocess
    try:
        if os.name == "nt":
            try:
                pids = _own_process_tree(pid, _process_table())
            except Exception as exc:
                print(f"replace-old: could not list processes ({exc}), "
                      f"stopping pid {pid} alone")
                pids = [pid]
            for p in pids:
                if p == os.getpid():
                    continue
                subprocess.run(["taskkill", "/PID", str(p), "/F"],
                               capture_output=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
    except Exception as exc:
        print(f"replace-old: could not stop pid {pid} ({exc})")


def _replace_older_server() -> bool:
    """True when an older running copy was stopped and the port is free."""
    pid = _old_server_to_replace(_bundled_build(), _read_running())
    if not pid:
        return False
    print(f"replace-old: an older build is serving (pid {pid}) — stopping it")
    _stop_pid(pid)
    end = time.time() + RESTART_DRAIN_S
    while time.time() < end:
        if not _health_ok():
            print("replace-old: the old copy has stopped — booting this one")
            return True
        time.sleep(RESTART_POLL_S)
    print("replace-old: the old copy is still serving — opening a tab to it")
    return False


# ── Main ─────────────────────────────────────────────────────────────────
def main() -> int:
    # Subprocess role: handle and exit before touching Streamlit/logs.
    if _maybe_run_engine():
        return 0
    _maybe_run_sharepoint_signin()
    window_code = _maybe_run_window()
    if window_code is not None:
        return window_code

    _redirect_output_to_log()
    _silence_first_run_prompt()
    _load_webhook()   # expose SS_ERROR_WEBHOOK + OTDR_SUITE_SOURCE before launch
    _export_edition()

    # Double-clicked .zfc/.zdb/.otdrproject: leave it for the hub to pick up
    # (whichever instance ends up serving -- this one or one already running).
    _write_open_request(_file_arg(sys.argv))

    # Started by the hub's Update & restart button: wait for the old server to
    # go away BEFORE the already-serving guard below can re-attach to it.  The
    # variable is popped so the engine subprocesses this boot spawns never
    # inherit it.
    restart_from = os.environ.pop(RESTART_ENV, None)
    if restart_from:
        print(f"restart: started by the running app (pid {restart_from}) "
              "— waiting for it to stop serving")
        _drain_old_instance()

    # Serialise the boot BEFORE the health check: the window this closes is
    # exactly the one where the port is not bound yet, so a health check on
    # its own can never see it (see _take_boot_lock).
    if _take_boot_lock() is None:
        print("Another instance is starting — waiting for it.")
        # A running server keeps this lock for its whole life, so an OLDER
        # copy left running has to be replaced here, not only at the guard
        # below.  Its death releases the lock; take it before booting.
        if _wait_for_the_other_boot() and not (
                _replace_older_server() and _take_boot_lock() is not None):
            print("Another instance is already serving — showing it.")
            _show_app()
            return 0

    if _health_ok() and not _replace_older_server():
        print("Another instance is already serving — showing it.")
        _show_app()
        return 0

    # Auto-update: choose the engine source (latest → cached → bundled) and
    # expose it so app.py + the engine subprocesses all load the same code.
    engine_dir, source = _prepare_engine()
    os.environ["OTDR_SUITE_HOME"] = str(engine_dir)
    os.environ["OTDR_SUITE_SOURCE"] = source
    print(f"engine source: {source}  ({engine_dir})")
    _write_running(_bundled_build() if engine_dir == bundled_dir()
                   else _cached_version())

    ui_script = str(engine_dir / "app.py")
    print(f"UI script: {ui_script}")

    try:
        # Import Streamlit INSIDE the guard: a missing/broken streamlit is a top
        # frozen-build failure mode, and as an ImportError above the try it would
        # escape the fatal-start handler and never reach Slack.  Only start the
        # browser-opener thread once the import is known-good (otherwise it polls
        # a server that will never come up, then bails on its own 90s deadline).
        from streamlit.web import cli as stcli

        threading.Thread(target=_open_browser_when_ready, daemon=True).start()

        sys.argv = [
            "streamlit", "run", ui_script,
            "--server.headless=true",
            f"--server.port={PORT}",
            f"--server.address={HOST}",
            "--browser.gatherUsageStats=false",
            "--global.developmentMode=false",
        ]
        return stcli.main()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    except Exception as exc:
        # Fatal START failure — the silent "won't even boot" class. Post it so
        # it surfaces in Slack instead of only landing in the local log.
        import platform
        import traceback
        try:
            who = "%s / %s" % (socket.gethostname(), __import__("getpass").getuser())
        except Exception:
            who = "?"
        _post_slack(
            ":rotating_light: *OTDR Suite error* — launcher failed to start\n"
            "*%s*: %s\n"
            "tech: `%s`  |  os: %s  |  source: %s\n```%s```"
            % (type(exc).__name__, exc, who, platform.platform(),
               os.environ.get("OTDR_SUITE_SOURCE", "?"),
               traceback.format_exc()[-1400:]))
        raise


if __name__ == "__main__":
    sys.exit(main() or 0)
