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

THE reported span, Banning <-> Indio, 576 fibers each way:

    A  Banning->Indio     launch 0.9993 (reel)   end 67.5238   cable 66.5245
    B  Indio->Banning V2  launch none            end 66.5245   cable 66.5245

A's cable length and B's end event agree to four decimals, and B carries no
reel at all — the exact configuration Zach described.  Across all 576 fibers,
A splices paired with a B splice inside 150 m:

    before      0 / 4734
    after    2813 / 4734  (59.4%), mean gap 14.0 m

Zero.  Nothing lined up.  The 40.6% still unpaired are events one direction
sees and the other does not, which is ordinary A/B asymmetry.

On his fiber 29, the cable start moved from display 0.0000 to 0.9993 — onto
A's launch connector, where it belongs — and the three splices B can see went
from 293 / 1007 / 992 m out to 10.2 / 7.7 / 7.6 m.

THE INDEPENDENT CHECK.  Anchoring forces the cable's near end to agree, so
that end proves nothing.  The FAR end is free, and its disagreement works out
to exactly

    (farConnA - launchA) - (farConnB - launchB)  =  L_A - L_B

the cable length as each direction measures it.  That is the only test there
is of the receive-reel detector, which has no ground truth anywhere else.
Across 13 A/B pairs on disk:

    Zach Banning / Indio V2     -0.1 m      NILAND / WINTERHAVEN   +0.2 m
    ELMNEW / NEWELM             -0.9 m      Tucu / Romero          +0.5 m
    BARTUL / TULBAR             +1.3 m      Mecca / Niland         +2.5 m
    MILTOP / TOPMIL             -3.1 m      ELMMIL / MILELM        +4.3 m
    ONTBOI / BOIONT             -6.2 m      VERSAL / SALVER        -6.5 m
    SANDUR / DURSAN             -8.6 m      SantaRosa / Tucumcari  -9.6 m
    NORSEA / SEANOR            -11.1 m

12 of 13 inside 11.1 m — 0.0103% — with tail values that vary independently
from span to span (1.002 to 1.112 km).  A systematically wrong detector could
not produce agreeing lengths measured from opposite ends off different reels.

The 13th is ELMMILsh / MILELMsh, the SHORT SHOTS: -19.6 m on a 3.96 km span,
0.5%, and only 2 matched splices.  That is not a frame error — a short shot
never reaches the far end, so A's trace is the first 4 km from the A end and
B's is the first 4 km from the B END.  Different glass.

RULED OUT OF SCOPE by the field: short shots are always run unidirectional,
so a bidi pair is never made of them.  Do not build short-shot alignment.
The `cable_end_known` guard stays anyway, because the only way it can now
fire is the accident it is worth catching — the wrong folder loaded.  On disk
`Short Shots/ELMMILsh_1550` and `Long Shots/ELMMIL_1550` differ by two
characters, and picking the wrong one used to produce a confident, entirely
plausible overlay of unrelated cable.

WHAT IS LEFT is a splice-position bias of 0 to 28 m, always positive, which is
NOT this frame: it does not correlate with the far-end check (NILAND is +0.2 m
of frame error and +23.2 m of splice bias; ELMMIL is +4.3 and +0.2), and it
does correlate with per-splice scatter at roughly 0.4x on every pair.  It is
how the two directions' stored event tables place a marker, it is identical
before and after this change, and the report's clustering already absorbs it.

Measured on other real spans, worst cable-start misalignment:

    ELMMIL / MILELM     both directions through a reel      25.5 m  ->  0.0 m
    Reubensville PTL    A through a reel, B trimmed       1006.7 m  ->  1.7 m
    BARTUL / TULBAR     neither direction, already right     0.0 m  ->  0.0 m

The 1.7 m residual is the span-median launch offset versus fiber 1's own, the
same frame noise the report grid already carries in `_vkm`.

