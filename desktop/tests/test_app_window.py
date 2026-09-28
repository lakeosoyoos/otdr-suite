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
def test_own_urls(L):
    assert L._is_own_url(L.APP_URL)
    assert L._is_own_url(L.APP_URL + "/?dir=both&fiber=12")
    assert L._is_own_url("about:blank")        # window.open('', 'otdr_hub')
    # The Viewer pop-out is the TRACE SERVER's page, on its own port.  The
    # first cut allowed only the hub's port and sent this to Edge (VM test,
    # 2026-09-27).
    assert L._is_own_url("http://127.0.0.1:8771/")
    assert L._is_own_url("http://127.0.0.1:8772/?dir=both&fiber=3&km=14.5&src=sr")
    assert L._is_own_url("http://localhost:8781/phone")   # Field Capture
    assert not L._is_own_url("https://github.com/lakeosoyoos")
    assert not L._is_own_url("http://127.0.0.2.example.com/")
    assert not L._is_own_url("file:///C:/Windows/notepad.exe")
    assert not L._is_own_url("not a url")


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
    viewer = _Args("http://127.0.0.1:8771/?dir=both&fiber=7")   # trace server
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
    assert made["url"] == L.APP_URL and made["title"] == L.EDITION
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


# ── no Streamlit toolbar (Deploy + three-dot menu) in the App ──────────
def test_the_app_hides_streamlits_deploy_and_menu(L, monkeypatch):
    monkeypatch.delenv("STREAMLIT_CLIENT_TOOLBAR_MODE", raising=False)
    L._export_edition()
    assert __import__("os").environ["STREAMLIT_CLIENT_TOOLBAR_MODE"] == "minimal"


def test_minimal_hides_the_menu_only_while_the_hub_adds_no_items():
    """"minimal" keeps any menu_items set_page_config adds; the hub adds none,
    which is what makes the three dots go away."""
    assert "menu_items" not in (REPO_ROOT / "app.py").read_text(encoding="utf-8")


def test_a_no_update_build_has_no_check_for_updates_button():
    """The App never updates itself, so the button could only ever report
    "could not reach the update server".  It is replaced, not left dead."""
    src = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    guard = src.index("if os.environ.get('OTDR_SUITE_NO_UPDATE'):\n    # This build never")
    button = src.index("'🔄 Check for updates', key='upd_check'")
    assert guard < button < guard + 400, "the button must sit in the else branch"


# ── Projects: a double-clicked .zfc/.zdb reaches the open window ────────
def test_the_hub_and_the_launcher_agree_on_the_open_request(L, monkeypatch):
    """The launcher writes it in ITS folder; the hub reads it there because
    the launcher exported that folder as OTDR_SUITE_APP_DIR."""
    import os
    from test_engine_self_verify import _load_app_helper
    monkeypatch.setenv("OTDR_SUITE_APP_DIR", str(Path.home() / L.APP_DIR_NAME))
    assert L._write_open_request(str(Path.home() / "job.zdb"))
    got = _load_app_helper("_consume_open_request", os=os, time=__import__("time"),
                           json=__import__("json"))()
    assert got and got.endswith("job.zdb")


def _run_watcher_once(L, monkeypatch, window=None):
    """Run _watch_for_raise through exactly one raise signal."""
    calls = []
    monkeypatch.setattr(L, "_reload_page", lambda w: calls.append("reload"))
    monkeypatch.setattr(L, "_bring_forward", lambda w: calls.append("forward"))
    L._window_raise_path().parent.mkdir(parents=True, exist_ok=True)
    ticks = iter(range(3))

    def sleep(_):
        if next(ticks, None) == 0:
            L._window_raise_path().write_text("x", encoding="utf-8")
        elif calls:
            raise SystemExit
    monkeypatch.setattr(L.time, "sleep", sleep)
    with pytest.raises(SystemExit):
        L._watch_for_raise(window or object())
    return calls


