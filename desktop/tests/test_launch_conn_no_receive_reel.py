"""Launch connector graded when the far direction has no receive reel.

Both launch-connector gates need a reading from each direction.  With a
launch reel at each end and no receive reel (Lumen Span 7, Monument <->
Grainfield), each trace ENDS at the other end's panel, so the far direction
never stores that connector as an event, and no gate could fire: a 4.787 dB
Monument connector on fiber 229 went unreported.

FastReporter does not skip it.  It transplants the near side's cursors into
the far trace and prints that as the far leg (FR 3 on these .sor pairs,
2026-09-25): 229 A 4.787 / B 0.009, average 2.398 FAIL; 1029 B 0.916 /
A 0.006, average 0.461.  The engine now takes the far leg from FR's own table.
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
FX = REPO_ROOT / "desktop" / "tests" / "fixtures" / "launch_noreceive"


def _run(body):
    header = ("import sys, os\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import sor_reader324802a as sr\n"
              "import splicereportmatchexfo as E\n"
              f"FX = {str(FX)!r}\n"
              "def pair(f):\n"
              "    A = sr.parse_sor_full(os.path.join(FX, 'MONGRA%s_1550.sor' % f), trim=False)\n"
              "    B = sr.parse_sor_full(os.path.join(FX, 'GRAMON%s_1550.sor' % f), trim=False)\n"
              "    for r, s in ((A, 'a'), (B, 'b')):\n"
              "        r['_source'] = 'sor'; r['_span_side'] = s\n"
              "    return A, B\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_far_leg_is_fastreporters_transplant():
    _run("""
        A, B = pair('0229')
        assert E._far_ends_at_panel(A, B) and E._far_ends_at_panel(B, A)
        ca = E._a_launch_conn_event(A)
        assert round(ca['splice_loss'], 3) == 4.787
        # the Grainfield trace has no event for it -- only its end reflection
        assert E._b_launch_conn_mirror(B, ca['dist_km']) is None
        far = E._fr_far_leg_at_launch(A, B, ca, near_is_a=True)
        assert round(far, 3) == 0.009, far
        assert round((ca['splice_loss'] + far) / 2.0, 3) == 2.398

        A, B = pair('1029')
        cb = E._a_launch_conn_event(B)
        assert round(cb['splice_loss'], 3) == 0.916
        far = E._fr_far_leg_at_launch(B, A, cb, near_is_a=False)
        assert round(far, 3) == 0.006, far
        assert round((cb['splice_loss'] + far) / 2.0, 3) == 0.461
        print('OK')
    """)


def test_the_gates_now_see_it():
    _run("""
        import copy
        fa, fb = {}, {}
        for f in ('0229', '1029', '0183'):
            A, B = pair(f)
            fa[int(f)], fb[int(f)] = A, B
        issues = E.detect_launch_issues(fa, fb)
        tags = {f: (i['a_tags'], i['b_tags']) for f, i in issues.items()}
        assert '4.78 LAUNCH A side' in tags[229][0], tags.get(229)
        assert '.91 LAUNCH B side' in tags[1029][1], tags.get(1029)
        assert not any('LAUNCH' in t for t in sum(tags.get(183, ([], [])), [])), tags.get(183)
        print('OK')
    """)
