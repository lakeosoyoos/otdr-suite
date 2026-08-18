"""B-reciprocity veto: zero-gainer dense "splices" that are really bends.

A bend RADIATES — the same fiber loses the same amount from either
direction.  A splice's one-direction loss carries a mode-field-mismatch
term that flips sign with direction, and the A-stored population is
selected for the positive sign, so re-measuring those fibers from B
collapses a real splice's median to ~0 with ~half gainers.  Validated on
9 sites / 2 spans: 8 confirmed cans B/A in [-0.80, +0.22] with 36-99.6%
B gainers; the KANLAN 9.46 bend B/A +0.80 with 11.7%.

The veto only sees the loss-distribution gate's blind spot (zero gainers
AND low median AND dense) and its fail-safe direction is always
keep-the-splice.  Repairs are exempt: a self-fusion is genuinely
reciprocal, indistinguishable from a bend by this test.

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"

# Pre-dedented so it composes with a separately-dedented body (see
# test_b_corr_promotion.py for the trap this avoids).
_HELPERS = textwrap.dedent("""
    import numpy as np
    RES = 5.0985

    def b_fiber(span_km, step_km=None, step_db=0.0):
        '''A B-direction fiber record: linear 0.19 dB/km trace with an
        optional loss step, plus a minimal normalized event list.'''
        n = int(span_km * 1000 / RES) + 400
        x_km = np.arange(n) * RES / 1000.0
        # The proprietary trace (64 - raw/1024) STEPS UP at a loss — proven
        # by the 186/186 .bdr validation, where after-minus-before at real
        # losses is positive.  Build it in that convention.
        db = 30.0 + 0.19 * x_km
        if step_km is not None:
            db = db + step_db * (x_km >= step_km)
        raw = ((64.0 - db) * 1024.0).astype('<u2')
        return {'exfo_raw': raw, 'exfo_res_m': RES,
                'events': [{'dist_km': span_km, 'splice_loss': 0.0,
                            'is_end': True, 'is_reflective': False,
                            'time_of_travel': 99}]}

    def a_fiber(pos_km, loss, span_km):
        return {'events': [
            {'dist_km': pos_km, 'splice_loss': loss, 'is_end': False,
             'is_reflective': False, 'time_of_travel': 10},
            {'dist_km': span_km, 'splice_loss': 0.0, 'is_end': True,
             'is_reflective': False, 'time_of_travel': 99}]}

    def make_span(n, pos_km, span_km, b_step_db_fn):
        '''n fibers; every A fiber stores a small positive loss at pos_km
        (zero gainers); B traces get b_step_db_fn(f) of true step at the
        mirror.'''
        rng = np.random.default_rng(7)
        fa, fb = {}, {}
        for f in range(1, n + 1):
            fa[f] = a_fiber(pos_km, float(0.04 + 0.04 * rng.random()),
                            span_km)
            fb[f] = b_fiber(span_km, step_km=span_km - pos_km,
                            step_db=b_step_db_fn(f))
        return fa, fb

    def candidate(pos_km, **extra):
        sp = {'position_km': pos_km, 'position_km_refined': pos_km}
        sp.update(extra)
        return sp
""")


def _run(body):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n")
    p = subprocess.run([sys.executable, "-c",
                        header + _HELPERS + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    out = p.stdout.strip().splitlines()
    assert out and out[-1] == "OK", p.stdout


def test_reciprocal_step_reads_as_bend():
    """Every B trace carries the SAME loss back at the mirror (bend
    physics) -> verdict is bend."""
    _run("""
        fa, fb = make_span(120, 9.46, 115.4, lambda f: 0.05)
        bend, why = E._b_reciprocity_verdict(candidate(9.46), fa, fb)
        assert bend, why
        print('OK')
    """)


def test_reciprocal_step_with_stored_b_events_stays_splice():
    """A real can whose slack storage bends every fiber measures reciprocal
    too (SEANOR Splice 1/13) — but the far truck STORES the discrete step.
    B stored fraction above B_RECIP_MAX_B_STORED must keep the splice."""
    _run("""
        fa, fb = make_span(120, 9.46, 115.4, lambda f: 0.05)
        # give half the B fibers a stored event at the mirror
        for f, r in fb.items():
            if f % 2 == 0:
                r['events'].insert(0, {'dist_km': 115.4 - 9.46,
                                       'splice_loss': 0.05,
                                       'is_end': False,
                                       'is_reflective': False,
                                       'time_of_travel': 50})
        bend, why = E._b_reciprocity_verdict(candidate(9.46), fa, fb)
        assert not bend, why
        assert 'stored 50%' in why, why
        print('OK')
    """)


def test_collapsing_step_reads_as_splice():
    """B sees ~zero median with ~half gainers (mismatch flips sign) ->
    verdict is splice, candidate keeps its column."""
    _run("""
        fa, fb = make_span(120, 9.46, 115.4,
                           lambda f: 0.02 if f % 2 else -0.02)
        bend, why = E._b_reciprocity_verdict(candidate(9.46), fa, fb)
        assert not bend, why
        print('OK')
    """)


def test_no_rawsamples_is_fail_safe():
    """JSON spans (no RawSamples) must return splice — today's behaviour —
    never a bend verdict on missing data."""
    _run("""
        fa, fb = make_span(120, 9.46, 115.4, lambda f: 0.05)
        for r in fb.values():
            r['exfo_raw'] = None
        bend, why = E._b_reciprocity_verdict(candidate(9.46), fa, fb)
        assert not bend, why
        assert 'measurable' in why or 'matched' in why, why
        print('OK')
    """)


def test_thin_population_is_fail_safe():
    """Below B_RECIP_MIN_N matched fibers -> no verdict."""
    _run("""
        fa, fb = make_span(30, 9.46, 115.4, lambda f: 0.05)
        bend, why = E._b_reciprocity_verdict(candidate(9.46), fa, fb)
        assert not bend, why
        print('OK')
    """)


def test_repair_exemption_is_enforced_at_the_gate():
    """The refinement hook must never send an is_repair / b_corroborated /
    entry-case candidate to the veto: a self-fusion is genuinely
    reciprocal and would demote wrongly.  Guarded in source."""
    _run("""
        import inspect
        src = inspect.getsource(E.refine_closure_centers)
        i = src.index('_b_reciprocity_verdict')
        gate = src[:i][-1500:]
        for guard in ("is_repair", "b_corroborated", "is_entry_case"):
            assert guard in gate, f'missing {guard} exemption before veto'
        # and the blind-spot shape is the only trigger
        assert "gainer_frac'] < min_gnr" in gate
        assert "median_loss_db'] <= med_max" in gate
        print('OK')
    """)
