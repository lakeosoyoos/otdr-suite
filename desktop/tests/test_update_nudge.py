"""Startup update nudge + a restart that can't re-attach to the dying server.

Two field failures motivate this file:

  * an always-on machine never restarts, so it never reaches the launcher's
    signed-update path (the launcher's already-serving guard runs BEFORE it)
    and sits on an old engine forever — the boss ran 134 while 136 was live.
    Fix: a fail-silent per-session manifest check that raises a sidebar banner
    ABOVE the page radio, wired to the SAME restart the footer button uses.
  * clicking "Update & restart now" could RACE its own shutdown: the new
    launcher health-checked port 8510, found the dying instance still
    answering, printed "Another instance is already serving" and re-attached —
    the click looked like it worked and the update never applied.  Fix: the
    detached restart helper waits for that health endpoint to go quiet BEFORE
    starting the exe, and surfaces a visible message if it never does.

Style follows test_update_button.py: helpers are lifted out of app.py by AST
(no Streamlit, no network, no engine imports), the wiring is source-locked,
and the rendering is exercised through the AppTest hub.  The one behavioural
test drives the real POSIX restart argv as a subprocess against a throwaway
health server.
"""
import ast
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import types
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from conftest import REPO_ROOT, run_streamlit
import error_report as R

APP = REPO_ROOT / "app.py"
APP_SRC = APP.read_text(encoding="utf-8")


def _load_helper(name, **namespace):
    """Exec a single top-level function out of app.py in a bare module (the
    test_update_button.py pattern).  `namespace` injects whatever module
    globals it closes over — _restart_command needs `os`."""
    tree = ast.parse(APP_SRC)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == name)
    mod = types.ModuleType("upd_nudge")
    mod.__dict__.update(namespace)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "app.py", "exec"),
         mod.__dict__)
    return getattr(mod, name)


def _fn_source(name):
    """Source text of one top-level function of app.py (for source-locks)."""
    tree = ast.parse(APP_SRC)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == name)
    return ast.get_source_segment(APP_SRC, fn)


# ═════════════════════════════════════════════════════════════════════════
#  1. _nudge_check — the decision, with fetcher + applied version injected
# ═════════════════════════════════════════════════════════════════════════
def test_nudge_fires_when_published_version_is_newer():
    """The boss's case: engine 134 applied, 136 published → banner data."""
    check = _load_helper("_nudge_check")
    assert check(lambda: 136, 134) == (136, 134)


def test_nudge_silent_when_up_to_date_or_ahead():
    """Equal (and a manifest that somehow trails the running engine) → nothing.
    Never nag a tech who is already current."""
    check = _load_helper("_nudge_check")
    assert check(lambda: 136, 136) is None
    assert check(lambda: 130, 136) is None


def test_nudge_silent_when_fetch_raises():
    """Offline / DNS dead / TLS blocked: the check must swallow it.  A tech in
    a hut with no signal gets no scary red box."""
    check = _load_helper("_nudge_check")

    def boom():
        raise OSError("network is unreachable")

    assert check(boom, 134) is None


def test_nudge_silent_when_fetch_returns_none():
    """_latest_manifest_version already degrades to None — treat it the same."""
    check = _load_helper("_nudge_check")
    assert check(lambda: None, 134) is None


def test_nudge_skips_the_fetch_entirely_when_running_version_unknown():
    """A dev checkout can't be updated, so it must short-circuit BEFORE the
    fetch — that is what keeps this whole suite (which runs the hub in dev via
    AppTest) off the network."""
    check = _load_helper("_nudge_check")
    calls = []

    def fetch():
        calls.append(1)
        return 999

    assert check(fetch, None) is None
    assert calls == [], "no manifest fetch may happen when the version is unknown"


