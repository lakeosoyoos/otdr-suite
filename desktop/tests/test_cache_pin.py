"""A machine that keeps losing engine files stops being asked to keep them.

One tech lost a different engine .py out of ~/.otdrSuite/engine twice in
three days, each time after a hash-verified download, while the same files
in his install directory were never touched.  The Repair button re-downloads,
the file goes again, and he is back on the same page.  Nobody has said what
removes the files, and the only fix on offer was an IT ticket.

So the launcher now counts losses.  The second one inside a week pins the
machine to the bundled engine (the copy that has survived everywhere), the
hub says so and points at the installer, and installing a newer build clears
the pin.  These tests drive that from both ends, plus the report that was
being killed by the restart before it could leave the machine.
"""
from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

import pytest

from test_engine_self_verify import (APP_SRC, LOST, _engine, _load_app_helper,
                                     _load_launcher)

REPAIR = ".otdrSuite/repair_requested"
PIN_ENV = "OTDR_SUITE_CACHE_PINNED"


@pytest.fixture(autouse=True)
def _no_pin_leaks():
    """The launcher SETS the pin variable on the real os.environ.  monkeypatch
    only restores what existed before a test, so a variable that was absent
    and then set would leak into every later test, including the AppTest hub
    in test_update_nudge.py, which would then show the pin notice instead of
    the update banner."""
    os.environ.pop(PIN_ENV, None)
    yield
    os.environ.pop(PIN_ENV, None)


# ═════════════════════════════════════════════════════════════════════════
#  driving the launcher
# ═════════════════════════════════════════════════════════════════════════

def _fetch_ok(L, version, calls):
    """A _try_auto_update stand-in that lands a complete engine in staging."""
    def fetch(staging):
        calls.append(version)
        _, hashes = _engine(L, staging, f"FETCHED {version}")
        return {"__version_int": version, "files": hashes, "commit": "abc"}
    return fetch


