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

APP_NAME = "OTDR Suite"
ENV_WEBHOOK = "SS_ERROR_WEBHOOK"      # shared var name across all our apps
ENV_SOURCE = "OTDR_SUITE_SOURCE"      # "bundled .exe" / "dev" — set by launcher

_ERR_LAST: dict[str, float] = {}      # signature -> last-sent epoch (hourly dedup)


def report_error(where, exc, context=None):
    """Report a tech-side error to Slack.  No-op without a webhook; never raises."""
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
        text = (
            ":rotating_light: *%s error* — %s\n"
            "*%s*: %s\n"
            "tech: `%s`  |  os: %s  |  source: %s%s\n```%s```"
            % (APP_NAME, where, type(exc).__name__, exc, who,
               platform.platform(), os.environ.get(ENV_SOURCE, "dev"),
               ctx, traceback.format_exc()[-1400:])
        )

        # Scrub the tech's home-directory prefix from the WHOLE message so we
        # never leak their local filesystem layout (absolute folder paths in
        # context values, absolute file paths in the exception text + the
        # traceback) onto the shared channel.  Basenames + the error type/text
        # survive, so the report stays useful.  Honors the module's PII
        # guarantee; can't raise (no-op if expanduser misbehaves).
        try:
            home = os.path.expanduser("~")
            if home and home != "~":
                text = text.replace(home, "~")
        except Exception:
            pass

        import json as _json
        import threading
        import urllib.request

        def _send():
            try:
                req = urllib.request.Request(
                    url, data=_json.dumps({"text": text}).encode(),
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=4)
            except Exception:
                pass

        threading.Thread(target=_send, daemon=True).start()
    except Exception:
        # Reporting must never break a run.
        pass


def safe_report(where, exc, context=None):
    """Import-and-call wrapper for callers that may not have error_report on
    sys.path (e.g. a subprocess in dev) — never raises and is itself a no-op
    if anything is missing."""
    try:
        report_error(where, exc, context)
    except Exception:
        pass
