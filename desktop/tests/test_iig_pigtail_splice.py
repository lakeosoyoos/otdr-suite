"""PIGTAIL_SPLICE_WINDOW_M — the splice a few metres behind a panel port.

A panel port is two elements, not one: the mated connector at 0 m and, a few
metres behind it, the splice joining the pigtail to the cable.  The OTDR
cannot always separate them — at 275 ns both sit inside one pulse — but the
iOLM's own element list does, and types them:

    Position 0.0   Connector  Loss 0.047  Verdict Pass
    Position 3.8   Splice     Loss 0.455  Verdict Fail

which is NCT's "near 0.047 / pigtail 0.455" on span 17 fiber 397, to the
millidecibel.  Their rule going forward (2026-09-12): the pigtail is graded
as a SPLICE, against the splice limit, "with the connector graded separately".

Read from the Exchange sidecar and nowhere else.  The .sor carries the
pigtail as a second event at 0 m on some fibers and merges it into the
connector on others — span 27 fiber 177 is one it merges — so grading
whatever sits at the port would move a panel figure on nothing more physical
than whether the firmware happened to split the events.
"""
from __future__ import annotations

import importlib
import json
import sys

from conftest import SPLICEREPORT_DIR

sys.path.insert(0, str(SPLICEREPORT_DIR))
import json_reader as J           # noqa: E402
import splicereportmatchexfo as E  # noqa: E402

IIG = "AWS / IIG MT.1085"
WINDOW = 50.0


def _elements(*els):
    return {"brief": {"Measurement": {"Elements": list(els)}}}


def _el(pos, kind, loss_1550, loss_1625=None, verdict="Pass"):
    res = [{"Wavelength": "1550", "Loss": str(loss_1550)}]
    if loss_1625 is not None:
        res.append({"Wavelength": "1625", "Loss": str(loss_1625)})
    return {"Position": str(pos), "Type": kind, "Results": res,
            "ElementVerdict": verdict}


def _write(folder, fiber, *els):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"BIL400-RPX400-OSP-0432F-01-{fiber:04d}-AB.json").write_text(
        json.dumps(_elements(*els)), encoding="utf-8")


def _on(window):
    before = E.PIGTAIL_SPLICE_WINDOW_M
    E.PIGTAIL_SPLICE_WINDOW_M = window
    return before


# ── reading it ───────────────────────────────────────────────────────────

def test_reads_the_splice_behind_the_connector(tmp_path):
    """Span 17 fiber 397's real shape."""
    _write(tmp_path, 397,
           _el(-1004.0, "Connector", 0.625),      # the launch reel's own end
           _el(0.0, "Connector", 0.047),          # the panel
           _el(3.8, "Splice", 0.455, 0.512, "Fail"),
           _el(6135.9, "Splice", -0.103))
    got = J.read_panel_pigtails(str(tmp_path), WINDOW)
    assert set(got) == {397}
    assert abs(got[397]["loss"] - 0.455) < 1e-9
    assert abs(got[397]["position_m"] - 3.8) < 1e-9
    assert got[397]["verdict"] == "Fail"


def test_the_reel_connector_at_a_negative_position_is_not_the_panel(tmp_path):
    """Only the connector at zero opens the window."""
    _write(tmp_path, 1,
           _el(-1004.0, "Connector", 0.6),
           _el(-1000.0, "Splice", 0.9),           # behind the reel, not ours
           _el(0.0, "Connector", 0.05),
           _el(4.0, "Splice", 0.30))
    got = J.read_panel_pigtails(str(tmp_path), WINDOW)
    assert abs(got[1]["loss"] - 0.30) < 1e-9


def test_a_splice_beyond_the_window_is_not_a_pigtail(tmp_path):
    _write(tmp_path, 2, _el(0.0, "Connector", 0.05), _el(120.0, "Splice", 0.9))
    assert J.read_panel_pigtails(str(tmp_path), WINDOW) == {}


def test_the_first_span_splice_does_not_become_a_pigtail(tmp_path):
    """The element right after the panel is the pigtail; the closure 6 km out
    is not, and must not be pulled in by a generous window."""
    _write(tmp_path, 3, _el(0.0, "Connector", 0.05), _el(6135.9, "Splice", 0.9))
    assert J.read_panel_pigtails(str(tmp_path), WINDOW) == {}


