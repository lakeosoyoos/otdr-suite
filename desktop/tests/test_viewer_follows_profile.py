"""The Viewer must judge a cell by the gates the REPORT ran at, not by the
engine's baseline constants.

The Viewer deliberately does not retype the engine's numbers — it reads them,
so that a cell flagged in the grid is flagged in the Viewer and vice versa.
It read them out of `splicereportmatchexfo.py`'s SOURCE, which is a half-truth:
app.py's CUSTOMER_PROFILES rewrite those same constants per customer by pushing
`--overrides` into the engine subprocess (Lumen 0.120, Zayo 0.200, AWS / IIG
0.200), and a source parse cannot see a value that only exists at run time in
another process.  So the Viewer judged every run at 0.160 whatever the report
did.  Under IIG that is a 40 mdB band — 0.160 up to 0.200 — where a cell the
report left clean reads as over-threshold in the Viewer.

The gates travel as an `thresholds` block on the run's own manifest, and three
properties matter, each with its own way of breaking quietly:

  1. The manifest states what the engine APPLIED, not what the hub ASKED for.
     run_splicereport's guards skip an override it rejects, so echoing the
     request back would have the Viewer gating at a number the run never used.
  2. trace_server prefers the run's gates and falls back to the source parse.
     Fallback is not cosmetic — an older cached manifest has no block, and a
     Viewer opened with no report behind it has no report to agree with.
  3. Both report grids push it.  The bidirectional grid is where the bug was
     reported, but the uni panel moves UNI_BEND_THRESHOLD the same way.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from conftest import (
    run_splicereport, import_trace_server,
    FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, REPO_ROOT,
)

IIG = "AWS / IIG MT.1085"
TS = import_trace_server()


def _push(mapping):
    """Push a run's gates at the trace server the way a report grid does.
    getattr, not a direct call, so a build without the channel fails on the
    assertion that follows — the property — instead of erroring in setup."""
    getattr(TS, "set_thresholds", lambda _m: None)(mapping)


@pytest.fixture(autouse=True)
def _clean_gate():
    """Never leak a pushed gate into another module's tests — trace_server is
    a process-global, and test_viewer_ab_frame pins the baseline."""
    _push(None)
    yield
    _push(None)


# ── 1. The engine states the gates it actually ran at ────────────────────
def test_manifest_states_the_gate_the_run_used(tmp_path):
    """A profile run has to be able to say what it graded by; without this
    there is nothing for the Viewer to follow."""
    rc, man, err = run_splicereport(
        FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, tmp_path / "r.xlsx",
        overrides={"REBURN_THRESHOLD": 0.200})
    assert rc == 0 and man and man.get("ok"), err[-2000:]
    assert man["thresholds"]["REBURN_THRESHOLD"] == 0.200
    # The other two gates ride along so one push can set the whole Viewer.
    assert "UNI_BEND_THRESHOLD" in man["thresholds"]
    assert "SINGLE_DIR_THRESHOLD" in man["thresholds"]


def test_a_baseline_run_states_the_baseline(tmp_path):
    """No overrides → the manifest reports the engine's own constants, so the
    Viewer that follows it lands exactly where it does today."""
    src = (REPO_ROOT / "splicereport" / "splicereportmatchexfo.py").read_text(
        encoding="utf-8")
    base = float(re.search(r"^REBURN_THRESHOLD\s*=\s*([\d.]+)", src, re.M).group(1))
    rc, man, err = run_splicereport(
        FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, tmp_path / "r.xlsx")
    assert rc == 0 and man and man.get("ok"), err[-2000:]
    assert man["thresholds"]["REBURN_THRESHOLD"] == base


def test_the_manifest_reports_the_applied_gate_not_the_requested_one(tmp_path):
    """run_splicereport REJECTS a non-positive REBURN_THRESHOLD and keeps the
    baseline.  Echoing the hub's request instead of reading the engine back
    would hand the Viewer a 0.0 gate — everything over threshold — for a run
    that graded at 0.160.  This is the reason the echo is a read-back."""
    src = (REPO_ROOT / "splicereport" / "splicereportmatchexfo.py").read_text(
        encoding="utf-8")
    base = float(re.search(r"^REBURN_THRESHOLD\s*=\s*([\d.]+)", src, re.M).group(1))
    rc, man, err = run_splicereport(
        FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, tmp_path / "r.xlsx",
        overrides={"REBURN_THRESHOLD": 0.0})
    assert rc == 0 and man and man.get("ok"), err[-2000:]
    assert man["thresholds"]["REBURN_THRESHOLD"] == base


# ── 2. The Viewer follows the run, and falls back when there is none ─────
def test_viewer_gate_follows_the_pushed_run():
    """The bug, in one assertion: a run that graded at IIG's 0.200 must make
    the Viewer grade at 0.200."""
    _push({"REBURN_THRESHOLD": 0.200,
                       "UNI_BEND_THRESHOLD": 0.250,
                       "SINGLE_DIR_THRESHOLD": 0.250})
    assert TS.engine_thresholds()["reburn"] == 0.200


def test_the_forty_millibel_disagreement_band_is_closed():
    """0.17 dB under IIG: unflagged in the report (0.200 gate), and the
    Viewer used to call it over threshold at 0.160.  Walk the band and
    require the two verdicts to agree at every step."""
    _push({"REBURN_THRESHOLD": 0.200})
    gate = TS.engine_thresholds()["reburn"]
    for mdb in range(160, 200):                    # 0.160 .. 0.199 inclusive
        loss = mdb / 1000.0
        # The engine gates on the value it PRINTS (round-then-test); the
        # Viewer's clearsGate() does the same, so compare that way here.
        assert round(loss, 3) < gate, f"{loss:.3f} dB must stay clean under IIG"
    assert round(0.200, 3) >= gate, "0.200 itself still flags"


def test_uni_gate_follows_its_own_run():
    """The uni settings panel moves UNI_BEND_THRESHOLD off 0.250; the Viewer
    switches to that gate whenever the click came from the uni report."""
    _push({"UNI_BEND_THRESHOLD": 0.400})
    assert TS.engine_thresholds()["uni_bend"] == 0.400


def test_no_report_pushed_leaves_the_engine_baseline():
    """A Viewer nobody clicked into from a report has no report to agree with,
    and an older cached manifest carries no gates.  Both must land on the
    source-parsed engine constants — today's behaviour, unchanged."""
    src = (REPO_ROOT / "splicereport" / "splicereportmatchexfo.py").read_text(
        encoding="utf-8")
    base = float(re.search(r"^REBURN_THRESHOLD\s*=\s*([\d.]+)", src, re.M).group(1))
    for pushed in (None, {}, {"BIDIR_CONNECTOR_LOSS": 0.5}, "not-a-dict", []):
        _push(pushed)
        assert TS.engine_thresholds()["reburn"] == base, repr(pushed)


