"""Event identity: the duplicate signal that survives an unplug and re-plug.

The duplicate that actually happens in the field is a tech unplugging the
jumper, plugging it back in, and shooting again.  That destroys the connector
mating, so the mating ranking is blind to it by construction -- on Dinwiddie's
48 known re-plugged pairs the glass fingerprint scores AUC 0.488, chance.  Only
glass survives, and mid-span splices are glass.

Three things this had to get right, two of them learned by building the wrong
thing first:

  1. Positions are NOT identity.  Every fibre in a cable passes the same
     closures, so matching positions reads the cable (Romero<->Tucu, position
     only: same fibre 0.615 vs different 0.556, AUC 0.59).  The per-fibre
     splice LOSSES identify.
  2. Agreement is only evidence in proportion to how surprising it is.  A fixed
     +/-0.05 dB tolerance confirmed PLACHE0244~0245, which differ by 1.44 dB of
     raw trace sigma -- on a well-built span nearly every splice sits between
     0.02 and 0.14 dB, so a fixed window matches almost anything.  Each closure
     is judged against the folder's OWN spread there instead, and 244~245 drops
     to 2.03, below that folder's p99.9.
  3. A pair must beat its OWN comparison class.  Fibres a multiple of 12 apart
     sit in the same ribbon column and mass fusion repeats its per-position
     behaviour across ribbons.  On Niland that group is 1% of pairs, its tail
     runs to 3.669 against 3.284 for everything else, and against one pooled
     null it supplied 100% of the confirmations.  Per-class bars restore Niland
     to its exact pre-change output.

Calibrated on the only true same-direction repeat on disk: PLACHE shot
2026-07-08 and again 2026-07-16, the same 1,152 fibres from the same end eight
days apart with different reels between rounds.  Null median 0.98, p99.9 2.31;
same-fibre median 3.80, p10 3.04.  The null is scale-free -- p99.9 ran
2.32/2.41/2.35/2.52 and max 2.80/3.05/3.21/2.97 on PLACHE, Niland, Eugene and
Romero-Tucu -- which is what makes an absolute floor legitimate here where it
was not for the speckle gate.

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

def mk(name, closures, losses, reel=1000.0, end=100000.0):
    ev = [{'dist_km': 0.0, 'splice_loss': 0.0},
          {'dist_km': reel / 1000.0, 'splice_loss': 0.3}]
    for c, l in zip(closures, losses):
        ev.append({'dist_km': (reel + c) / 1000.0, 'splice_loss': float(l)})
    ev.append({'dist_km': end / 1000.0, 'splice_loss': 0.0})
    return {'name': name, 'events': ev}

rng = np.random.default_rng(4)
CLOS = [5000 * (i + 1) for i in range(14)]          # 14 closures
def folder(n, prefix='F', spread=0.25, start=1):
    out = []
    for k in range(n):
        losses = rng.normal(0.30, spread, len(CLOS))
        out.append(mk(f'{prefix}{start + k:04d}', CLOS, losses))
    return out

def pairs_of(files):
    return [{'a': files[i]['name'], 'b': files[j]['name']}
            for i in range(len(files)) for j in range(i + 1, len(files))]

out = {}

# ── a folder with no interior structure must abstain, out loud ────────────
flat = [mk(f'S{k:04d}', [], [], end=5000.0) for k in range(40)]
pr = pairs_of(flat)
s = RS._event_identity(flat, pr)
out['flat_usable'] = s['usable']
out['flat_note_says_not_usable'] = 'NOT USABLE' in s['note']
out['flat_scores_none'] = all(p['event_id'] is None for p in pr)

# ── a real re-plug: the same fibre's losses, re-measured with jitter ──────
files = folder(80)
twin_src = files[3]
twin = mk('F9001', CLOS,
          [l + rng.normal(0, 0.01) for l in
           [e['splice_loss'] for e in twin_src['events'][2:-1]]])
files2 = files + [twin]
pr2 = pairs_of(files2)
s2 = RS._event_identity(files2, pr2)
out['usable'] = s2['usable']
out['median_events'] = s2['median_events']
scored = {(p['a'], p['b']): p['event_id'] for p in pr2}
out['twin_score'] = scored[(twin_src['name'], 'F9001')]
others = [v for k, v in scored.items() if 'F9001' not in k]
out['null_median'] = float(np.median(others))
out['null_max'] = float(np.max(others))
out['twin_confirmed'] = out['twin_score'] >= [p for p in pr2
    if p['a'] == twin_src['name'] and p['b'] == 'F9001'][0]['event_id_bar']

# ── a fixed tolerance would confirm near-zero agreement; this must not ────
tight = []
for k in range(60):
    losses = rng.normal(0.05, 0.012, len(CLOS))     # every splice near zero
    tight.append(mk(f'T{k:04d}', CLOS, losses))
prt = pairs_of(tight)
st = RS._event_identity(tight, prt)
tv = np.array([p['event_id'] for p in prt])
out['tight_confirmed'] = int(st['n_confirmed'])
out['tight_max_score'] = float(tv.max())

# ── ribbon-column pairs get their own, higher bar ─────────────────────────
rib = []
base = {}
for k in range(72):
    col = k % 12
    if col not in base:
        base[col] = rng.normal(0.30, 0.25, len(CLOS))
    # same ribbon column => shared per-position behaviour at every closure
    rib.append(mk(f'R{k + 1:04d}', CLOS, base[col] + rng.normal(0, 0.02, len(CLOS))))
prr = pairs_of(rib)
sr = RS._event_identity(rib, prr)
out['bar_other'] = sr['bar']
out['bar_ribbon'] = sr['bar_ribbon_column']
out['ribbon_bar_is_higher'] = sr['bar_ribbon_column'] > sr['bar']
print(json.dumps(out))
"""


