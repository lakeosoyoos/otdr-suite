"""The bounce-echo guard has to do its arithmetic in the RAW frame.

THE BUG.  `_is_likely_echo` suppresses a reflective event when a STRONGER
reflector sits at 1/n of its distance, because a strong reflector bounces
light off the OTDR port and the instrument draws a phantom at n times that
reflector's distance.  The scan that calls it hands it launch-NORMALIZED
km, and the bounce is anchored at the port, not at the launch connector.
Normalizing both sides first shifts the predicted echo position by

    n*(k + L) - n*k  -  (cand + L - cand)  =  (n-1) * L

which at n=2 is one whole launch cord.  ECHO_PARENT_TOL_KM is 0.7 km and a
launch reel is about 1 km, so the guard could not fire on a second-order
echo on any span shot with a reel.

WHAT IT COST.  ELLINWOOD<->INMAN fiber 381 carries the span's only
saturated reflector, 14.6662 km from the Inman OTDR.  Its second-order
ghost lands at 29.3631 km, 31 m from the predicted 29.3324, and the report
printed it as a real reflective event at 32.37 km in the A frame, on its
own report column.  The field tech reviewed the A trace, found nothing
there, and reported the 32.37 entry as wrong.  He was right.

    raw frame          2 x 14.6662 = 29.3324  vs 29.3631   miss   31 m  -> echo
    normalized frame   2 x 13.6618 = 27.3236  vs 28.3587   miss 1035 m  -> silent

and 1035 m is the 1.0044 km launch cord.

WHY THE A SIDE LOOKS CLEAN.  From Ellinwood the same reflector sits at
48.0751 km, so its second-order ghost would land at 96.15 km, past the end
of an 80 km acquisition.  The one-sided visibility is the ghost geometry,
not a directional reflector.

THE CONTROL is in this file: a genuine isolated reflection with no
upstream parent, and a no-reel span, must both be untouched.
"""
import importlib.util
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
FIXDIR = os.path.join(HERE, 'fixtures', 'satrefl')

EIB = os.path.join(FIXDIR, 'INMELL0381_1550.sor')   # B-dir, carries the ghost
EIA = os.path.join(FIXDIR, 'ELLINM0381_1550.sor')   # A-dir, ghost out of range

LAUNCH_B = 1.0044          # this fiber's B-side launch cord
PARENT_RAW = 14.6662       # the saturated reflector, raw km from the OTDR
GHOST_RAW = 29.3631        # the phantom the report printed as 32.37 km


def _engine():
    sys.path.insert(0, os.path.join(ROOT, 'splicereport'))
    try:
        import splicereportmatchexfo as E
    finally:
        sys.path.pop(0)
    return E


def _reader():
    path = os.path.join(ROOT, 'splicereport', 'sor_reader324802a.py')
    spec = importlib.util.spec_from_file_location('_rdr_echo', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _norm_parents(launch):
    """The parent list as the scan builds it: launch-normalized km."""
    return [(0.0 - launch, -51.686),
            (1.0044 - launch, -55.333),
            (PARENT_RAW - launch, -23.285),
            (GHOST_RAW - launch, -67.384)]


# ── the arithmetic that defines the bug ───────────────────────────────────

def test_the_ghost_is_at_twice_the_parent_in_the_raw_frame():
    assert abs(GHOST_RAW - 2 * PARENT_RAW) * 1000 < 50      # 31 m


def test_normalizing_first_breaks_it_by_exactly_one_launch_cord():
    p, c = PARENT_RAW - LAUNCH_B, GHOST_RAW - LAUNCH_B
    miss = abs(c - 2 * p)
    assert miss == pytest.approx(LAUNCH_B, abs=0.05)
    eng = _engine()
    assert miss > eng.ECHO_PARENT_TOL_KM      # which is why it went silent


# ── the guard itself ──────────────────────────────────────────────────────

def test_guard_catches_the_ghost_when_given_the_launch_offset():
    eng = _engine()
    assert eng._is_likely_echo(GHOST_RAW - LAUNCH_B, -67.384,
                               _norm_parents(LAUNCH_B),
                               launch_km=LAUNCH_B) is True


def test_guard_is_blind_without_the_offset():
    """Documents the old behaviour, so a regression is loud."""
    eng = _engine()
    assert eng._is_likely_echo(GHOST_RAW - LAUNCH_B, -67.384,
                               _norm_parents(LAUNCH_B)) is False


def test_a_no_reel_span_is_completely_unaffected():
    """launch_km defaults to 0, and 0 must reproduce the old arithmetic."""
    eng = _engine()
    parents = [(0.0, -51.0), (PARENT_RAW, -23.285)]
    assert (eng._is_likely_echo(GHOST_RAW, -67.384, parents, launch_km=0.0)
            is eng._is_likely_echo(GHOST_RAW, -67.384, parents))


def test_a_genuine_isolated_reflection_is_still_kept():
    """No stronger parent at 1/n, so nothing may be suppressed. This is the
    TOPMIL0195 @30.92 km class the guard was written to preserve."""
    eng = _engine()
    lone = [(0.0 - LAUNCH_B, -51.0), (1.0044 - LAUNCH_B, -55.0),
            (30.92 - LAUNCH_B, -70.0)]
    assert eng._is_likely_echo(30.92 - LAUNCH_B, -70.0, lone,
                               launch_km=LAUNCH_B) is False


def test_a_weaker_parent_never_suppresses_a_stronger_candidate():
    """An echo is always weaker than what produced it."""
    eng = _engine()
    parents = [(PARENT_RAW - LAUNCH_B, -70.0)]
    assert eng._is_likely_echo(GHOST_RAW - LAUNCH_B, -23.0, parents,
                               launch_km=LAUNCH_B) is False


# ── the real bytes ────────────────────────────────────────────────────────

def test_fixture_carries_the_parent_and_the_ghost():
    rd = _reader()
    d = rd.parse_sor_full(EIB)
    evs = d['events']
    parent = next(e for e in evs if e['type'].startswith('2'))
    ghost = next(e for e in evs if abs(e['dist_km'] - GHOST_RAW) < 0.01)
    assert parent['dist_km'] == pytest.approx(PARENT_RAW, abs=5e-4)
    assert parent['reflection'] == pytest.approx(-23.285, abs=5e-4)
    # the ghost carries no loss, because there is nothing there
    assert ghost['splice_loss'] == pytest.approx(-0.001, abs=5e-4)
    assert ghost['reflection'] == pytest.approx(-67.384, abs=5e-4)


def test_the_same_fiber_carries_a_second_admitted_ghost():
    """EOF + parent, well past the fiber end. Corroborates that this trace
    produces ghosts from this reflector."""
    rd = _reader()
    evs = rd.parse_sor_full(EIB)['events']
    end = next(e for e in evs if e['is_end'])
    beyond = [e for e in evs if e['dist_km'] > end['dist_km'] + 0.05]
    assert beyond, 'expected the 76.44 km ghost'
    assert beyond[0]['dist_km'] == pytest.approx(end['dist_km'] + 14.7019,
                                                 abs=0.05)


def test_from_the_a_side_the_ghost_falls_off_the_end_of_the_acquisition():
    """So the A trace being clean at the mirrored position is the ghost
    geometry, not evidence of a directional reflector."""
    rd = _reader()
    d = rd.parse_sor_full(EIA)
    parent_a = next(e for e in d['events'] if e['type'].startswith('2'))
    assert parent_a['dist_km'] == pytest.approx(48.0751, abs=5e-4)
    acq_km = d['num_points'] * 125 * 61.6986 / 3025250
    assert 2 * parent_a['dist_km'] > acq_km