def test_an_open_window_reloads_for_a_waiting_file(L, monkeypatch):
    L._window_raise_path().parent.mkdir(parents=True, exist_ok=True)
    L._write_open_request(str(Path.home() / "job.zfc"))
    assert _run_watcher_once(L, monkeypatch) == ["reload", "forward"]


def test_a_plain_raise_does_not_reload(L, monkeypatch):
    """No file waiting: bring it forward, keep the tech's page as it is."""
    assert _run_watcher_once(L, monkeypatch) == ["forward"]


def test_a_failed_reload_still_brings_the_window_forward(L, monkeypatch):
    L._window_raise_path().parent.mkdir(parents=True, exist_ok=True)
    L._write_open_request(str(Path.home() / "job.zdb"))
    calls = []

    def boom(w):
        raise RuntimeError("page not loaded")
    monkeypatch.setattr(L, "_reload_page", boom)
    monkeypatch.setattr(L, "_bring_forward", lambda w: calls.append("forward"))
    ticks = iter(range(3))

    def sleep(_):
        if next(ticks, None) == 0:
            L._window_raise_path().write_text("x", encoding="utf-8")
        elif calls:
            raise SystemExit
    monkeypatch.setattr(L.time, "sleep", sleep)
    with pytest.raises(SystemExit):
        L._watch_for_raise(object())
    assert calls == ["forward"]


def test_the_reload_is_a_real_reload_not_load_url(L):
    """load_url(APP_URL) sets WebView2's Source to the URL it already shows,
    which is no navigation at all: on the VM test the request sat unclaimed."""
    seen = []

    class Win:
        def evaluate_js(self, js):
            seen.append(js)

        def load_url(self, url):
            pytest.fail("load_url of the same URL does not navigate")

    L._reload_page(Win())
    assert len(seen) == 1 and "location.reload()" in seen[0]
    assert "setTimeout" in seen[0], "evaluate_js must get its answer first"


def test_bring_forward_keeps_a_maximized_window_maximized():
    """On Windows the raise must not use pywebview's restore() (it sets the
    form to Normal = un-maximizes: VM test) nor its on_top setter (it changes
    the form from the watcher thread).  Plain Win32, asynchronous."""
    src = LAUNCHER.read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "_bring_forward")
    body = ast.get_source_segment(src, fn)
    windows_part = body[body.index("import ctypes"):]
    assert "window.restore" not in windows_part and "on_top" not in windows_part
    assert "IsIconic" in windows_part and "SW_RESTORE" in windows_part
    assert "SWP_ASYNCWINDOWPOS" in windows_part


def test_a_second_launch_passes_on_the_foreground(L, monkeypatch):
    """Windows only lets the process the tech is using take the foreground:
    the asking launch hands that right to the window before signalling."""
    order = []
    monkeypatch.setattr(L, "_let_the_window_take_focus", lambda: order.append("allow"))
    monkeypatch.setattr(L, "_lock_file", lambda p: None)          # a window is open
    real_write = Path.write_text

    def write(self, *a, **k):
        if self.name == "window.raise":
            order.append("raise")
        return real_write(self, *a, **k)
    monkeypatch.setattr(Path, "write_text", write)
    L._window_raise_path().parent.mkdir(parents=True, exist_ok=True)
    assert L._ask_open_window_to_raise() is True
    assert order == ["allow", "raise"]


# ── closing the App stops OUR processes, never the tech's browser ───────
# VM test 2026-09-27: `taskkill /T` on the server ended Edge and every tab in
# it, because the Viewer pop-out had started Edge as a child of the App.
TABLE = {
    100: (1, "otdrsuite.exe"),        # the hub server
    101: (100, "otdrsuite.exe"),      # the app window
    102: (100, "otdrsuite.exe"),      # a Splice Report engine subprocess
    103: (102, "otdrsuite.exe"),      #   ...and one it started
    104: (101, "msedge.exe"),         # Edge, started by a link from the window
    105: (104, "msedge.exe"),         #   ...and its renderers
    106: (100, "conhost.exe"),
    200: (1, "otdrsuite.exe"),        # the REGULAR OTDR Suite (another tree)
}


