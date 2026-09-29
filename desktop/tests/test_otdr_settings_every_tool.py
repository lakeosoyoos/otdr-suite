"""The OTDR Settings on every tool but Secret Sauce (Robert 2026-09-28): "we
need to have our OTDR settings available in all of our tools except Secret
Sauce".

    Viewer            the Customer profile + Settings box at the top; with no
                      report behind it the Viewer judges pass/fail at these
                      settings and runs its own report with them
    Splice Report     the same box, where it always was
    Unidirectional    the same box at the top; three of its rows drive the
                      Uni engine (connector loss in one direction, the
                      mid-span reflectance band and its ceiling)
    Secret Sauce      no box

One set of values: the three pages read and write the same session slots, so
a profile picked on one tool is the profile on all of them.
"""
from __future__ import annotations

import json

import pytest

from conftest import REPO_ROOT, run_streamlit, import_trace_server

TS = import_trace_server()
SRC = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
BOX = "Settings (Thresholds, Connector & Launch)"
DEFAULT = "Default (engine baseline)"


def _profile(test):
    """The first profile whose settings pass `test`, found by what it sets
    rather than by name: the repo is public, so no customer names here."""
    import app as hub
    for name in hub.CUSTOMER_PROFILES:
        if "Custom" not in name and test(hub._otdr_settings_from_profile(name)):
            return name
    raise AssertionError("no profile sets that")


def _bidir_200():
    """A profile that moves the bidirectional splice gate to 0.200 dB."""
    return _profile(lambda s: s["bidir_splice_loss"]["apply"]
                    and s["bidir_splice_loss"]["fail"] == 0.200)


def _uni_conn(value):
    """A profile whose one-direction connector gate is `value` (0 = off)."""
    if value == 0:
        return _profile(lambda s: not s["unidir_connector_loss"]["apply"])
    return _profile(lambda s: s["unidir_connector_loss"]["apply"]
                    and s["unidir_connector_loss"]["fail"] == value)


@pytest.fixture(autouse=True)
def _clean_viewer():
    """trace_server is process-global: never leak a gate into another test."""
    TS.set_thresholds(None)
    TS.set_settings(None)
    yield
    TS.set_thresholds(None)
    TS.set_settings(None)


def _open(at, page):
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, at.exception
    return at


def _has_box(at):
    return (any(e.label == BOX for e in at.main.expander)
            and any(s.key == "otdr_profile_select" for s in at.main.selectbox))


# ── the box is on every tool but Secret Sauce ───────────────────────────

def test_viewer_splice_report_and_uni_draw_the_box_secret_sauce_does_not():
    at = run_streamlit(default_timeout=180).run()
    assert not at.exception, at.exception
    for page in ("Viewer", "Splice Report", "Unidirectional"):
        assert _has_box(_open(at, page)), page
    at = _open(at, "Secret Sauce")
    assert not any(e.label == BOX for e in at.main.expander)
    assert not any(s.key == "otdr_profile_select" for s in at.main.selectbox)


def test_a_profile_picked_on_one_tool_is_the_profile_on_all():
    picked = _bidir_200()
    at = _open(run_streamlit(default_timeout=180).run(), "Viewer")
    at.selectbox(key="otdr_profile_select").set_value(picked).run()
    assert not at.exception, at.exception
    for page in ("Unidirectional", "Splice Report", "Viewer"):
        at = _open(at, page)
        assert at.selectbox(key="otdr_profile_select").value == picked, page
        assert at.session_state["otdr_profile"] == picked


def test_the_viewer_page_hands_its_settings_to_the_viewer():
    """What the box shows is what the Viewer judges by: the same overrides a
    Splice Report run would send, a profile's 0.200 bidirectional gate
    included."""
    import app as hub
    picked = _bidir_200()
    want = hub._overrides_from_settings(hub._otdr_settings_from_profile(picked))
    at = _open(run_streamlit(default_timeout=180).run(), "Viewer")
    at.selectbox(key="otdr_profile_select").set_value(picked).run()
    assert not at.exception, at.exception
    got = TS.CONFIG["settings"]
    assert got["REBURN_THRESHOLD"] == want["REBURN_THRESHOLD"] == 0.200
    assert TS.gate_source() == "settings"
    assert TS.engine_thresholds()["reburn"] == 0.200


