"""The relaunched exe waits for the old server BEFORE its already-serving guard.

"Update & restart now" starts a second copy of the exe with RESTART_ENV set,
then exits.  The launcher's guard ("Another instance is already serving —
opening new tab") runs before its signed-update path, so if the new copy
health-checks while the old one still answers it re-attaches and the update
never applies.  ``_drain_old_instance`` is the wait that stops that.

The wait used to live in a detached PowerShell / sh+curl helper.  The Windows
one had never run on a Windows box, and the boss reported that Update did not
restart the app.  Now the wait is in the launcher, so it is driven here for
real against a throwaway health server.
"""
import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from conftest import REPO_ROOT
from test_engine_self_verify import _load_launcher

LAUNCHER = REPO_ROOT / "desktop" / "launcher.py"
APP_SRC = (REPO_ROOT / "app.py").read_text(encoding="utf-8")


# ─── pure: fake health, fake clock ───────────────────────────────────────
def _drive(answers, deadline_s=30, tmp_path=None):
    """Run _drain_old_instance with a scripted health endpoint (a list of
    True/False, the last value repeating) and a clock that advances by the
    sleeps.  Returns (result, marker_path)."""
    L = _load_launcher()
    marker = tmp_path / "update_restart_blocked"
    L._restart_blocked_marker = lambda: marker
    it = iter(answers)
    last = [answers[-1]]

    def health():
        try:
            last[0] = next(it)
        except StopIteration:
            pass
        return last[0]

    now = [1000.0]
    return (L._drain_old_instance(health_ok=health, deadline_s=deadline_s,
                                  clock=lambda: now[0],
                                  sleep=lambda s: now.__setitem__(0, now[0] + s)),
            marker)


def test_returns_the_moment_the_old_server_stops(tmp_path):
    ok, marker = _drive([True, True, True, False], tmp_path=tmp_path)
    assert ok is True
    assert not marker.exists(), "a successful restart must not leave the marker"


def test_an_old_server_that_is_already_gone_costs_nothing(tmp_path):
    ok, marker = _drive([False], tmp_path=tmp_path)
    assert ok is True and not marker.exists()


def test_a_server_that_never_lets_go_leaves_the_marker(tmp_path):
    ok, marker = _drive([True], deadline_s=2, tmp_path=tmp_path)
    assert ok is False
    assert marker.exists(), "the hub turns this file into 'close it or reboot'"


def test_marker_path_is_the_one_the_hub_reads(tmp_path, monkeypatch):
    """launcher writes it, app.py reads it — same file, or the message never
    shows."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    L = _load_launcher()
    launcher_path = os.path.normpath(str(L._restart_blocked_marker()))
    m = re.search(r"def _restart_marker_path\(\):.*?return (os\.path\.join\(.*?\))\n",
                  APP_SRC, re.S)
    app_path = os.path.normpath(eval(m.group(1), {"os": os}))
    assert launcher_path == app_path


# ─── wiring: main() drains BEFORE the guard, and the env never leaks ─────
def test_main_drains_before_the_already_serving_guard():
    src = LAUNCHER.read_text(encoding="utf-8")
    main = src[src.index("\ndef main("):]
    pop = main.index("os.environ.pop(RESTART_ENV")
    drain = main.index("_drain_old_instance()")
    lock = main.index("_take_boot_lock()")
    health = main.index("_health_ok()")
    assert pop < drain < lock < health, (
        "the wait must come before the boot lock and the health check — "
        "either one re-attaches to a still-serving old instance")


def test_env_is_popped_so_engine_subprocesses_never_inherit_it():
    """An engine subprocess that inherited RESTART_ENV would be fine today
    (it never reaches main's boot path) — but pop it anyway so the variable
    means exactly one thing: 'I was started by the Update button'."""
    src = LAUNCHER.read_text(encoding="utf-8")
    assert "os.environ.pop(RESTART_ENV, None)" in src


def test_no_shell_helper_anywhere():
    for f in (APP_SRC, LAUNCHER.read_text(encoding="utf-8")):
        for gone in ("Invoke-WebRequest", "Start-Process", "curl -sf",
                     "powershell"):
            assert gone not in f, f"{gone}: the shell helper must be gone"


# ─── behaviour: the real _health_ok against a throwaway server ───────────
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):                                   # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def _serve():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    httpd = HTTPServer(("127.0.0.1", port), _Health)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def test_real_probe_waits_while_the_old_server_answers_then_goes(tmp_path):
    L = _load_launcher()
    httpd, port = _serve()
    L.HEALTH_URL = f"http://127.0.0.1:{port}/_stcore/health"
    L._restart_blocked_marker = lambda: tmp_path / "blocked"
    assert L._health_ok(), "sanity: the throwaway server answers like Streamlit"

    result = {}

    def run():
        t0 = time.time()
        result["ok"] = L._drain_old_instance(deadline_s=20)
        result["took"] = time.time() - t0

    th = threading.Thread(target=run)
    th.start()
    time.sleep(1.5)
    assert th.is_alive(), "returned while the old server was still answering"
    httpd.shutdown()
    httpd.server_close()
    th.join(timeout=15)
    assert result["ok"] is True
    assert result["took"] < 10, result
    assert not (tmp_path / "blocked").exists()


def test_real_probe_gives_up_and_marks_when_the_server_stays(tmp_path):
    L = _load_launcher()
    httpd, port = _serve()
    try:
        L.HEALTH_URL = f"http://127.0.0.1:{port}/_stcore/health"
        L._restart_blocked_marker = lambda: tmp_path / "blocked"
        assert L._drain_old_instance(deadline_s=1.5) is False
        assert (tmp_path / "blocked").exists()
    finally:
        httpd.shutdown()
        httpd.server_close()
