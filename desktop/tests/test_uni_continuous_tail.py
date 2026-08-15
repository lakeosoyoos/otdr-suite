"""A continuous-fiber acquisition has no far end, so it gets no far-end guard.

`uni_fiber_eof` deliberately falls back to the `1O` continuous-fiber marker
when a fiber has no real end event — the range was set shorter than the cable,
and span detection and break detection both need SOME distance to work with.
That fallback is right for those two jobs and wrong for a third: the far-end
exclusion.

The exclusion exists because a real fiber end is a large reflective event whose
skirt corrupts loss fits in front of it.  A `1O` marker is not an event at all;
the digitizer just stops.  The glass right up to the edge is ordinary mid-span
fiber, and excluding it throws away real plant to dodge a reflection that isn't
there.

Caught on MILELMsh_1550 — 1151 fibers, 3.99 km short shot, every fiber ending
in `1O`.  Guarding off the continuous marker dropped 117 events at 3.90 km.
All 117 reproduce on their own traces: stored median 0.206 dB against
trace-measured 0.205, 117 of 117 inside 0.1 dB.
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'splicereport'))

import splicereportmatchexfo as E  # noqa: E402

SP = 5e-08
M0 = SP * (299792458.0 / 1.468) / 2.0


def _ev(km, loss, typ='0F9999LS', end=False, refl=None):
    return {'dist_km': km, 'splice_loss': loss, 'reflection': refl,
            'is_end': end, 'is_reflective': typ.startswith('1'),
            'type': typ, 'time_of_travel': int(km * 1e6)}


def _fiber(last_type, feature_km=3.90, end_km=3.99, loss=0.41):
    """One fiber with a real event near the far edge, terminated either by a
    genuine end (`1E`) or by a continuous-fiber marker (`1O`)."""
    n = int((end_km + 0.4) * 1000 / M0)
    rng = np.random.RandomState(4)
    tr = 47.0 + 0.19 * (np.arange(n) * M0 / 1000.0) + rng.normal(0, 0.004, n)
    tr[int(feature_km * 1000 / M0):] += loss
    return {'events': [_ev(0.0, 0.0, '1F9999LS'),
                       _ev(feature_km, loss),
                       _ev(end_km, 0.0, last_type, end=(last_type[1] == 'E'))],
            'trace': tr, 'exfo_sampling_period': SP, 'fxd_pulse_ns': 10.0,
            'num_points': n, 'filename': 'f.sor'}


def _off_splice(rec):
    fibers = {1: rec}
    return E.uni_find_off_splice_events(fibers, [], launch_box_present=False,
                                        span_km=3.99)


def test_continuous_fiber_keeps_events_near_the_acquisition_edge():
    """`1O` — the cable continues past the range.  Nothing to guard against."""
    got = _off_splice(_fiber('1O9999LS'))
    assert [round(e['position_km'], 2) for e in got] == [3.90], got


def test_a_real_fiber_end_still_gets_its_guard():
    """`1E` — a genuine end.  The 0.5 km exclusion in front of it stands, so
    the same event at the same distance is correctly withheld."""
    assert _off_splice(_fiber('1E9999LS')) == []


def test_the_guard_keys_on_the_strict_end_not_the_fallback():
    """The distinction lives in one place, so both finders inherit it."""
    cont, real = _fiber('1O9999LS'), _fiber('1E9999LS')
    assert E.uni_tail_ceiling(cont) is None
    assert E.uni_tail_ceiling(real) is not None
    # and the fallback still reports a distance — span and break detection
    # depend on it, which is exactly why it cannot double as "the fiber ends
    # here" for the exclusion.
    assert E.uni_fiber_eof(cont) is not None
    assert E.uni_fiber_eof_strict(cont) is None


def test_reflective_finder_follows_the_same_rule():
    """The mid-span reflectance band had the identical bug."""
    rec = _fiber('1O9999LS')
    rec['events'][1] = _ev(3.90, 0.21, '1F9999LS', refl=-60.0)
    old = E.UNI_REFL_FLOOR_DB
    try:
        E.UNI_REFL_FLOOR_DB = -80.0
        got = E.uni_find_reflective_events({1: rec}, 3.99,
                                           launch_box_present=False,
                                           bs_level=-75.0)
    finally:
        E.UNI_REFL_FLOOR_DB = old
    assert [round(e['position_km'], 2) for e in got] == [3.90], got
