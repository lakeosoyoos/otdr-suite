"""A bidir job too small for the 20-fibre floor to be a quarter of it must
count a fibre at a closure when EITHER end stored an event there.

Field, 2026-09-29 (Viewer on a 55 km route, 24 fibres per folder): the
Viewer's report found 2 of the 10 closures and called the 34 km closure a
bend.  Discovery needs MIN_POP_SPLICE (20) fibres storing an event at a
closure; on 24 fibres that is 83%, and a low-loss fusion is stored by only
16-19 from one end.  The either-end count used to switch on only under 20
fibres.  It now covers every job where 20 is more than MIN_POP_FRACTION of
the fibres (under 80), so the 20-fibre bar stays but counts fibres, not one
end's events.  With it the 24-fibre run matched the full 1,152-fibre cable
cell for cell.  Jobs of 80+ fibres never reach the branch.

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"

_BODY = """
def ev(km, typ='0F9999LS', end=False):
    return {'dist_km': km, 'splice_loss': 0.05, 'type': typ,
            'is_end': end, 'is_reflective': False}

SPAN = 40.0
def fibre(kms):
    return {'events': [ev(0.0, typ='1F9999LS')] + [ev(k) for k in kms]
                      + [ev(SPAN, typ='1E9999LS', end=True)]}

def job(n):
    # Closure at 10 km: fibres 1-16 store it from A, fibres 10-21 from B
    # (21 fibres from one end or the other).  Closure at 25 km: every fibre
    # from A, the control that discovery works at all.
    fa = {f: fibre(([10.0] if f <= 16 else []) + [25.0]) for f in range(1, n + 1)}
    fb = {f: fibre(([SPAN - 10.0] if 10 <= f <= 21 else []) + [SPAN - 25.0])
          for f in range(1, n + 1)}
    return fa, fb

def found(n):
    fa, fb = job(n)
    cand, _ = E.discover_splices(fa, return_subgate=True, fibers_b=fb)
    return sorted(round(c['position_km']) for c in cand)
"""


def _run(tail):
    src = ("import sys\n"
           f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
           "import splicereportmatchexfo as E\n"
           + textwrap.dedent(_BODY) + textwrap.dedent(tail))
    p = subprocess.run([sys.executable, "-c", src],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    out = p.stdout.strip().splitlines()
    assert out and out[-1] == "OK", p.stdout


def test_24_fibres_find_a_closure_seen_by_21_from_either_end():
    """THE regression: 16 from A alone is under 20; 21 from either end is not."""
    _run("""
    got = found(24)
    assert got == [10, 25], got
    print('OK')
    """)


def test_either_end_still_needs_twenty_fibres():
    """The bar is unchanged: 21 fibres from either end on a 24-fibre job,
    but cut B's share so only 19 fibres see it and it stays out."""
    _run("""
    fa, fb = job(24)
    for f in (20, 21):
        fb[f]['events'] = [e for e in fb[f]['events']
                           if abs(e['dist_km'] - (SPAN - 10.0)) > 0.1]
    cand, _ = E.discover_splices(fa, return_subgate=True, fibers_b=fb)
    got = sorted(round(c['position_km']) for c in cand)
    assert got == [25], got
    print('OK')
    """)


def test_eighty_fibre_job_is_untouched():
    """At 80 fibres the 20-fibre floor is a quarter of the job: the branch is
    off, so 16 A fibres at 10 km stay a sub-gate cluster as before."""
    _run("""
    got = found(80)
    assert got == [25], got
    print('OK')
    """)