# ═════════════════════════════════════════════════════════════════════════
#  2. _restart_command — wait for the old server BEFORE spawning
#
#  _restart_command branches on the platform, so every shape assertion below
#  passes os_name EXPLICITLY.  Without that these tests only ever exercised
#  whichever branch the test machine happened to be (they were written on
#  macOS and went red the first time CI ran them on the Windows runner — the
#  branch that actually ships to the techs).  Both shapes are now asserted on
#  every platform; `os_name` defaults to os.name, so nothing in the app
#  changes.
# ═════════════════════════════════════════════════════════════════════════
def _mk(**kw):
    """_restart_command with sane defaults; pass os_name= per shape."""
    fn = _load_helper("_restart_command", os=os)

    def call(exe="/x/OTDRSuite", marker="/x/blocked", port=8510, wait_s=10,
             os_name="posix"):
        return fn(exe, marker, port, wait_s, os_name=os_name)

    return call(**kw)


def test_restart_command_defaults_to_this_machines_platform():
    """The seam is test-only: with os_name omitted the app gets exactly the
    branch it always got."""
    fn = _load_helper("_restart_command", os=os)
    assert fn("/x/e", "/x/m", 8510, 10) == fn("/x/e", "/x/m", 8510, 10,
                                              os_name=os.name)


def test_restart_command_os_name_beats_the_ambient_platform():
    """The regression this seam exists for: with the module's os.name forced
    the other way (a Windows CI runner asserting the POSIX shape, and the
    reverse), the explicit os_name still decides the branch."""
    class _StubOS:
        path, environ = os.path, {}

    for ambient, asked, head in (("nt", "posix", "/bin/sh"),
                                 ("posix", "nt", "powershell")):
        _StubOS.name = ambient
        cmd = _load_helper("_restart_command", os=_StubOS)(
            "/x/e", "/x/m", 8510, 10, os_name=asked)
        assert cmd[0].lower().replace(".exe", "").endswith(head), (ambient, cmd)


def test_restart_command_posix_waits_before_it_spawns():
    """Ordering lock: the health poll must appear before the exec of the exe,
    and the exe must only start from inside the 'not answering' branch."""
    cmd = _mk(exe="/Apps/OTDR Suite.app", marker="/home/t/.otdrSuite/blocked",
              os_name="posix")
    assert cmd[:2] == ["/bin/sh", "-c"]
    sh = cmd[2]
    probe = sh.index("http://127.0.0.1:8510/_stcore/health")
    spawn = sh.index("exec '/Apps/OTDR Suite.app'")
    assert probe < spawn, f"must poll the health URL before spawning:\n{sh}"
    assert "curl" in sh
    assert "while" in sh[:probe], "the probe must be a retry loop, not one shot"


def test_restart_command_windows_waits_before_it_spawns():
    """Mirror shape — the fleet's actual platform, via PowerShell (present on
    every Win7+ box; -Command is not gated by ExecutionPolicy)."""
    cmd = _mk(exe=r"C:\Program Files\OTDR Suite\OTDRSuite.exe",
              marker=r"C:\Users\t\.otdrSuite\blocked", os_name="nt")
    assert cmd[0].lower().replace(".exe", "").endswith("powershell")
    assert cmd[-2] == "-Command"
    ps = cmd[-1]
    probe = ps.index("Invoke-WebRequest")
    spawn = ps.index("Start-Process")
    marker = ps.index("New-Item")
    assert probe < spawn < marker, f"poll → spawn → (only then) marker:\n{ps}"
    assert "http://127.0.0.1:8510/_stcore/health" in ps
    assert r"C:\Program Files\OTDR Suite\OTDRSuite.exe" in ps


def test_restart_command_posix_writes_the_marker_when_port_never_frees():
    """If the old instance never lets go we must NOT launch into the
    launcher's 'already serving' no-op — write the marker instead."""
    sh = _mk(marker="/home/t/.otdrSuite/blocked", os_name="posix")[2]
    assert sh.rstrip().endswith(": > '/home/t/.otdrSuite/blocked'"), sh
    assert sh.index("exec '/x/OTDRSuite'") < sh.index("'/home/t/.otdrSuite/blocked'")


def test_restart_command_windows_writes_the_marker_when_port_never_frees():
    """Mirror shape — the marker write is the loop's fall-through, reached
    only when Start-Process never ran."""
    ps = _mk(exe=r"C:\OTDRSuite.exe", marker=r"C:\Users\t\.otdrSuite\blocked",
             os_name="nt")[-1]
    assert ps.rstrip().endswith(
        r"New-Item -Force -ItemType File -Path 'C:\Users\t\.otdrSuite\blocked'"
        r"|Out-Null"), ps
    assert "exit 0" in ps[:ps.index("New-Item")], (
        "the spawn branch must exit before the marker write")


