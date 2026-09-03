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

APP_NAME     = "OTDRSuite"
APP_DIR_NAME = ".otdrSuite"
HOST         = "127.0.0.1"
PORT         = 8510                       # see project-desktop-ports-registry
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
GH_BRANCH   = "main"
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
MANIFEST_URL      = RAW_URL_FMT.format(path=MANIFEST_PATH)
MANIFEST_SIG_URL  = RAW_URL_FMT.format(path=MANIFEST_SIG_PATH)

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

    # 3. the signed manifest must cover EXACTLY the files we run — a manifest
    #    missing one of our files (or padded with extras) is a tampering signal.
    if set(files) != set(ENGINE_FILES):
        print("auto-update: manifest file set != ENGINE_FILES — rejecting")
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
    print(f"auto-update: fetching signed update {GH_OWNER}/{GH_REPO}@{GH_BRANCH} ...")
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
    # Light theme to match the viewer (per-process so it doesn't touch the
    # tech's other Streamlit apps via a global config).
    os.environ.setdefault("STREAMLIT_THEME_BASE", "light")
    os.environ.setdefault("STREAMLIT_THEME_PRIMARY_COLOR", "#2c5b8a")
    os.environ.setdefault("STREAMLIT_THEME_BACKGROUND_COLOR", "#ffffff")
    os.environ.setdefault("STREAMLIT_THEME_SECONDARY_BACKGROUND_COLOR", "#eef3f8")
    os.environ.setdefault("STREAMLIT_THEME_TEXT_COLOR", "#1f2a36")
    # NOTE: OTDR_SUITE_HOME is set in main() AFTER _prepare_engine() chooses the
    # engine source (updated cache vs bundled), so the hub + subprocess load the
    # same code.


# ── Health poll + browser opener ────────────────────────────────────────
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
            try:
                webbrowser.open(APP_URL)
            except Exception as exc:
                print(f"webbrowser.open failed: {exc}")
            return
        time.sleep(0.5)
    print("browser opener: server never returned ok within 90s")


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
    try:
        path = _lock_path()
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
    _LOCK_FH = fh                     # released by the OS when we exit
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


# ── Main ─────────────────────────────────────────────────────────────────
def main() -> int:
    # Subprocess role: handle and exit before touching Streamlit/logs.
    if _maybe_run_engine():
        return 0

    _redirect_output_to_log()
    _silence_first_run_prompt()
    _load_webhook()   # expose SS_ERROR_WEBHOOK + OTDR_SUITE_SOURCE before launch

    # Serialise the boot BEFORE the health check: the window this closes is
    # exactly the one where the port is not bound yet, so a health check on
    # its own can never see it (see _take_boot_lock).
    if _take_boot_lock() is None:
        print("Another instance is starting — waiting for it.")
        if _wait_for_the_other_boot():
            print("Another instance is already serving — opening new tab.")
            try:
                webbrowser.open(APP_URL)
            except Exception:
                pass
            return 0

    if _health_ok():
        print("Another instance is already serving — opening new tab.")
        try:
            webbrowser.open(APP_URL)
        except Exception:
            pass
        return 0

    # Auto-update: choose the engine source (latest → cached → bundled) and
    # expose it so app.py + the engine subprocesses all load the same code.
    engine_dir, source = _prepare_engine()
    os.environ["OTDR_SUITE_HOME"] = str(engine_dir)
    os.environ["OTDR_SUITE_SOURCE"] = source
    print(f"engine source: {source}  ({engine_dir})")

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
