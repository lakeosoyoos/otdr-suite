"""BREAK_LOSS_DB — a loss big enough to be a break on its own terms.

The engine's own rule is that a BREAK is a reflective event with dead glass
past it: the trace has to stop.  AWS / IIG MT.1085 grades differently
(NCT, 2026-09-12):

    we treat any event over 5 dB as a break.  It may technically be a
    high-loss event rather than a clean separation, but the fiber is
    unusable until it's repaired, so from our standpoint it's a break and
    gets reported that way.

Span 27 fiber 202 is the case that exposed it: 14.5 dB at 34.77 km with the
trace still reaching the far end, which we printed as an ordinary splice
while their review counted the fiber broken.  Their threshold is explicitly
loss-based rather than tied to the instrument's off-scale flag.

EITHER direction, never the average: a break blocks light, so the far end
can read almost nothing across it.  Fiber 202 reads -0.106 from A and 14.525
from B, so the mean is 7.2 — a rule written against the mean would need an
event to lose twice the specified figure before it tripped.
"""
from __future__ import annotations

import importlib
import sys

from conftest import SPLICEREPORT_DIR

sys.path.insert(0, str(SPLICEREPORT_DIR))
import splicereportmatchexfo as E  # noqa: E402

IIG = "AWS / IIG MT.1085"
GATE = 5.0

# Span 27 fiber 202, the two directions as the engine reads them.
F202_A, F202_B = -0.106, 14.525


def _on(gate):
    before = E.BREAK_LOSS_DB
    E.BREAK_LOSS_DB = gate
    return before


def test_ships_off():
    assert E.BREAK_LOSS_DB == 0.0


def test_off_is_never_a_break():
    """Off, the engine's reflective-with-dead-glass rule is the only one."""
    before = _on(0.0)
    try:
        for a, b in ((F202_A, F202_B), (50.0, 50.0), (0.1, 0.1), (None, 99.0)):
            assert E._break_by_loss(a, b, True) is False, (a, b)
    finally:
        _on(before)


def test_fiber_202_is_a_break():
    before = _on(GATE)
    try:
        assert E._break_by_loss(F202_A, F202_B, True) is True
    finally:
        _on(before)


def test_the_average_would_not_have_caught_it_at_the_customers_figure():
    """Why the rule reads EITHER direction.  Pin the arithmetic so nobody
    'simplifies' this to the bidirectional value later."""
    assert abs((F202_A + F202_B) / 2.0 - 7.2095) < 1e-6
    assert (F202_A + F202_B) / 2.0 > GATE          # fiber 202 happens to clear
    # ...but a break just past the gate does not: 6 dB one side, nothing the
    # other, averages to 3 and would sail through a mean-based rule.
    before = _on(GATE)
    try:
        assert E._break_by_loss(0.0, 6.0, True) is True
        assert (0.0 + 6.0) / 2.0 < GATE
    finally:
        _on(before)


def test_either_direction_trips_it():
    before = _on(GATE)
    try:
        assert E._break_by_loss(9.0, 0.02, True) is True
        assert E._break_by_loss(0.02, 9.0, True) is True
        assert E._break_by_loss(-9.0, 0.02, True) is True   # magnitude
    finally:
        _on(before)


def test_an_ordinary_splice_is_not_a_break():
    before = _on(GATE)
    try:
        for a, b in ((0.166, 0.235), (0.595, 0.66), (4.9, 4.9), (2.0, 3.0)):
            assert E._break_by_loss(a, b, True) is False, (a, b)
        # exactly the gate is not OVER the gate
        assert E._break_by_loss(5.0, 5.0, True) is False
    finally:
        _on(before)


def test_off_link_events_are_left_alone():
    """The rule is for ON-LINK events.  The end of the fiber is not one."""
    before = _on(GATE)
    try:
        assert E._break_by_loss(F202_A, F202_B, False) is False
    finally:
        _on(before)


def test_missing_directions_do_not_crash_or_trip():
    before = _on(GATE)
    try:
        assert E._break_by_loss(None, None, True) is False
        assert E._break_by_loss(None, 9.0, True) is True
        assert E._break_by_loss(9.0, None, True) is True
        assert E._break_by_loss(None, 0.2, True) is False
    finally:
        _on(before)


def test_profile_carries_the_switch_and_the_whitelist_allows_it():
    app = importlib.import_module('app')  # engine imported first, on purpose
    assert "BREAK_LOSS_DB" in app._PROFILE_ENGINE_KEYS
    assert app.CUSTOMER_PROFILES[IIG]["engine"]["BREAK_LOSS_DB"] == 5.0
    assert app._engine_extras_from_profile(IIG)["BREAK_LOSS_DB"] == 5.0
    for name, prof in app.CUSTOMER_PROFILES.items():
        if name == IIG:
            continue
        assert "BREAK_LOSS_DB" not in (prof.get("engine") or {}), name