def test_an_unusable_pushed_gate_falls_back_rather_than_flagging_everything():
    """Belt and braces on the runner's own guard: a 0 / negative / NaN gate
    would make the Viewer flag every event on the screen.  Keep the baseline
    instead of trusting the number.  Same shape the runner demands before it
    applies an override, so the two ends cannot disagree about what is usable.
    (A numeric STRING is usable — the runner coerces with float() too — so it
    is deliberately not in this list.)"""
    src = (REPO_ROOT / "splicereport" / "splicereportmatchexfo.py").read_text(
        encoding="utf-8")
    base = float(re.search(r"^REBURN_THRESHOLD\s*=\s*([\d.]+)", src, re.M).group(1))
    for bad in (0, -0.5, float("nan"), float("inf"), None, "abc", {}, []):
        _push({"REBURN_THRESHOLD": bad})
        assert TS.engine_thresholds()["reburn"] == base, repr(bad)


# ── 3. End to end: the report's own manifest drives the Viewer ───────────
def test_report_manifest_drives_the_viewer_gate(tmp_path):
    """The whole path in one test, minus the Streamlit line: run the engine
    under the IIG contract threshold, hand its manifest to the trace server
    the way the report grid does, and read the gate the Viewer is served."""
    ov = {"REBURN_THRESHOLD": 0.200}               # IIG's bidir splice loss
    rc, man, err = run_splicereport(
        FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, tmp_path / "r.xlsx",
        overrides=ov)
    assert rc == 0 and man and man.get("ok"), err[-2000:]
    _push(man.get("thresholds"))
    assert TS.engine_thresholds()["reburn"] == 0.200


