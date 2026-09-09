"""Bidirectional Pass 0: the table->raw-trace bridge includes the declared span start.

Follow-up to the uni fix (#167).  Both bidirectional loaders — the engine's
main() and the hub's run_splicereport — stamp `_trace_offset_km`, the one
number every raw-trace probe adds to a table position (silent-side windows,
FR sweep, reflectance measure, backscatter anchors, far-end geometry).  They
derived it from the EVENT TABLE alone: the launch reel Pass 0 consumes.  On a
pre-trimmed export (span start set on the launch connector; GenParams user
offset = the reel length) the table shows no reel, so the stamp read 0.0 and
every probe on HOWLAN<->LANHOW indexed the glass 1 km upstream of the event
it was about.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'splicereport'))

import splicereportmatchexfo as E  # noqa: E402


def _ev(km, loss=0.05, refl=-55.0, end=False, reflective=True, tot=1):
    t = ('1E' if reflective else '0E') if end else ('1F' if reflective else '0F')
    return {'dist_km': km, 'splice_loss': loss, 'reflection': refl,
            'is_end': end, 'is_reflective': reflective, 'type': t + '9999LS',
            'time_of_travel': tot}


PRETRIMMED = [_ev(0.0, 0.17, tot=0), _ev(17.19, 0.09, reflective=False),
              _ev(60.64, 0.0, refl=-46.0, end=True)]
UNTRIMMED = [_ev(0.0, 0.0, refl=-80.0, tot=0), _ev(1.0044, 0.17),
             _ev(18.19, 0.09, reflective=False), _ev(61.64, 0.0, refl=-46.0, end=True)]


def test_pretrimmed_file_offset_is_the_declared_start():
    r = {'user_offset_km': 1.0044, 'events': PRETRIMMED}
    assert E._trace_frame_offset_km(r, r['events']) == 1.0044
    # Untrimmed file, no declared start: the reel the table carries, as before.
    r = {'user_offset_km': 0.0, 'events': UNTRIMMED}
    assert E._trace_frame_offset_km(r, r['events']) == 1.0044
    # No reel anywhere: 0.0, bit-for-bit the legacy stamp.
    r = {'events': PRETRIMMED}
    assert E._trace_frame_offset_km(r, r['events']) == 0.0


def test_reel_facts_still_govern_the_event_half():
    # A direction polled reel-absent rejects a lone reflective candidate;
    # the declared start still counts because it is not an event.
    r = {'user_offset_km': 0.5, 'events': UNTRIMMED}
    assert E._trace_frame_offset_km(r, r['events'], None, True, None) == 0.5


def test_both_bidirectional_loaders_stamp_through_the_helper():
    eng = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    hub = open(os.path.join(ROOT, 'splicereport', 'run_splicereport.py'),
               encoding='utf-8').read()
    # Neither loader may go back to the event-only derivation for the stamp.
    bad = re.compile(r"_trace_offset_km'\]\s*=\s*E?\.?_untrimmed_launch_offset_km\(")
    assert not bad.search(eng) and not bad.search(hub)
    assert eng.count("_trace_offset_km'] = _trace_frame_offset_km(") == 1
    assert hub.count("_trace_offset_km'] = E._trace_frame_offset_km(") == 1