def test_own_process_tree_is_our_program_only(L):
    got = L._own_process_tree(100, TABLE)
    assert set(got) == {100, 101, 102, 103}
    assert got[-1] == 100, "children first, the server last"
    assert got.index(103) < got.index(102)


def test_own_process_tree_survives_a_parent_loop(L):
    table = {1: (2, "otdrsuite.exe"), 2: (1, "otdrsuite.exe")}   # pid reuse
    assert set(L._own_process_tree(1, table)) == {1, 2}


def test_stop_never_uses_a_tree_kill(L, monkeypatch):
    import subprocess
    ran = []
    monkeypatch.setattr(L.os, "name", "nt")
    monkeypatch.setattr(L, "_process_table", lambda: TABLE)
    monkeypatch.setattr(L.os, "getpid", lambda: 101)          # we are the window
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: ran.append(args))
    L._stop_pid(100)
    assert all("/T" not in a for a in ran)
    stopped = [int(a[a.index("/PID") + 1]) for a in ran]
    assert set(stopped) == {100, 102, 103}, "not Edge, not ourselves, not the regular app"
    assert stopped[-1] == 100


def test_stop_falls_back_to_the_server_alone(L, monkeypatch):
    import subprocess
    ran = []
    monkeypatch.setattr(L.os, "name", "nt")

    def broken():
        raise OSError("no snapshot")
    monkeypatch.setattr(L, "_process_table", broken)
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: ran.append(args))
    L._stop_pid(100)
    assert ran == [["taskkill", "/PID", "100", "/F"]]


# ── the Viewer pop-out's "Back to reports" raises the App's main window ─
# VM test 2026-09-27: from the pop-out WebView2 cannot find the hub window by
# name, so window.open('', 'otdr_hub') opened a SECOND hub window.
def test_raise_hub_without_the_app_is_false(tmp_path, monkeypatch):
    from conftest import import_trace_server
    ts = import_trace_server()
    monkeypatch.delenv("OTDR_SUITE_APP_DIR", raising=False)
    assert ts.raise_app_window() is False           # regular OTDR Suite
    monkeypatch.setenv("OTDR_SUITE_APP_DIR", str(tmp_path))
    assert ts.raise_app_window() is False           # App, but no window lock
    (tmp_path / "window.lock").write_bytes(b"\0")
    assert ts.raise_app_window() is False           # lock file left, nobody holds it
    assert not (tmp_path / "window.raise").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="holder uses fcntl; CI's window step covers Windows")
def test_raise_hub_with_the_app_window_signals_it(tmp_path, monkeypatch):
    from conftest import import_trace_server
    ts = import_trace_server()
    lock = tmp_path / "window.lock"
    holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import fcntl, time
        fh = open({str(lock)!r}, "a+b"); fh.write(b"\\0"); fh.flush()
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        print("held", flush=True); time.sleep(30)
    """)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        monkeypatch.setenv("OTDR_SUITE_APP_DIR", str(tmp_path))
        assert ts.raise_app_window() is True
        assert (tmp_path / "window.raise").exists()
    finally:
        holder.kill()
        holder.wait()


def test_back_to_reports_asks_the_app_first():
    html = (REPO_ROOT / "viewer" / "viewer.html").read_text(encoding="utf-8")
    handler = html[html.index("getElementById('btn-back').addEventListener"):]
    handler = handler[:handler.index("function openHubByName")]
    assert "fetch('/api/raise_hub'" in handler
    assert "openHubByName(url)" in handler and "window.open(" not in handler, (
        "the named-window route runs only when no App window took the raise")
    src = (REPO_ROOT / "viewer" / "trace_server.py").read_text(encoding="utf-8")
    route = src[src.index("if u.path == '/api/raise_hub':"):][:300]
    assert "_origin_is_local()" in route, "a page on another origin must not raise it"