def test_restart_command_posix_loop_covers_the_full_wait():
    """~10 s of budget at 0.5 s a turn = 20 turns (the loop bound scales)."""
    assert "$i -lt 20 " in _mk(wait_s=10, os_name="posix")[2]
    assert "$i -lt 4 " in _mk(wait_s=2, os_name="posix")[2]


def test_restart_command_windows_loop_covers_the_full_wait():
    """Mirror shape — PowerShell polls against a wall-clock deadline."""
    assert "AddSeconds(10)" in _mk(wait_s=10, os_name="nt")[-1]
    assert "AddSeconds(2)" in _mk(wait_s=2, os_name="nt")[-1]


def test_restart_command_no_blind_sleep_and_no_shell_string():
    """The old path was `timeout /t 3 & start` through shell=True on Windows
    and `sleep 3; exec` on POSIX — a fixed guess at how long shutdown takes,
    and an unquoted command string."""
    for os_name in ("nt", "posix"):
        cmd = _mk(os_name=os_name)
        assert isinstance(cmd, list), "argv list, not a shell string"
        assert "timeout /t" not in cmd[-1]
        assert "sleep 3;" not in cmd[-1]


# ═════════════════════════════════════════════════════════════════════════
#  3. Behaviour: run the real POSIX argv against a throwaway health server
# ═════════════════════════════════════════════════════════════════════════
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):                                   # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):                          # keep pytest output clean
        pass


