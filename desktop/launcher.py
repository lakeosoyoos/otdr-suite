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
import time
import socket
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


# ── Where the bundled files live ────────────────────────────────────────
def bundled_dir() -> Path:
    if getattr(sys, "frozen", False):
        # one-folder build → files sit next to the exe (or _MEIPASS for onefile)
        return Path(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
    return Path(__file__).resolve().parent.parent   # repo root in dev


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
        urllib.request.urlopen(req, timeout=4)
    except Exception:
        pass


# ── Secret-Sauce subprocess dispatch (must run BEFORE anything Streamlit) ─
def _maybe_run_secretsauce() -> bool:
    if "--run-secretsauce" not in sys.argv:
        return False
    # Bundle root on path so run_secretsauce can import error_report; load the
    # webhook so engine errors in this subprocess can report too.
    sys.path.insert(0, str(bundled_dir()))
    sys.path.insert(0, str(bundled_dir() / "secretsauce"))
    _load_webhook()
    # Drop the sentinel so run_secretsauce's argparse sees only its flags.
    sys.argv = [a for a in sys.argv if a != "--run-secretsauce"]
    import run_secretsauce
    run_secretsauce.main()
    return True


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
    # The hub reads this to locate itself when frozen.
    os.environ["OTDR_SUITE_HOME"] = str(bundled_dir())


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
    if _maybe_run_secretsauce():
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

    ui_script = str(bundled_dir() / "app.py")
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
