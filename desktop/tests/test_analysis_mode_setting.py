"""The Analysis setting: OTDR Suite / FastReporter.

One switch in the hub sidebar, remembered across launches, carried to the
three engine subprocesses (--analysis) and the Viewer (trace_server.CONFIG),
and echoed in every manifest.  Nothing branches on it yet -- the
FastReporter rules land behind it one at a time -- so this pins the
plumbing and that the two modes are identical today.
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest

from conftest import REPO_ROOT

# The hub is imported inside a fixture, not at module level.  That was once
# needed: this file sorts near the top of the suite, and importing app.py at
# collection put the Viewer's sor_reader324802a copy in sys.modules before
# any engine test had imported the engine's, so every later
# `import splicereportmatchexfo` failed.  conftest.py now loads the engine's
# readers before any test module is imported, so the order no longer
# matters.  The fixture stays: the tests take the hub from it.

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
FIX = REPO_ROOT / "desktop" / "tests" / "fixtures"
SRC = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
RUNNER = (SPLICEREPORT_DIR / "run_splicereport.py").read_text(encoding="utf-8")
SS_RUNNER = (REPO_ROOT / "secretsauce" / "run_secretsauce.py").read_text(encoding="utf-8")
ENGINE = (SPLICEREPORT_DIR / "splicereportmatchexfo.py").read_text(encoding="utf-8")
TS = (REPO_ROOT / "viewer" / "trace_server.py").read_text(encoding="utf-8")
VIEWER = (REPO_ROOT / "viewer" / "viewer.html").read_text(encoding="utf-8")


@pytest.fixture
def hub():
    import app
    return app


@pytest.fixture
def settings_dir(tmp_path, monkeypatch, hub):
    monkeypatch.setenv("OTDR_SETTINGS_DIR", str(tmp_path))
    # no live sidebar in a test: the accessor falls through to the file
    monkeypatch.setattr(hub, "analysis_mode", lambda: hub.load_analysis_mode())
    return tmp_path


# ── persistence ───────────────────────────────────────────────────────────

def test_default_is_otdr_suite_and_the_file_round_trips(settings_dir, hub):
    assert hub.load_analysis_mode() == "suite"
    hub.save_analysis_mode("fr")
    assert hub.load_analysis_mode() == "fr"
    data = json.loads((settings_dir / "settings.json").read_text(encoding="utf-8"))
    assert data == {"analysis_mode": "fr"}
    hub.save_analysis_mode("suite")
    assert hub.load_analysis_mode() == "suite"


def test_a_damaged_or_foreign_settings_file_means_otdr_suite(settings_dir, hub):
    p = settings_dir / "settings.json"
    p.write_text("{not json", encoding="utf-8")
    assert hub.load_analysis_mode() == "suite"
    p.write_text(json.dumps({"analysis_mode": "something else"}), encoding="utf-8")
    assert hub.load_analysis_mode() == "suite"
    p.write_text(json.dumps([1, 2]), encoding="utf-8")
    assert hub.load_analysis_mode() == "suite"


def test_saving_keeps_other_keys_and_refuses_unknown_modes(settings_dir, hub):
    p = settings_dir / "settings.json"
    p.write_text(json.dumps({"other": 1}), encoding="utf-8")
    hub.save_analysis_mode("fr")
    assert json.loads(p.read_text(encoding="utf-8")) == {"other": 1, "analysis_mode": "fr"}
    with pytest.raises(ValueError):
        hub.save_analysis_mode("beta")


# ── every engine subprocess carries the mode ──────────────────────────────

def test_the_report_command_builders_carry_the_mode_and_secret_sauce_does_not(settings_dir, hub):
    for mode in ("suite", "fr"):
        hub.save_analysis_mode(mode)
        sr = hub.splicereport_cmd("/a", "/b", "/o.xlsx", "X", "Y")
        uni = hub.uni_cmd("/a", "/o.xlsx")
        for cmd in (sr, uni):
            i = cmd.index("--analysis")
            assert cmd[i + 1] == mode, (mode, cmd)
            assert cmd.count("--analysis") == 1
        # Secret Sauce works on the trace samples, not on what FastReporter
        # displays: the setting is not its business and is not passed.
        ss = hub.secretsauce_cmd("/a", "/out", "xlsx")
        assert "--analysis" not in ss and "analysis" not in " ".join(ss), ss


def test_the_viewer_is_told_the_same_mode():
    assert "'analysis_mode': 'suite'," in TS.split("CONFIG = {", 1)[1][:900]
    assert "'analysis_mode': (CONFIG.get('analysis_mode')" in TS
    assert "trace_server.CONFIG['analysis_mode'] = analysis_mode()" in SRC
    assert "gInfo.analysis_mode" in VIEWER and "let gAnalysisMode = 'suite';" in VIEWER


def test_the_sidebar_control_sits_under_the_tool_list():
    # under, not above: the Tool radio stays sidebar.radio[0] for the AppTests
    i_ctl = SRC.index("    _render_analysis_mode_control()\n")
    i_tool = SRC.index("page = st.radio('Tool',")
    assert i_tool < i_ctl < i_tool + 800
    body = SRC.split("def _render_analysis_mode_control():", 1)[1].split("\ndef ", 1)[0]
    assert "load_analysis_mode()" in body and "save_analysis_mode(_mode)" in body
    # FR Mode | switch | OTDR Mode (2026-09-24): knob right = OTDR Mode.
    assert ".toggle(" in body and "value=not _on" in body and "st.rerun()" in body
    # Keyless on purpose: a keyed switch came back from the App's home screen
    # in its old position and flipped OTDR Mode to FR Mode on the next run.
    assert "m.toggle('Analysis mode', value=not _on, label_visibility='collapsed')" in body
    assert "st.radio(" not in body                      # a switch, not a radio
    # the radio's key holds the label; the mode lives in its own slot
    assert "st.session_state['analysis_mode'] = _mode" in body


def test_the_name_in_use_has_a_green_halo_and_the_switches_are_always_on(hub):
    # Robert, 2026-09-30: a green halo round the name in use, and the switch
    # always drawn "on"; only the knob moves.  Analysis Mode and Theme alike.
    for fn, names in (("_render_analysis_mode_control", ("'FR Mode', _on", "'OTDR Mode', not _on")),
                      ("_render_theme_control", ("'Dark', dark", "'Light', not dark"))):
        body = SRC.split("def %s(" % fn, 1)[1].split("\ndef ", 1)[0]
        assert "_SWITCH_BOX_CSS" in body
        for n in names:
            assert "_mode_name(%s)" % n in body, (fn, n)
    css = SRC.split("_SWITCH_BOX_CSS = (", 1)[1].split("'</style>')", 1)[0]
    for box in (".st-key-analysis_mode_box", ".st-key-theme_box"):
        assert box + " .mode-on" in css
        assert box + ' [data-testid="stCheckbox"] label[data-baseweb="checkbox"]>div:first-child' in css
    assert "#22c55e" in css and "background-color:var(--otdr-accent" in css
    assert 'class="mode-on">FR Mode<' in hub._mode_name("FR Mode", True)
    assert 'class="mode-off">Light<' in hub._mode_name("Light", False)


def test_the_halo_styling_stays_on_the_switch():
    # #410 on main once changed every text-align:center / white-space:nowrap
    # in app.py along with the switch's own: the ribbon grids' cells wrapped.
    # Only the switch names keep words whole; the grid cells stay on one line.
    assert SRC.count("word-break:keep-all") == 2
    assert SRC.count(".st-key-analysis_mode_box .mode-o") == 2
    assert "f\"white-space:nowrap'>F{f0}–{f1}</td>\")" in SRC
    assert "\"white-space:nowrap'>\" + \"<br>\".join(links)" in SRC


def test_the_halo_styling_stays_on_the_switch():
    # #410 once changed every text-align:center / white-space:nowrap in app.py
    # along with the switch's own: the ribbon grids' cells wrapped.  Only the
    # two switch names keep words whole; the grid cells stay on one line.
    assert SRC.count("word-break:keep-all") == 2
    assert SRC.count(".st-key-analysis_mode_box .mode-o") == 2
    assert "f\"white-space:nowrap'>F{f0}–{f1}</td>\")" in SRC
    assert "\"white-space:nowrap'>\" + \"<br>\".join(links)" in SRC


# ── the runners accept it, set it, echo it ────────────────────────────────

def test_runners_accept_the_flag_set_the_engine_and_echo_it():
    assert "ap.add_argument('--analysis', default='suite', choices=('suite', 'fr')," in RUNNER
    i_set = RUNNER.index("E.ANALYSIS_MODE = args.analysis")
    i_ov = RUNNER.index("if args.overrides:")
    assert i_set < i_ov, "the mode is set before the overrides, before anything runs"
    assert RUNNER.count("'analysis_mode': args.analysis") == 2, "bidir and uni manifests"
    assert "--analysis" not in SS_RUNNER and "analysis_mode" not in SS_RUNNER
    assert "\nANALYSIS_MODE = 'suite'\n" in ENGINE and "def fr_mode():" in ENGINE


def test_engine_default_is_suite_and_fr_mode_toggles():
    code = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})
        import splicereportmatchexfo as E
        assert E.ANALYSIS_MODE == 'suite' and E.fr_mode() is False
        E.ANALYSIS_MODE = 'fr'
        assert E.fr_mode() is True
        print('OK')
    """)
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout.strip().endswith("OK"), p.stderr


