"""A short shot is named as one and graded as nothing.

Tooele<->Knolls Span 2 F1 (2026-09-25): the Knolls long-shot folder held a
6 s, 10 ns shot whose table ends on an out-of-range marker ('1O') at 4.99 km
of a 77 km span.  FastReporter will not pair it.  The report printed
DURATION_MISMATCH at the B end, a .275 FR-mode cell at the Knolls end, and
PASS on the Span Attenuation and ORL sheet.  Now: one 'SHORT SHOT ...,
reshoot' tag, no FR-mode cells for the fiber, and 'SHORT SHOT' in place of
both verdicts.

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"


def _run(body):
    code = (f"import sys\nsys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
            "import splicereportmatchexfo as E\n" + textwrap.dedent(body))
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_out_of_range_end_is_a_short_shot_and_a_break_is_not():
    _run("""
        def ev(km, t):
            return {'dist_km': km, 'type': t, 'is_end': t[1:2] == 'E'}
        short = {'events': [ev(0.0, '1F9999LS'), ev(1.002, '1F9999LS'),
                            ev(4.994, '1O9999LS')]}
        broken = {'events': [ev(0.0, '1F9999LS'), ev(30.9, '1E9999LS')]}
        whole = {'events': [ev(0.0, '1F9999LS'), ev(77.3, '1E9999LS')]}
        assert E.short_shot_km(short) == 4.994
        assert E.short_shot_km(broken) is None
        assert E.short_shot_km(whole) is None
        assert E.short_shot_km(None) is None
        # TK15 F336 (Knolls): 'O' mid-trace, then the far connector
        mid = {'events': [ev(0.0, '1F9999LS'), ev(77.383, '1O9999LS'),
                          ev(78.277, '1F9999LS')]}
        assert E.short_shot_km(mid) is None
        # ends 'O' but within END_REGION_KM of where the other end reached
        near = {'events': [ev(0.0, '1F9999LS'), ev(77.0, '1O9999LS')]}
        assert E.short_shot_km(near, whole) is None
        assert E.short_shot_km(short, whole) == 4.994
        print('OK')
    """)


def test_span_stats_carry_the_short_shot():
    _run("""
        def rec(end_type, end_km):
            return {'events': [{'dist_km': end_km, 'type': end_type,
                                'is_end': end_type[1:2] == 'E'}],
                    'exfo_spans_loss': 1.357, 'exfo_spans_length': 5000.0,
                    'exfo_total_orl': 37.59}
        st = E.fiber_span_attenuation_orl({1: rec('1E9999LS', 77.3),
                                           2: rec('1E9999LS', 77.3)},
                                          {1: rec('1O9999LS', 4.994),
                                           2: rec('1E9999LS', 77.3)})
        assert st[1]['short_shot'] is True and st[2]['short_shot'] is False, st
        print('OK')
    """)