# ── Unidirectional reads the three one-direction rows ───────────────────

def test_the_default_profile_leaves_a_uni_run_unchanged():
    """The Default profile lands on the Uni engine's own defaults, so a
    default Uni report is byte-identical to before."""
    import app as hub
    import re
    eng = (REPO_ROOT / "splicereport" / "splicereportmatchexfo.py").read_text(
        encoding="utf-8")

    def const(name):
        return float(re.search(r"^%s\s*=\s*(-?[\d.]+)" % name, eng, re.M).group(1))

    got = hub._uni_overrides_from_settings(hub._otdr_settings_from_profile(DEFAULT))
    assert got == {g: const(g) for g in
                   ("UNI_CONN_LOSS_DB", "UNI_REFL_FLOOR_DB", "UNI_REFL_CEIL_DB")}


def test_a_customer_profile_s_connector_gate_reaches_uni():
    import app as hub
    for name in hub.CUSTOMER_PROFILES:
        row = hub._otdr_settings_from_profile(name)["unidir_connector_loss"]
        got = hub._uni_overrides_from_settings(hub._otdr_settings_from_profile(name))
        # A profile that turns the one-direction gate off turns it off in
        # Uni too (0 = off there).
        assert got["UNI_CONN_LOSS_DB"] == (row["fail"] if row["apply"] else 0.0), name
    off = _uni_conn(0)
    assert hub._uni_overrides_from_settings(
        hub._otdr_settings_from_profile(off))["UNI_CONN_LOSS_DB"] == 0.0


def test_unticked_rows_switch_uni_off_and_a_missing_row_adds_nothing():
    import app as hub
    s = hub._otdr_settings_from_profile(DEFAULT)
    s["midspan_reflectance"]["apply"] = False
    s["midspan_refl_ceiling"]["apply"] = True
    s["midspan_refl_ceiling"]["fail"] = -40.0
    got = hub._uni_overrides_from_settings(s)
    assert got["UNI_REFL_FLOOR_DB"] == 0.0          # 0 = the band is off
    assert got["UNI_REFL_CEIL_DB"] == -40.0
    del s["unidir_connector_loss"]
    assert "UNI_CONN_LOSS_DB" not in hub._uni_overrides_from_settings(s)
    assert hub._uni_overrides_from_settings(None) == {}


@pytest.mark.parametrize("gate,flagged", [
    (None, 0),       # the Default profile, 0.649 dB: none of the 48
    (0.300, 27),     # a profile whose one-direction connector gate is 0.300
])
def test_the_profile_s_connector_gate_reaches_a_real_uni_run(
        gate, flagged, tmp_path, monkeypatch):
    """End to end through the page: the fixture's A folder holds 48 one-
    direction connector readings, none at 0.649 dB and 27 at 0.300 dB or
    more.  The report is saved under tmp_path, never the real Downloads."""
    from conftest import FIXTURE_SPLICE_A_DIR
    profile = DEFAULT if gate is None else _uni_conn(gate)
    monkeypatch.setenv("OTDR_CACHE_DIR", str(tmp_path / "cache"))
    at = run_streamlit(default_timeout=300).run()
    at = _open(at, "Unidirectional")
    at.selectbox(key="otdr_profile_select").set_value(profile).run()
    at.session_state["uni_folder_input"] = str(FIXTURE_SPLICE_A_DIR)
    at.session_state["uni_report_dest"] = str(tmp_path)
    at.run()
    next(b for b in at.main.button
         if b.label == "Run unidirectional report").click().run()
    for _ in range(3):                       # the page reruns itself to finish
        if "uni_result" in at.session_state:
            break
        at.run()
    assert not at.exception, at.exception
    assert at.session_state["uni_result"]["uni"]["connector_readings"] == 48
    assert at.session_state["uni_result"]["uni"]["connector_flagged"] == flagged