def test_grades_at_the_wavelength_asked_for(tmp_path):
    _write(tmp_path, 4, _el(0.0, "Connector", 0.05),
           _el(3.8, "Splice", 0.455, 0.512))
    assert abs(J.read_panel_pigtails(str(tmp_path), WINDOW, 1550.0)[4]["loss"]
               - 0.455) < 1e-9
    assert abs(J.read_panel_pigtails(str(tmp_path), WINDOW, 1625.0)[4]["loss"]
               - 0.512) < 1e-9


def test_a_fiber_with_no_resolved_pigtail_is_simply_absent(tmp_path):
    """About three quarters of fibers on the measured spans resolve none.
    Absent, never zero — a zero would read as a measured 0.000 dB."""
    _write(tmp_path, 5, _el(0.0, "Connector", 0.05))
    assert J.read_panel_pigtails(str(tmp_path), WINDOW) == {}


def test_no_sidecars_or_a_damaged_one_never_raises(tmp_path):
    assert J.read_panel_pigtails(str(tmp_path), WINDOW) == {}
    assert J.read_panel_pigtails(str(tmp_path / "nope"), WINDOW) == {}
    _write(tmp_path, 6, _el(0.0, "Connector", 0.05), _el(3.0, "Splice", 0.9))
    (tmp_path / "BIL400-RPX400-OSP-0432F-01-0007-AB.json").write_text(
        "{not json", encoding="utf-8")
    assert set(J.read_panel_pigtails(str(tmp_path), WINDOW)) == {6}


def test_window_of_zero_reads_nothing(tmp_path):
    _write(tmp_path, 8, _el(0.0, "Connector", 0.05), _el(3.0, "Splice", 0.9))
    assert J.read_panel_pigtails(str(tmp_path), 0.0) == {}


# ── grading it ───────────────────────────────────────────────────────────

def test_a_gainer_at_the_panel_is_never_graded():
    """The one place in this engine where the gate is deliberately one-sided.

    The splice gate reads MAGNITUDE, so a gainer flags as hard as a loss.
    That is right for glass and wrong here: an element measured a few metres
    after a connector sits on the far side of its backscatter step, and the
    median resolved pigtail on these spans is about -0.25 dB.  Gating on
    magnitude flagged 150 of them on span 27 alone."""
    before = _on(WINDOW)
    try:
        for loss in (-0.455, -0.25, -0.9, -0.201):
            assert not (loss > 0 and E._clears_splice_threshold(loss, 0.20)), loss
        # a real loss still grades
        assert 0.455 > 0 and E._clears_splice_threshold(0.455, 0.20)
    finally:
        _on(before)


def test_attach_is_a_no_op_with_the_switch_off(tmp_path):
    _write(tmp_path, 9, _el(0.0, "Connector", 0.05), _el(3.0, "Splice", 0.9))
    fibers = {9: {}}
    before = _on(0.0)
    try:
        E._attach_panel_pigtails(str(tmp_path), fibers)
        assert fibers[9] == {}
    finally:
        _on(before)


def test_attach_stamps_the_fiber_when_asked(tmp_path):
    _write(tmp_path, 9, _el(0.0, "Connector", 0.05), _el(3.0, "Splice", 0.9))
    fibers = {9: {}, 10: {}}
    before = _on(WINDOW)
    try:
        E._attach_panel_pigtails(str(tmp_path), fibers)
        assert abs(fibers[9]['_panel_pigtail']['loss'] - 0.9) < 1e-9
        assert '_panel_pigtail' not in fibers[10]   # no sidecar, no stamp
    finally:
        _on(before)


def test_attach_survives_a_folder_that_is_not_there():
    fibers = {1: {}}
    before = _on(WINDOW)
    try:
        E._attach_panel_pigtails("/no/such/folder", fibers)
        assert fibers[1] == {}
    finally:
        _on(before)


# ── wiring ───────────────────────────────────────────────────────────────

def test_switch_ships_off():
    assert E.PIGTAIL_SPLICE_WINDOW_M == 0.0


def test_profile_carries_the_switch_and_the_whitelist_allows_it():
    app = importlib.import_module('app')  # engine imported first, on purpose
    assert "PIGTAIL_SPLICE_WINDOW_M" in app._PROFILE_ENGINE_KEYS
    assert app.CUSTOMER_PROFILES[IIG]["engine"]["PIGTAIL_SPLICE_WINDOW_M"] == 50.0
    assert app._engine_extras_from_profile(IIG)["PIGTAIL_SPLICE_WINDOW_M"] == 50.0
    for name, prof in app.CUSTOMER_PROFILES.items():
        if name == IIG:
            continue
        assert "PIGTAIL_SPLICE_WINDOW_M" not in (prof.get("engine") or {}), name
