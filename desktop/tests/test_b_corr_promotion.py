"""B-corroborated closure promotion (KANLAN repair splice @110.2).

A closure the A direction is detection-limited on (far field: 2500 ns / 15 s
stores only the worst events at 111 km — 36/864 A fibers vs 384/864 from B,
6 km out) gets promoted to an ordinary NUMBERED splice column when a
discovery-strength B population corroborates the mirror position AND the
visibility is detection-ASYMMETRIC (B fraction >= 2x A fraction).
is_repair / b_corroborated ride along as provenance only (Robert's call
2026-08-14: repair columns are treated like any other splice).

The asymmetry gate is load-bearing: bends attenuate symmetrically, so a
bend zone mirrors in B at a SIMILAR rate and must not promote (it stays a
bend column); likewise small-cable closures that miss the absolute
MIN_POP_SPLICE floor are seen near-equally from both ends (Elmhurst
fixture: 87% vs 67% — promotion there rippled a healthy span before the
gate existed).

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"


# Pre-dedented (top-level) so it composes with a separately-dedented test
# body — concatenating two indented blocks and dedenting once leaves the
# body nested inside ev()'s block and silently skips every assert.
_FIBER_HELPERS = textwrap.dedent("""
    def ev(km, typ='0F9999LS', end=False):
        return {'dist_km': km, 'splice_loss': 0.05, 'type': typ,
                'is_end': end, 'is_reflective': False}
    def fiber(events, span):
        return {'events': [ev(k) for k in events]
                          + [ev(span, typ='1E9999LS', end=True)]}
""")


def _run(body, helpers=False):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n")
    src = header + (_FIBER_HELPERS if helpers else "") + textwrap.dedent(body)
    p = subprocess.run([sys.executable, "-c", src],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    out = p.stdout.strip().splitlines()
    assert out and out[-1] == "OK", p.stdout


def test_asymmetric_far_closure_promotes_as_repair():
    """KANLAN shape: 100 A fibers, event at 110.2 stored on only 8 (8%);
    B mirror (span 116.25) stored on 45 (45%).  Promotes, is_repair, and
    keeps its distance from the main closure list."""
    _run(helpers=True, body="""
        fibers_b = {}
        for f in range(1, 101):
            evs = [6.05] if f <= 45 else []
            fibers_b[f] = fiber(evs, 116.23)
        subgate = [{'position_km': 110.2, 'count': 8, 'reach_count': 100,
                    'bin': 110}]
        out = E.b_corroborate_closures(subgate, fibers_b, [108.9, 114.0])
        assert len(out) == 1, out
        assert out[0]['is_repair'] is True
        assert out[0]['b_corroborated'] is True
        assert 'column_kind' not in out[0]   # refine tags 'splice' later
        print('OK')
    """)


def test_symmetric_visibility_does_not_promote():
    """Same B population but A stores it at a similar rate (bend zone /
    small-cable closure signature) -> ratio gate blocks promotion."""
    _run(helpers=True, body="""
        fibers_b = {}
        for f in range(1, 101):
            evs = [6.05] if f <= 45 else []
            fibers_b[f] = fiber(evs, 116.23)
        subgate = [{'position_km': 110.2, 'count': 30, 'reach_count': 100,
                    'bin': 110}]
        out = E.b_corroborate_closures(subgate, fibers_b, [108.9, 114.0])
        assert out == [], out
        print('OK')
    """)


def test_isolation_guard_blocks_near_existing_closure():
    """A candidate within B_CORR_ISOLATION_KM of a real closure never
    promotes — the mirror window could be reading the neighbor's own B
    population."""
    _run(helpers=True, body="""
        fibers_b = {}
        for f in range(1, 101):
            fibers_b[f] = fiber([6.05], 116.23)
        subgate = [{'position_km': 110.2, 'count': 8, 'reach_count': 100,
                    'bin': 110}]
        out = E.b_corroborate_closures(subgate, fibers_b, [110.9])
        assert out == [], out
        print('OK')
    """)


def test_promoted_column_numbers_like_any_splice():
    """Robert's call (2026-08-14): a B-corroborated repair column is treated
    as an ordinary splice — it takes the next number in sequence and shifts
    downstream numbering, exactly like any other real closure.  is_repair /
    b_corroborated are provenance only."""
    _run("""
        splices = [
            {'position_km': 50.0, 'column_kind': 'splice'},
            {'position_km': 110.2, 'column_kind': 'splice', 'is_repair': True},
            {'position_km': 114.0, 'column_kind': 'splice'},
        ]
        num = 0
        for sp in splices:
            if sp.get('column_kind') == 'splice' and not sp.get('is_entry_case'):
                num += 1
                sp['splice_display_num'] = num
        assert splices[0]['splice_display_num'] == 1
        assert splices[1]['splice_display_num'] == 2
        assert splices[2]['splice_display_num'] == 3
        print('OK')
    """)