def test_the_uni_page_sends_them_with_its_own_box():
    page = SRC.split("def page_unidirectional():", 1)[1].split("\ndef ", 1)[0]
    i_box = page.index("uni_overrides = _render_uni_settings_panel()")
    i_otdr = page.index("_uni_overrides_from_settings(")
    i_run = page.index("overrides=uni_overrides")
    assert i_box < i_otdr < i_run
    # the box is drawn at the top, above the loader (and blocks the report)
    assert (page.index("_render_settings_box('unidirectional'")
            < page.index("_panel_traces()"))


# ── the Viewer follows the settings when no report is behind it ─────────

def test_a_report_on_screen_still_wins_and_clearing_it_falls_back():
    TS.set_settings({"REBURN_THRESHOLD": 0.150})
    assert TS.engine_thresholds()["reburn"] == 0.150
    TS.set_thresholds({"REBURN_THRESHOLD": 0.200}, source="sr")
    assert TS.engine_thresholds()["reburn"] == 0.200
    assert TS.gate_source() == "report"
    TS.set_thresholds(None)
    assert TS.engine_thresholds()["reburn"] == 0.150
    TS.set_settings(None)
    assert TS.gate_source() == "engine"
    assert TS.engine_thresholds()["reburn"] == TS._source_thresholds()["reburn"]


def test_a_setting_the_engine_would_reject_is_not_a_viewer_gate():
    base = TS._source_thresholds()["reburn"]
    for bad in (0.0, -0.1, float("nan"), "x"):
        TS.set_settings({"REBURN_THRESHOLD": bad})
        assert TS.engine_thresholds()["reburn"] == base, repr(bad)


def test_the_viewer_says_so_when_the_settings_moved_after_a_splice_report():
    TS.set_settings({"REBURN_THRESHOLD": 0.160})
    TS.set_thresholds({"REBURN_THRESHOLD": 0.160}, source="sr")
    assert not TS.settings_differ_from_report()
    TS.set_settings({"REBURN_THRESHOLD": 0.200})
    assert TS.settings_differ_from_report()
    # A Uni report's loss gate is the Uni box's, not the settings'.
    TS.set_thresholds({"REBURN_THRESHOLD": 0.160}, source="uni")
    assert not TS.settings_differ_from_report()
    assert "settings_differ_from_report()" in SRC.split("def page_viewer():", 1)[1]


def test_the_viewer_s_own_report_runs_with_the_settings(monkeypatch, tmp_path):
    """No report behind the Viewer: its background report run is the same
    run the Splice Report page makes, settings included, and a changed
    setting runs it again."""
    seen = []

    class _Done:
        stdout = json.dumps({"ok": True, "end_refl": [], "panel_span": False})

    monkeypatch.setattr(TS.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or _Done())
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir(); b.mkdir()
    saved = dict(TS.CONFIG)
    try:
        TS.CONFIG.update(dir_a=str(a), dir_b=str(b), end_refl=None, panel_span=None)
        TS.set_settings({"REBURN_THRESHOLD": 0.200})
        key = TS._end_verdict_key()
        TS._run_end_verdicts(key)
        cmd = seen[-1]
        assert json.loads(cmd[cmd.index("--overrides") + 1]) == {"REBURN_THRESHOLD": 0.200}
        TS.set_settings({"REBURN_THRESHOLD": 0.150})
        assert TS._end_verdict_key() != key
        TS.set_settings(None)
        TS._run_end_verdicts(TS._end_verdict_key())
        assert "--overrides" not in seen[-1]
    finally:
        TS.CONFIG.clear(); TS.CONFIG.update(saved)
        TS._END_VERDICTS.clear()


