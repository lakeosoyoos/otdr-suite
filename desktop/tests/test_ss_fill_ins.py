"""Shot out of order: fibres skipped in the run and shot later.

A fibre shot more than 30 minutes after BOTH neighbouring fibre numbers, while
those neighbours were shot back to back, was skipped and filled in: the tech
had to find its port again.  On the Goodland Finals the list holds exactly the
two known re-shoots (0012 at 21:25 among 14:59 neighbours, 0841 at 21:27) plus
the 14 fill-ins already in the originals; Monument 7; the RDR4RDR5 tray, LSC
and both Dinwiddie panels none.

Two traps the rule has to avoid, both seen on real folders:

  * Mirror images.  Two fill-ins with a fibre shot on time between them make
    that fibre look "far from both neighbours" too, only earlier.  Only runs
    shot LATER count (Goodland 694/697 with 695-696 between).
  * Whole ribbons shot out of order (Duran->Ancho flips at every twelfth fibre
    from 684).  The neighbours of such a block were not shot back to back, so
    the bridge test keeps them off the list.

It says where to look.  It is not a duplicate finding and touches no verdict.
"""
from __future__ import annotations

import json
import subprocess
import sys

from conftest import SECRETSAUCE_DIR


def _run(script: str):
    p = subprocess.run([sys.executable, "-c", script, str(SECRETSAUCE_DIR)],
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-3000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


_SCRIPT = r"""
import sys, json
sys.path.insert(0, sys.argv[1])
import report_sor as RS
T0 = 1700000000
def f(name, t): return {'name': name, 'timestamp': T0 + t}
files = [f('SPANsh%04d_1550' % n, 25 * n) for n in range(1, 41)]
by = {x['name']: x for x in files}
def move(n, when): by['SPANsh%04d_1550' % n]['timestamp'] = T0 + when
move(10, 7200)                     # skipped, shot two hours later
move(20, 7260); move(21, 7285)     # two skipped together
move(30, 10800); move(32, 10830)   # two fill-ins, 31 shot on time between them
for k, n in enumerate(range(35, 41)):
    move(n, -3600 + 25 * k)        # a ribbon shot before fibre 1: not a fill-in
runs = RS._fill_ins(files)
out = {'runs': [[r['first'], r['last']] for r in runs],
       'names_20': [r['names'] for r in runs if r['first'] == 20],
       'minutes_10': [round(r['minutes_later']) for r in runs if r['first'] == 10],
       'before_after_10': [[r['before'], r['after']] for r in runs if r['first'] == 10]}
dup = [f('PNLsh%04d' % n, 25 * n) for n in range(1, 20)] + [f('PNLsh0005', 9000)]
out['dup_runs'] = len(RS._fill_ins(dup))
three = [f('ANCDURsh%03d_1550' % n, 25 * n) for n in range(1, 30)]
three[9]['timestamp'] = T0 + 9000
out['three'] = [[r['first'], r['last']] for r in RS._fill_ins(three)]
out['no_times'] = len(RS._fill_ins([{'name': 'Xsh%04d' % n} for n in range(1, 10)]))
print(json.dumps(out))
"""


def test_finds_skipped_fibres_shot_later():
    r = _run(_SCRIPT)
    assert r["runs"] == [[10, 10], [20, 21], [30, 30], [32, 32]], r["runs"]
    assert r["names_20"] == [["SPANsh0020_1550", "SPANsh0021_1550"]]
    assert r["minutes_10"] == [115]
    assert r["before_after_10"] == [["SPANsh0009_1550", "SPANsh0011_1550"]]


def test_a_fibre_shot_on_time_between_two_fill_ins_is_not_listed():
    r = _run(_SCRIPT)
    assert [31, 31] not in r["runs"]


def test_a_ribbon_shot_out_of_order_is_not_listed():
    r = _run(_SCRIPT)
    assert all(not (35 <= a <= 40) for a, _ in r["runs"])


def test_says_nothing_it_cannot_support():
    r = _run(_SCRIPT)
    assert r["dup_runs"] == 0, "the same fibre number twice means panels are mixed"
    assert r["three"] == [[10, 10]], "three-digit names must parse"
    assert r["no_times"] == 0


def test_source_locks_display_only():
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    i = src.index("def _fill_ins(files):")
    body = src[i:src.index("\ndef ", i + 10)]
    for use in ("['p_dup']", "'p_dup'", "p_dup =", "p_dup["):
        assert use not in body
    assert "t[k] - max(lo, hi) > _FILL_IN_FAR_S" in body, "only runs shot LATER count"
    assert "if _fi:\n        ws = wb.create_sheet('Shot out of order')" in src
