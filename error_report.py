"""
OTDR Suite — Slack error reporting (shared, stdlib-only).
=========================================================
Posts scrubbed tech-side errors to the shared Slack webhook so failures in the
field surface in one channel across all our apps instead of dying in a local
log.  This module is deliberately STDLIB-ONLY and engine-free so every part of
the suite can import it without tripping the viewer/secretsauce sor_reader
namespace collision (the hub, the viewer trace server, AND the Secret Sauce
subprocess all import it).

The webhook URL is read from env SS_ERROR_WEBHOOK (set by the launcher from a
build-time-only _webhook.cfg that CI writes from the SLACK_ERROR_WEBHOOK secret
— NEVER committed; the repo is public and Slack auto-revokes leaked webhooks).
No webhook -> silent no-op (dev runs never spam Slack).

Guarantees:
  * NEVER raises (a reporting hiccup must not break a tech's run).
  * NEVER sends customer / trace / PII data — only error metadata + a small
    caller-supplied context dict (counts, mode).
  * Deduped to one message per (where, type, message) signature per hour, in
    process, so a repeat can't flood the channel; distinct errors fire at once.
  * Fire-and-forget POST in a daemon thread with a ~4 s timeout.
"""
from __future__ import annotations

import os
import re

APP_NAME = "OTDR Suite"
ENV_WEBHOOK = "SS_ERROR_WEBHOOK"      # shared var name across all our apps

# Process-wide cache for the Slack-POST TLS context.  The frozen Windows .exe
# has no system trust store, so we verify with certifi's CA bundle — but build
# it ONCE: doing it per-report added latency to the fire-and-forget thread.
_TLS_CACHE = {}
ENV_SOURCE = "OTDR_SUITE_SOURCE"      # "bundled .exe" / "dev" — set by launcher

_ERR_LAST: dict[str, float] = {}      # signature -> last-sent epoch (hourly dedup)

# POSIX absolute path: /a/b/c…  → replaced with its basename (…/c).  Anchored on
# a non-path char (or start) so we don't chew a leading slash mid-token.  The
# home prefix is collapsed to ~ first, so ~/… is handled before this runs.
#
# SPACE-TOLERANCE (prod issue #7): a path whose directories contain spaces
# ("…/ILA 1 to ILA 6/A-F West 145-288/report.pdf") used to redact only up to the
# first space, mangling the rest into what looked like prose — a FileNotFoundError
# on a real spaced job folder reached the shared channel as an undiagnosable
# fragment AND still leaked the mid-path username.  We now let a path segment
# carry spaces, but ONLY an INTERNAL segment (one immediately followed by another
# separator): `[\w.\-]+(?: +[\w.\-]+)*(?=[\\/])`.  The `(?=[\\/])` lookahead is
# the guard that keeps us from bleeding across a space into trailing prose or an
# adjacent path — the space-joined words must still be bounded by a separator to
# count as part of the path.  The FINAL segment (the basename, not followed by a
# separator) stays space-free, so "/Volumes/A/B and C and D" still redacts to
# "B and C and D" and two space-separated paths ("a.sor vs /Users/bob/b.sor")
# each collapse to their own basename.  Net: usernames/home dirs are still
# redacted (now even inside spaced paths) and the surrounding message survives.
_INT_SEG = r"(?:{sep}[\w.\-]+(?: +[\w.\-]+)*(?={sep}))"   # internal seg, spaces ok
_POSIX_PATH_RE = re.compile(
    r"(?<![\w/])(/[\w.\-]+(?: +[\w.\-]+)*(?=/)"
    r"(?:/[\w.\-]+(?: +[\w.\-]+)*(?=/))*/[\w.\-]+)")
# Windows absolute path: C:\Users\… (or forward-slash variants).  Down to base.
_WIN_PATH_RE = re.compile(
    r"(?<![\w])([A-Za-z]:" + _INT_SEG.format(sep=r"[\\/]") + r"*[\\/][\w.\-]+)")
# Home-relative remainder after the $HOME→~ collapse, EITHER separator
# (POSIX "~/a/b/f" or Windows "~\a\b\f") → reduced to "~/<basename>".  Run
# before the absolute-path passes so "~/a/b/f" can't be stripped to a stranded
# "~f", and so Windows "~\a\b\f" subpaths are redacted too (not just POSIX).
_TILDE_PATH_RE = re.compile(
    r"~" + _INT_SEG.format(sep=r"[\\/]") + r"*[\\/][\w.\-]+")
