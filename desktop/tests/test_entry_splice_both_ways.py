"""An entry splice stored from both ends gets an Entry column (Suite mode).

Tooele<->Knolls Span 2 (Lumen, 2026-09-25): the splice 84 m past the Tooele
launch reel is stored on only 39/432 Tooele fibers and 26/432 Knolls fibers,
under the 25% discovery gate, so Suite mode dropped the column in both load
orders and F85's .232 (EXFO .23) never printed.  FastReporter, on all 431
pairs built in FR, prints that splice as its own row only on fibers that
stored it from BOTH ends.  `entry_splice_closures` gives such a cluster an
Entry column; `far_entry_candidates` routes the far-end copy of it (which
passes discovery and then dies as an end-region phantom) to the same test.

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"


def _run(body):
    code = (f"import sys\nsys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
            "import splicereportmatchexfo as E\n" + textwrap.dedent("""
        SPAN = 77.3
        def ev(km, end=False):
            return {'dist_km': km, 'is_end': end,
                    'type': '1E9999LS' if end else '0F9999LS'}
        def rec(kms, reel):
            return {'events': [ev(k) for k in kms] + [ev(SPAN, True)],
                    '_launch_reel_km': reel}
        def span(entry_a, entry_b, both, reel_a=1.0, reel_b=1.0, n=40):
            # A stores the entry splice at 0.084 km on `entry_a` fibers, B
            # stores its mirror on `entry_b`; the first `both` are shared.
            fa, fb = {}, {}
            for f in range(1, n + 1):
                a = [0.084] if f <= entry_a else []
                b = [SPAN - 0.084] if (f <= both or n - f < entry_b - both) else []
                fa[f] = rec(a + [20.0, 40.0], reel_a)
                fb[f] = rec(b + [SPAN - 40.0, SPAN - 20.0], reel_b)
            return fa, fb
    """) + textwrap.dedent(body))
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_entry_stored_from_both_ends_gets_a_column():
    _run("""
        fa, fb = span(entry_a=12, entry_b=8, both=1)
        sg = [{'position_km': 0.084, 'count': 12, 'reach_count': 40}]
        out = E.entry_splice_closures(sg, fa, fb, [20.0, 40.0])
        assert len(out) == 1 and out[0]['entry_both_ways'], out
        assert out[0]['entry_both_fibers'] == [1], out
        print('OK')
    """)


def test_entry_stored_from_one_end_only_gets_none():
    # FR folds a one-sided entry splice into the other end's reel connector.
    _run("""
        fa, fb = span(entry_a=12, entry_b=8, both=0)
        sg = [{'position_km': 0.084, 'count': 12, 'reach_count': 40}]
        assert E.entry_splice_closures(sg, fa, fb, [20.0, 40.0]) == []
        print('OK')
    """)


def test_no_launch_reel_no_entry_column():
    # BARTUL loaded Tulsa-first: a cluster 51 m from a reel-less launch is
    # the launch connector's dead zone, not a splice.
    _run("""
        fa, fb = span(entry_a=12, entry_b=8, both=3, reel_a=0.0)
        sg = [{'position_km': 0.084, 'count': 12, 'reach_count': 40}]
        assert E.entry_splice_closures(sg, fa, fb, [20.0, 40.0]) == []
        print('OK')
    """)


def test_far_end_candidate_is_routed_to_the_entry_test():
    _run("""
        fa, fb = span(entry_a=0, entry_b=0, both=0)
        c = {'position_km': SPAN - 0.09, 'count': 26, 'reach_count': 26}
        keep, far = E.far_entry_candidates([c, {'position_km': 20.0,
                                                'count': 40}], fa, fb)
        assert [k['position_km'] for k in keep] == [20.0], keep
        assert far == [c], far
        print('OK')
    """)
