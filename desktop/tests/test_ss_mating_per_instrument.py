"""The mating ranking reads connectors, so it must not read the OTDR instead.

A folder can carry two instruments.  Goodland->Monument is 1,152 files shot in
parallel by an FTBx-730D (serial 1882155, fibres 1-576) and an FTBx-730C
(1723374, fibres 577-1152), both named _1550 though their lasers sit 8 nm
apart.  Pooled into one density, the ranking stopped being about connectors:
99.9% of the pairs scoring 10x or better were same-instrument against a 50%
baseline, and the displayed top 20 was 20 pairs from one meter, tied.

Two rules:

  1. Each instrument gets its own density and its own ranking.  Pairs that
     span two instruments carry no mating likelihood, because their features
     compare hardware rather than matings.
  2. The displayed list takes from each instrument in turn, since the two
     ratios are calibrated against different densities and a straight merge
     lets the louder one fill the whole list.

A single-instrument folder has exactly one group and is unchanged.  Verified
on the two labelled sets: retruetest hidden in LSC (66 same-fibre pairs among
41,328) keeps 10 in the top 10 and a median rank of 85, and the RDR4RDR5 tray
keeps true-pair ranks 2, 4, 5, 6, 10, 13.

Ties: the likelihood ratio reads bin indices, so only 24 ** len(features)
distinct values exist and the top one is shared (36 pairs on Goodland ->
Monument).  Raising the bin count breaks ties but costs discrimination
(retruetest: 10 of 66 true pairs in the top 10 at 24 bins, 7 at 400), so the
ratio stays exactly as calibrated and ties are ordered by a continuous
closeness key.  Display only.

Namespace isolation rule: the engine is only exercised through subprocesses.
"""
from __future__ import annotations

import json
import subprocess
import sys

from conftest import SECRETSAUCE_DIR


def _run(script: str, *args):
    p = subprocess.run([sys.executable, "-c", script, str(SECRETSAUCE_DIR), *args],
                       capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr[-3000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


_SCRIPT = r"""
import sys, json
sys.path.insert(0, sys.argv[1])
import numpy as np
import report_sor as RS

def mk(n, serial, seed, port_loss=0.30, port_refl=-53.0):
    rng = np.random.default_rng(seed)
    out = []
    for k in range(n):
        ev = [{'dist_km': 0.0, 'reflection': -55.0, 'is_reflective': True,
               'splice_loss': 0.0},
              {'dist_km': 1.0, 'reflection': float(port_refl + rng.normal(0, 1.2)),
               'is_reflective': True,
               'splice_loss': float(port_loss + rng.normal(0, 0.10))},
              {'dist_km': 1.2, 'reflection': 0.0, 'is_reflective': False,
               'splice_loss': 0.0}]
        out.append({'name': f'{serial}_{k:04d}', 'serial_number': serial,
                    'events': ev, 'length': 1200.0,
                    'pos': np.arange(1500) * 0.8,
                    'trace': np.zeros(1500, dtype=np.float32),
                    'pulse_samples': 3.0})
    return out

def rank(files):
    pairs = [{'a': files[i]['name'], 'b': files[j]['name']}
             for i in range(len(files)) for j in range(i + 1, len(files))]
    m = RS._mating_likelihood(files, pairs)
    return pairs, m

out = {}

# one instrument: one group, every pair scored
solo = mk(60, 'AAA', 1)
pairs, m = rank(solo)
out['solo_groups'] = m['instruments']
out['solo_cross'] = m['n_cross_instrument']
out['solo_scored'] = sum(1 for p in pairs if p['mating_lr'] is not None)
out['solo_total'] = len(pairs)

# two instruments, and the second sits at a clearly different connector level
mixed = mk(60, 'AAA', 1) + mk(60, 'BBB', 2, port_loss=0.55, port_refl=-47.0)
pairs, m = rank(mixed)
out['mixed_groups'] = sorted(m['instruments'])
out['mixed_cross'] = m['n_cross_instrument']
cross = [p for p in pairs
         if p['a'].split('_')[0] != p['b'].split('_')[0]]
out['cross_all_none'] = all(p['mating_lr'] is None for p in cross)
out['cross_count'] = len(cross)
within = [p for p in pairs if p['a'].split('_')[0] == p['b'].split('_')[0]]
out['within_all_scored'] = all(p['mating_lr'] is not None for p in within)

# the displayed list must carry both instruments
top = RS._mating_top({'mating': m, 'pairs': pairs}, n=10)
out['top_instruments'] = sorted({t.get('instrument') for t in top})
out['top_len'] = len(top)

# the solo folder's list carries no instrument label at all
pairs_s, m_s = rank(solo)
top_s = RS._mating_top({'mating': m_s, 'pairs': pairs_s}, n=10)
out['solo_top_labelled'] = any('instrument' in t for t in top_s)

# ties are ordered, not left to filename order
lrs = [p['mating_lr'] for p in pairs_s if p['mating_lr'] is not None]
top_lr = max(lrs)
tied = [p for p in pairs_s if p['mating_lr'] == top_lr]
out['n_tied'] = len(tied)
out['tie_key_present'] = all(p.get('mating_tie') is not None for p in tied)
if len(tied) > 1:
    order = [p for p in top_s if p['mating_lr'] == round(top_lr, 1)]
    keys = []
    for o in order:
        for p in tied:
            if p['a'] == o['a'] and p['b'] == o['b']:
                keys.append(p['mating_tie'])
    out['tie_order_descending'] = keys == sorted(keys, reverse=True)
else:
    out['tie_order_descending'] = True
print(json.dumps(out))
"""


def test_two_instruments_are_ranked_separately():
    r = _run(_SCRIPT)
    assert r["mixed_groups"] == ["AAA", "BBB"]
    # every pair spanning the two instruments is refused a likelihood
    assert r["cross_count"] == 60 * 60
    assert r["mixed_cross"] == r["cross_count"]
    assert r["cross_all_none"] is True
    assert r["within_all_scored"] is True


def test_one_instrument_folder_is_unchanged():
    r = _run(_SCRIPT)
    assert r["solo_groups"] == ["AAA"]
    assert r["solo_cross"] == 0
    assert r["solo_scored"] == r["solo_total"]
    # nothing to disambiguate, so no instrument label is added to the list
    assert r["solo_top_labelled"] is False


def test_displayed_list_carries_both_instruments():
    r = _run(_SCRIPT)
    assert r["top_len"] == 10
    assert r["top_instruments"] == ["AAA", "BBB"], (
        "one instrument's ratios must not crowd the other out of the list")


def test_ties_are_ordered_by_closeness():
    r = _run(_SCRIPT)
    assert r["tie_key_present"] is True
    assert r["tie_order_descending"] is True


def test_source_locks_the_calibration():
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    # The bin count is calibrated; raising it breaks ties at the cost of
    # discrimination.  Ties are handled in the sort, not by rebinning.
    assert "_MATING_NULL_BINS = 24" in src
    assert "p.get('mating_tie')" in src
    # Ratios stay exactly as calibrated: the tie key only orders equals.
    assert "-p['mating_lr'], -(p.get('mating_tie') or 0.0)" in src
