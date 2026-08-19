"""A B side that dies mid-span is not a short cable (SUI↔EMR, 2026-08-19).

`_population_span_cap` clips an end marker that OVERRUNS the cable (KANLAN
F1, 650 m long) and deliberately leaves the short side alone — "a fiber can
legitimately be SHORT (breaks, short lays)".  That is true of the fiber's own
REACH but not of the frame its events are mirrored into.  A fiber whose B
trace dies mid-span still has glass to the far end; the OTDR just cannot see
past the damage.  Anchoring the mirror on its own end marker therefore drags
every B event toward the A launch by exactly the break distance.

The tech's report: "calling out bidi bends on traces with broken fibers (369,
524 and 908)".  All four BEND cells in the 1152-fiber SUI↔EMR report were this
and nothing else — an ordinary B splice at a real closure, displaced off-grid,
and off-grid + positive loss is a bend by definition:

    F369 BEND .122 @  5.644 km  is the B splice at closure 32.810 km
    F369 BEND .096 @ 16.277 km  is the B splice at closure 43.443 km
    F542 BEND .167 @ 10.812 km  is the B splice at closure 37.977 km
    F908 BEND .117 @ 16.622 km  is the B splice at closure 37.977 km

Nine of the nineteen fibers that trip the engine's `eof < span - END_REGION_KM`
test on this span are not broken at all — they carry live backscatter and the
genuine far-end Fresnel past a high-loss point the firmware wrote `0E` on.  The
mirror must not depend on telling those two cases apart, and with `_mirror_span`
it does not: both are re-anchored on the cable span.

The reverse case matters too.  F689 is B-broken and A-healthy, which no guard
covered: its real 0.198 dB B splice at 5.998 km mirrored to 66.310 km — 4.2 km
off the 70.547 closure, outside POSITION_TOL — and was silently DROPPED.  On
the cable span it lands 16 m from the closure and reports .172.

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"


def _run(body):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    out = p.stdout.strip().splitlines()
    assert out and out[-1] == "OK", p.stdout


def test_mirror_span_reanchors_short_leaves_long_rule_intact():
    """Short → cable span; long → population (KANLAN F1); jitter → untouched."""
    _run("""
        pop, cap = 76.5331, 76.6831      # one pulse smear of slack
        span_a = 76.5300

        # Short side — the new rule.  A B end 27 km early is damage, not a
        # short lay: re-anchor on the cable span and say so.
        s, short = E._mirror_span(49.380, pop, cap, span_a)
        assert short is True, short
        assert abs(s - span_a) < 1e-9, s

        # Long side — KANLAN F1 must keep behaving exactly as before.
        s, short = E._mirror_span(77.400, pop, cap, span_a)
        assert short is False, short
        assert abs(s - pop) < 1e-9, s

        # Ordinary end-detection jitter is never "corrected".
        for eof in (76.4831, 76.5331, 76.6000):
            s, short = E._mirror_span(eof, pop, cap, span_a)
            assert short is False, (eof, short)
            assert abs(s - eof) < 1e-9, (eof, s)

        # A fiber exactly at the END_REGION_KM boundary is not short yet.
        s, short = E._mirror_span(pop - E.END_REGION_KM, pop, cap, span_a)
        assert short is False, short

        # No population span (no end markers anywhere) → legacy passthrough.
        s, short = E._mirror_span(49.380, 0.0, 0.0, span_a)
        assert short is False and abs(s - 49.380) < 1e-9, (s, short)

        # Falls back to the population span when no A span is supplied.
        s, short = E._mirror_span(49.380, pop, cap, None)
        assert short is True and abs(s - pop) < 1e-9, (s, short)
        print('OK')
    """)


def test_broken_b_side_no_longer_manufactures_offgrid_bends():
    """The four SUI↔EMR BEND cells, by their real numbers.

    Each B event must mirror onto its real closure (the report's own
    CLOSURE_MATCH_KM window), not 0.87–1.42 km off-grid where the bend
    classifier is guaranteed to fire.
    """
    _run("""
        pop, cap = 76.5229, 76.6729
        span_a   = 76.5300
        # (label, own B "end" km, B event km, real closure km)
        CASES = [
            ('F369 -> 5.644',  49.380, 43.736, 32.810),
            ('F369 -> 16.277', 49.380, 33.103, 43.443),
            ('F542 -> 10.812', 49.380, 38.569, 37.977),
            ('F908 -> 16.622', 55.170, 38.548, 37.977),
            # B-broken / A-healthy: the case no guard covered.  Its cell was
            # DROPPED, not mis-flagged — 4.2 km off-grid is past POSITION_TOL.
            ('F689 -> lost',   72.309,  5.998, 70.547),
        ]
        for name, b_eof, b_evt, closure in CASES:
            span, short = E._mirror_span(b_eof, pop, cap, span_a)
            assert short is True, (name, short)
            fixed = span - b_evt
            broken = b_eof - b_evt
            assert abs(fixed - closure) <= E.CLOSURE_MATCH_KM, \\
                (name, fixed, closure, abs(fixed - closure))
            # And confirm the old anchor really did put it off-grid, so this
            # test fails loudly if the mirror is ever reverted.
            assert abs(broken - closure) > 0.300, (name, broken, closure)
        print('OK')
    """)


def test_consensus_bend_pass_uses_the_same_mirror():
    """flag_consensus_bends hard-flags without any later veto, so it must not
    be left on the raw own-EOF mirror that scan_b_events no longer uses."""
    _run("""
        import inspect
        src = inspect.getsource(E.flag_consensus_bends)
        assert '_mirror_span(' in src, \\
            'flag_consensus_bends still mirrors on the raw per-fiber b_span'
        # ...and it must not have quietly gone back to the bare end marker.
        assert "b_span_own = b_ends[0]['dist_km']" not in src, src[:400]
        print('OK')
    """)


def test_short_b_side_keeps_its_pre_break_events():
    """Re-anchoring must not re-point the B tailbox guard at the fiber's own
    damage — that would delete the pre-break zone, which is where the tech is
    looking.  Only the events inside the damage step itself are dropped."""
    _run("""
        pop, cap = 76.5229, 76.6729
        span_a   = 76.5300
        b_eof    = 49.380
        span, short = E._mirror_span(b_eof, pop, cap, span_a)
        assert short is True

        fold = E._fold_km()
        # The far-end tailbox guard keys off the corrected span, so a fiber
        # that never reaches the far end loses nothing to it.
        assert 43.736 <= span - E.LAUNCH_FIBER_MAX
        assert 47.500 <= span - E.LAUNCH_FIBER_MAX

        # A real splice 1.9 km before the damage survives...
        assert not (47.500 > b_eof - fold), (fold,)
        # ...while an event sitting inside the damage step is dropped.
        assert 49.300 > b_eof - fold, (fold,)
        print('OK')
    """)
