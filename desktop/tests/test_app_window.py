"""The app window: the hub shows in its own native window, not a browser tab.

launcher.py starts a second copy of itself with WINDOW_ARG; that copy shows
APP_URL through pywebview (Edge WebView2 on Windows).  These tests pin the
parts that decide what the tech sees: one window, raised rather than
duplicated; a browser tab whenever the window cannot open; the Viewer pop-out
kept as a real popup; and the pywebview defaults we override.  pywebview is
never imported for real here -- a fake module stands in."""
from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

from conftest import REPO_ROOT

LAUNCHER = REPO_ROOT / "desktop" / "launcher.py"


@pytest.fixture
def L(tmp_path, monkeypatch):
    monkeypatch.delenv("OTDR_SUITE_BROWSER", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    spec = importlib.util.spec_from_file_location("launcher_window", str(LAUNCHER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "WINDOW_START_S", 10)
    return mod


@pytest.fixture
def opened(L, monkeypatch):
    calls = []
    monkeypatch.setattr(L.webbrowser, "open", lambda u: calls.append(u))
    return calls


def _child(L, monkeypatch, body):
    """Make _spawn_window start `body` (python) instead of the real window."""
    monkeypatch.setattr(L, "_window_command",
                        lambda: [sys.executable, "-c", textwrap.dedent(body)])


# ── _show_app: window first, browser tab as the fallback ────────────────
def test_browser_env_forces_the_tab(L, opened, monkeypatch):
    monkeypatch.setenv("OTDR_SUITE_BROWSER", "1")
    monkeypatch.setattr(L, "_spawn_window", lambda: pytest.fail("spawned"))
    L._show_app()
    assert opened == [L.APP_URL]


def test_a_window_that_shows_the_page_means_no_tab(L, opened, monkeypatch):
    ready = L._window_ready_path()
    _child(L, monkeypatch, f"""
        import os, time
        open({str(ready)!r}, "w", encoding="utf-8").write(str(os.getpid()))
        time.sleep(3)
    """)
    L._show_app()
    assert opened == []


def test_a_window_that_cannot_start_falls_back_to_a_tab(L, opened, monkeypatch):
    """No WebView2 runtime / pywebview missing: the child dies with an error."""
    _child(L, monkeypatch, "import sys; sys.exit(3)")
    L._show_app()
    assert opened == [L.APP_URL]


def test_a_missing_exe_falls_back_to_a_tab(L, opened, monkeypatch):
    monkeypatch.setattr(L, "_window_command",
                        lambda: [str(Path(L.__file__).parent / "no-such-exe")])
    L._show_app()
    assert opened == [L.APP_URL]


def test_a_stale_ready_file_from_another_pid_is_not_success(L, opened, monkeypatch):
    L._window_ready_path().parent.mkdir(parents=True, exist_ok=True)
    L._window_ready_path().write_text("1", encoding="utf-8")
    _child(L, monkeypatch, "import sys; sys.exit(3)")
    L._show_app()
    assert opened == [L.APP_URL]


def test_losing_the_race_to_another_window_is_success(L, opened, monkeypatch):
    """Exit 0 = a window was already up and was raised instead."""
    _child(L, monkeypatch, "import sys; sys.exit(0)")
    L._show_app()
    assert opened == []


def test_an_open_window_is_raised_not_duplicated(L, opened, monkeypatch, tmp_path):
    if sys.platform == "win32":
        pytest.skip("holder uses fcntl; Windows is covered by the CI window test")
    lock = L._window_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import fcntl, sys, time
        fh = open({str(lock)!r}, "a+b"); fh.write(b"\\0"); fh.flush()
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        print("held", flush=True); time.sleep(30)
    """)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        monkeypatch.setattr(L, "_spawn_window", lambda: pytest.fail("spawned"))
        L._show_app()
        assert opened == []
        assert L._window_raise_path().exists()
    finally:
        holder.kill()
        holder.wait()


def test_the_window_role_exits_quietly_when_one_is_open(L, monkeypatch):
    held = L._lock_file(L._window_lock_path())
    assert held not in (None, True)
    if sys.platform != "win32":
        # flock is per open file: a second open in THIS process conflicts too.
        monkeypatch.setattr(L.sys, "argv", ["OTDRSuite", L.WINDOW_ARG])
        monkeypatch.setattr(L, "_redirect_output_to_log", lambda: None)
        monkeypatch.setattr(L, "_run_window", lambda: pytest.fail("second window"))
        assert L._maybe_run_window() == 0
        assert L._window_raise_path().exists()
    held.close()


def test_not_the_window_role_is_none(L, monkeypatch):
    monkeypatch.setattr(L.sys, "argv", ["OTDRSuite"])
    assert L._maybe_run_window() is None


def test_a_window_that_crashes_reports_an_error_code(L, monkeypatch):
    monkeypatch.setattr(L.sys, "argv", ["OTDRSuite", L.WINDOW_ARG])
    monkeypatch.setattr(L, "_redirect_output_to_log", lambda: None)
    monkeypatch.setattr(L, "_run_window", lambda: 1 / 0)
    assert L._maybe_run_window() == 3


# ── main(): the window role is dispatched before anything server-side ───
def _main_source():
    src = LAUNCHER.read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "main")
    return ast.get_source_segment(src, fn)


def test_the_window_never_takes_the_boot_lock_or_starts_a_server():
    src = _main_source()
    assert src.index("_maybe_run_window") < src.index("_take_boot_lock")
    assert src.index("_maybe_run_window") < src.index("_redirect_output_to_log")


def test_main_never_opens_a_tab_directly():
    """Every path goes through _show_app, so none of them skips the window."""
    assert "webbrowser.open" not in _main_source()


# ── the Viewer pop-out stays a real popup ──────────────────────────────
def test_hub_urls(L):
    assert L._is_hub_url(L.APP_URL)
    assert L._is_hub_url(L.APP_URL + "/?dir=both&fiber=12")
    assert L._is_hub_url("about:blank")        # window.open('', 'otdr_hub')
    assert not L._is_hub_url("https://github.com/lakeosoyoos")
    assert not L._is_hub_url(L.APP_URL + "0/")  # another port


class _Args:
    def __init__(self, uri):
        self.uri, self.handled = uri, False

    def get_Uri(self):
        return self.uri


def test_pop_out_is_left_to_webview2_and_links_go_to_the_browser(L, monkeypatch):
    sent = []

    class EdgeChrome:
        def on_new_window_request(self, sender, args):
            args.handled = True
            sent.append(args.get_Uri())

    edge = types.ModuleType("webview.platforms.edgechromium")
    edge.EdgeChrome = EdgeChrome
    pkg = types.ModuleType("webview.platforms")
    pkg.edgechromium = edge
    monkeypatch.setitem(sys.modules, "webview", types.ModuleType("webview"))
    monkeypatch.setitem(sys.modules, "webview.platforms", pkg)
    monkeypatch.setitem(sys.modules, "webview.platforms.edgechromium", edge)

    L._let_the_viewer_pop_out()
    viewer = _Args(L.APP_URL + "/?dir=both&fiber=7")
    EdgeChrome().on_new_window_request(None, viewer)
    assert viewer.handled is False and sent == []
    link = _Args("https://example.com/")
    EdgeChrome().on_new_window_request(None, link)
    assert link.handled is True and sent == ["https://example.com/"]


def test_pop_out_hook_without_pywebview_is_harmless(L, monkeypatch):
    monkeypatch.setitem(sys.modules, "webview.platforms.edgechromium", None)
    L._let_the_viewer_pop_out()


# ── the pywebview defaults we override ─────────────────────────────────
def test_run_window_settings(L, monkeypatch):
    made, started = {}, {}

    class Event(list):
        def __iadd__(self, fn):
            self.append(fn)
            return self

    class Window:
        def __init__(self):
            self.events = types.SimpleNamespace(loaded=Event())

    def create_window(title, url, **kw):
        made.update(kw, title=title, url=url)
        made["window"] = Window()
        return made["window"]

    def start(func, args, **kw):
        started.update(kw)
        for fn in made["window"].events.loaded:
            fn()

    fake = types.ModuleType("webview")
    fake.settings = {"ALLOW_DOWNLOADS": False, "OPEN_EXTERNAL_LINKS_IN_BROWSER": True}
    fake.create_window, fake.start = create_window, start
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(L, "_let_the_viewer_pop_out", lambda: None)
    monkeypatch.setattr(L, "_quit_server", lambda: None)

    assert L._run_window() == 0
    assert made["url"] == L.APP_URL and made["title"] == "OTDR Suite"
    assert made["text_select"] is True, "techs copy values out of the grids"
    assert fake.settings["ALLOW_DOWNLOADS"] is True, "report downloads"
    assert started["private_mode"] is False, "Viewer settings live in localStorage"
    assert Path(started["storage_path"]).parent == Path.home() / L.APP_DIR_NAME
    assert L._window_ready_path().read_text(encoding="utf-8") == str(__import__("os").getpid())


def _fake_webview(monkeypatch, L):
    fake = types.ModuleType("webview")
    fake.settings = {}
    class Event(list):
        def __iadd__(self, fn):
            self.append(fn)
            return self
    win = types.SimpleNamespace(events=types.SimpleNamespace(loaded=Event()))
    fake.create_window = lambda *a, **k: win
    fake.start = lambda *a, **k: None          # returns = the tech closed it
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(L, "_let_the_viewer_pop_out", lambda: None)


# ── closing the window quits the app ───────────────────────────────────
def test_closing_the_window_stops_the_server(L, monkeypatch):
    _fake_webview(monkeypatch, L)
    stopped = []
    monkeypatch.setattr(L, "_health_ok", lambda: True)
    monkeypatch.setattr(L, "_pid_listening_on", lambda port: 4242)
    monkeypatch.setattr(L, "_stop_pid", stopped.append)
    assert L._run_window() == 0
    assert stopped == [4242]


def test_the_port_owner_wins_over_a_stale_running_record(L, monkeypatch):
    """After Update & restart the open window talks to the NEW server."""
    L._running_path().parent.mkdir(parents=True, exist_ok=True)
    L._running_path().write_text('{"pid": 1111, "version": 1}', encoding="utf-8")
    monkeypatch.setattr(L, "_health_ok", lambda: True)
    monkeypatch.setattr(L, "_pid_listening_on", lambda port: 2222)
    assert L._server_pid() == 2222
    monkeypatch.setattr(L, "_pid_listening_on", lambda port: None)
    assert L._server_pid() == 1111                 # no netstat (dev box)


def test_nothing_serving_means_nothing_to_stop(L, monkeypatch):
    _fake_webview(monkeypatch, L)
    monkeypatch.setattr(L, "_health_ok", lambda: False)
    monkeypatch.setattr(L, "_stop_pid", lambda pid: pytest.fail("stopped"))
    assert L._run_window() == 0


def test_a_window_that_crashes_does_not_stop_the_server(L, monkeypatch):
    """Only a close quits; a crash falls back to the tab, server kept."""
    monkeypatch.setattr(L.sys, "argv", ["OTDRSuite", L.WINDOW_ARG])
    monkeypatch.setattr(L, "_redirect_output_to_log", lambda: None)
    fake = types.ModuleType("webview")
    fake.settings = {}
    def boom(*a, **k):
        raise RuntimeError("WebView2 runtime not found")
    fake.create_window = boom
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(L, "_let_the_viewer_pop_out", lambda: None)
    monkeypatch.setattr(L, "_stop_pid", lambda pid: pytest.fail("stopped"))
    assert L._maybe_run_window() == 3


# ── packaging ───────────────────────────────────────────────────────────
def test_pywebview_is_pinned_for_windows():
    req = (REPO_ROOT / "desktop" / "requirements-desktop.txt").read_text(encoding="utf-8")
    assert 'pywebview==6.2.1; sys_platform == "win32"' in req


def test_the_spec_bundles_pywebview():
    spec = (REPO_ROOT / "desktop" / "OTDRSuite.spec").read_text(encoding="utf-8")
    line = next(l for l in spec.splitlines() if l.startswith("_optional"))
    for name in ("webview", "clr_loader", "pythonnet"):
        assert f'"{name}"' in line