Everything here is synthetic — CI has no .sor files.
"""
import os
import re
import statistics
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


def test_the_span_is_the_top_quartile_median_like_the_engine():
    """The Splice Report estimates a span as

        b_span_est = np.median(b_eofs[int(len(b_eofs) * 0.75):])

    Taking the TOP QUARTILE, not the median, is what survives breaks.  The
    viewer has to place B traces on the same cable the report does, so it
    uses the same idiom."""
    src = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    assert 'int(len(b_eofs) * 0.75)' in src, 'engine idiom moved — re-check parity'
    healthy = [67.5] * 9
    broken = [12.0, 30.0, 47.2]                 # three snapped fibers
    assert abs(TS._span_estimate(healthy + broken) - 67.5) < 1e-9


def test_a_plain_median_would_have_been_dragged_short():
    """States what the top quartile buys, so the choice is not mistaken for
    an arbitrary one."""
    lengths = [67.5] * 5 + [12.0, 30.0, 40.0, 47.2, 50.0, 55.0, 58.0]
    assert abs(TS._span_estimate(lengths) - 67.5) < 1e-9
    assert statistics.median(lengths) < 60.0, (
        'the plain median lands among the broken fibers — 11 km short')


def test_a_broken_fiber_is_placed_by_the_population_not_its_own_end():
    """MILELM F231 snaps at 47.26 km on a 69.57 km span.  Mirroring a B trace
    about that fiber's own end event drew it 22.3 km out of place — the same
    hole the old end-event mirror had.  The cable ends where the population
    says it ends; a broken fiber simply does not reach it."""
    facts = {'launch_km': 1.0, 'tail_km': 1.0, 'span_km': 67.5}
    broken = {'events': [ev(0.0, refl=True, tot=0), ev(1.0, refl=True),
                         ev(20.0), ev(47.26, refl=True, end=True)],
              'dist_km': [0.0, 47.5]}
    launch, far = _frame_with(facts, broken)
    assert abs(far - (1.0 + 67.5)) < 1e-6, 'broken fiber followed its own end'


def test_a_healthy_fiber_keeps_its_own_measured_length():
    """Per-fiber length genuinely varies across a ribbon (23 m across ELMMIL's
    1152), so a fiber that DOES reach the far end is placed by its own
    connector, not flattened onto the population median."""
    facts = {'launch_km': 1.0, 'tail_km': 1.0, 'span_km': 67.5}
    healthy = {'events': [ev(0.0, refl=True, tot=0), ev(1.0, refl=True),
                          ev(20.0), ev(68.52, refl=True),
                          ev(69.52, end=True)],
               'dist_km': [0.0, 69.6]}
    _launch, far = _frame_with(facts, healthy)
    assert abs(far - 68.52) < 1e-6


def _frame_with(facts, trace):
    """_trace_frame against a stubbed population, no folder needed."""
    real = TS.frame_facts
    TS.frame_facts = lambda _d: facts
    try:
        return TS._trace_frame('<stub>', trace)
    finally:
        TS.frame_facts = real


def short_shot(launch=1.0, reach=4.0):
    """A truncated near-end acquisition: launch reel, a few km of cable, then
    the acquisition simply runs out.  NO end-of-fiber event — measured on
    ELMMILsh / MILELMsh, 0 of 29 fibers have one, last sample 4.98 km on a
    67.5 km cable."""
    return [ev(0.0, refl=True, tot=0), ev(launch, refl=True),
            ev(launch + reach * 0.3), ev(launch + reach, refl=True)]


def test_a_short_shot_admits_it_does_not_know_where_the_cable_ends():
    """A short shot DOES carry a connector — its launch connector.  What it
    has no knowledge of is the cable's FAR end, which is why A and B short
    shots cannot be put on one frame: they cover opposite ends of the cable
    with tens of kilometres in between that neither file contains.

    Without this the end-event fallback mirrors B about the ACQUISITION
    RANGE, which draws a perfectly plausible overlay of unrelated glass."""
    assert TS._trace_end_km(short_shot()) is None
    assert TS._trace_launch_km(short_shot()) == 1.0     # it has its launch


def test_the_server_reports_cable_end_known_per_direction():
    src = open(os.path.join(ROOT, 'viewer', 'trace_server.py'),
               encoding='utf-8').read()
    assert "'cable_end_known'" in src
    assert "'cable_end_known_b'" in src, 'the viewer needs it for B'
    vw = _viewer_src()
    assert 'cable_end_known_b' in vw, 'the viewer never reads it'
    assert 'cable length unknown' in vw, 'the tech is never told'


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


def test_the_far_end_disagreement_is_the_two_measured_lengths():
    """Pins the identity the real-span validation rests on.

    Near end: forced by the anchor, proves nothing.  Far end: free.  Where A
    draws the cable's far end (its own far connector) versus where the
    mirrored B draws it must come out as L_A - L_B and nothing else — no
    dependence on either reel, which is what makes it an independent test of
    the receive-reel detector."""
    launch_a, launch_b = 1.0049, 1.0203
    far_a, far_b = launch_a + 67.5, launch_b + 67.4      # B reads 100 m short
    a_draws = disp_a(far_a)                              # A: its own far conn
    b_draws = disp_b(launch_b, far_b, launch_a)          # B: its launch conn
    L_a, L_b = far_a - launch_a, far_b - launch_b
    assert abs((a_draws - b_draws) - (L_a - L_b)) < 1e-9
    assert abs((a_draws - b_draws) - 0.100) < 1e-9       # the 100 m, exactly


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


# ─── FastReporter event-table layout ───────────────────────────────────
#
# Zach supplied FR3 screenshots of WSC_SUI_0001.sor and asked the Viewer's
# event table to mirror that structure.  Validated cell-by-cell against the
# same file — every event kind, loss and reflectance matches FR:
#
#   Event 1  Launch Level     0.0000 km   Refl -54.1     FR -54.1
#   Event 4  Positive        15.6452 km   Loss -0.072    FR -0.072
#   Event 7  Positive        31.7390 km   Loss -0.027    FR -0.027
#   Event 10 Reflective      64.0440 km   Refl -16.3     FR -16.3
#   Splice Loss  min/max/avg  -0.072 / 0.138 / 0.049     FR identical
#   Connector Refl. avg       -54.1                      FR identical
#   Section Att. min/max/avg   0.183 / 0.197 / 0.187     FR identical
#
# Connector statistics come out right ONLY because the end event is excluded:
# counting its -16.3 would move the average off FR's -54.1.
#
# Known gap: Section Loss reads up to 2 mdB light (1.046 vs FR's 1.044). It
# is derived as Length x Att, and the SOR stores attenuation at 0.001 dB/km,
# so the rounding is in the file.  FR measures the section directly.

def test_the_table_carries_fastreporters_column_groups():
    src = _viewer_src()
    for lead in ('Identifiers', 'P/F', 'λ (nm)', 'Dir.'):
        assert lead in src, lead
    for grp in ('fr-sechdr', 'fr-stathdr', 'Length<br>(km)', 'Att.<br>(dB/km)'):
        assert grp in src, grp
    for stat in ('Splice Loss (dB)', 'Connector Loss (dB)',
                 'Section Loss (dB)', 'Section Att. (dB/km)'):
        assert stat in src, stat


def test_it_uses_fastreporters_words_for_the_event_kinds():
    """'Positive' is FR's term for a gainer, and the tech reads it on their
    own screen — our own vocabulary here would not match what they see."""
    src = _viewer_src()
    for kind in ('Launch Level', 'Continuous Fiber', 'Reflective',
                 'Non-reflective', 'Positive'):
        assert f"'{kind}'" in src, kind


def test_connector_statistics_exclude_the_end_event():
    """The one rule that had to be inferred.  WSC_SUI_0001 ends on a
    reflective event at -16.3 dB; FR's Connector Reflectance average is
    -54.1, the launch alone.  Counting the end would break the match."""
    src = _viewer_src()
    fn = src[src.index('const bodyRows = traces.map'):][:1400]
    assert 'if (e.is_end) return;' in fn, 'end event reaches the connector stats'


def test_a_launch_level_event_reports_no_loss_of_its_own():
    """FR prints --- for it: the launch is the reference the rest of the
    trace is measured against, not a loss in the span."""
    src = _viewer_src()
    assert 'e.time_of_travel === 0 || e.is_end) ? null : e.splice_loss' in src


def test_the_wavelength_column_is_nominal_not_measured():
    """FR reads 1550; this file's FxdParams says 1546.0 and the proprietary
    block carries the laser's true centre.  Neither matches the tech's
    screen, so the label snaps to the nearest standard window."""
    src = open(os.path.join(ROOT, 'viewer', 'trace_server.py'),
               encoding='utf-8').read()
    assert 'NOMINAL_WAVELENGTHS_NM' in src
    assert 1550 in TS.NOMINAL_WAVELENGTHS_NM
    near = min(TS.NOMINAL_WAVELENGTHS_NM, key=lambda n: abs(n - 1546.0))
    assert near == 1550 and abs(near - 1546.0) <= TS.WAVELENGTH_SNAP_NM


def test_a_column_two_directions_disagree_about_names_both():
    """The cable's A end is A's Launch Level and B's End of Fiber — the same
    physical point.  Grouping them is right; letting whichever trace loaded
    first name the column is not."""
    src = _viewer_src()
    assert "new Set(c.ev.filter(x => x).map(evKind))" in src
    assert 'present.every(x => x.is_end)' in src, (
        'a column only one direction ends at would be labelled End')


# ─── the verdict gate follows the report that opened the Viewer ────────
#
# Robert: "we need to keep ours connected to what the tech sets" and "viewer
# should agree with splice report".  Both, which means the gate is seeded from
# the report and remains editable.
#
# The FR-table rewrite had collapsed two DIFFERENT numbers that were living in
# the same table: a hardcoded 0.16 on the reburn highlight (the Splice
# Report's REBURN_THRESHOLD) and the panel's 0.100 browse filter (which is
# actually UNI_BEND_THRESHOLD — its own tooltip said so).  Collapsing them
# onto the browse filter made the Viewer contradict the report it was opened
# from, which is worse than contradicting FastReporter because nobody expects
# it.  Verified live: from the Splice Report the gate reads 0.160, from Uni
# 0.100, and an override is labelled as one.

def test_the_gate_comes_from_the_engine_not_from_javascript():
    """Retyping 0.16 in the Viewer is how it drifts from the engine.  The
    thresholds are read out of the engine's SOURCE — the two modules cannot be
    imported together across their divergent sor_reader copies."""
    assert TS.engine_thresholds()['reburn'] == 0.160
    assert TS.engine_thresholds()['uni_bend'] == 0.100
    src = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    for name in ('REBURN_THRESHOLD', 'UNI_BEND_THRESHOLD'):
        assert re.search(r'^%s\s*=' % name, src, re.M), name


def test_the_viewer_rounds_before_comparing_like_the_engine_does():
    """_clears_threshold gates on the value the report PRINTS: 0.1595 shows as
    '.160' and must flag against 0.160.  A raw comparison would disagree with
    the report on exactly those cells — the engine's comment names WSC<->SUI
    637@Splice 7 and 1067@Splice 2."""
    vw = _viewer_src()
    fn = vw[vw.index('function clearsGate'):][:400]
    assert 'Math.round(Math.abs(loss) * 1000) / 1000' in fn
    eng = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    assert 'def _clears_threshold' in eng, 'engine rule moved — recheck parity'


def test_uni_and_bidi_get_different_gates():
    """One number everywhere cannot be right: the Splice Report flags bidi
    reburns at 0.160 and the uni report at its own 0.100 tech rule."""
    vw = _viewer_src()
    fn = vw[vw.index('function activeGateDb'):][:300]
    assert "gSourceReport === 'uni'" in fn and 'uni_bend' in fn and 'reburn' in fn


def test_the_pop_out_path_carries_the_source_report():
    """The in-tab href has carried &src= all along; the pop-out postMessage
    was the one missing it, so a popped Viewer could not know which report
    it belonged to."""
    app = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    assert 'src: SRC' in app, 'postMessage does not name the report'
    assert "src=_p" in app and "src='uni'" in app, 'grids do not pass their key'


def test_the_viewer_says_which_gate_it_is_following():
    vw = _viewer_src()
    assert 'following ${gateLabel()}' in vw
    assert "classList.toggle('overridden'" in vw, 'an override is not marked'


def test_one_loss_rule_for_the_whole_viewer():
    """flagEvent drives 'flagged only'; the table drives the verdict column.
    Two thresholds meant those two could disagree with each other."""
    vw = _viewer_src()
    fn = vw[vw.index('function flagEvent'):][:700]
    assert 'clearsGate(e.splice_loss)' in fn
    assert 'gViewerSettings.lossDb' not in fn, 'the browse filter is back'
