"""Viewer stacked mode: A and B must land on the same physical metre.

Field report (Zach Kuhlmann, screenshot of the Viewer):

    "in viewer we need to match up A and B traces even if one is shot with a
     launch box and one is not."

A shot taken through a launch reel carries ~1 km of the tech's own fiber
before the cable, and usually another reel after it.  Stacked mode draws A
untouched and MIRRORS B, so the mirror origin decides whether the two traces
describe the same metre of glass.

It used to mirror about B's END event.  Expanding that for a cable position p
(metres from the A end):

    A raw   = launchA + p
    B raw   = farConnB - p
    old B display = eofB - B raw = (farConnB + tailB) - (farConnB - p)
                  = p + tailB
    A display     = p + launchA

so the two separate by `launchA - tailB`.  On a normal both-ends shoot A's
launch reel and B's receive reel are the SAME physical reel, that difference
is ~0, and the bug is invisible — which is exactly why it survived.  Shoot one
direction through a reel and trim the other and the traces stand a full
kilometre apart.

Measured on real spans, worst cable-start misalignment:

    ELMMIL / MILELM     both directions through a reel      25.5 m  ->  0.0 m
    Reubensville PTL    A through a reel, B trimmed       1006.7 m  ->  1.7 m
    BARTUL / TULBAR     neither direction, already right     0.0 m  ->  0.0 m

The 1.7 m residual is the span-median launch offset versus fiber 1's own, the
same frame noise the report grid already carries in `_vkm`.

Everything here is synthetic — CI has no .sor files.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'viewer'))

import trace_server as TS      # noqa: E402


def ev(km, refl=False, end=False, tot=1):
    return {'dist_km': km, 'is_reflective': refl, 'is_end': end,
            'time_of_travel': tot}


def reel_shot(launch=1.0, cable=60.0, tail=1.0):
    """A trace taken through a launch reel and into a receive reel."""
    return [ev(0.0, refl=True, tot=0),                 # OTDR port
            ev(launch, refl=True),                     # launch connector
            ev(launch + cable * 0.4),                  # a splice
            ev(launch + cable, refl=True),             # far connector
            ev(launch + cable + tail, end=True)]       # end of receive reel


def trimmed_shot(cable=60.0):
    """The same cable with start/stop already picked — no reels at all.

    Leads with the ~87,594 km time-of-travel artifact the viewer's reader
    still emits, because real trimmed files do."""
    return [ev(87593.9386, refl=True, tot=0),
            ev(0.0, refl=True),
            ev(cable * 0.4),
            ev(cable, end=True)]


# ─── the launch rule must be the ENGINE's launch rule ──────────────────

def test_launch_is_the_first_reflective_event_after_the_port():
    assert TS._trace_launch_km(reel_shot(launch=1.0049)) == 1.0049


def test_a_trimmed_trace_has_no_launch_reel():
    """THE regression that made the panel folders misbehave first time round.

    The lead artifact is not the OTDR port, so the positional test fails and
    the answer is 'no reel' — which is the truth."""
    assert TS._trace_launch_km(trimmed_shot()) is None


def test_a_launch_further_out_than_a_reel_is_not_a_reel():
    assert TS._trace_launch_km(reel_shot(launch=9.0)) is None


def test_the_port_event_must_carry_a_zero_time_of_travel():
    """Without this the rule would take any early reflective event — and the
    Splice Report, which the viewer has to agree with, tests it."""
    ev_list = reel_shot()
    ev_list[0] = ev(0.0, refl=True, tot=42)
    assert TS._trace_launch_km(ev_list) is None


def test_it_matches_the_splice_report_engine_rule_verbatim():
    """Source-level parity, not a re-implementation.

    The report grid hands the viewer cell distances ALREADY shifted by the
    engine's launch offset (`_vkm` adds launch_a_km), so if these two ever
    measure it differently every deep link lands in the wrong place.  Checked
    by reading the engine's source: importing both modules in one process
    would collide on their two different `sor_reader324802a` copies."""
    src = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    fn = src[src.index('def _untrimmed_launch_offset_km'):][:1200]
    assert "time_of_travel'] == 0" in fn
    assert 'LAUNCH_FIBER_MAX' in fn
    engine_max = float(re.search(r'^LAUNCH_FIBER_MAX\s*=\s*([\d.]+)',
                                 src, re.M).group(1))
    assert TS.LAUNCH_MAX_KM == engine_max, (
        'viewer and engine disagree on how far out a launch reel can be')


# ─── the receive reel, and the geometry that has to close ──────────────

def test_receive_reel_setback_is_measured_from_the_end():
    assert abs(TS._trace_tail_setback_km(reel_shot(tail=1.02)) - 1.02) < 1e-9


def test_no_receive_reel_when_the_far_connector_is_the_end():
    ev_list = [ev(0.0, refl=True, tot=0), ev(1.0, refl=True),
               ev(30.0), ev(61.0, refl=True, end=True)]
    assert TS._trace_tail_setback_km(ev_list) is None


def test_a_short_jumper_does_not_read_its_own_far_end_as_a_reel():
    """THE guard.  A 60 m panel jumper has its whole length inside the window
    a reel occupies; without a closing-geometry check the trimmed Reubensville
    folder claimed an 81 m launch reel and a 61 m receive reel on a 61 m
    cable, and the frame collapsed."""
    assert TS._trace_tail_setback_km(trimmed_shot(cable=0.0613)) is None


def test_reel_plus_cable_plus_reel_must_leave_real_cable():
    ev_list = [ev(0.0, refl=True, tot=0), ev(1.0, refl=True),
               ev(1.02, refl=True), ev(2.0, end=True)]
    assert TS._trace_tail_setback_km(ev_list) is None


# ─── population rules ──────────────────────────────────────────────────

def test_the_receive_reel_needs_a_majority_of_the_whole_sample():
    """Fibers with NO reading count against.  One fiber's reflective event
    near the end must not decide where a whole B direction is drawn."""
    assert TS._agreed([1.0, 1.0, None, None, None, None], 6) is None
    assert TS._agreed([1.0, 1.0, 1.0, 1.01, None, None], 6) is not None


def test_a_scattered_population_is_not_a_reel():
    assert TS._agreed([0.4, 1.0, 1.7, 2.4, 0.1, 2.9], 6) is None


def test_the_launch_offset_ignores_fibers_that_have_none():
    """Aggregated the way the engine aggregates it — median over the readings
    that exist — so a folder where some fibers were trimmed still reports the
    reel the rest of them share."""
    assert TS._median_of([1.0, None, 1.0, None]) == 1.0
    assert TS._median_of([None, None]) is None


# ─── the alignment invariant itself ────────────────────────────────────

def disp_a(km):
    """A is drawn untouched: display frame IS A's raw frame."""
    return km


