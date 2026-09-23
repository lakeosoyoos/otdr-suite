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
    relaunched exe itself waits for that health endpoint to go quiet BEFORE
    its already-serving guard runs (launcher._drain_old_instance), and leaves
    a marker the hub turns into a visible message if it never does.  (This
    used to be a detached PowerShell / sh+curl helper; the Windows one never
    ran on a Windows box before it shipped and the boss reported that Update
    did not restart the app, so the helper is gone.)

Style follows test_update_button.py: helpers are lifted out of app.py by AST
(no Streamlit, no network, no engine imports), the wiring is source-locked,
and the rendering is exercised through the AppTest hub.  The launcher half
(the wait) is driven for real in test_update_restart_drain.py.
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
    globals it closes over — _restart_spawn_args needs `os`."""
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
#  2. _restart_spawn_args — the exe relaunches ITSELF, and the launcher waits
#
#  The shape branches on the platform, so every assertion below passes
#  os_name EXPLICITLY: both shapes are asserted on every platform, and the
#  Windows one is the shape that actually ships to the techs.
# ═════════════════════════════════════════════════════════════════════════
def _const(name):
    """Value of one top-level constant of app.py."""
    for node in ast.parse(APP_SRC).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise KeyError(name)


def _spawn(exe="/x/OTDRSuite", pid=4242, environ=None, os_name="posix"):
    fn = _load_helper("_restart_spawn_args", os=os,
                      RESTART_ENV=_const("RESTART_ENV"),
                      _LAUNCHER_DERIVED_ENV=_const("_LAUNCHER_DERIVED_ENV"))
    return fn(exe, pid, {} if environ is None else environ, os_name=os_name)


def test_restart_env_name_matches_the_launcher():
    """The hub sets it, the launcher reads it; one typo and the new exe boots
    straight into the already-serving guard."""
    launcher = (REPO_ROOT / "desktop" / "launcher.py").read_text(encoding="utf-8")
    assert f'RESTART_ENV = "{_const("RESTART_ENV")}"' in launcher


def test_spawn_is_the_exe_itself_with_no_shell_and_no_helper():
    for os_name in ("nt", "posix"):
        argv, kw = _spawn(exe=r"C:\Program Files\OTDR Suite\OTDRSuite.exe",
                          os_name=os_name)
        assert argv == [r"C:\Program Files\OTDR Suite\OTDRSuite.exe"], argv
        assert "shell" not in kw
        joined = " ".join(map(str, argv))
        for gone in ("powershell", "/bin/sh", "curl", "Invoke-WebRequest"):
            assert gone not in joined


def test_spawn_tells_the_new_exe_who_to_wait_for():
    for os_name in ("nt", "posix"):
        _, kw = _spawn(pid=777, os_name=os_name)
        assert kw["env"][_const("RESTART_ENV")] == "777"


def test_spawn_strips_what_the_launcher_derives_but_keeps_the_rest():
    env = {"PATH": "/usr/bin", "OTDR_SUITE_HOME": "/old/engine",
           "OTDR_SUITE_SOURCE": "cached", "OTDR_SUITE_CACHE_PINNED": "yes",
           "OTDR_SUITE_NO_UPDATE": "1"}
    _, kw = _spawn(environ=env)
    assert kw["env"]["PATH"] == "/usr/bin"
    assert kw["env"]["OTDR_SUITE_NO_UPDATE"] == "1", "a tech's own setting stays"
    for k in ("OTDR_SUITE_HOME", "OTDR_SUITE_SOURCE", "OTDR_SUITE_CACHE_PINNED"):
        assert k not in kw["env"], f"{k} must be worked out afresh by the new boot"
    assert env == {"PATH": "/usr/bin", "OTDR_SUITE_HOME": "/old/engine",
                   "OTDR_SUITE_SOURCE": "cached", "OTDR_SUITE_CACHE_PINNED": "yes",
                   "OTDR_SUITE_NO_UPDATE": "1"}, "the caller's environ is untouched"


def test_spawn_outlives_the_instance_that_started_it():
    """We os._exit 0.7 s after spawning; the child must not go with us."""
    _, nt = _spawn(os_name="nt")
    assert nt["creationflags"] & 0x00000008, "DETACHED_PROCESS"
    assert nt["creationflags"] & 0x00000200, "CREATE_NEW_PROCESS_GROUP"
    assert "start_new_session" not in nt
    _, posix = _spawn(os_name="posix")
    assert posix["start_new_session"] is True
    assert "creationflags" not in posix
    assert nt["close_fds"] is True and posix["close_fds"] is True


def test_spawn_os_name_beats_the_ambient_platform():
    class _StubOS:
        name = "nt"
        environ = {}
    fn = _load_helper("_restart_spawn_args", os=_StubOS,
                      RESTART_ENV="X", _LAUNCHER_DERIVED_ENV=())
    assert "start_new_session" in fn("/e", 1, {}, os_name="posix")[1]
    assert "creationflags" in fn("/e", 1, {})[1], "defaults to os.name"


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
    assert APP_SRC.count("\ndef _restart_spawn_args(") == 1
    assert "_restart_command" not in APP_SRC, "the shell helper is gone"


def test_relaunch_spawns_the_exe_and_never_a_shell():
    src = _fn_source("_relaunch_and_exit")
    assert "_restart_spawn_args(sys.executable, os.getpid(), os.environ)" in src
    assert src.index("_restart_spawn_args(") < src.index("Popen")
    for gone in ("shell=True", "sleep 3", "timeout /t 3", "powershell", "curl"):
        assert gone not in src


def test_nudge_fetches_once_per_recheck_window_with_a_short_timeout():
    """Cached with a TTL, 3 s cap: a rerun-heavy page must not re-hit GitHub,
    and a hung host must not stall the first paint.

    The cache MOVED out of this function into _stale_check, because a
    once-per-session check meant a machine that stays open for days never
    noticed a publish.  The banner must now READ that shared answer instead of
    keeping a second one of its own — one cache, one fetch, and a banner that
    can never disagree with the report block about whether this engine is
    stale.  (Behaviour, not just shape, is asserted in
    test_stale_engine_gate.py.)"""
    src = _fn_source("_render_update_nudge")
    assert "_update_state()" in src, "the banner must use the shared check"
    # The session_state KEY, quoted — bare 'upd_nudge' also matches the
    # banner's own button key 'upd_nudge_restart', which must stay.
    assert "'upd_nudge'" not in src, "no second per-session cache of its own"
    state = _fn_source("_update_state")
    assert "_latest_manifest_version(timeout=3)" in state, "3 s cap on the fetch"
    assert "_stale_check(" in state
    assert "_nudge_check(" in _fn_source("_stale_check")
    assert APP_SRC.count("\ndef _nudge_check(") == 1, "one version compare only"


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
    assert len([l for l in block.splitlines() if l.strip().startswith('"')]) == 34


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