def test_the_viewer_names_the_settings_as_its_gate():
    html = (REPO_ROOT / "viewer" / "viewer.html").read_text(encoding="utf-8")
    label = html.split("function gateLabel()", 1)[1].split("\n}", 1)[0]
    assert "gInfo.gate_source === 'settings'" in label
    assert "'OTDR Settings'" in label
    ts = (REPO_ROOT / "viewer" / "trace_server.py").read_text(encoding="utf-8")
    assert "'gate_source': gate_source()," in ts


def test_the_stand_alone_suite_table_is_built_at_the_settings(monkeypatch):
    """With #345 the Viewer's own run also writes the OTDR Suite table, so
    the same run carries --overrides and --viewer-table together.  A real
    run on the fixture span: 9 cells flag at the engine defaults and 36
    with the bidirectional gate at 0.05 dB.  A changed setting is a new
    cache key, so the Viewer drops to pending and runs again; going back to
    the first settings reuses the first run."""
    import time
    from conftest import FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR
    for k in ("dir_a", "dir_b", "suite_table", "end_refl", "analysis_mode"):
        monkeypatch.setitem(TS.CONFIG, k, TS.CONFIG.get(k))
    TS.CONFIG.update({"dir_a": str(FIXTURE_SPLICE_A_DIR),
                      "dir_b": str(FIXTURE_SPLICE_B_DIR),
                      "suite_table": None, "end_refl": None,
                      "analysis_mode": "suite"})
    monkeypatch.setattr(TS, "_END_VERDICTS", {})
    monkeypatch.setattr(TS, "_SUITE_TABLE_FILE", {})
    monkeypatch.setattr(TS, "_TRACE_SIG", {})
    fibers = list(range(1, 25))

    def settle():
        for _ in range(3000):
            out = TS.suite_tables(fibers)
            if not out["pending"]:
                return out
            time.sleep(0.1)
        raise AssertionError("the Viewer's run never landed")

    def flagged(out):
        assert out["source"] == "viewer" and out["error"] is None, out
        return sum(1 for cells in out["tables"].values() for c in cells if c.get("flag"))

    assert flagged(settle()) == 9
    TS.set_settings({"REBURN_THRESHOLD": 0.05})
    assert TS.suite_tables(fibers)["pending"] is True
    assert flagged(settle()) == 36
    TS.set_settings(None)
    assert TS.suite_tables(fibers)["pending"] is False
    assert flagged(settle()) == 9


# ── the Settings box is down: the Viewer flags nothing ──────────────────
# Robert 2026-09-28: "Viewer shouldn't show any flags if the settings box or
# connector launch knobs fail but it can still show events and values",
# then "just not flag".

def _broken_box(monkeypatch, only_knobs):
    """components.otdr_settings raising the way it does when Windows will
    not load pandas; `only_knobs` lets the threshold table draw."""
    import sys
    import types
    mod = types.ModuleType("components.otdr_settings")

    def otdr_settings(*a, **k):
        if only_knobs and k.get("mode") != "knobs":
            return None
        raise ImportError("DLL load failed while importing indexers: An "
                          "Application Control policy has blocked this file.")

    mod.otdr_settings = otdr_settings
    monkeypatch.setitem(sys.modules, "components.otdr_settings", mod)


@pytest.mark.parametrize("only_knobs", [False, True])
def test_a_box_that_cannot_draw_turns_the_viewer_s_flags_off(
        monkeypatch, only_knobs):
    _broken_box(monkeypatch, only_knobs)
    at = _open(run_streamlit(default_timeout=180).run(), "Viewer")
    warned = [w.value for w in at.warning]
    assert any("the Viewer flags only breaks" in w for w in warned), warned
    assert not any("running with default" in w for w in warned), warned
    assert TS.flags_off() is True


def test_a_box_down_on_a_report_page_turns_the_pop_out_s_flags_off_too(
        monkeypatch):
    """The pop-out Viewer follows the hub whatever page the tech is on."""
    _broken_box(monkeypatch, only_knobs=True)
    _open(run_streamlit(default_timeout=180).run(), "Splice Report")
    assert TS.flags_off() is True


