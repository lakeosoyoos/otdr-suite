"""A .sor that carries a DECLARED span states its events in a different frame.

FastReporter lets a tech mark where the cable starts and stops.  When it saves
that, it re-bases every KeyEvent so the span start is 0 and LEAVES THE TRACE
ALONE.  Verified by diffing one file before and after FR set a span: DataPts,
FxdParams and SupParams byte-identical, only KeyEvents, the proprietary block
and the checksum move.

So the file ends up with its events about a kilometre from its own samples, and
stores nothing recording the gap.  Every numeric field was searched on real
production files: FR's Launch / Receive / Absolute lengths appear nowhere, only
the span length.  FR derives the rest, and so must we.

THIS IS PRODUCTION DATA, not a curiosity.  Both tie-panel folders on disk are
span-declared files -- a 1 km launch reel, the ~62 m jumper as the declared
span, a 1 km receive reel -- and until this the Viewer drew their events a
kilometre off the curve, with the pre-span one flung to 87,594 km by the
unsigned read fixed alongside (test_viewer_signed_events.py).

THE DERIVATION.  In the span frame the LAST event is the far end of the receive
reel; the same point measured off the samples is the absolute end of fiber.
The difference is where the span start sits in the raw acquisition, which is
FR's own "Launch fiber length".  Read off FR's Spans by Distance dialog for
both folders and compared:

    FTH01 tie panel        FR 1.0449 km    ours 1.0449    -0.0 m
    PTL1PTL6 Reubensville  FR 1.0294 km    ours 1.0287    -0.7 m

Those two were -1.6 m and -2.3 m until the detector stopped reporting the START
of the window it measured the drop across instead of its middle.  The bias was
half a window every time -- 20 samples at ~0.08 m -- which is the whole of the
error it used to carry.

Per FIBER it is not always that good.  FTH01 fiber 010 carries seven events and
a trace that stops early, and votes about a kilometre out.  So the folder
decides by consensus, exactly as it already does for a reel: 13 of 14 fibers
agreed within 3 m there and the odd one is outvoted.

After it, on the same folders: every event lands inside the trace, and the last
event sits 0.1 to 11 m from where the curve actually falls off.

Everything here is synthetic -- CI has no .sor files.
"""
import os
import re
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'viewer'))

import trace_server as TS      # noqa: E402


def _trace(events, eof_km=2.14, n=4000, span_km=2.5):
    """A trace whose curve falls off at `eof_km`, with the given events."""
    x = np.linspace(0.0, span_km, n)
    y = np.where(x < eof_km, 10.0 - 0.2 * x, -18.0)
    # a little noise so the smoother has something realistic to chew on
    y = y + np.sin(np.arange(n)) * 0.05
    return {'dist_km': list(x), 'trace_db': list(y),
            'events': [{'dist_km': e} for e in events]}


# ── detection ────────────────────────────────────────────────────────────

def test_a_negative_event_is_the_marker():
    """Nothing but a re-base to a span start puts an event before zero."""
    t = _trace([-0.0153, 0.0, 0.0624, 0.0775, 1.0956])
    assert TS._trace_span_launch_km(t) is not None


def test_an_ordinary_file_votes_nothing():
    """No negative event, no declared span, and the reel rules are left to it."""
    t = _trace([0.0, 0.9993, 1.5, 2.04])
    assert TS._trace_span_launch_km(t) is None


def test_a_trace_too_short_to_measure_abstains():
    assert TS._trace_span_launch_km({'dist_km': [0, 1], 'trace_db': [1, 1],
                                     'events': [{'dist_km': -0.1}]}) is None


# ── the derivation ───────────────────────────────────────────────────────

def test_the_offset_is_the_end_of_fiber_minus_the_last_event():
    """The panel shape: span start at 0, receive reel out to 1.0956, and the
    curve really ending at 2.14 -- so the span starts at 1.0444 in the raw
    acquisition.  FR says 1.0449 for this folder."""
    t = _trace([-0.0153, 0.0, 0.0624, 0.0775, 1.0956], eof_km=2.14)
    got = TS._trace_span_launch_km(t)
    # This fixture's samples are 0.625 m apart, an order coarser than a real
    # acquisition, so a couple of samples of slack is the resolution floor here.
    assert abs(got - (2.14 - 1.0956)) < 0.005
    assert abs(got - 1.0449) < 0.01, 'should land within metres of FR'


def test_the_end_of_fiber_is_the_steepest_drop_not_a_noise_threshold():
    """A threshold on the noise floor picks up a single spike kilometres past
    the real end -- measured, it put the end of a 2.04 km fiber at 4.7 km.  The
    steepest drop of a smoothed trace does not."""
    t = _trace([0.0, 1.0], eof_km=2.04, span_km=5.0)
    t['trace_db'][3900] = 30.0                    # a lone spike out in the noise
    assert abs(TS._trace_eof_km(t) - 2.04) < 0.05


# ── the folder decides, not one fiber ────────────────────────────────────

def test_one_odd_fiber_cannot_move_the_folder():
    """FTH01 fiber 010 has seven events and a trace that stops early; it votes
    about a kilometre out.  Thirteen of fourteen agreeing must win."""
    good = [1.0433] * 13
    votes = good + [0.0]                       # the outlier
    assert abs(TS._agreed(votes, len(votes)) - 1.0433) < 0.001


def test_a_folder_that_does_not_agree_gets_no_answer():
    """Scattered votes are not evidence.  Better to leave the file alone than
    to shift every event by a number nothing supports."""
    votes = [1.0, 1.4, 1.9, 2.4, 2.9, 3.4]
    assert TS._agreed(votes, len(votes)) is None


def test_frame_facts_carries_the_declared_span():
    """And every early return has to carry the key, or callers reading it on an
    empty folder get a KeyError instead of 'no declared span'."""
    for d in (None, '/definitely/not/a/directory'):
        assert TS.frame_facts(d)['span_launch_km'] is None


# ── the wiring ───────────────────────────────────────────────────────────

def _server_src():
    return open(os.path.join(ROOT, 'viewer', 'trace_server.py'), encoding='utf-8').read()


def test_the_span_is_settled_before_the_reels_are_looked_for():
    """Order matters: the reel rules read event positions, and a span-declared
    file states them in a frame a kilometre from its samples.  Measure the span
    first, shift, then look for reels."""
    src = _server_src()
    fn = src[src.index('def frame_facts('):]
    fn = fn[:fn.index('\ndef ')]
    i_span = fn.index('_trace_span_launch_km')
    i_reel = fn.index('_trace_launch_km(ev)')
    assert i_span < i_reel, 'reels are being detected in the wrong frame'


def test_the_events_are_put_back_before_anything_frames_the_trace():
    """The mirror, the report deep links and the event grid all speak the raw
    frame, so a span-declared file has to arrive looking like any other."""
    src = _server_src()
    fn = src[src.index('def load_trace('):]
    fn = fn[:fn.index('\ndef ')]
    i_shift = fn.index("t['span_launch_km']")
    i_frame = fn.index('_trace_frame(d, t)')
    assert i_shift < i_frame


def test_the_cached_trace_is_never_mutated():
    """`_load_trace_cached` is keyed on the file alone and shared by every
    caller; shifting its events in place would hand the next reader a
    double-shifted frame."""
    src = _server_src()
    fn = src[src.index('def load_trace('):]
    fn = fn[:fn.index('\ndef ')]
    assert 't = dict(t)' in fn
    assert re.search(r"t\['events'\] = \[dict\(e, dist_km=", fn)