def test_both_modes_run_the_same_pair_and_name_themselves(tmp_path):
    """A FastReporter run and an OTDR Suite run of the same pair both
    succeed, each manifest names the mode it ran in, and the span facts
    they share (gates, span length, fiber count) agree.  The grids differ
    by design since #267: FR mode prints FR's table under the tech's gates
    and knows no bend or damage columns (test_fr_report_grid pins both)."""
    outs = {}
    for mode in ("suite", "fr"):
        out = tmp_path / f"{mode}.xlsx"
        p = subprocess.run(
            [sys.executable, str(SPLICEREPORT_DIR / "run_splicereport.py"),
             "--dir-a", str(FIX / "span_A"), "--dir-b", str(FIX / "span_B"),
             "--out", str(out), "--site-a", "A", "--site-b", "B",
             "--analysis", mode],
            capture_output=True, text=True)
        assert p.returncode == 0, p.stderr[-800:]
        man = None
        for line in reversed(p.stdout.splitlines()):
            if line.startswith("{"):
                man = json.loads(line)
                break
        assert man and man.get("ok"), p.stdout[-500:]
        assert man["analysis_mode"] == mode
        outs[mode] = man
    a, b = outs["suite"], outs["fr"]
    for key in ("thresholds", "span_km", "n_fibers"):
        assert a[key] == b[key], key
    assert {c["kind"] for c in b["columns"]} <= {"splice", "connector"}, b["columns"]
    assert all(c["category"] in ("reburn", "event", "gainer", "ref", "dirty_connector") for c in b["cells"]), b["cells"]