def _boot(L, tmp_path, monkeypatch, reasons, fetch=None, bundled_build=400):
    monkeypatch.delenv("OTDR_SUITE_NO_UPDATE", raising=False)
    monkeypatch.delenv(L.CACHE_PINNED_ENV, raising=False)
    monkeypatch.setattr(L, "update_signing_configured", lambda: True)
    monkeypatch.setattr(L, "_try_auto_update", fetch or (lambda staging: None))
    monkeypatch.setattr(L, "_cache_dir", lambda: tmp_path / "engine")
    monkeypatch.setattr(L.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(L, "_report_update_stuck", lambda r: reasons.append(r))
    monkeypatch.setattr(L, "_bundled_build", lambda: bundled_build)
    return L._prepare_engine()


def _setup(L, tmp_path, monkeypatch):
    """An intact cache at version 500 (newer than the build-400 exe bundles),
    so the cache is the engine that runs.  The cache dir and home are patched
    HERE, before any helper can touch the real ~/.otdrSuite."""
    monkeypatch.setattr(L, "_cache_dir", lambda: tmp_path / "engine")
    monkeypatch.setattr(L.Path, "home", staticmethod(lambda: tmp_path))
    cache, hashes = _engine(L, tmp_path / "engine", "CACHE 500")
    cache.with_name("engine.meta.json").write_text(
        json.dumps({"version": 500, "files": hashes}), encoding="utf-8")
    bundled, _ = _engine(L, tmp_path / "bundled", "BUNDLED 400")
    monkeypatch.setattr(L, "bundled_dir", lambda: bundled)
    return cache, bundled


def _click_repair(tmp_path):
    marker = tmp_path / REPAIR
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("repair", encoding="utf-8")


def _health(tmp_path):
    try:
        return json.loads((tmp_path / "cache_health.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def test_two_repairs_in_a_week_pin_the_machine_to_bundled(tmp_path, monkeypatch):
    """The loop: verified download, file gone, Repair, verified download,
    file gone, Repair.  The second Repair is the last one the tech clicks."""
    L = _load_launcher()
    cache, bundled = _setup(L, tmp_path, monkeypatch)
    reasons, fetched = [], []

    engine_dir, _ = _boot(L, tmp_path, monkeypatch, reasons)
    assert engine_dir == cache
    assert L._cache_ok_last_boot()

    _click_repair(tmp_path)                       # the hub found a file missing
    engine_dir, _ = _boot(L, tmp_path, monkeypatch, reasons,
                          fetch=_fetch_ok(L, 501, fetched))
    assert (engine_dir / "app.py").read_text(encoding="utf-8") == "FETCHED 501"
    assert not L._cache_pin(), "one loss is not a pattern"
    assert L.CACHE_PINNED_ENV not in os.environ
    assert len(_health(tmp_path)["losses"]) == 1

    _click_repair(tmp_path)                       # and it is gone again
    engine_dir, label = _boot(L, tmp_path, monkeypatch, reasons,
                              fetch=_fetch_ok(L, 502, fetched))
    assert engine_dir == bundled
    assert "cache pinned" in label
    assert fetched == [501], "a pinned boot must not fetch into the cache"
    assert os.environ[L.CACHE_PINNED_ENV]
    assert any("cache pinned to bundled" in r and "Install the newest version" in r
               for r in reasons), reasons
    assert _health(tmp_path)["pinned"]["bundled_build"] == 400

    # Every later boot, no click at all: still bundled, still no fetch.
    engine_dir, label = _boot(L, tmp_path, monkeypatch, reasons,
                              fetch=_fetch_ok(L, 503, fetched))
    assert engine_dir == bundled and "cache pinned" in label
    assert fetched == [501]
    assert os.environ[L.CACHE_PINNED_ENV]


def test_installing_a_newer_build_clears_the_pin(tmp_path, monkeypatch):
    """The notice tells the tech to install the newest version.  Doing so
    must actually end the pin, or the advice is a lie."""
    L = _load_launcher()
    cache, bundled = _setup(L, tmp_path, monkeypatch)
    reasons, fetched = [], []
    monkeypatch.setattr(L, "_bundled_build", lambda: 400)
    L._pin_cache("engine files disappeared from the cache 2 times in 7 days")
    assert L._cache_pin()

    engine_dir, label = _boot(L, tmp_path, monkeypatch, reasons,
                              fetch=_fetch_ok(L, 600, fetched), bundled_build=410)
    assert "pinned" not in label
    assert fetched == [600], "the new exe gets to try the cache again"
    assert (engine_dir / "app.py").read_text(encoding="utf-8") == "FETCHED 600"
    assert "pinned" not in _health(tmp_path)
    assert _health(tmp_path).get("losses") == []


def test_a_damaged_cache_with_no_way_to_refetch_counts_once(tmp_path, monkeypatch):
    """A cache that lost a file and cannot be re-downloaded (no signal) sits
    there boot after boot.  That is ONE loss, not one per boot: it must not
    pin a machine that lost a file once and then went into a tunnel."""
    L = _load_launcher()
    cache, bundled = _setup(L, tmp_path, monkeypatch)
    reasons = []
    _boot(L, tmp_path, monkeypatch, reasons)
    assert L._cache_ok_last_boot()

    (cache / LOST).unlink()
    engine_dir, _ = _boot(L, tmp_path, monkeypatch, reasons)   # fetch fails
    assert engine_dir == bundled
    assert len(_health(tmp_path)["losses"]) == 1
    assert any("engine cache incomplete" in r for r in reasons)

    engine_dir, label = _boot(L, tmp_path, monkeypatch, reasons)
    assert engine_dir == bundled
    assert len(_health(tmp_path)["losses"]) == 1, "the same damage counted twice"
    assert not L._cache_pin()
    assert "pinned" not in label


def test_a_boot_time_loss_and_a_repair_add_up(tmp_path, monkeypatch):
    """Whether the launcher catches the loss or the app does, it is the same
    evidence about the same machine."""
    L = _load_launcher()
    cache, bundled = _setup(L, tmp_path, monkeypatch)
    reasons, fetched = [], []
    _boot(L, tmp_path, monkeypatch, reasons)

    (cache / LOST).unlink()                       # eaten overnight
    engine_dir, _ = _boot(L, tmp_path, monkeypatch, reasons,
                          fetch=_fetch_ok(L, 501, fetched))
    assert (engine_dir / "app.py").read_text(encoding="utf-8") == "FETCHED 501"
    assert len(_health(tmp_path)["losses"]) == 1

    _click_repair(tmp_path)                       # eaten again, app noticed
    engine_dir, label = _boot(L, tmp_path, monkeypatch, reasons,
                              fetch=_fetch_ok(L, 502, fetched))
    assert engine_dir == bundled and "cache pinned" in label


def test_a_repair_from_a_bundled_session_is_not_a_cache_loss(tmp_path, monkeypatch):
    """The repair page can also appear when the INSTALL is damaged.  A Repair
    click from a session that never ran the cache says nothing about the
    cache, so it must not count toward pinning."""
    L = _load_launcher()
    cache, bundled = _setup(L, tmp_path, monkeypatch)
    reasons = []
    L._mark_cache_ok(False)                       # last boot ran bundled
    _click_repair(tmp_path)
    _boot(L, tmp_path, monkeypatch, reasons)
    assert _health(tmp_path).get("losses", []) == []


def test_losses_outside_the_window_are_forgotten(tmp_path, monkeypatch):
    L = _load_launcher()
    monkeypatch.setattr(L, "_cache_dir", lambda: tmp_path / "engine")
    day = 86400
    assert L._note_cache_loss("x", now=0) == 1
    assert L._note_cache_loss("x", now=8 * day) == 1, "a week-old loss is gone"
    assert L._note_cache_loss("x", now=8 * day + 60) == 2


def test_a_pin_is_not_honoured_on_a_damaged_install(tmp_path, monkeypatch):
    """Pinned to bundled, and bundled has lost a file too: the pin would hand
    the tech a second unbootable engine.  The ladder is still the best bet."""
    L = _load_launcher()
    cache, bundled = _setup(L, tmp_path, monkeypatch)
    (bundled / LOST).unlink()
    monkeypatch.setattr(L, "_bundled_build", lambda: 400)
    L._pin_cache("engine files disappeared from the cache 2 times in 7 days")
    reasons = []

    engine_dir, label = _boot(L, tmp_path, monkeypatch, reasons)
    assert engine_dir == cache
    assert "pinned" not in label
    assert L.CACHE_PINNED_ENV not in os.environ


def test_a_stale_repair_click_does_not_outlive_the_pin(tmp_path, monkeypatch):
    L = _load_launcher()
    cache, bundled = _setup(L, tmp_path, monkeypatch)
    monkeypatch.setattr(L, "_bundled_build", lambda: 400)
    L._pin_cache("engine files disappeared from the cache 2 times in 7 days")
    _click_repair(tmp_path)
    _boot(L, tmp_path, monkeypatch, [])
    assert not (tmp_path / REPAIR).exists()


# ═════════════════════════════════════════════════════════════════════════
#  the hub side
# ═════════════════════════════════════════════════════════════════════════

class _Stop(Exception):
    pass


class _FakeSt:
    """Just enough Streamlit to run one page function and record what it
    handed to the screen."""
    def __init__(self, button=False):
        self.calls = []
        self.session_state = {}
        self._button = button
        self.sidebar = self

    def _rec(self, name):
        def f(*a, **k):
            self.calls.append((name, a[0] if a else ""))
        return f

    def __getattr__(self, name):
        if name in ("set_page_config", "title", "error", "write", "warning",
                    "caption", "info", "code", "success"):
            return self._rec(name)
        raise AttributeError(name)

    def button(self, *a, **k):
        self.calls.append(("button", a[0]))
        return self._button

    def expander(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def stop(self):
        raise _Stop()


def _app_constant(name):
    node = next(n for n in ast.parse(APP_SRC).body
                if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == name for t in n.targets))
    return ast.literal_eval(node.value)


def test_the_missing_file_page_reports_before_anyone_clicks():
    """The report used to go out on the click, on a thread the restart killed
    0.7 s later.  Shown is the moment we know; that is when it must be sent."""
    st = _FakeSt(button=False)
    reports, repairs = [], []
    page = _load_app_helper(
        "_engine_file_missing_page", st=st, os=os, HERE="/x/engine",
        report_error=lambda where, exc, ctx=None: reports.append(where),
        _request_repair=lambda: repairs.append(1),
        _relaunch_and_exit=lambda: True,
        _render_restart_watchdog=lambda: None)
    try:
        page(ModuleNotFoundError("No module named 'trace_server'"))
    except _Stop:
        pass
    assert reports == ["engine file missing"]
    assert repairs == [], "no click, no repair"


def test_the_nudge_shows_the_pin_notice_and_offers_no_restart(tmp_path):
    """A restart lands on the same bundled engine.  While pinned the nudge
    must say what is going on and never consult the update state at all."""
    st = _FakeSt()
    shown = []

    def boom():
        raise AssertionError("update state consulted while pinned")

    nudge = _load_app_helper(
        "_render_update_nudge", st=st, os=os, sys=sys,
        _restart_marker_path=lambda: str(tmp_path / "no-such-marker"),
        _update_state=boom,
        _cache_pinned=lambda: "engine files disappeared from the cache 2 times in 7 days",
        _render_cache_pinned_notice=lambda sidebar=False: shown.append("notice"),
        _relaunch_and_exit=lambda: True, _render_restart_watchdog=lambda: None)
    nudge()
    assert shown == ["notice"]
    assert not any(c[0] == "button" for c in st.calls)


def test_the_pin_notice_names_the_installer_and_no_jargon():
    st = _FakeSt()
    notice = _load_app_helper("_render_cache_pinned_notice", st=st,
                              INSTALLER_URL=_app_constant("INSTALLER_URL"))
    notice()
    (kind, text), = st.calls
    assert kind == "warning"
    assert "OTDRSuite-Setup.exe" in text
    assert "install" in text.lower()
    for jargon in ("cache", "pin", "launcher", "manifest", "—"):
        assert jargon not in text, f"{jargon!r} is not for a tech to read"


def test_the_sidebar_update_block_checks_the_pin_before_offering_a_restart():
    i = APP_SRC.index("key='upd_restart'")
    assert "_cache_pinned()" in APP_SRC[i - 600:i]


def test_the_app_and_the_launcher_agree_on_the_pin_variable():
    L = _load_launcher()
    assert _app_constant("_CACHE_PINNED_ENV") == L.CACHE_PINNED_ENV
    assert L.CACHE_PINNED_ENV == "OTDR_SUITE_CACHE_PINNED"
