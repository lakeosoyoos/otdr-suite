"""The per-file best partner is found in one pass over the pairs, with the same answer.

Measured 2026-09-15: `_analyze_sor` spent 63% of its time scanning every pair
once per file to pick each file's best partner.  That is files x pairs steps,
about 760 million on a 1,152-file folder, and it slowed 3.3x more when PR #184
added two keys to every pair dict (21 keys, 640 bytes -> 23 keys, 1,176 bytes).
EMVSUI Long: 81 s on main, 147 s with #184.

`_best_partners` walks the pairs once.  The choice must not move: the highest
p_dup wins, then the smallest score, and on an exact tie the EARLIER pair in list
order stays, which is what the old strict-comparison scan did.  These tests hold
the new function to the old scan, pair object for pair object.

Namespace isolation rule: the Secret Sauce engine only runs in subprocesses.
"""
from __future__ import annotations

import json
import subprocess
import sys

from conftest import SECRETSAUCE_DIR


def _run(script, *args, timeout=240):
    p = subprocess.run([sys.executable, "-c", script, str(SECRETSAUCE_DIR),
                        *[str(a) for a in args]],
                       capture_output=True, text=True, timeout=timeout)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


_OLD_SCAN = r"""
def old_scan(files, pairs):
    # The scan exactly as it stood before the one-pass rewrite.
    best_partner = {}
    for idx, f in enumerate(files):
        best = None
        for p in pairs:
            if f['name'] not in (p['a'], p['b']):
                continue
            if best is None:
                best = p
            elif (p['p_dup'] > best['p_dup']
                  or (p['p_dup'] == best['p_dup'] and p['score'] < best['score'])):
                best = p
        best_partner[f['name']] = best
    return best_partner

def same(old, new):
    # Same keys in the same order, and the very same pair object per file.
    return list(old) == list(new) and all(old[k] is new[k] for k in old)
"""


def test_one_pass_matches_the_old_scan_on_ties_and_edge_cases():
    out = _run(r"""
import sys, json, math, random
sys.path.insert(0, sys.argv[1])
import report_sor as RS
""" + _OLD_SCAN + r"""
nan = float('nan')
f = lambda *names: [{'name': n} for n in names]
P = lambda a, b, d, s: {'a': a, 'b': b, 'p_dup': d, 'score': s}
cases = {}
# exact tie on p_dup AND score: the earlier pair must stay
t1, t2 = P('A', 'B', 0.5, 0.1), P('A', 'C', 0.5, 0.1)
cases['exact_tie'] = (f('A', 'B', 'C'), [t1, t2])
# p_dup tie broken by the smaller score, either order
cases['score_breaks_tie'] = (f('A', 'B', 'C'), [P('A', 'B', 0.5, 0.2), P('A', 'C', 0.5, 0.1)])
cases['score_breaks_tie_rev'] = (f('A', 'B', 'C'), [P('A', 'C', 0.5, 0.1), P('A', 'B', 0.5, 0.2)])
# higher p_dup beats a smaller score
cases['p_dup_first'] = (f('A', 'B', 'C'), [P('A', 'B', 0.4, 0.01), P('A', 'C', 0.9, 0.5)])
# NaN never wins a comparison, so the first pair stays
cases['nan'] = (f('A', 'B', 'C'), [P('A', 'B', nan, nan), P('A', 'C', 0.9, 0.1)])
# a file with no pair, a pair naming a file not in the list, a duplicate file name
cases['orphans'] = (f('A', 'B', 'Z', 'A'), [P('A', 'B', 0.3, 0.1), P('B', 'X', 0.9, 0.1)])
# a pair of a file with itself
cases['self_pair'] = (f('A', 'B'), [P('A', 'A', 0.7, 0.1), P('A', 'B', 0.7, 0.1)])
res = {k: same(old_scan(fs, ps), RS._best_partners(fs, ps)) for k, (fs, ps) in cases.items()}
res['exact_tie_keeps_first'] = RS._best_partners(*cases['exact_tie'])['A'] is t1
res['orphan_none'] = RS._best_partners(*cases['orphans'])['Z'] is None
# randomised folders with heavy ties, several pair orders
rng = random.Random(7)
bad = 0
for trial in range(40):
    n = rng.randint(2, 25)
    fs = f(*['F%03d' % i for i in range(n)])
    ps = [P(fs[i]['name'], fs[j]['name'], rng.choice([0.0, 0.1, 0.5, 0.5, 0.99, 1.0]),
            rng.choice([0.01, 0.02, 0.02, 0.05]))
          for i in range(n) for j in range(i + 1, n)]
    rng.shuffle(ps)
    bad += not same(old_scan(fs, ps), RS._best_partners(fs, ps))
res['random_mismatches'] = bad
print(json.dumps(res))
""")
    for k in ("exact_tie", "score_breaks_tie", "score_breaks_tie_rev", "p_dup_first",
              "nan", "orphans", "self_pair"):
        assert out[k] is True, (k, out)
    assert out["exact_tie_keeps_first"] is True
    assert out["orphan_none"] is True
    assert out["random_mismatches"] == 0


def test_the_engine_uses_it_and_gets_the_old_answer(tmp_path):
    """Real `_analyze_sor` on a stubbed folder: its best_partner must equal the
    old scan over the same files and pairs."""
    out = _run(r"""
import sys, os, json, hashlib, contextlib, io
import numpy as np
sys.path.insert(0, sys.argv[1])
import report_sor as RS
""" + _OLD_SCAN + r"""
def _stub_load(path):
    name = os.path.splitext(os.path.basename(path))[0]
    eof, glass = open(path).read().strip().split(',')
    n = int(float(eof))
    rng = np.random.RandomState(int(hashlib.md5(glass.encode()).hexdigest()[:8], 16))
    trace = 20.0 - 0.0002 * np.arange(n, dtype=np.float64) + rng.randn(n) * 0.05
    if glass != name:
        own = np.random.RandomState(int(hashlib.md5(name.encode()).hexdigest()[:8], 16))
        trace = trace + own.randn(n) * 0.005
    return {'name': name, 'filepath': path, 'trace': trace.astype(np.float32),
            'pos': np.arange(n, dtype=np.float64), 'length': float(eof), 'loss': None,
            'max_splice_dB': None, 'timestamp': None, 'wavelength': 1550,
            'serial_number': 'SN1', 'events': []}
RS.load_sor_file = _stub_load
d = sys.argv[2]
for i in range(1, 15):
    nm = 'AAABBB%04d' % i
    open(os.path.join(d, nm + '.sor'), 'w').write('%s,%s' % (2190.0 + 2 * i, nm))
open(os.path.join(d, 'AAABBB0015.sor'), 'w').write('2200.0,AAABBB0005')
with contextlib.redirect_stdout(io.StringIO()):
    a = RS._analyze_sor(d)
print(json.dumps({'same': same(old_scan(a['files'], a['pairs']), a['best_partner']),
                  'n_files': len(a['files']), 'n_pairs': len(a['pairs']),
                  'twin_points_at_each_other': {a['best_partner']['AAABBB0005']['a'], a['best_partner']['AAABBB0005']['b']} == {'AAABBB0005', 'AAABBB0015'}}))
""", tmp_path)
    assert out["n_files"] == 15 and out["n_pairs"] == 105, out
    assert out["same"] is True, out
    assert out["twin_points_at_each_other"] is True, out
