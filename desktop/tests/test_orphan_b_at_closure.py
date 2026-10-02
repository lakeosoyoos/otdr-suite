"""A closure event stored by B alone keeps its cell when A's nearest reading
is far away (Robert 2026-10-01, the 432-fibre review span's FR keys).

The per-fibre pairing rule refuses a B event that sits a closure gap from
A's reading.  A then took a measured B leg, but the B event AT the closure
was orphaned: the column printed A's reading 300 m to 1.1 km off centre and
FastReporter's row there (B's stored leg, A measured at B's place, their
average) had no Suite cell.  Now the reading nearer the column wins: B's
stored leg, A measured (grey) at B's place, and the exact mean of the two.

Built from the 24-fibre fixture: fibre 1's A event at the 36.7 km closure is
moved 500 m downstream, so only B stores the closure there.

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import subprocess
import sys
import textwrap

from conftest import FIXTURE_DIR, REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"


def test_b_event_at_the_closure_wins_over_a_far_a_event():
    src = textwrap.dedent(f"""
    import sys, io, contextlib
    sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})
    import splicereportmatchexfo as E
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        fa, fb = E.load_all({str(FIXTURE_DIR / 'splice_A')!r}, {str(FIXTURE_DIR / 'splice_B')!r})
        cand, sub = E.discover_splices(fa, return_subgate=True, fibers_b=fb)
        splices = E.refine_closure_centers(fa, cand, fibers_b=fb)
    si = min(range(len(splices)), key=lambda i: abs(
        splices[i].get('position_km_refined', splices[i]['position_km']) - 36.7))
    col = splices[si].get('position_km_refined', splices[si]['position_km'])
    ev = min((e for e in fa[1]['events'] if not e['is_end']),
             key=lambda e: abs(e['dist_km'] - col))
    assert abs(ev['dist_km'] - col) < 0.1, (ev, col)
    far = dict(ev, dist_km=ev['dist_km'] + 0.5)
    fa[1]['events'] = sorted([e for e in fa[1]['events'] if e is not ev] + [far],
                             key=lambda e: e['dist_km'])
    pop = {{}}
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        res = E.analyze_all(fa, fb, splices, 10.0, population=pop)
    cell = res.get((1, si)) or pop.get((1, si))
    assert cell is not None, 'no cell for fibre 1 at the closure'
    # B's stored reading, A measured at B's place, the exact mean
    assert cell.get('_eb') is not None and cell.get('_ea') is None, cell
    assert cell.get('b_loss') == cell['_eb']['splice_loss']
    assert abs(cell['bidir_dist'] - col) < 0.1, (cell['bidir_dist'], col)
    a, b = cell['a_loss'], cell['b_loss']
    loss = cell.get('bidir_loss', cell.get('loss'))
    assert abs(loss - (a + b) / 2.0) < 1e-12, (a, b, loss)
    print('OK')
    """)
    p = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True)
    assert p.returncode == 0, f"{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout
