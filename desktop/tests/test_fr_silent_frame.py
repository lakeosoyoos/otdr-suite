"""The silent-side transplant must survive Pass-0's launch normalization.

`_fr_exact_silent_loss` reproduces FastReporter's own number for the direction
that never detected an event, by transplanting the DETECTING direction's cursor
geometry out of the proprietary event list.  The proprietary list is always in
the file's RAW frame (sample 0 = the OTDR port).  The engine event handed to it
is not: Pass-0 re-references an untrimmed direction to its launch connector and
records the shift in `_trace_offset_km`.

Matching the twin without adding that shift back looks for it a launch-reel
length upstream, nothing lands inside the 60 m window, and the transplant
abstains on EVERY event of every untrimmed loud direction.  It abstains
silently, because `_grey_loss` has a fallback — so the failure shows up not as
an error but as a fiber quietly leaving the report.

KAN↔LAN 8.20 is the span that surfaced it, and it pins both polarities against
the field tech's FastReporter numbers:

  F150 @12.42 km  A stored 0.358, B silent   → transplant −0.0264 → .166
  F439 @108.87 km A silent, B stored 0.273   → transplant +0.0529 → .163

A third of the cable (325 of 864 fibers) is A-only at the 12.42 closure, so
this is the rule for that column, not one fiber's oddity.

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
FRAME_DIR = REPO_ROOT / "desktop" / "tests" / "fixtures" / "frsilentframe"
SILENT_DIR = REPO_ROOT / "desktop" / "tests" / "fixtures" / "frsilent"
NORECV_DIR = REPO_ROOT / "desktop" / "tests" / "fixtures" / "frnoreceivereel"
TRUNC_DIR = REPO_ROOT / "desktop" / "tests" / "fixtures" / "frtruncated"


def _run(body):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import sor_reader324802a as sr\n"
              "import splicereportmatchexfo as E\n"
              f"FRAME = {str(FRAME_DIR)!r}\n"
              f"SILENT = {str(SILENT_DIR)!r}\n"
              f"NORECV = {str(NORECV_DIR)!r}\n"
              f"TRUNC = {str(TRUNC_DIR)!r}\n"
              + textwrap.dedent("""
        # The KAN<->LAN 8.20 acquisition's own Pass-0 constants, as
        # `reciprocal_reels` + `_direction_end_median_km` compute them over
        # all 864 fibers of each direction.  Two fixture files cannot vote,
        # so the population's verdict is written down here instead.
        #
        # This used to declare `reel_absent=True` for B and no receive reel
        # for A, which is a frame the runner never produces: BOTH KAN<->LAN
        # directions are on reels at BOTH ends.  Under that mock F150's B
        # record came back with `_trace_offset_km = 0.0` against the
        # runner's 1.0758 km, so its cable read a launch reel too long and
        # the tests below were reasoning about geometry no report has.
        REEL_A, RECV_A, ENDMED_A = 1.0095, 1.0452, 118.2935
        REEL_B, RECV_B, ENDMED_B = 1.0146, 1.0809, 118.3241
        REEL_TOL = 0.1276190479839259

        def pass0(fiber):
            '''Load a KAN<->LAN pair exactly as the runner's Pass-0 leaves
            it.  Verified against a full 864-fiber load of the same
            acquisition: F150 offsets 1.0146/1.0758 and cable ends
            116.2286/116.1623, F439 offsets 1.0095/1.0146 — see
            `test_pass0_really_moves_the_loud_frame`.'''
            ra = sr.parse_sor_full(FRAME + '/LANKAN%d_1550.sor' % fiber)
            rb = sr.parse_sor_full(FRAME + '/KANLAN%d_1550.sor' % fiber)
            for r, (reel, recv, endmed) in ((ra, (REEL_A, RECV_A, ENDMED_A)),
                                            (rb, (REEL_B, RECV_B, ENDMED_B))):
                r['_source'] = 'sor'
                r['_raw_events'] = list(r['events'])
                r['_launch_reel_km'] = reel
                r['_receive_reel_km'] = recv
                r['_launch_reel_absent'] = False
                r['_launch_reel_tol_km'] = REEL_TOL
                r['_trace_offset_km'] = E._untrimmed_launch_offset_km(
                    r['_raw_events'], reel, False, REEL_TOL)
                r['events'] = E._normalize_untrimmed_events(
                    r['_raw_events'], reel, recv, False, REEL_TOL, endmed)
            return ra, rb

        def b_span(rb):
            return [e for e in rb['events'] if e['is_end']][0]['dist_km']

        def at(events, km, tol=0.6, mirror=None):
            hits = [e for e in events if not e['is_end']
                    and abs(((mirror - e['dist_km']) if mirror else e['dist_km']) - km) < tol]
            return hits[0] if len(hits) == 1 else None
                                """))
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    out = p.stdout.strip().splitlines()
    assert out and out[-1] == "OK", p.stdout


def test_frame_fixtures_present():
    for name in ("LANKAN150_1550.sor", "KANLAN150_1550.sor",
                 "LANKAN439_1550.sor", "KANLAN439_1550.sor"):
        assert (FRAME_DIR / name).exists(), name


def test_pass0_really_moves_the_loud_frame():
    """The premise: KAN↔LAN is untrimmed at BOTH ends in BOTH directions, so
    Pass-0 shifts each direction's events a launch-reel length while the
    proprietary list stays raw.  If this ever stops being true the tests below
    stop testing anything.

    The numbers are not this mock's opinion — they are what a full 864-fiber
    load of the same acquisition leaves on these two fibers, so a mock that
    drifts from the runner fails here rather than quietly re-deciding the
    geometry the projection constant is built from."""
    _run("""
        ra, rb = pass0(150)
        assert abs(ra['_trace_offset_km'] - 1.0146) < 1e-4, ra['_trace_offset_km']
        assert abs(rb['_trace_offset_km'] - 1.0758) < 1e-4, rb['_trace_offset_km']
        end = lambda r: [e for e in r['events'] if e['is_end']][-1]['dist_km']
        assert abs(end(ra) - 116.2286) < 1e-3, end(ra)
        assert abs(end(rb) - 116.1623) < 1e-3, end(rb)
        # both trucks measured ONE cable: the two lengths agree to ~66 m,
        # three orders below the reel that separates a good projection
        # constant from a bad one.  This is the quantity the reciprocity
        # gate reads.
        assert abs(E._cable_far_end_raw_m(ra) - ra['_trace_offset_km'] * 1000.0
                   - (E._cable_far_end_raw_m(rb) - rb['_trace_offset_km'] * 1000.0)) < 150.0
        ra4, rb4 = pass0(439)
        assert abs(ra4['_trace_offset_km'] - 1.0095) < 1e-4, ra4['_trace_offset_km']
        assert abs(rb4['_trace_offset_km'] - 1.0146) < 1e-4, rb4['_trace_offset_km']
        ea = at(ra['events'], 12.4968)
        assert ea is not None and abs(ea['dist_km'] - 12.4815) < 1e-4, ea
        prop = [e['Position'] for e in ra['exfo_events']
                if isinstance(e.get('Position'), float)]
        # the twin sits ~1 km further out in the raw list than the engine event
        assert min(abs(p - 12481.5) for p in prop) > 900.0
        assert min(abs(p - 13495.8) for p in prop) < 1.0
        print('OK')
    """)


def test_silent_b_reproduces_fastreporter_through_a_shifted_loud_frame():
    """F150 @12.42 km: A stored 0.358, B never detected it.  FastReporter (and
    the tech's sheet built from it) prints .166; the transplant returns
    −0.0264 dB for the B leg, which averages to exactly that.

    Before the raw-frame twin lookup this returned None — the transplant could
    not find A's proprietary record — and the fiber left the report."""
    _run("""
        ra, rb = pass0(150)
        ea = at(ra['events'], 12.4968)
        assert at(rb['events'], 12.4968, mirror=b_span(rb)) is None, 'B must be silent'
        v = E._fr_exact_silent_loss(rb, ra, ea)
        assert v is not None, 'transplant abstained — loud frame not mapped to raw'
        assert abs(v - (-0.026382083590341665)) < 5e-5, v
        bidir = round((ea['splice_loss'] + v) / 2.0, 4)
        assert E._format_loss(bidir) == '.166', (bidir, E._format_loss(bidir))
        print('OK')
    """)


def test_silent_a_reproduces_fastreporter_reverse_polarity():
    """F439 @108.87 km, the mirror case in the same span: B stored 0.273, A
    never detected it, tech's sheet .163.  The loud side here is B, whose frame
    Pass-0 does NOT shift — so this pins that adding the offset back is a no-op
    when there is nothing to add back."""
    _run("""
        ra, rb = pass0(439)
        eb = at(rb['events'], 108.8866, mirror=b_span(rb))
        assert at(ra['events'], 108.8866) is None, 'A must be silent'
        v = E._fr_exact_silent_loss(ra, rb, eb)
        assert v is not None
        assert abs(v - 0.05287897183275447) < 5e-5, v
        bidir = round((eb['splice_loss'] + v) / 2.0, 4)
        assert E._format_loss(bidir) == '.163', (bidir, E._format_loss(bidir))
        print('OK')
    """)


def test_transplant_is_invariant_to_the_loud_frame():
    """The general contract, on the SEANOR .bdr calibration pair: re-referencing
    the loud direction's events must not change the answer, in either polarity.
    Shifting the events by X and recording X in `_trace_offset_km` is exactly
    what Pass-0 does, and the transplant must be blind to it.

    Pass-0 re-references EVERY event of the direction, the end marker
    included — so the mock has to move the whole list, not just the twin.
    Moving one event and leaving the end marker behind is a frame the runner
    cannot produce, and it is not what this contract is about: the projection
    constant reads the loud record's far-end connector, so a half-shifted
    record would have its cable a reel too long by construction."""
    _run("""
        ra = sr.parse_sor_full(SILENT + '/SEANOR109_1550.sor', trim=False)
        rb = sr.parse_sor_full(SILENT + '/NORSEA109_1550.sor', trim=False)
        t_a = [e['Position'] for e in ra['exfo_events']
               if isinstance(e.get('Status'), int) and e['Status'] & 0x80]
        t_b = [e['Position'] for e in rb['exfo_events']
               if isinstance(e.get('Status'), int) and e['Status'] & 0x80]
        L = min(t_a[0], t_b[0])
        checked = 0
        for silent, loud in ((ra, rb), (rb, ra)):
            for pos_m in (54798.8, 61503.3):
                twin_pos = L - pos_m
                cands = [e for e in loud['events'] if not e['is_end']]
                tw = min(cands, key=lambda e: abs(e['dist_km'] * 1000 - twin_pos))
                if abs(tw['dist_km'] * 1000 - twin_pos) > 60:
                    continue
                base = E._fr_exact_silent_loss(silent, loud, tw)
                if base is None:
                    continue
                for shift in (0.9, 1.0146, 2.05):
                    moved = dict(loud)
                    moved['_trace_offset_km'] = shift
                    moved['events'] = [dict(e, dist_km=e['dist_km'] - shift)
                                       for e in loud['events']]
                    tw2 = dict(tw)
                    tw2['dist_km'] = tw['dist_km'] - shift
                    v = E._fr_exact_silent_loss(silent, moved, tw2)
                    assert v is not None, (pos_m, shift)
                    assert abs(v - base) < 1e-12, (pos_m, shift, v, base)
                checked += 1
        assert checked >= 2, checked
        print('OK')
    """)


def test_twin_lookup_uses_the_loud_records_offset():
    """Guard the direction of the correction in the source.  The twin lookup
    maps the LOUD event back to raw (its events are what Pass-0 shifted); the
    silent record's offset appears only in the glass guard below it, where it
    bounds the projected window.  Swapping them would reintroduce the bug in
    the other direction."""
    _run("""
        import inspect
        src = inspect.getsource(E._fr_exact_silent_loss)
        head = src[:src.index('twin = None')]
        assert "rec_loud.get('_trace_offset_km')" in head, head[-400:]
        assert "rec_silent.get('_trace_offset_km')" not in head, head[-400:]
        print('OK')
    """)


def test_projection_into_either_end_zone_is_refused():
    """A projection landing within ENDZONE_REACH_KM of either of the SILENT
    fiber's cable ends must return None, so the caller falls back to the
    end-zone reconstruction that owns those stretches.  Fitting them anyway is
    how WSC↔SUI Splice 12 grew 48 phantom cells ranging -0.68 to +2.87 dB: it
    sits 66-80 m past the Suisun launch AND, in the other polarity, ~100 m
    before the Sacramento far-end connector."""
    _run("""
        ra, rb = pass0(150)
        ea = at(ra['events'], 12.4968)
        assert E._fr_exact_silent_loss(rb, ra, ea) is not None
        # Near end: walk the silent fiber's launch connector up under the
        # projection.  Pass-0 re-references a direction's EVENTS together
        # with the offset it records — a record whose launch moved but whose
        # events did not is a frame the runner cannot produce, and it is also
        # a fiber whose two directions no longer agree about the cable, which
        # is precisely what `_fr_proj_constant`'s reciprocity gate is looking
        # for.  So move both, the way Pass-0 does.
        for off, want_none in ((104.5, True), (104.7, True), (100.0, False)):
            shift = off - rb['_trace_offset_km']
            rb2 = dict(rb)
            rb2['_trace_offset_km'] = off
            rb2['events'] = [dict(e, dist_km=e['dist_km'] - shift)
                             for e in rb['events']]
            got = E._fr_exact_silent_loss(rb2, ra, ea)
            assert (got is None) == want_none, ('launch', off, got)
        # Far end: walk the silent fiber's end marker down onto it.  The
        # guard's clearance is `hi_m - cur_b` with
        #     hi_m = (end.dist_km + rb['_trace_offset_km']) * 1000
        # so these are the same three physical positions the pre-gate
        # version of this test used, carried into a frame where B's launch
        # is at its real 1.0758 km instead of a mocked zero.  The flip lands
        # on ENDZONE_REACH_KM to 10 m, which is what 104.53/104.54 pin.
        for end_km, want_none in ((104.03, True), (104.53, True),
                                  (104.54, False), (109.0, False)):
            rb3 = dict(rb)
            rb3['events'] = [dict(e, dist_km=(end_km if e['is_end'] else e['dist_km']))
                             for e in rb['events']]
            got = E._fr_exact_silent_loss(rb3, ra, ea)
            assert (got is None) == want_none, ('end', end_km, got)
        # the boundary is the shared end-zone constant, not a private number
        import inspect
        assert 'ENDZONE_REACH_KM' in inspect.getsource(E._fr_exact_silent_loss)
        print('OK')
    """)


# ══════════════════════════════════════════════════════════════════════════
#  A launch reel with NO receive reel — the geometry the terminal cannot mirror
# ══════════════════════════════════════════════════════════════════════════
# `_fr_proj_constant` projects through a proprietary END-OF-FIBRE position,
# and that is what makes the transplant machine-exact against FastReporter's
# .bdr output.  But a terminal only equals the projection constant
#
#     L = launch_silent + G + launch_loud
#
# while the shot ran PAST the cable's far-end connector into a receive spool
# the length of the opposite launch reel.  On the standard both-ends-on-reels
# shoot that holds by construction, and on a trimmed span every term is zero
# — which is every span this was calibrated on, so the failure never showed.
#
# PLACHE<->CHYPTV 7-8-2026 is the production geometry that breaks it: a ~1.01
# km launch reel at each end and NO receive reel either way, so both traces
# stop at the far-end connector and both terminals come up a whole reel short.
# Measured over all 4,982 of that acquisition's transplant calls the terminal
# sat a median 1,012 m upstream of the physical constant — every single call.
# Three cells the delivered report carries (249 .181, 511 .162, 991 .164) left
# our report because the transplant was fitting glass a kilometre away.


def _norecv_pass0():
    """The two-file Pass-0 for the July-8 pair, as the runner leaves it."""
    return """
        ra = sr.parse_sor_full(NORECV + '/PLACHE0249_1550.sor')
        rb = sr.parse_sor_full(NORECV + '/CHYPTV0249.sor')
        for r in (ra, rb):
            r['_source'] = 'sor'
            r['_raw_events'] = list(r['events'])
            r['_trace_offset_km'] = E._untrimmed_launch_offset_km(
                r['_raw_events'], None, False)
            r['events'] = E._normalize_untrimmed_events(
                r['_raw_events'], None, None, False)
        def terminal(r):
            for e in r['exfo_events']:
                st = e.get('Status')
                if isinstance(st, int) and st & 0x80:
                    return e['Position']
        def raw_end(r):
            return [e for e in r['_raw_events'] if e.get('is_end')][-1]['dist_km']
        def norm_end(r):
            return [e for e in r['events'] if e.get('is_end')][-1]['dist_km']
    """


def test_july8_pair_has_a_launch_reel_and_no_receive_reel():
    """The premise.  If this stops being true the tests below stop testing
    anything: both directions must be untrimmed (a real launch reel) AND
    Pass-0 must find no receive spool to pull the end marker back off."""
    _run(_norecv_pass0() + """
        for r in (ra, rb):
            assert abs(r['_trace_offset_km'] - 1.0095) < 1e-4, r['_trace_offset_km']
            # no receive reel => Pass-0 pulls the end marker back by exactly 0
            assert abs(raw_end(r) - (norm_end(r) + r['_trace_offset_km'])) < 1e-4, r
        print('OK')
    """)


def test_terminal_is_disqualified_when_the_shot_reached_no_receive_reel():
    """The two terminals agree with each other — so the ambiguity gate passes
    and the old rule happily returned one — yet both are a full launch reel
    short of the constant.  Agreement between the terminals is not evidence
    that either one is the mirror anchor."""
    _run(_norecv_pass0() + """
        t_a, t_b = terminal(ra), terminal(rb)
        assert abs(t_a - t_b) < E.FR_PROJ_AGREE_M, (t_a, t_b)   # gate passes
        l_term = min(t_a, t_b)
        l_phys = (rb['_trace_offset_km'] * 1000.0
                  + E._cable_far_end_raw_m(ra))
        short = l_phys - l_term
        assert 950.0 < short < 1100.0, short          # one launch reel
        got = E._fr_proj_constant(rb, ra)
        assert abs(got - l_phys) < 1e-6, (got, l_phys, l_term)
        print('OK')
    """)


def test_silent_side_lands_on_the_splice_not_a_reel_upstream():
    """F249 @1.92 km, one of the three the delivered report carries.  A stores
    0.301 dB and CHYPTV never detected it.

    The claim in this test's name is POSITIONAL, so it is asserted
    positionally.  The transplant fits the silent side at
    `CurA = L_proj − twin.Position`, in the silent file's raw frame, and the
    regression this test exists to catch moved that cursor a whole launch reel
    upstream — onto glass a kilometre from the splice it was asked about.  So
    the cursor the engine ACTUALLY measured at is captured, by intercepting
    `measure_fr_exact_loss` rather than re-deriving the projection here (a
    re-derivation can agree with itself while the engine fits somewhere else
    entirely), and compared against where the splice has to be:

        the splice is 1.9069 km in from the LOUD launch connector, so it sits
        `raw_end(silent) − 1906.9 m` from the SILENT OTDR port

    — a reference that is the silent file's own Bellcore end marker and
    nothing else: no reel poll, no projection constant, and not the LOUD end
    marker, which is the term `_fr_proj_constant` actually returns.  Its one
    premise — that Pass-0 leaves this pair's end markers alone, there being no
    receive reel to strip — is pinned separately by
    `test_july8_pair_has_a_launch_reel_and_no_receive_reel`.

    Shipped, CurA lands 5 m from that reference.  Through `min(terminal)` it
    lands 1,012 m upstream: seventeen times outside the 60 m window this
    function calls "the same event", and the same reel `_fr_proj_constant`
    measures over all 4,982 of this acquisition's transplant calls.

    The printed value is deliberately NOT pinned to a digit, because the third
    decimal here is a coin flip and not a measurement.  The pair averages
    0.1825 off the Bellcore int16 event table and 0.1826 off the same event's
    float64 proprietary record — an exact `.xxx5` tie, so which digit prints
    is IEEE-754's business, not the fiber's.  An independent raw-trace
    bidirectional fit puts the true loss at 0.1945, so neither .182 nor .183
    is the physical answer, and pinning either one pins the artifact.  What IS
    load-bearing is the decision the value carries, and that is asserted from
    both sides: through the constant the pair is 0.1826 and the cell reports;
    through the terminal it is 0.1423, under the 0.160 line, and the fiber
    leaves the report — which is exactly how 249/511/991 went missing."""
    _run(_norecv_pass0() + """
        ea = [e for e in ra['events'] if not e['is_end']
              and abs(e['dist_km'] - 1.9069) < 0.05][0]
        splice_m = ea['dist_km'] * 1000.0

        # run the transplant, and report the CurA it measured at
        def fit(silent, loud, evt):
            seen = []
            real = E.measure_fr_exact_loss
            def spy(rec, cur_a, cur_b, sub_a, sub_b):
                seen.append(cur_a)
                return real(rec, cur_a, cur_b, sub_a, sub_b)
            E.measure_fr_exact_loss = spy
            try:
                got = E._fr_exact_silent_loss(silent, loud, evt)
            finally:
                E.measure_fr_exact_loss = real
            assert len(seen) == 1, seen
            return got, seen[0]

        # where the splice IS, in silent-raw metres, out of the silent file
        # alone.  `raw_end` is the Bellcore end marker Pass-0 never touched.
        want = raw_end(rb) * 1000.0 - splice_m

        # ── the claim in the name ────────────────────────────────────────
        v, cur_a = fit(rb, ra, ea)
        assert v is not None, 'transplant abstained'
        # 60 m is this function's OWN "same event" window — the tolerance its
        # twin lookup matches the loud record with.
        assert abs(cur_a - want) < 60.0, (cur_a, want, cur_a - want)

        # ── and the regression, in the same metres ───────────────────────
        # min(terminal) is what `_fr_proj_constant` used to return.
        def terminal(r):
            for e in r['exfo_events']:
                st = e.get('Status')
                if isinstance(st, int) and st & 0x80:
                    return float(e['Position'])
        keep = E._fr_proj_constant
        try:
            E._fr_proj_constant = lambda s, l: min(terminal(ra), terminal(rb))
            bad, cur_a_bad = fit(rb, ra, ea)
        finally:
            E._fr_proj_constant = keep
        assert 950.0 < (want - cur_a_bad) < 1100.0, (want, cur_a_bad)
        assert abs(cur_a_bad - want) > 900.0, cur_a_bad     # nowhere near it

        # ── the value: FR's own transplant, and the decision it carries ──
        assert abs(v - 0.06394525564254394) < 5e-5, v
        bidir = round((ea['splice_loss'] + v) / 2.0, 4)
        assert 0.180 <= bidir <= 0.185, bidir        # a band, not a digit
        assert bidir >= 0.160, bidir                 # so the cell REPORTS
        assert bad is not None
        assert round((ea['splice_loss'] + bad) / 2.0, 4) < 0.160, bad
        print('OK')
    """)


# ══════════════════════════════════════════════════════════════════════════
#  A RECEIVE REEL THAT NEVER STRIPPED — the other way the frame goes wrong
# ══════════════════════════════════════════════════════════════════════════
# `_fr_proj_constant` disqualifies the terminal when it disagrees with the
# physical constant `launch_silent + _cable_far_end_raw_m(loud)`.  That test
# is only as good as Pass-0's end marker on the LOUD file, and Pass-0 does
# NOT unconditionally strip a receive spool off it — the short-fiber guard
# skips the pull-back by design, and a direction whose reel poll came up
# short skips it too.  On KAN<->LAN's tail (fibers 725-864) A's 1.0452 km
# receive reel never comes off, so A's "cable" reads a reel long, the
# physical constant inherits the error, and the CORRECT answer is the one
# that gets thrown out.
#
# F812 is that fiber, and it is the one that crossed the reporting line: a
# `812 .177` at Splice 4 on BOTH KAN<->LAN workbooks — the pre-reshoot one
# and the FINAL the field tech signed off 6/6 — measured 1,043 m from the
# splice it claims to be about.  Nothing in the suite exercised this shape,
# so it printed green.
#
# The reference below uses NO reel poll, NO end marker and NO Pass-0
# decision.  Both files' proprietary lists are in their own raw frames, and
# any point both trucks detected satisfies
#     p_silent + p_loud = launch_silent + G + launch_loud = L
# exactly.  Twelve co-detected pairs on this fiber agree on one value, and
# that value IS the projection constant.


def test_a_reel_that_never_stripped_does_not_overrule_the_terminal():
    """F812's premise, then the rule.  A's cable end reads a whole receive
    reel longer than B's — one cable, two answers a kilometre apart — so the
    physical constant is the unreliable one here and the terminal is kept."""
    _run("""
        import statistics
        ra, rb = pass0(812)

        def terminal(r):
            for e in r['exfo_events']:
                st = e.get('Status')
                if isinstance(st, int) and st & 0x80:
                    return float(e['Position'])

        def props(r):
            return [float(e['Position']) for e in r['exfo_events']
                    if e.get('Position')
                    and not (isinstance(e.get('Status'), int) and e['Status'] & 0x80)]

        launch_s = rb['_trace_offset_km'] * 1000.0
        launch_l = ra['_trace_offset_km'] * 1000.0
        cab_l = E._cable_far_end_raw_m(ra) - launch_l
        cab_s = E._cable_far_end_raw_m(rb) - launch_s

        # PREMISE — Pass-0 left A's receive reel on.  If this ever stops
        # being true this test stops testing anything.
        assert 950.0 < abs(cab_s - cab_l) < 1200.0, (cab_s, cab_l)

        l_term = min(terminal(ra), terminal(rb))
        l_phys = launch_s + E._cable_far_end_raw_m(ra)
        assert abs(l_term - l_phys) > E.FR_PROJ_AGREE_M, (l_term, l_phys)

        # REFERENCE — densest 40 m cluster of the pair sums.
        sums = sorted(x + y for x in props(ra) for y in props(rb))
        best = (0, None)
        for i, v in enumerate(sums):
            j = i
            while j < len(sums) and sums[j] - v <= 40.0:
                j += 1
            if j - i > best[0]:
                best = (j - i, statistics.median(sums[i:j]))
        support, l_ref = best
        assert support >= 8, support
        assert abs(l_term - l_ref) < 1.0, (l_term, l_ref)
        assert abs(l_phys - l_ref) > 900.0, (l_phys, l_ref)

        # THE RULE
        got = E._fr_proj_constant(rb, ra)
        assert got is not None and abs(got - l_term) < 1e-6, (got, l_term, l_phys)
        print('OK')
    """)


def test_the_812_phantom_does_not_reach_the_report():
    """The report-level consequence, at the cell the phantom appeared in.
    A stores 0.149 dB at Splice 4 (13.7358 km) and B never detected it.

    Projected through the terminal the silent window lands on the splice and
    the pair averages to .067 — under the 0.160 line, no cell, which is what
    both delivered KAN<->LAN workbooks show.  Projected through the
    unstripped reel it lands 1,043 m away, on B's own neighbouring closure,
    and returns +0.205 — a `812 .177` on a span with no fault there."""
    _run("""
        ra, rb = pass0(812)
        ea = [e for e in ra['events'] if not e['is_end']
              and abs(e['dist_km'] - 13.7358) < 0.05][0]
        assert abs(ea['splice_loss'] - 0.149) < 5e-4, ea['splice_loss']
        assert not [e for e in rb['events'] if not e['is_end'] and abs(
            ([x for x in rb['events'] if x['is_end']][-1]['dist_km']
             - e['dist_km']) - 13.7358) < 0.3], 'B must be silent here'

        v = E._fr_exact_silent_loss(rb, ra, ea)
        assert v is not None, 'transplant abstained'
        assert abs(v - (-0.014534851422993711)) < 5e-5, v
        bidir = round((ea['splice_loss'] + v) / 2.0, 4)
        assert E._format_loss(bidir) == '.067', (bidir, E._format_loss(bidir))
        assert bidir < 0.160, bidir

        # and what the un-gated constant would have printed instead
        keep = E._fr_proj_constant
        try:
            l_phys = (rb['_trace_offset_km'] * 1000.0
                      + E._cable_far_end_raw_m(ra))
            E._fr_proj_constant = lambda s, l: l_phys
            bad = E._fr_exact_silent_loss(rb, ra, ea)
        finally:
            E._fr_proj_constant = keep
        assert bad is not None
        assert E._format_loss(round((ea['splice_loss'] + bad) / 2.0, 4)) == '.177'
        print('OK')
    """)


# ══════════════════════════════════════════════════════════════════════════
#  NOTHING TO CHECK THE TERMINAL AGAINST — the truncated shot
# ══════════════════════════════════════════════════════════════════════════
# `_fr_proj_constant` validates FastReporter's exact-but-conditional terminal
# against the physical constant `launch_silent + _cable_far_end_raw_m(loud)`.
# That check is only available while the loud file HAS a cable end.
#
# A truncated shot does not.  Its acquisition window stops thousands of metres
# short of the far end, the Bellcore table carries no end-of-fibre event at
# all — and the firmware STILL writes a proprietary terminal, at the edge of
# the window.  Returned unvalidated, that terminal is a mirror anchor a few km
# into a cable tens of km long, and every projected cursor is a reflection
# about the wrong point.
#
#   WSC<->SUI, August round, 5 km "Short" set (the fixture below): both
#   directions terminate at 4,993 m and AGREE TO 0.3 m, so the
#   terminal-vs-terminal test reads near-perfect agreement.  The same cable,
#   same round, shot full length ("Long") measures L = 66,086.9 m against the
#   pair-sum reference.  MILELMsh is the same shape on 120 of 120 calls with
#   a bigger gap still: terminal 4,993.4 m against L = 69,553.9 m measured on
#   10 agreeing co-detected closures of the full-length MIL<->ELM shoot.
#
# Four acquisitions on disk are this shape on every call (MILELMsh, TULORO,
# DURANCfec, WSC<->SUI August Short).  None of them discovers a splice today,
# so nothing printed wrong — the transplant simply was not asked.  Over the
# 73,510 live projection calls of a 31-span sweep only 4 reach this branch at
# all, and all 4 are refused earlier by the span-shape test, so closing this
# changes no cell on anything we hold.  It is closed anyway, because "no
# caller happens to ask" is not a guarantee.


def _trunc_pass0():
    """The real WSC<->SUI August 5 km pair, Pass-0'd as the runner leaves it.

    No reel poll is available for a two-file load, so the launch offset is
    taken from the trace itself — exactly what `_untrimmed_launch_offset_km`
    does for a direction whose poll came up empty."""
    return """
        ra = sr.parse_sor_full(TRUNC + '/WSCSUIsh0001.sor')
        rb = sr.parse_sor_full(TRUNC + '/SUIWSCsh0001.sor')
        for r in (ra, rb):
            r['_source'] = 'sor'
            r['_raw_events'] = list(r['events'])
            r['_trace_offset_km'] = E._untrimmed_launch_offset_km(
                r['_raw_events'], None, False)
            r['events'] = E._normalize_untrimmed_events(
                r['_raw_events'], None, None, False)
        def terminal(r):
            for e in r['exfo_events']:
                st = e.get('Status')
                if isinstance(st, int) and st & 0x80:
                    return float(e['Position'])
    """


def test_a_truncated_shot_carries_a_terminal_but_no_cable_end():
    """The premise.  If this stops being true the rule below stops testing
    anything: BOTH directions must have a proprietary terminal (so the
    function gets that far), NO Bellcore end-of-fibre event (so there is
    nothing to validate it with), and the two terminals must AGREE — so the
    span-shape test is not what refuses this pair."""
    _run(_trunc_pass0() + """
        for r in (ra, rb):
            assert terminal(r) is not None, 'no proprietary terminal'
            assert not any(e.get('is_end') for e in r['events']), 'has an end marker'
            assert E._cable_far_end_raw_m(r) is None, E._cable_far_end_raw_m(r)
        t_s, t_l = terminal(rb), terminal(ra)
        assert abs(t_s - t_l) < 1.0, (t_s, t_l)          # agree to 0.3 m
        assert abs(t_s - t_l) <= E.FR_PROJ_AGREE_M       # so the shape test passes
        assert 4900.0 < min(t_s, t_l) < 5100.0, (t_s, t_l)
        print('OK')
    """)


def test_a_terminal_with_nothing_to_check_it_against_is_refused():
    """The rule, in both polarities.  There is no cable end on either file,
    so the physical constant cannot be built, so the terminal cannot be
    validated — and an unvalidated terminal is not returned.

    Nothing in either file distinguishes "the fibre ends at 4,993 m" from
    "the acquisition stopped at 4,993 m"; that is precisely why guessing is
    not allowed here."""
    _run(_trunc_pass0() + """
        assert E._fr_proj_constant(rb, ra) is None, E._fr_proj_constant(rb, ra)
        assert E._fr_proj_constant(ra, rb) is None, E._fr_proj_constant(ra, rb)
        print('OK')
    """)


def test_a_refused_projection_leaves_the_transplant_to_the_fallback():
    """The consequence at the caller.  `_fr_exact_silent_loss` must abstain
    when the constant is refused, so `_grey_loss` keeps the legacy
    reconstruction — coverage stops, it does not go wrong."""
    _run(_norecv_pass0() + """
        # the July-8 pair with its spools moved to the far ends AND its end
        # markers gone: a receive-only truncated shot (see the section below)
        for r in (ra, rb):
            r['_trace_offset_km'] = 0.0
            r['events'] = [e for e in r['events'] if not e.get('is_end')]
        ea = [e for e in ra['events'] if not e.get('is_end')
              and abs(e['dist_km'] - 1.9069) < 0.05][0]
        assert E._fr_proj_constant(rb, ra) is None
        assert E._fr_exact_silent_loss(rb, ra, ea) is None, 'transplant projected anyway'
        print('OK')
    """)


# ══════════════════════════════════════════════════════════════════════════
#  THE MIRROR GEOMETRY — a receive reel and NO launch reel
# ══════════════════════════════════════════════════════════════════════════
# July-8 is a launch reel with no receive reel, and its terminal comes up a
# reel SHORT.  The mirror — spools spliced in at the far ends instead of the
# near ones — makes it come up a reel LONG, same magnitude, opposite sign.
# No such acquisition exists in the archive, so it is built here out of a
# real one.
#
# The construction is one field, and that is not a shortcut: a trace covers
# reel + cable whichever end the spool is on, so the proprietary terminal,
# the raw event positions and the cursors are all numerically the SAME file.
# The only thing that moves is where the launch connector sits, and Pass-0
# records exactly that in `_trace_offset_km`.  Setting it to 0 on the real
# July-8 pair therefore IS the mirror acquisition, in real numbers:
#
#     launch 1,009.5 m -> 0        cable 107,484.4 m (unchanged, real)
#     receive 0 -> 1,009.5 m       terminals 108,491.2 / 108,486.1 (real)
#
#     L = launch_s + G + launch_l = 107,484.4      l_term = 108,486.1
#     error +1,001.7 m, and |t_s - t_l| = 5.1 m — the span-shape test reads
#     near-perfect agreement on a constant a full spool past the far end.


def _mirror_pass0():
    """The July-8 pair with its two spools moved to the far ends."""
    return _norecv_pass0() + """
        for r in (ra, rb):
            r['_trace_offset_km'] = 0.0
    """


def test_the_mirror_geometry_is_a_reel_long_and_the_shape_test_cannot_see_it():
    """The premise: same spool length, opposite sign, and the two terminals
    still agree with each other — so nothing about the terminals themselves
    reveals the error."""
    _run(_mirror_pass0() + """
        t_s, t_l = terminal(rb), terminal(ra)
        l_true = rb['_trace_offset_km'] * 1000.0 + E._cable_far_end_raw_m(ra)
        l_term = min(t_s, t_l)
        assert abs(t_s - t_l) <= E.FR_PROJ_AGREE_M, (t_s, t_l)   # shape test passes
        assert 950.0 < (l_term - l_true) < 1100.0, (l_term, l_true)  # a reel LONG
        print('OK')
    """)


def test_the_mirror_geometry_projects_through_the_cable_not_the_reel():
    """The rule.  The physical constant is built from the cable end, which
    carries no reel term either way, so it is right in the mirror for the
    same reason it is right on July-8."""
    _run(_mirror_pass0() + """
        l_true = rb['_trace_offset_km'] * 1000.0 + E._cable_far_end_raw_m(ra)
        got = E._fr_proj_constant(rb, ra)
        assert got is not None, 'abstained on a checkable geometry'
        assert abs(got - l_true) < 1e-6, (got, l_true, min(terminal(ra), terminal(rb)))
        print('OK')
    """)


def test_the_mirror_geometry_truncated_is_refused():
    """Mirror AND truncated: the shape test reads 5.1 m of agreement, the
    terminal is 1,001.7 m past the far end, and there is no cable end to
    catch it.  This is the pair of holes crossing, and the answer is None."""
    _run(_mirror_pass0() + """
        for r in (ra, rb):
            r['events'] = [e for e in r['events'] if not e.get('is_end')]
        t_s, t_l = terminal(rb), terminal(ra)
        assert abs(t_s - t_l) <= E.FR_PROJ_AGREE_M, (t_s, t_l)
        assert E._cable_far_end_raw_m(ra) is None
        assert E._fr_proj_constant(rb, ra) is None, E._fr_proj_constant(rb, ra)
        print('OK')
    """)


# ══════════════════════════════════════════════════════════════════════════
#  THE SPAN-SHAPE TEST IS NOT A SAFETY NET
# ══════════════════════════════════════════════════════════════════════════
# `abs(t_s - t_l) > FR_PROJ_AGREE_M` reads like protection and is not.
# Substituting the definitions,
#
#     t_s - t_l = (launch_s + recv_s) - (launch_l + recv_l)
#
# while the error in `min(t_s, t_l)` is `min(recv_s - launch_l,
# recv_l - launch_s)` — so it measures the DIFFERENCE of the two directions'
# errors and is blind to whatever they share.  On the ordinary one-truck kit,
# where one pair of spools serves both ends, the two errors are equal by
# construction: the test passes the geometries that are a full reel wrong and
# abstains on the milder mismatched ones.
#
# This is asserted by DRIVING the real function, not by reading it.


def test_the_span_shape_test_passes_the_geometries_that_are_a_reel_wrong():
    """Six geometries over the real July-8 glass, through the real function.

    The two rows that are a reel wrong are exactly the two the shape test
    waves through; the two it stops are less wrong than either.  If someone
    ever rewrites this as "the terminals agreeing means the projection is
    sound", this goes red."""
    _run("""
        G = 107484.4                      # real July-8 cable, metres
        def rec(launch, recv, endmark=True):
            return {'exfo_events': [{'Position': 500.0, 'Status': 0x01},
                                    {'Position': launch + G + recv, 'Status': 0x80}],
                    'events': ([{'dist_km': G / 1000.0, 'is_end': True}]
                               if endmark else []),
                    '_trace_offset_km': launch / 1000.0}
        # label,                       lS,     lL,     rS,     rL
        cases = [
            ('both-ends-on-reels',  1009.5, 1004.4, 1004.4, 1009.5),
            ('fully trimmed',          0.0,    0.0,    0.0,    0.0),
            ('launch-only',         1009.5, 1004.4,    0.0,    0.0),
            ('receive-only',           0.0,    0.0, 1050.0, 1048.0),
            ('receive-only 1050/700',  0.0,    0.0, 1050.0,  700.0),
            ('launch-only 1010/300',1010.0,  300.0,    0.0,    0.0),
        ]
        waved_through_wrong, stopped = [], []
        for lab, lS, lL, rS, rL in cases:
            s, l = rec(lS, rS), rec(lL, rL)
            L = lS + G + lL
            t_s = s['exfo_events'][-1]['Position']
            t_l = l['exfo_events'][-1]['Position']
            passes_shape = abs(t_s - t_l) <= E.FR_PROJ_AGREE_M
            term_err = min(t_s, t_l) - L
            if passes_shape and abs(term_err) > 150.0:
                waved_through_wrong.append((lab, round(term_err, 1)))
            if not passes_shape:
                stopped.append((lab, round(term_err, 1)))
            # THE POINT: whatever the shape test thinks, the function is right
            got = E._fr_proj_constant(s, l)
            assert got is None or abs(got - L) < 1.0, (lab, got, L)

        # the shape test waves through the two maximally-wrong geometries ...
        labs = sorted(x[0] for x in waved_through_wrong)
        assert labs == ['launch-only', 'receive-only'], waved_through_wrong
        assert all(950.0 < abs(e) < 1100.0 for _, e in waved_through_wrong), \
            waved_through_wrong
        # ... and stops two that are LESS wrong than the ones it passed
        assert sorted(x[0] for x in stopped) == \
            ['launch-only 1010/300', 'receive-only 1050/700'], stopped
        print('OK')
    """)


def test_the_physical_constant_is_what_saves_those_two_geometries():
    """Same six geometries with the cable end removed, so the physical
    constant cannot be built and the span-shape test is all that is left.
    Every one of them must now be refused — including the four the function
    answers correctly when it CAN check itself.  That gap between the two
    tests is the measure of how much the shape test contributes: nothing."""
    _run("""
        G = 107484.4
        def rec(launch, recv):
            return {'exfo_events': [{'Position': 500.0, 'Status': 0x01},
                                    {'Position': launch + G + recv, 'Status': 0x80}],
                    'events': [],                      # no cable end
                    '_trace_offset_km': launch / 1000.0}
        cases = [(1009.5, 1004.4, 1004.4, 1009.5), (0.0, 0.0, 0.0, 0.0),
                 (1009.5, 1004.4, 0.0, 0.0), (0.0, 0.0, 1050.0, 1048.0),
                 (0.0, 0.0, 1050.0, 700.0), (1010.0, 300.0, 0.0, 0.0)]
        for lS, lL, rS, rL in cases:
            assert E._fr_proj_constant(rec(lS, rS), rec(lL, rL)) is None, (lS, lL, rS, rL)
        print('OK')
    """)
