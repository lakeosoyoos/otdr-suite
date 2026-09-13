"""PANEL_UNGRADEABLE_GAP_DB — an end whose far readings are a recovery reel.

A panel connector is normally graded on the average of its two views: the
reading taken AT that end, and the other direction's view of the same
connector a reel length back from its own end of fiber.  When the far side is
patched into a recovery reel, every far reading at that end carries the reel
with it (NCT, 2026-09-12, on span 27's Lavina end):

    every far-end shot at Lavina terminates into the reel, which adds ~1.2 dB
    that is not in the fiber [...] Suppressing them would hide that a reshoot
    is owed; failing them would be wrong, since the loss is not in the plant.

So such an end is graded on its NEAR readings alone and the report says so.
It is a whole-end offset, which is why the test is a population median rather
than a per-fiber comparison.

There is NO per-cell third state: at an ungradeable end a connector is graded
on its near reading and either flags or does not.  "Not gradeable" is a
statement about the END, and it lives in the report's threshold table.

The gate sits at 0.45 dB, between the two populations measured against NCT's
own published verdicts:

    graded bidirectionally   0.074  0.074  0.285  0.428
    graded near-side only    0.495  0.592  0.719  0.773
"""
from __future__ import annotations

import importlib
import sys

from conftest import SPLICEREPORT_DIR

sys.path.insert(0, str(SPLICEREPORT_DIR))
import splicereportmatchexfo as E  # noqa: E402

IIG = "AWS / IIG MT.1085"
GATE = 0.45

# far-minus-near medians measured on the four spans we hold, with the verdict
# NCT's own reviews record for that end.
MEASURED = [
    ("span 25 Clyde Park", 0.074, False),
    ("span 27 Rapelje", 0.074, False),
    ("span 17 Superior", 0.285, False),
    ("span 17 Frenchtown", 0.428, False),
    ("span 19 Drummond", 0.495, True),
    ("span 19 Turah", 0.592, True),
    ("span 27 Lavina", 0.719, True),
    ("span 25 Big Timber", 0.773, True),
]


def _on(gate):
    before = E.PANEL_UNGRADEABLE_GAP_DB
    E.PANEL_UNGRADEABLE_GAP_DB = gate
    return before


def _fiber(loss, km=0.0, is_end=False):
    return {'events': [{'dist_km': km, 'splice_loss': loss, 'reflection': -55.0,
                        'type': '1F9999LS', 'is_end': is_end}],
            '_raw_events': [{'dist_km': km, 'splice_loss': loss,
                             'reflection': -55.0, 'type': '1F9999LS',
                             'is_end': is_end}]}


def test_ships_off():
    assert E.PANEL_UNGRADEABLE_GAP_DB == 0.0


def test_off_no_end_is_ever_ungradeable():
    before = _on(0.0)
    try:
        for _, gap, _v in MEASURED:
            assert E._end_is_ungradeable(gap) is False, gap
        assert E._end_is_ungradeable(99.0) is False
    finally:
        _on(before)


def test_the_gate_separates_every_end_we_have_measured():
    """The load-bearing test: 8 of 8 against NCT's own published verdicts."""
    before = _on(GATE)
    try:
        for name, gap, ungradeable in MEASURED:
            assert E._end_is_ungradeable(gap) is ungradeable, (name, gap)
    finally:
        _on(before)


def test_the_margin_either_side_of_the_gate():
    """Pin how much room the gate actually has.  It is not much: if a future
    span lands between these two numbers the rule needs NCT's own guard, not
    a guess."""
    graded = max(g for _, g, u in MEASURED if not u)
    near_only = min(g for _, g, u in MEASURED if u)
    assert graded < GATE < near_only
    assert round(GATE - graded, 3) == 0.022
    assert round(near_only - GATE, 3) == 0.045


def test_a_missing_or_negative_gap_is_never_ungradeable():
    before = _on(GATE)
    try:
        assert E._end_is_ungradeable(None) is False
        assert E._end_is_ungradeable(-0.9) is False
        assert E._end_is_ungradeable(GATE) is False      # not OVER the gate
    finally:
        _on(before)


def test_gap_is_far_median_minus_near_median():
    """One bad fiber must not make an end ungradeable, and one good one must
    not rescue it: the reel is a property of the end."""
    before = _on(GATE)
    try:
        near = {i: _fiber(0.10) for i in range(1, 21)}
        far = {i: _fiber(0.10, km=70.0, is_end=True) for i in range(1, 21)}
        gaps = E._panel_gap_by_end(near, far)
        assert abs(gaps['A']) < 1e-9
        assert E._end_is_ungradeable(gaps['A']) is False

        # one wild far reading — still a healthy end
        far[7] = _fiber(9.0, km=70.0, is_end=True)
        assert E._end_is_ungradeable(E._panel_gap_by_end(near, far)['A']) is False

        # the whole far population lifted by a reel — ungradeable
        far = {i: _fiber(0.80, km=70.0, is_end=True) for i in range(1, 21)}
        gaps = E._panel_gap_by_end(near, far)
        assert abs(gaps['A'] - 0.70) < 1e-9
        assert E._end_is_ungradeable(gaps['A']) is True
    finally:
        _on(before)


def test_an_end_with_nothing_to_measure_reads_none():
    assert E._panel_gap_by_end({}, {})['A'] is None
    assert E._panel_gap_by_end(None, None)['B'] is None


def test_the_report_says_which_end_and_that_a_reshoot_is_owed(tmp_path):
    before = _on(GATE)
    try:
        near = {i: _fiber(0.10) for i in range(1, 21)}
        far = {i: _fiber(0.80, km=70.0, is_end=True) for i in range(1, 21)}
        note = E._ungradeable_note(near, far, "Rapelje BIL400", "Lavina RPX400")
        assert "NOT graded bidirectionally at Rapelje BIL400" in note
        assert "recovery reel" in note
        assert "NEAR readings alone" in note
        assert "reshoot is owed" in note
        # a healthy span says nothing at all
        assert E._ungradeable_note(near, {i: _fiber(0.10, km=70.0, is_end=True)
                                          for i in range(1, 21)},
                                   "A", "B") == ""
    finally:
        _on(before)


def test_the_note_is_silent_with_the_switch_off():
    before = _on(0.0)
    try:
        near = {i: _fiber(0.10) for i in range(1, 21)}
        far = {i: _fiber(0.80, km=70.0, is_end=True) for i in range(1, 21)}
        assert E._ungradeable_note(near, far, "A", "B") == ""
    finally:
        _on(before)


def test_profile_carries_the_switch_and_the_whitelist_allows_it():
    app = importlib.import_module('app')  # engine imported first, on purpose
    assert "PANEL_UNGRADEABLE_GAP_DB" in app._PROFILE_ENGINE_KEYS
    assert app.CUSTOMER_PROFILES[IIG]["engine"]["PANEL_UNGRADEABLE_GAP_DB"] == 0.45
    assert app._engine_extras_from_profile(IIG)["PANEL_UNGRADEABLE_GAP_DB"] == 0.45
    for name, prof in app.CUSTOMER_PROFILES.items():
        if name == IIG:
            continue
        assert "PANEL_UNGRADEABLE_GAP_DB" not in (prof.get("engine") or {}), name