# Windows UNC path: \\host\share\dir\file → basename.  The drive-letter pass
# (needs "X:") and the POSIX pass (needs a leading "/") both miss UNC, so an
# internal fileserver/share (and customer) name would leak to the shared
# channel.  Techs on Windows commonly use mapped/UNC paths.  Kept contiguous
# (non-spaced) for now — the reported spaced-path leak was a drive path, and a
# spaced UNC share would only under-redact its tail, never corrupt the message.
_UNC_PATH_RE = re.compile(r"\\\\[\w.\-]+\\[\w.\-]+(?:\\[\w.\-]+)*")


# ── Build identity (shared by the app sidebar + the Slack payload) ────────
def _bundle_dir():
    """Where the CI-written build stamp (version.json) lives: next to the frozen
    exe (PyInstaller onedir → _MEIPASS), or this file's directory (the repo
    root) in dev.  Mirrors launcher.bundled_dir() without importing it — this
    module stays stdlib-only + engine-free."""
    import sys
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def version_labels(bundle_dir=None, meta_path=None):
    """(app_label, engine_label) identifying THIS build — e.g.
    ('build 54 (2026-07-14)', 'bundled') or ('build 54 (2026-07-14)',
    'update 56 applied').  Dev (no CI-written version.json, not launched by the
    frozen launcher) → ('dev', 'dev').  Never raises.

      app    — version.json bundled next to the exe, written by CI before the
               PyInstaller step ({"build": run_number, "date": ..., "commit": ...}).
      engine — which code the launcher chose at boot (env OTDR_SUITE_SOURCE):
               'bundled*' labels → the code frozen into the exe; a cached /
               latest verified signed update → its manifest version, which the
               launcher records in ~/.otdrSuite/engine.meta.json on every
               verified swap (see launcher._prepare_engine).

    `bundle_dir` / `meta_path` exist for tests only."""
    import json
    app_label = "dev"
    try:
        base = bundle_dir if bundle_dir is not None else _bundle_dir()
        with open(os.path.join(base, "version.json"), "rb") as fh:
            v = json.loads(fh.read().decode("utf-8"))
        app_label = "build %d" % int(v["build"])
        if v.get("date"):
            app_label += " (%s)" % v["date"]
    except Exception:
        pass
    engine_label = "dev"
    try:
        src = os.environ.get(ENV_SOURCE, "") or ""
        if src.startswith("bundled"):
            engine_label = "bundled"
        elif src and src != "dev":
            # cached / latest verified update — read the applied manifest
            # version the launcher recorded (binary read; utf-8).
            engine_label = "update applied (version unknown)"
            if meta_path is None:
                meta_path = os.path.join(os.path.expanduser("~"), ".otdrSuite",
                                         "engine.meta.json")
            with open(meta_path, "rb") as fh:
                n = int(json.loads(fh.read().decode("utf-8")).get("version", 0))
            if n > 0:
                engine_label = "update %d applied" % n
                # When the update was applied = when the launcher last wrote
                # the meta file — mtime needs no launcher change, so existing
                # installs get the timestamp via code update alone.
                try:
                    import time as _t
                    ts = _t.strftime("%Y-%m-%d %H:%M",
                                     _t.gmtime(os.path.getmtime(meta_path)))
                    engine_label += " %s UTC" % ts
                except Exception:
                    pass
    except Exception:
        pass
    return app_label, engine_label


def _basename_any(p):
    """Last path component of a POSIX- or Windows-style path (stdlib os.path is
    POSIX-only on POSIX hosts, so split on both separators ourselves)."""
    parts = p.replace("\\", "/").rstrip("/").split("/")
    return parts[-1] or p


def _scrub_paths(text):
    """Redact absolute filesystem paths down to a basename so the shared channel
    never leaks a tech's layout — the home prefix (~), other POSIX roots
    (/Volumes/…, another user's /Users/…), and Windows drive paths
    (C:\\Users\\…\\, D:\\Jobs\\…).  Error type/message and basenames survive.
    Stdlib-only; never raises (caller wraps it, but be defensive anyway)."""
    try:
        home = os.path.expanduser("~")
        if home and home != "~":
            text = text.replace(home, "~")
    except Exception:
        pass
    try:
        # Home-relative remainder first (~/… or ~\…, either separator) →
        # ~/<basename>, BEFORE the POSIX pass could strip the leading slash and
        # strand the tilde — this also redacts Windows ~\… subpaths.
        text = _TILDE_PATH_RE.sub(lambda m: "~/" + _basename_any(m.group(0)), text)
        # Windows UNC (\\host\share\…) → basename, before the drive/POSIX passes.
        text = _UNC_PATH_RE.sub(lambda m: _basename_any(m.group(0)), text)
        # Absolute paths (POSIX /…, Windows X:\…) → basename.
        text = _WIN_PATH_RE.sub(lambda m: _basename_any(m.group(1)), text)
        text = _POSIX_PATH_RE.sub(lambda m: _basename_any(m.group(1)), text)
    except Exception:
        pass
    return text