def _serve():
    """A health endpoint on a free port; returns (httpd, port)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    httpd = HTTPServer(("127.0.0.1", port), _HealthHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


posix_only = pytest.mark.skipif(
    os.name == "nt" or shutil.which("curl") is None,
    reason="POSIX restart helper (needs /bin/sh + curl)")


@posix_only
def test_restart_helper_spawns_only_after_the_server_goes_quiet(tmp_path):
    """The race the boss hit: while the old server still answers, the helper
    must sit and wait; the moment it stops, the exe starts."""
    httpd, port = _serve()
    stamp = tmp_path / "launched"
    exe = tmp_path / "fake_exe.sh"
    exe.write_text(f'#!/bin/sh\ndate +%s.%N > "{stamp}"\n', encoding="utf-8")
    exe.chmod(0o755)
    marker = tmp_path / "blocked"

    cmd = _mk(exe=str(exe), marker=str(marker), port=port, wait_s=10,
              os_name="posix")
    proc = subprocess.Popen(cmd, start_new_session=True)
    time.sleep(2.0)
    assert not stamp.exists(), "helper launched while the old server was alive"

    httpd.shutdown()
    httpd.server_close()
    deadline = time.time() + 8
    while time.time() < deadline and not stamp.exists():
        time.sleep(0.2)
    proc.wait(timeout=15)
    assert stamp.exists(), "helper never launched after the port was released"
    assert not marker.exists(), "a successful restart must not leave the marker"


@posix_only
def test_restart_helper_marks_blocked_instead_of_reattaching(tmp_path):
    """Server never goes away → do NOT start the exe (that is the silent
    'already serving' re-attach) → leave the marker the UI turns into words."""
    httpd, port = _serve()
    try:
        stamp = tmp_path / "launched"
        exe = tmp_path / "fake_exe.sh"
        exe.write_text(f'#!/bin/sh\ntouch "{stamp}"\n', encoding="utf-8")
        exe.chmod(0o755)
        marker = tmp_path / "blocked"

        cmd = _mk(exe=str(exe), marker=str(marker), port=port, wait_s=2,
                  os_name="posix")
        subprocess.Popen(cmd, start_new_session=True).wait(timeout=30)
        assert not stamp.exists(), "must not re-attach to a live old instance"
        assert marker.exists(), "a blocked restart must leave the marker"
    finally:
        httpd.shutdown()
        httpd.server_close()


# ═════════════════════════════════════════════════════════════════════════
#  4. Source-locks — placement, single restart path, once-per-session
# ═════════════════════════════════════════════════════════════════════════
def test_nudge_renders_above_the_page_radio():
    """It only works if it is the first thing in the sidebar — below the tool
    radio a tech scrolls past it."""
    call = APP_SRC.index("\n    _render_update_nudge()")     # the call, not the def
    radio = APP_SRC.index("page = st.radio(")
    sidebar = APP_SRC.index("with st.sidebar:\n    st.markdown('## 🔬 OTDR Suite')")
    assert sidebar < call < radio, "the nudge belongs at the top of the nav sidebar"


def test_nudge_reuses_the_existing_restart_path():
    """One restart implementation, shared with the footer button.  A second
    copy is how the two paths drift apart."""
    src = _fn_source("_render_update_nudge")
    assert "_relaunch_and_exit()" in src, "the banner button must call the shared restart"
    for dup in ("Popen", "os._exit", "Start-Process", "/bin/sh"):
        assert dup not in src, f"{dup} must live only in _relaunch_and_exit"
    assert APP_SRC.count("\ndef _relaunch_and_exit(") == 1
    assert APP_SRC.count("\ndef _restart_command(") == 1


def test_relaunch_delegates_the_wait_and_drops_the_blind_sleep():
    """_relaunch_and_exit builds the waiting argv instead of guessing a sleep."""
    src = _fn_source("_relaunch_and_exit")
    assert "_restart_command(" in src
    assert "sleep 3" not in src and "timeout /t 3" not in src, (
        "the fixed 3 s guess is what raced the shutdown")
    assert src.index("_restart_command(") < src.index("Popen"), (
        "the waiting command must be built before anything is spawned")
    assert "shell=True" not in src


def test_nudge_fetches_once_per_session_with_a_short_timeout():
    """Cached in session_state, 3 s cap: a rerun-heavy page must not re-hit
    GitHub, and a hung host must not stall the first paint."""
    src = _fn_source("_render_update_nudge")
    assert "'upd_nudge' not in st.session_state" in src, "must cache per session"
    assert "_latest_manifest_version(timeout=3)" in src, "3 s cap on the fetch"
    assert "_nudge_check(" in src


def test_manual_check_for_updates_button_survives_unchanged():
    """The loud path stays: same label, same key, same helpers."""
    assert "'🔄 Check for updates', key='upd_check'" in APP_SRC
    assert "st.session_state['upd_latest'] = _latest_manifest_version()" in APP_SRC
    assert "key='upd_restart'" in APP_SRC          # footer's own restart button
    assert "key='upd_nudge_restart'" in APP_SRC    # banner's, distinct key


def test_app_py_is_the_only_engine_file_touched():
    """app.py already ships in ENGINE_FILES, so the nudge auto-updates with no
    manifest file-set change (the launcher rejects a manifest whose set
    differs) — and nothing new was added to that list."""
    launcher = (REPO_ROOT / "desktop" / "launcher.py").read_text(encoding="utf-8")
    block = launcher.split("ENGINE_FILES = [", 1)[1].split("]", 1)[0]
    assert '"app.py",' in block
    assert len([l for l in block.splitlines() if l.strip().startswith('"')]) == 21


# ═════════════════════════════════════════════════════════════════════════
#  5. AppTest — what the tech actually sees
# ═════════════════════════════════════════════════════════════════════════
def _fake_manifest(version):
    """Stand in for raw.githubusercontent.com without touching the network."""
    body = json.dumps({"version": version, "commit": "abc1234"}).encode()

    class _Resp:
        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return lambda req, timeout=None, **kw: _Resp()


def _fake_home(monkeypatch, tmp_path):
    """Point ~ at tmp_path on BOTH platforms and return the marker path the app
    will actually use.  posixpath.expanduser reads HOME; ntpath.expanduser
    ignores HOME entirely and reads USERPROFILE (then HOMEDRIVE+HOMEPATH) —
    patching only HOME left the Windows runner writing/reading the real
    profile, which is what made this test unrunnable there."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    drive, tail = os.path.splitdrive(str(tmp_path))
    monkeypatch.setenv("HOMEDRIVE", drive)
    monkeypatch.setenv("HOMEPATH", tail)
    marker = _load_helper("_restart_marker_path", os=os)()
    assert os.path.realpath(marker).startswith(os.path.realpath(str(tmp_path))), (
        f"~ still resolves outside tmp_path: {marker}")
    return marker


