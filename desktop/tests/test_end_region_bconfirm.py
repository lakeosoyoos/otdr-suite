"""B-confirmation of end-region closures (the HOWLAN direction-swap bug).

The END_REGION_KM phantom filter blanket-dropped any discovered closure in the
last 3 km of the cable, assuming no real splice lives there.  HOWLAN broke
that: Splice 1 sits 1.8 km from Howe, so loading Lancaster as the A side put
it inside the end region and silently deleted it — plus its ~57 reburn flags
(57 of production's 82).  Fix: before dropping, the B direction gets a veto —
a candidate near A's far end sits near B's LAUNCH (B's cleanest region), so a
discovery-strength population of B fibers seeing an event at the mirror
position proves the closure is real.

These tests run the engine in a clean subprocess (single sor_reader copy —
the 3-engine isolation rule).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"


def _run(body):
    # Dedent SYNTH and the test body SEPARATELY: they carry different
    # indentation depths, and a joint dedent leaves the body indented four
    # spaces — dead code inside mk_fibers after its `return` (runs clean,
    # prints nothing).
    if body.startswith(SYNTH):
        body = textwrap.dedent(SYNTH) + textwrap.dedent(body[len(SYNTH):])
    else:
        body = textwrap.dedent(body)
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n")
    p = subprocess.run([sys.executable, "-c", header + body],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


SYNTH = """
    def mk_fibers(n, ev_km, span=100.0, frac=1.0, losses=None):
        '''n fibers with an end event at `span`; the first int(n*frac) also
        get an interior event at ev_km.'''
        out = {}
        k = int(n * frac)
        for i in range(1, n + 1):
            evs = []
            if i <= k:
                loss = (losses[(i - 1) % len(losses)] if losses else 0.08)
                evs.append({'dist_km': ev_km, 'splice_loss': loss,
                            'is_end': False, 'type': '0F'})
            evs.append({'dist_km': span, 'splice_loss': 0.0,
                        'is_end': True, 'type': '1E'})
            out[i] = {'events': evs}
        return out
"""


def test_b_population_confirms_far_closure():
    _run(SYNTH + """
        # Candidate at 97.5 of a 100 km cable (inside the 3 km end region).
        # Mirror = 2.5 km from B launch; all 40 B fibers see it -> confirmed.
        fb = mk_fibers(40, 2.5)
        ok, n_hits, n_b, mirror = E._b_confirms_far_closure(97.5, fb)
        assert ok and n_hits == 40 and n_b == 40, (ok, n_hits, n_b)
        assert abs(mirror - 2.5) < 0.05, mirror
        print('OK')
    """)


def test_weak_b_population_does_not_confirm():
    _run(SYNTH + """
        # Only 5 of 40 B fibers see an event at the mirror -> below both the
        # MIN_POP_SPLICE floor (20) and the 25% fraction -> NOT confirmed.
        fb = mk_fibers(40, 2.5, frac=0.125)
        ok, n_hits, n_b, _ = E._b_confirms_far_closure(97.5, fb)
        assert not ok and n_hits == 5, (ok, n_hits)
        print('OK')
    """)


def test_launch_guard_blocks_cable_end_candidate():
    _run(SYNTH + """
        # Candidate at 99.6 km mirrors to 0.4 km -- inside B's launch zone.
        # Even a full B population there (launch-connector cluster) must NOT
        # confirm it: the cable-end phantom stays dropped.
        fb = mk_fibers(40, 0.4)
        ok, n_hits, n_b, mirror = E._b_confirms_far_closure(99.6, fb)
        assert not ok, (ok, n_hits, mirror)
        print('OK')
    """)


def test_no_b_data_keeps_old_behavior():
    _run(SYNTH + """
        ok, n_hits, n_b, _ = E._b_confirms_far_closure(97.5, None)
        assert not ok and n_b == 0
        ok2, _, _, _ = E._b_confirms_far_closure(97.5, {})
        assert not ok2
        print('OK')
    """)


def test_post_eof_events_cannot_confirm():
    _run(SYNTH + """
        # B fibers whose only 'event' at the mirror sits PAST their own EOF
        # (the post-EOL instrument-noise tail that creates cable-boundary
        # phantoms in the first place) must not count as confirmation.
        fb = mk_fibers(40, 2.5, span=100.0)
        for r in fb.values():                      # move EOF before the event
            r['events'] = ([{'dist_km': 2.0, 'splice_loss': 0.0,
                             'is_end': True, 'type': '1E'}]
                           + [e for e in r['events'] if not e.get('is_end')])
        ok, n_hits, n_b, _ = E._b_confirms_far_closure(97.5, fb)
        assert not ok and n_hits == 0, (ok, n_hits)
        print('OK')
    """)


def test_refine_keeps_b_confirmed_end_region_closure():
    _run(SYNTH + """
        # End-to-end through refine_closure_centers: a tight, valid cluster
        # at 97.5 km (inside the end region) is DROPPED without B data and
        # KEPT when B confirms.  Losses include >5% gainers and a low median
        # so the normal cluster validation passes once the drop is vetoed.
        losses = [0.08, 0.08, 0.08, -0.05]         # 25% gainers, median .08
        fa = mk_fibers(40, 97.5, losses=losses)
        sp = [{'position_km': 97.5, 'count': 40}]
        kept_none = E.refine_closure_centers(fa, [dict(s) for s in sp])
        assert kept_none == [], f"old behavior broken: {kept_none}"

        fb = mk_fibers(40, 2.5)
        kept_b = E.refine_closure_centers(fa, [dict(s) for s in sp], fibers_b=fb)
        assert len(kept_b) == 1, f"B-confirmed closure was not kept: {kept_b}"
        assert abs(kept_b[0]['position_km_refined'] - 97.5) < 0.1
        print('OK')
    """)


def test_runner_passes_fibers_b_to_refine():
    """Source lock: BOTH pipeline replicas must pass fibers_b to
    refine_closure_centers — the engine's main() AND run_splicereport.py's
    replicated SOR path (missing the latter is exactly how the first draft
    of this fix silently did nothing in production)."""
    eng = (SPLICEREPORT_DIR / "splicereportmatchexfo.py").read_text(encoding="utf-8")
    run = (SPLICEREPORT_DIR / "run_splicereport.py").read_text(encoding="utf-8")
    assert "return_phantoms=True, fibers_b=fibers_b" in eng, \
        "engine main() no longer passes fibers_b to refine_closure_centers"
    assert "fibers_b=fb" in run, \
        "run_splicereport replica no longer passes fibers_b to refine_closure_centers"
