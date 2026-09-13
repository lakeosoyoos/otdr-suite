"""SPLICE_STRICT_BOUNDARY — the AWS / IIG MT.1085 splice-loss boundary rule.

The contract reads "0.20 dB or less", so a splice flags only when it is
strictly OVER 0.20.  The subtlety is WHICH number that test is applied to.

NCT grades the iOLM bidirectional table, where each direction is reported to
3 decimals; their bidirectional figure is the mean of those two readings.  We
were averaging the full-precision legs instead, and on a knife-edge cell the
fraction of a millidecibel that neither instrument reports cast the deciding
vote.  Both rules agree on six of the seven boundary cells across Spans 17,
19, 25 and 27 — and disagree on Span 25 fiber 193, which NCT passes:

    fiber             A       B      full-precision mean   mean of the pair
    Span 19 / 84    0.166   0.235        0.20027 flag         0.2005 flag
    Span 19 / 408   0.137   0.264        0.20028 flag         0.2005 flag
    Span 25 / 22    0.230   0.169        0.19958 pass         0.1995 pass
    Span 25 / 193   0.326   0.074        0.20037 FLAG         0.2000 pass
    Span 27 / 62    0.285   0.115        0.19977 pass         0.2000 pass
    Span 17 / 2     0.109   0.290        0.19956 pass         0.1995 pass
    Span 17 / 348   0.159   0.240        0.19951 pass         0.1995 pass

Rounding each direction first is 7 for 7.  It also lands the value on a
half-mdB about half the time, which three decimals cannot show — so a value
straddling the gate prints to 4 decimals (0.2005), and only one straddling
the gate, so the grid is not sprayed with false precision.

Everything here ships OFF for every other profile.
"""
from __future__ import annotations

import importlib
import sys

from conftest import SPLICEREPORT_DIR

sys.path.insert(0, str(SPLICEREPORT_DIR))
import splicereportmatchexfo as E  # noqa: E402

IIG = "AWS / IIG MT.1085"
GATE = 0.20

# (span, fiber, A, B, NCT's verdict) — read off their acceptance workbooks.
BOUNDARY = [
    (19, 84, 0.166, 0.235, True),
    (19, 408, 0.137, 0.264, True),
    (25, 22, 0.230, 0.169, False),
    (25, 193, 0.326, 0.074, False),
    (27, 62, 0.285, 0.115, False),
    (17, 2, 0.109, 0.290, False),
    (17, 348, 0.159, 0.240, False),
]


def _on(flag):
    """Set the switch, returning the previous value."""
    before = E.SPLICE_STRICT_BOUNDARY
    E.SPLICE_STRICT_BOUNDARY = flag
    return before


def test_ships_off():
    assert E.SPLICE_STRICT_BOUNDARY == 0


def test_default_leaves_the_value_and_the_gate_alone():
    """Off, _splice_bidir hands back the caller's own average untouched and
    the gate is _clears_threshold to the bit."""
    before = _on(0)
    try:
        for _, _, a, b, _v in BOUNDARY:
            default = round((a + b) / 2.0, 4)
            assert E._splice_bidir(a, b, default) == default
        for loss in (0.200, 0.2005, 0.1996, 0.246, 0.0, -0.25, None):
            assert (E._clears_splice_threshold(loss, GATE)
                    == E._clears_threshold(loss, GATE)), loss
    finally:
        _on(before)


def test_reproduces_every_boundary_verdict():
    """The load-bearing test: 7 of 7 against NCT's own reviews."""
    before = _on(1)
    try:
        for span, fiber, a, b, should_flag in BOUNDARY:
            value = E._splice_bidir(a, b, round((a + b) / 2.0, 4))
            got = E._clears_splice_threshold(value, GATE)
            assert got is should_flag, (span, fiber, value, got, should_flag)
    finally:
        _on(before)