def _arm(monkeypatch, tmp_path, applied_label, urlopen):
    """Frozen-build identity + a stubbed manifest fetch + an isolated home
    (the restart marker and the rollout-ping marker both live in ~/.otdrSuite)."""
    marker = _fake_home(monkeypatch, tmp_path)
    monkeypatch.delenv("SS_ERROR_WEBHOOK", raising=False)
    monkeypatch.setattr(R, "version_labels", lambda *a, **k: applied_label)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return marker


def _sidebar_text(at):
    out = []
    for kind in ("warning", "error", "info", "success", "caption"):
        out += [e.value for e in getattr(at.sidebar, kind)]
    return out


def test_apptest_banner_shows_applied_134_vs_live_136(monkeypatch, tmp_path):
    """(a) applied 134 + published 136 → the banner, worded for a tech."""
    _arm(monkeypatch, tmp_path,
         ("build 134 (2026-08-01)", "update 134 applied 2026-08-01 09:00 PDT"),
         _fake_manifest(136))
    at = run_streamlit().run()
    assert not at.exception, f"page raised: {list(at.exception)}"
    assert any("Update 136 is available (running 134)" in t
               for t in _sidebar_text(at)), _sidebar_text(at)


def test_apptest_no_banner_when_current(monkeypatch, tmp_path):
    """(a) applied 136 + published 136 → nothing at all."""
    _arm(monkeypatch, tmp_path,
         ("build 136 (2026-08-05)", "update 136 applied 2026-08-05 09:00 PDT"),
         _fake_manifest(136))
    at = run_streamlit().run()
    assert not at.exception, f"page raised: {list(at.exception)}"
    assert not any("is available" in t for t in _sidebar_text(at)), _sidebar_text(at)


def test_apptest_no_banner_when_fetch_raises(monkeypatch, tmp_path):
    """(a) the update server is unreachable → silence, not an error box."""
    def boom(*a, **k):
        raise OSError("no route to host")

    _arm(monkeypatch, tmp_path, ("build 134 (2026-08-01)", "bundled"), boom)
    at = run_streamlit().run()
    assert not at.exception, f"page raised: {list(at.exception)}"
    assert not any("is available" in t for t in _sidebar_text(at)), _sidebar_text(at)


def test_apptest_dev_checkout_never_touches_the_network(monkeypatch, tmp_path):
    """A dev run (this suite) must not fetch the manifest at all."""
    hits = []

    def spy(*a, **k):
        hits.append(1)
        raise OSError("blocked")

    monkeypatch.delenv("OTDR_SUITE_SOURCE", raising=False)
    _fake_home(monkeypatch, tmp_path)
    monkeypatch.setattr(urllib.request, "urlopen", spy)
    at = run_streamlit().run()
    assert not at.exception, f"page raised: {list(at.exception)}"
    assert hits == [], "dev run hit the update server"


def test_apptest_frozen_build_gets_the_restart_button(monkeypatch, tmp_path):
    """The banner is actionable on a real install: a primary button keyed
    upd_nudge_restart, distinct from the footer's."""
    _arm(monkeypatch, tmp_path, ("build 134 (2026-08-01)", "bundled"),
         _fake_manifest(136))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    at = run_streamlit().run()
    assert not at.exception, f"page raised: {list(at.exception)}"
    assert any(b.key == "upd_nudge_restart" for b in at.sidebar.button), (
        [b.key for b in at.sidebar.button])


def test_apptest_blocked_restart_marker_becomes_a_visible_message(monkeypatch, tmp_path):
    """The helper couldn't get the port back → the tech is told what to do,
    and the marker is consumed so it doesn't nag the next boot."""
    marker = _arm(monkeypatch, tmp_path, ("build 136 (2026-08-05)", "bundled"),
                  _fake_manifest(136))
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write("blocked")
    at = run_streamlit().run()
    assert not at.exception, f"page raised: {list(at.exception)}"
    assert any("previous OTDR Suite is still running" in t
               for t in _sidebar_text(at)), _sidebar_text(at)
    assert not os.path.exists(marker), "the marker must be consumed once shown"
