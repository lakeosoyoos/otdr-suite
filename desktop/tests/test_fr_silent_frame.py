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


def _run(body):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import sor_reader324802a as sr\n"
              "import splicereportmatchexfo as E\n"
              f"FRAME = {str(FRAME_DIR)!r}\n"
              f"SILENT = {str(SILENT_DIR)!r}\n"
              + textwrap.dedent("""
        def pass0(fiber):
            '''Load a KAN<->LAN pair exactly as the runner's Pass-0 leaves it:
            A untrimmed at the near end, B untrimmed at the FAR end only.'''
            ra = sr.parse_sor_full(FRAME + '/LANKAN%d_1550.sor' % fiber)
            rb = sr.parse_sor_full(FRAME + '/KANLAN%d_1550.sor' % fiber)
            for r in (ra, rb):
                r['_source'] = 'sor'
                r['_raw_events'] = list(r['events'])
            ra['events'] = E._normalize_untrimmed_events(ra['_raw_events'],
                                                         None, None, False)
            ra['_trace_offset_km'] = E._untrimmed_launch_offset_km(
                ra['_raw_events'], None, False)
            rb['events'] = E._normalize_untrimmed_events(rb['_raw_events'],
                                                         None, 1.0809, True)
            rb['_trace_offset_km'] = E._untrimmed_launch_offset_km(
                rb['_raw_events'], None, True)
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
    """The premise: KAN↔LAN's A direction is untrimmed, so Pass-0 shifts its
    events a launch-reel length while the proprietary list stays raw.  If this
    ever stops being true the two tests below stop testing anything."""
    _run("""
        ra, rb = pass0(150)
        assert abs(ra['_trace_offset_km'] - 1.0146) < 1e-6, ra['_trace_offset_km']
        assert rb['_trace_offset_km'] == 0.0, rb['_trace_offset_km']
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
    what Pass-0 does, and the transplant must be blind to it."""
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
        # near end: walk the silent fiber's launch up under the projection
        for off, want_none in ((104.5, True), (104.7, True), (100.0, False)):
            rb2 = dict(rb); rb2['_trace_offset_km'] = off
            got = E._fr_exact_silent_loss(rb2, ra, ea)
            assert (got is None) == want_none, ('launch', off, got)
        # far end: walk the silent fiber's end marker down onto it
        for end_km, want_none in ((105.3, True), (105.5, True), (110.0, False)):
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