def report_error(where, exc, context=None, log=None):
    """Report a tech-side error to Slack.  No-op without a webhook; never raises.

    `log`, when given, becomes the ```code``` block — pass a crashed engine
    SUBPROCESS's stderr here.  The no-manifest / not-ok report sites are NOT
    inside an `except`, so traceback.format_exc() is empty there ('NoneType:
    None') and the report carried no cause; `log` is what makes it diagnosable.
    Falls back to the live traceback when None (genuine except-block callers)."""
    try:
        url = os.environ.get(ENV_WEBHOOK)
        if not url:
            return
        import time
        import hashlib
        import traceback
        import platform

        sig = hashlib.md5(
            ("%s|%s|%s" % (where, type(exc).__name__, exc)).encode()
        ).hexdigest()
        now = time.time()
        if now - _ERR_LAST.get(sig, 0) < 3600:
            return
        _ERR_LAST[sig] = now

        try:
            import getpass
            import socket
            who = "%s / %s" % (socket.gethostname(), getpass.getuser())
        except Exception:
            who = "?"

        ctx = "".join("\n• %s: %s" % (k, v) for k, v in (context or {}).items())
        # The real cause: a provided engine log (e.g. a crashed subprocess's
        # stderr) when given, else the live exception traceback.  At the
        # no-manifest / not-ok sites there is no active exception, so
        # format_exc() is the useless 'NoneType: None' — `log` is what makes the
        # report diagnosable.  Scrubbed with the rest of the message below.
        tb = (log if log else traceback.format_exc()) or ""
        # Build identity — ADDITIVE line only: the Slack→issues bridge parses
        # these messages, so the existing lines must stay byte-identical; the
        # new `build:` line slots in before the code block.
        try:
            _appv, _engv = version_labels()
            build = "app %s  |  engine %s" % (_appv, _engv)
        except Exception:
            build = "unknown"
        text = (
            ":rotating_light: *%s error* — %s\n"
            "*%s*: %s\n"
            "tech: `%s`  |  os: %s  |  source: %s%s\n"
            "build: %s\n```%s```"
            % (APP_NAME, where, type(exc).__name__, exc, who,
               platform.platform(), os.environ.get(ENV_SOURCE, "dev"),
               ctx, build, tb[-1800:])
        )

        # Scrub local filesystem layout from the WHOLE message so we never leak
        # a tech's paths (absolute folder paths in context values, absolute file
        # paths in the exception text + the traceback) onto the shared channel.
        # Basenames + the error type/text survive, so the report stays useful.
        # Honors the module's PII guarantee; can't raise.
        try:
            text = _scrub_paths(text)
        except Exception:
            pass

        import json as _json
        import threading
        import urllib.request
        # Snapshot urlopen NOW (at report time), not at thread-run time: this
        # fire-and-forget thread may run after a LATER caller (or a test) has
        # swapped urllib.request.urlopen, which would otherwise send this report
        # to the wrong target / capture.
        _urlopen = urllib.request.urlopen

        def _send():
            try:
                # certifi CA bundle so the report sends from the frozen Windows
                # build (no system trust store) — built once, cached.
                if 'ctx' not in _TLS_CACHE:
                    import ssl
                    try:
                        import certifi
                        _TLS_CACHE['ctx'] = ssl.create_default_context(cafile=certifi.where())
                    except Exception:
                        try:
                            _TLS_CACHE['ctx'] = ssl.create_default_context()
                        except Exception:
                            _TLS_CACHE['ctx'] = None
                req = urllib.request.Request(
                    url, data=_json.dumps({"text": text}).encode(),
                    headers={"Content-Type": "application/json"})
                _urlopen(req, timeout=4, context=_TLS_CACHE['ctx'])
            except Exception:
                pass

        threading.Thread(target=_send, daemon=True).start()
    except Exception:
        # Reporting must never break a run.
        pass


def safe_report(where, exc, context=None, log=None):
    """Import-and-call wrapper for callers that may not have error_report on
    sys.path (e.g. a subprocess in dev) — never raises and is itself a no-op
    if anything is missing."""
    try:
        report_error(where, exc, context, log)
    except Exception:
        pass