def disp_b(km, far_conn_b, launch_a):
    """B mirrored about its far connector, offset into A's raw frame."""
    return (far_conn_b + launch_a) - km


def test_both_directions_put_the_cable_start_on_the_same_metre():
    launch_a, cable, tail_b = 1.0, 60.0, 1.0
    far_conn_b = cable                       # B trimmed: no reels
    # cable position 0 == A's launch connector == B's far connector
    assert abs(disp_a(launch_a) - disp_b(far_conn_b, far_conn_b, launch_a)) < 1e-9


def test_it_holds_when_only_one_direction_has_a_reel():
    """Robert's case, stated as arithmetic."""
    launch_a = 1.0049
    cable = 0.0613
    far_conn_b = cable                       # B trimmed
    assert abs(disp_a(launch_a)
               - disp_b(far_conn_b, far_conn_b, launch_a)) < 1e-9
    # ...and the far end of the cable, too
    assert abs(disp_a(launch_a + cable) - disp_b(0.0, far_conn_b, launch_a)) < 1e-9


def test_the_old_mirror_is_off_by_launch_minus_tail():
    """Pins the DIAGNOSIS, so nobody re-derives it from scratch: the old
    origin was B's end event, and the error is exactly launchA - tailB."""
    launch_a, cable, tail_b = 1.0049, 0.0613, 0.0
    far_conn_b, eof_b = cable, cable + tail_b
    old_b_start = eof_b - far_conn_b
    assert abs((disp_a(launch_a) - old_b_start) - (launch_a - tail_b)) < 1e-9
    assert abs(launch_a - tail_b) > 1.0          # a full kilometre apart


def test_a_matched_pair_of_reels_hides_the_bug():
    """Why it went unnoticed: A's launch reel and B's receive reel are the
    same physical reel on a normal shoot, so the old error cancels."""
    launch_a = tail_b = 1.0
    cable = 60.0
    far_conn_b, eof_b = cable, cable + tail_b
    assert abs(disp_a(launch_a) - (eof_b - far_conn_b)) < 1e-9


# ─── wiring: the viewer must actually USE this ─────────────────────────

def _viewer_src():
    return open(os.path.join(ROOT, 'viewer', 'viewer.html'),
                encoding='utf-8').read()


def test_the_mirror_origin_is_the_far_connector_not_the_end_event():
    src = _viewer_src()
    fn = src[src.index('function mirrorOriginKm'):][:600]
    assert 'far_conn_km' in fn and 'gLaunchA' in fn
    disp = src[src.index('function dispKm'):][:200]
    assert 'mirrorOriginKm' in disp and 'eofKm(t) -' not in disp


def test_the_inverse_transform_goes_through_the_same_origin():
    """dataKmFromDisp and the draw-slice bounds both invert dispKm; if either
    kept its own copy of the origin the drawn slice would drift out of step
    with the drawn points."""
    src = _viewer_src()
    inv = src[src.index('function dataKmFromDisp'):][:200]
    assert 'mirrorOriginKm' in inv
    assert 'const eof = eofKm(t);' not in src, (
        'the draw-slice still inverts the transform by hand')


def test_the_server_ships_the_frame_with_every_trace():
    src = open(os.path.join(ROOT, 'viewer', 'trace_server.py'),
               encoding='utf-8').read()
    assert "'far_conn_km': far_conn_km" in src
    assert "'launch_a_km':" in src
    assert "'time_of_travel':" in src, 'the launch rule needs it in the payload'