def test_the_box_drawing_again_brings_the_flags_back():
    at = _open(run_streamlit(default_timeout=180).run(), "Viewer")
    assert TS.flags_off() is False


def test_no_end_verdicts_while_the_box_is_down_not_even_a_report_s(monkeypatch):
    runs = []
    monkeypatch.setattr(TS, "_run_end_verdicts", lambda key: runs.append(key))
    saved = dict(TS.CONFIG)
    try:
        TS.CONFIG.update(dir_a="/a", dir_b="/b", end_refl=None)
        TS.set_settings(None, failed=True)
        assert TS.end_verdicts() == {"end_refl": [], "panel_span": None,
                                     "end_pending": False}
        TS.set_end_refl([{"fiber": 7, "dir": "B", "refl": -45.0}])
        assert TS.end_verdicts()["end_refl"] == []
        assert runs == []
    finally:
        TS.CONFIG.clear(); TS.CONFIG.update(saved)


def test_the_suite_table_keeps_its_numbers_and_loses_its_flags(tmp_path,
                                                                monkeypatch):
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir(); b.mkdir()
    (a / "X_0007_1550.sor").write_bytes(b"a")
    (b / "Y_0007_1550.sor").write_bytes(b"b")
    for k in ("dir_a", "dir_b", "suite_table", "end_refl", "analysis_mode"):
        monkeypatch.setitem(TS.CONFIG, k, TS.CONFIG.get(k))
    TS.CONFIG.update({"dir_a": str(a), "dir_b": str(b), "suite_table": None,
                      "end_refl": None, "analysis_mode": "suite"})
    monkeypatch.setattr(TS, "_END_VERDICTS", {})
    monkeypatch.setattr(TS, "_TRACE_SIG", {})
    cell = {"col": 0, "loss": 0.25, "flag": True, "label": "7 .250",
            "category": "bidir",
            "a": {"loss": 0.3, "refl": -40.0, "flag": True, "flag_refl": True},
            "b": {"loss": 0.2, "refl": None, "flag": False, "flag_refl": False}}
    # a sure break keeps its flag and the report's words (Robert 2026-09-29)
    broke = {"col": 1, "loss": None, "flag": True, "label": "7 broke@12.3k",
             "category": "broke", "a": None, "b": None}

    def fake_run(key):
        with TS._END_VERDICTS_LOCK:
            TS._END_VERDICTS[key] = {
                "end_refl": [], "panel_span": False, "error": None,
                "suite_table": {"columns": [{"title": "Splice 1", "kind": "splice",
                                             "km": 5.0}],
                                "fibers": {"7": [cell, broke]},
                                "launch_a_km": 1.0, "span_km": 20.0}}
    monkeypatch.setattr(TS, "_run_end_verdicts", fake_run)
    TS.set_settings(None, failed=True)
    TS.suite_tables([7])                     # starts the (fake) run
    got = TS.suite_tables([7])["tables"]["7"][0]
    assert (got["loss"], got["a"]["loss"], got["a"]["refl"], got["b"]["loss"]) \
        == (0.25, 0.3, -40.0, 0.2)
    assert not got["flag"] and not got["a"]["flag"] and not got["a"]["flag_refl"]
    brk = TS.suite_tables([7])["tables"]["7"][1]
    assert brk["flag"] is True and brk["label"] == "7 broke@12.3k"
    assert cell["flag"] is True, "the cached table itself is left alone"
    TS.set_settings(None)
    assert TS.suite_tables([7])["tables"]["7"][0]["flag"] is True