def test_abstains_when_the_span_has_no_interior_structure():
    r = _run(_SCRIPT)
    assert r["flat_usable"] is False
    assert r["flat_note_says_not_usable"] is True
    assert r["flat_scores_none"] is True, (
        "a span with no closures must score nothing, not zero")


def test_a_replugged_reshoot_is_confirmed():
    r = _run(_SCRIPT)
    assert r["usable"] is True
    assert r["median_events"] >= 9
    # the re-shoot stands far above the folder's own spread
    assert r["twin_score"] > r["null_max"], (r["twin_score"], r["null_max"])
    assert r["twin_score"] > 3.0
    assert r["twin_confirmed"] is True


def test_agreement_on_uniformly_tight_losses_is_not_evidence():
    # Every splice within 0.012 dB of every other: a fixed-tolerance score
    # would call the whole folder duplicates.  Self-calibration must not.
    r = _run(_SCRIPT)
    assert r["tight_confirmed"] == 0, (
        f"confirmed {r['tight_confirmed']} pairs on a folder where every splice "
        f"is identical by construction (max score {r['tight_max_score']:.2f})")


def test_ribbon_column_pairs_are_judged_against_their_own_class():
    r = _run(_SCRIPT)
    assert r["ribbon_bar_is_higher"] is True, (
        f"same-ribbon-column bar {r['bar_ribbon']:.2f} must exceed the general "
        f"bar {r['bar_other']:.2f}; mass fusion repeats per-position behaviour")


def test_source_locks_the_safeguards():
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    # It may only ever RAISE p_dup, like the raw-identity short-circuit.
    assert "np.maximum(p_dup, _EVENT_ID_CONFIRM_P)" in src
    # Per-class bars, not one pooled null.
    assert "_EVENT_ID_RIBBON" in src
    assert "ribbon_column" in src
    # Judged against the folder's own spread per closure, never a fixed window.
    assert "np.searchsorted(q[i], d)" in src