def test_the_full_precision_average_would_miss_fiber_193():
    """Pin the reason this rule exists: averaging the raw legs flags a fiber
    NCT passes.  If this ever stops being true the rule can be simplified."""
    a, b = 0.32634429463112724, 0.07438605330919046   # Span 25 fiber 193
    assert (a + b) / 2.0 > GATE                        # raw mean: over
    before = _on(1)
    try:
        assert E._splice_bidir(a, b, (a + b) / 2.0) == 0.200
        assert E._clears_splice_threshold(
            E._splice_bidir(a, b, (a + b) / 2.0), GATE) is False
    finally:
        _on(before)


def test_exactly_the_gate_passes_and_a_hair_over_flags():
    before = _on(1)
    try:
        assert E._clears_splice_threshold(0.200, GATE) is False
        assert E._clears_splice_threshold(0.2005, GATE) is True
        assert E._clears_splice_threshold(0.246, GATE) is True
        assert E._clears_splice_threshold(None, GATE) is False
        # magnitude in both modes — a gainer past the gate still flags
        assert E._clears_splice_threshold(-0.25, GATE) is True
        assert E._clears_splice_threshold(-0.20, GATE) is False
    finally:
        _on(before)


def test_half_mdb_detection():
    for v in (0.2005, 0.1995, -0.2005, 7.2095, 0.0005):
        assert E._is_half_mdb(v) is True, v
    for v in (0.200, 0.201, 0.2465001, 0.0, None):
        assert E._is_half_mdb(v) is False, v


def test_only_a_value_straddling_the_gate_prints_four_decimals():
    """A flagged cell must not print a number that reads like a pass, but a
    half-mdB nowhere near the gate stays at three decimals."""
    before = _on(1)
    thr = E.REBURN_THRESHOLD
    try:
        E.REBURN_THRESHOLD = GATE
        # straddling the gate: the fourth decimal IS the verdict
        assert E._format_loss(0.2005) == ".2005"
        assert E._format_loss(0.1995) == ".1995"
        assert E._format_loss(-0.2005) == "-.2005"
        # clear of the gate: three decimals, no false precision.  Which way a
        # x.xxx5 literal rounds is IEEE-754's business, so pin the WIDTH.
        for v in (0.2085, 0.0005, 7.2095, 0.1465):
            assert len(E._format_loss(v).split('.')[-1]) == 3, v
        assert E._format_loss(0.200) == ".200"
    finally:
        E.REBURN_THRESHOLD = thr
        _on(before)


def test_labels_are_untouched_with_the_switch_off():
    before = _on(0)
    try:
        for v in (0.2005, 0.1995, 0.2085, 0.0005):
            assert len(E._format_loss(v).split('.')[-1]) == 3, v
    finally:
        _on(before)


def test_splice_bidir_falls_back_when_a_direction_is_missing():
    """Single-direction cells have no pair to round; they keep the caller's
    value rather than silently becoming half of one reading."""
    before = _on(1)
    try:
        assert E._splice_bidir(None, 0.24, 0.24) == 0.24
        assert E._splice_bidir(0.24, None, 0.24) == 0.24
    finally:
        _on(before)


def test_profile_carries_the_switch_and_the_whitelist_allows_it():
    app = importlib.import_module('app')  # engine imported first, on purpose
    assert "SPLICE_STRICT_BOUNDARY" in app._PROFILE_ENGINE_KEYS
    assert app.CUSTOMER_PROFILES[IIG]["engine"]["SPLICE_STRICT_BOUNDARY"] == 1
    assert app._engine_extras_from_profile(IIG)["SPLICE_STRICT_BOUNDARY"] == 1.0
    for name, prof in app.CUSTOMER_PROFILES.items():
        if name == IIG:
            continue
        assert "SPLICE_STRICT_BOUNDARY" not in (prof.get("engine") or {}), name


def test_other_gates_keep_the_house_rule():
    """Scope guard: bend, single-direction, uni and connector gates still
    round-then->=, switch or no switch."""
    before = _on(1)
    try:
        assert E._clears_threshold(0.200, GATE) is True
        assert E._clears_threshold(0.1996, GATE) is True
        assert E._clears_threshold(0.100, 0.100) is True
    finally:
        _on(before)
