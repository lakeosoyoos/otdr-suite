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
MANIFEST_PATH     = "update_manifest.json"
MANIFEST_SIG_PATH = "update_manifest.json.sig"
MANIFEST_URL      = RAW_URL_FMT.format(path=MANIFEST_PATH)
MANIFEST_SIG_URL  = RAW_URL_FMT.format(path=MANIFEST_SIG_PATH)

# ── Ed25519 update-signing PUBLIC key ────────────────────────────────────
# *** HUMAN STEP REQUIRED — see README_BUILD.txt "Update signing key" ***
# This is the PUBLIC half of the update-signing keypair; it is safe to commit.
# Robert generates the keypair locally, pastes the 64-hex-char public key here,
# and sets the PRIVATE half as the `OTDR_UPDATE_SIGNING_KEY` repo secret.
# While this is the placeholder below, auto-update is DISABLED (fail closed)
# and the app runs the bundled engine — no network code-fetch happens at all.
UPDATE_PUBLIC_KEY_PLACEHOLDER = "REPLACE_WITH_ED25519_PUBLIC_KEY_HEX"
UPDATE_PUBLIC_KEY_HEX = UPDATE_PUBLIC_KEY_PLACEHOLDER  # TODO(Robert): paste pubkey


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


def _cached_version() -> int:
    """The version currently in the cache (0 if no cache / unreadable) — the
    floor for anti-rollback.  We persist it next to the cached engine."""
    try:
        meta = _cache_dir().with_name(_cache_dir().name + ".meta.json")
        if meta.exists():
            return int(json.loads(meta.read_text(encoding="utf-8")).get("version", 0))
    except Exception:
        pass
    return 0


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
    for rel in ENGINE_FILES:
        data = _fetch(RAW_URL_FMT.format(path=rel))
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
        if (cache / "app.py").exists():
            return cache, "cached (last verified update; auto-update disabled)"
        return bundled_dir(), "bundled (auto-update disabled — no signing key)"

    cache = _cache_dir()
    staging = cache.with_name(cache.name + ".staging")
    meta = cache.with_name(cache.name + ".meta.json")
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
                }), encoding="utf-8")
                print(f"auto-update: ok — verified v{new_version} → using {cache}")
                return cache, f"latest (verified update v{new_version})"
            except Exception as exc:
                # Swap failed mid-flight — restore the prior cache from .old.
                print(f"auto-update: swap failed ({exc}); restoring previous cache")
                shutil.rmtree(staging, ignore_errors=True)
                if not cache.exists() and old.exists():
                    try:
                        old.rename(cache)
                    except Exception:
                        pass
    # Verification/fetch failed or version not newer — use the last verified
    # cache if present, else bundled.  Never an unverified fetch.
    if (cache / "app.py").exists():
        print(f"auto-update: keeping verified cache {cache}")
        return cache, "cached (last verified update)"
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


# ── Main ─────────────────────────────────────────────────────────────────
def main() -> int:
    # Subprocess role: handle and exit before touching Streamlit/logs.
    if _maybe_run_engine():
        return 0

    _redirect_output_to_log()
    _silence_first_run_prompt()
    _load_webhook()   # expose SS_ERROR_WEBHOOK + OTDR_SUITE_SOURCE before launch

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

    threading.Thread(target=_open_browser_when_ready, daemon=True).start()

    from streamlit.web import cli as stcli
    sys.argv = [
        "streamlit", "run", ui_script,
        "--server.headless=true",
        f"--server.port={PORT}",
        f"--server.address={HOST}",
        "--browser.gatherUsageStats=false",
        "--global.developmentMode=false",
    ]
    try:
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