def test_the_gate_reaches_the_wire_not_just_the_function(tmp_path):
    """/api/list is what viewer.html actually reads (gThresholds).  Serve a
    real request and assert the pushed gate is in the JSON — a fix that stops
    at engine_thresholds() would leave the browser on the old number."""
    import threading
    from http.server import HTTPServer
    from urllib.request import urlopen

    _push({"REBURN_THRESHOLD": 0.200})
    TS.set_dirs(str(FIXTURE_SPLICE_A_DIR), str(FIXTURE_SPLICE_B_DIR))
    srv = HTTPServer(("127.0.0.1", 0), TS.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        with urlopen(f"http://127.0.0.1:{srv.server_port}/api/list",
                     timeout=30) as r:
            payload = json.loads(r.read().decode("utf-8"))
    finally:
        srv.shutdown()
        srv.server_close()
        TS.set_dirs(None, None)
    assert payload["thresholds"]["reburn"] == 0.200


# ── 4. Both report grids push their run's gates ──────────────────────────
def test_both_report_grids_push_their_gates():
    """Wiring pin.  The bidirectional grid is where the disagreement was
    reported; the uni grid has the same shape and would be the next one to
    drift.  Each must hand the trace server the manifest's gates right where
    it points it at the span."""
    s = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    assert s.count("trace_server.set_thresholds(res.get('thresholds'))") == 2, (
        "both the Splice Report/FR grid and the Uni grid must push the gates "
        "the run reported")
    # …and beside the set_dirs call, so a grid can never point the Viewer at a
    # span without also telling it how that span was graded.
    for anchor in ("trace_server.set_dirs(_sd[0]",
                   "trace_server.set_dirs(folder, None)"):
        i = s.index(anchor)
        assert "set_thresholds" in s[i:i + 900], anchor


def test_the_iig_profile_reaches_the_viewer(tmp_path):
    """The reported bug, end to end and with nothing hand-typed: take the IIG
    profile's overrides the way the hub builds them for a real run, run the
    engine with exactly those, and require the gate the Viewer is served to
    equal the gate the profile asked for.

    Pre-fix this fails with 0.16 != 0.2 — the 40 mdB band.  Deriving the
    number from CUSTOMER_PROFILES rather than writing 0.200 here means the
    test still guards the Viewer if the contract value is ever renegotiated.
    """
    import app as hub

    ov = hub._overrides_from_settings(hub._otdr_settings_from_profile(IIG))
    want = ov["REBURN_THRESHOLD"]
    assert want != TS._source_thresholds()["reburn"] if hasattr(
        TS, "_source_thresholds") else True, (
        "IIG must differ from the baseline or this test proves nothing")

    rc, man, err = run_splicereport(
        FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, tmp_path / "r.xlsx",
        overrides=ov)
    assert rc == 0 and man and man.get("ok"), err[-2000:]

    # What the report grid does with the manifest it just got back.
    _push(man.get("thresholds"))
    assert TS.engine_thresholds()["reburn"] == want, (
        "the Viewer is judging at %r while the report graded at %r"
        % (TS.engine_thresholds()["reburn"], want))
