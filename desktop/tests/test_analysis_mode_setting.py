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

# The hub is imported INSIDE a fixture, not at module level: this file sorts
# near the top of the suite, and importing app.py at collection pulls the
# Viewer's sor_reader324802a copy into sys.modules before any engine test
# has imported the engine's -- every later `import splicereportmatchexfo`
# then fails (31 collection errors).  A run-time import lands after
# collection, when the engine's copy is already the cached one, which is the
# position every other hub-importing test happens to sort into.

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

def test_all_three_command_builders_carry_the_mode(settings_dir, hub):
    for mode in ("suite", "fr"):
        hub.save_analysis_mode(mode)
        sr = hub.splicereport_cmd("/a", "/b", "/o.xlsx", "X", "Y")
        uni = hub.uni_cmd("/a", "/o.xlsx")
        ss = hub.secretsauce_cmd("/a", "/out", "xlsx")
        for cmd in (sr, uni, ss):
            i = cmd.index("--analysis")
            assert cmd[i + 1] == mode, (mode, cmd)
            assert cmd.count("--analysis") == 1


def test_the_viewer_is_told_the_same_mode():
    assert "'analysis_mode': 'suite'}" in TS.split("CONFIG = {", 1)[1][:900]
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
    assert "key='analysis_radio'" in body and "st.rerun()" in body
    # the radio's key holds the label; the mode lives in its own slot
    assert "st.session_state['analysis_mode'] = _mode" in body


# ── the runners accept it, set it, echo it ────────────────────────────────

def test_runners_accept_the_flag_set_the_engine_and_echo_it():
    assert "ap.add_argument('--analysis', default='suite', choices=('suite', 'fr')," in RUNNER
    i_set = RUNNER.index("E.ANALYSIS_MODE = args.analysis")
    i_ov = RUNNER.index("if args.overrides:")
    assert i_set < i_ov, "the mode is set before the overrides, before anything runs"
    assert RUNNER.count("'analysis_mode': args.analysis") == 2, "bidir and uni manifests"
    assert "ap.add_argument('--analysis', default='suite', choices=('suite', 'fr')," in SS_RUNNER
    assert "payload.setdefault('analysis_mode', args.analysis)" in SS_RUNNER
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


def test_both_modes_produce_the_same_report_today(tmp_path):
    """Plumbing only: with nothing branching on the mode yet, a FastReporter
    run and an OTDR Suite run of the same pair must be identical cell for
    cell, and each manifest must name the mode it ran in."""
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
    for key in ("cells", "columns", "n_flagged", "thresholds", "span_km", "n_fibers"):
        assert a[key] == b[key], key
