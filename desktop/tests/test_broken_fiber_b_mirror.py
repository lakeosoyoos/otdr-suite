"""B events on a broken fiber are mirrored on the CABLE span, not the fiber's
own truncated reach (DFWMH0340, Dallas -> ILA 1, 2026-09-16).

Fibers 51 / 89 / 209 / 410 broke at km 40.6 from the A end; their B traces
read 7.96 km.  analyze_all's event pairing mirrored each B event on that
7.96 km "end", so the real closure at km 43.75 (4.8 km from the B launch)
landed at km 3.16 and was paired with the A event at the km 3.49 closure.
The cell printed a "bidirectional" value that no two readings of the same
glass produced: 89 -.112, 209 -.114, 410 -.100, 51 .104 — all flagged at
the tech's 0.1 dB gate.  #189 guarded the grey-value fallback against the
truncated span but not the pairing that runs before it.

The engine already knows the rule (_mirror_span, SUI<->EMR F369); this test
locks its use in analyze_all's pairing.  Engine runs in a clean subprocess.
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"


def _run(body, engine_dir=SPLICEREPORT_DIR):
    body = "".join(textwrap.dedent(part) for part in body.split("\x00"))
    header = ("import sys\n"
              f"sys.path.insert(0, {str(engine_dir)!r})\n"
              "import splicereportmatchexfo as E\n")
    p = subprocess.run([sys.executable, "-c", header + body],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


# DFW geometry: 48.55 km span, closures at 3.49 / 20.0 / 43.75 km.
# Fiber 89 breaks at km 40.6 and carries a -0.160 gainer at the 3.49 closure.
# Its B trace reaches 7.96 km and holds the 43.75 closure's B event at 4.8 km.
_SETUP = """
    SPAN = 48.55
    BREAK = 40.6
    splices = [{'position_km': km, 'column_kind': 'splice', 'count': 11}
               for km in (3.49, 20.0, 43.75)]
    fibers_a, fibers_b = {}, {}
    fibers_a[89] = {'events': [
        {'dist_km': 3.49, 'splice_loss': -0.160, 'is_end': False, 'type': '0F', 'reflection': 0.0},
        {'dist_km': BREAK, 'splice_loss': 0.0, 'is_end': True, 'type': '1E', 'reflection': 0.0}]}
    fibers_b[89] = {'events': [
        {'dist_km': SPAN - 43.75, 'splice_loss': -0.064, 'is_end': False, 'type': '0F', 'reflection': 0.0},
        {'dist_km': SPAN - BREAK, 'splice_loss': 0.0, 'is_end': True, 'type': '1E', 'reflection': 0.0}]}
    for f in range(10, 20):
        fibers_a[f] = {'events': [
            {'dist_km': SPAN, 'splice_loss': 0.0, 'is_end': True, 'type': '1E', 'reflection': 0.0}]}
        fibers_b[f] = {'events': [
            {'dist_km': SPAN, 'splice_loss': 0.0, 'is_end': True, 'type': '1E', 'reflection': 0.0}]}
    res = E.analyze_all(fibers_a, fibers_b, splices, 0.1)
"""

_CHECK = """
    cell = res.get((89, 0))
    # The A gainer alone reads |0.160| < SINGLE_DIR_THRESHOLD, so the upstream
    # column is either absent or an unflagged A-only cell.  It is NEVER a
    # bidirectional pair: the B trace ends 37 km short of this closure.
    assert cell is None or cell.get('b_loss') is None, cell
    assert cell is None or not cell.get('is_flagged'), cell
    print('OK')
"""


def test_b_event_past_the_break_is_not_paired_upstream():
    _run(_SETUP + "\x00" + _CHECK)


def test_the_broke_cell_is_unchanged():
    """The 43.75 closure is the one nearest the km 40.6 break, so that column
    is the BROKE cell.  Mirrored on the cable span the 4.8 km B event belongs
    there, past the break, and is absorbed by it: it does not travel upstream."""
    _run(_SETUP + "\x00" + """
        cell = res.get((89, 2))
        assert cell is not None and cell['is_broke'], cell
        assert cell['label'].startswith('89 broke'), cell
        assert abs(cell['bidir_dist'] - BREAK) < 1e-9, cell
        print('OK')
    """)