def test_the_viewer_flags_nothing_while_flags_off_is_served():
    html = (REPO_ROOT / "viewer" / "viewer.html").read_text(encoding="utf-8")
    assert "function flagsOff() { return !!(gInfo && gInfo.flags_off); }" in html

    def first_line(fn):
        body = html.split("function " + fn + "(", 1)[1]
        return body.split("\n", 2)[1].strip()

    for fn, off in (("activeGateDb", "return Infinity;"),
                    ("gateFor", "return Infinity;"),
                    ("warnFor", "return null;"),
                    ("reflFails", "return false;"),
                    ("gateLabel", "return 'breaks only: the Settings box did not load';")):
        assert first_line(fn) == "if (flagsOff()) " + off, fn
    assert "if (e.is_end || flagsOff()) return { flag: false, why: '' };" in html
    assert "if (isBreak && !flagsOff()) cls = ' class=\"fr-brk\"';" in html
    # every P/F mark goes blank
    assert html.count("${pfClass(fail)}") == 3 and html.count("${pfMark(fail)}") == 3
    assert "'fr-pf-fail' : 'fr-pf-pass'}\" title=" not in html
    ts = (REPO_ROOT / "viewer" / "trace_server.py").read_text(encoding="utf-8")
    assert "'flags_off': flags_off()," in ts


def test_only_a_sure_break_keeps_its_flag_while_the_box_is_down():
    """Robert 2026-09-29: "we can keep break wording and flagging if we are
    sure it is a break".  Sure = the report's break calls that rest on the
    trace itself: every one of them must still be a category the engine
    writes, or the Viewer silently stops keeping it."""
    eng = (REPO_ROOT / "splicereport" / "splicereportmatchexfo.py").read_text(
        encoding="utf-8")
    assert TS.SURE_BREAKS == {"broke", "broke_b", "break_standalone"}
    for cat in TS.SURE_BREAKS:
        assert f"'event_source': '{cat}'" in eng, cat
    # a threshold call next to a break is not one of them
    assert not ({"bfill", "bend", "bend_standalone", "connector",
                 "dead_zone"} & TS.SURE_BREAKS)
    html = (REPO_ROOT / "viewer" / "viewer.html").read_text(encoding="utf-8")
    # a fibre failed by a sure break reads ✗; nothing reads ✓ with no flags
    assert "const pfMark = fail => fail ? '✗' : (flagsOff() ? '' : '✓');" in html
    assert ("const pfClass = fail => fail ? 'fr-pf-fail' : "
            "(flagsOff() ? '' : 'fr-pf-pass');") in html


def test_with_the_table_down_the_viewer_s_run_keeps_the_profile(monkeypatch):
    """A break call can move with a profile's engine settings (an iOLM
    export's missing end marker), which live outside the box.  So the
    Viewer's own run keeps them when the table does not draw: its sure
    breaks then read as the report's would.  No threshold row goes with
    them."""
    import app as hub
    prof = next(n for n in hub.CUSTOMER_PROFILES
                if hub._engine_extras_from_profile(n).get("IOLM_END_FALLBACK"))
    _broken_box(monkeypatch, only_knobs=False)
    at = _open(run_streamlit(default_timeout=180).run(), "Viewer")
    at.selectbox(key="otdr_profile_select").set_value(prof).run()
    assert not at.exception, at.exception
    sent = TS.CONFIG["settings"]
    assert TS.flags_off() is True
    assert sent.get("IOLM_END_FALLBACK") == 1.0
    assert not (set(sent) & set(hub._OTDR_KEY_TO_ENGINE_GLOBAL.values())), sent
    # and the Viewer's own run carries them
    assert json.loads(TS._settings_arg())["IOLM_END_FALLBACK"] == 1.0


def test_a_kept_break_fails_its_fibre_in_the_suite_table():
    """The Suite table's verdict reads the report's flag first, so a sure
    break the server keeps fails its fibre (P/F ✗) while nothing else can."""
    html = (REPO_ROOT / "viewer" / "viewer.html").read_text(encoding="utf-8")
    assert "return !!x.flag || (!c.isEnd && clearsAt(x.loss, gateFor(!!x.reflective, false)));" in html
    assert "const legFails = (fi, which) => cols.some(c => c.ev[fi] && cellFails(c, c.ev[fi], which));" in html
