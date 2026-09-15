"""The splice behind the panel: the glass a re-plug cannot change.

Goodland->Monument is 5 km at 10 ns with a clean 4 km trunk, and eleven ways of
reading it for a duplicate all came back empty.  What every FEC span on disk
DOES carry is one splice a few tens of metres behind the panel, where the
pigtail meets the cable (Goodland 55 m, Monument 43 m, Ancho->Duran 87 m,
Duran->Ancho 84 m past the port).  Its loss varies from fibre to fibre by about
0.08-0.11 dB and one shot measures it to 0.011-0.019 dB.  It is glass, so an
unplug and re-plug leaves it alone: Goodland 0012 and 0841, re-shot about 7 h
later with the connector redone (0012 on another meter), repeat it within 0.5
and 1.0 sd while their connector losses moved 0.47 and 0.41 dB.

The tech's two reported duplicates, Goodland 350/351 and Monument 596/597,
differ by 3.8 and 3.7 sd; shots of one fibre differ that much in 0.26% and
0.07% of cases.

What has to hold:

  1. It measures the splice, with noise taken from the SAME window geometry at
     splice-free stretches.  (The first estimate used a reference window ten
     times longer that also straddled the splice.)
  2. It abstains out loud where there is no shared splice, and when the pulse
     puts the splice inside the connector dead zone.
  3. It never measures a CONNECTOR.  On a reel + jumper tray the step past the
     first connector is the port mating, which a re-plug changes; measuring it
     would veto the very duplicates this is for.
  4. It never touches p_dup.  35-60% of different-fibre pairs also agree within
     3 sd, so agreement proves nothing; only a large difference is evidence.

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

DZ = 0.32
def mk(name, splice_loss, rng, port=1005.0, off=55.0, n=15600, noise=0.015,
       pulse_samples=3.2, connector_step=False):
    pos = np.arange(n) * DZ
    tr = 20.0 + 0.19e-3 * pos + rng.normal(0.0, noise, n)
    tr[(pos > port - 1.0) & (pos < port + 8.0)] -= 3.0     # connector + dead zone
    ev = [{'dist_km': 0.0, 'splice_loss': 0.0, 'reflection': -60.0,
           'is_reflective': True},
          {'dist_km': port / 1000.0, 'splice_loss': 0.1, 'reflection': -55.0,
           'is_reflective': True}]
    if connector_step:
        # reel connector at `port`, a 31 m jumper, then the panel port: the
        # only step is the port MATING, which a re-plug changes
        tr[pos > port + 31.0] += splice_loss
        tr[(pos > port + 30.0) & (pos < port + 38.0)] -= 3.0
        ev.append({'dist_km': (port + 31.0) / 1000.0, 'splice_loss': float(splice_loss),
                   'reflection': -57.0, 'is_reflective': True})
    else:
        tr[pos > port + off] += splice_loss
    ev.append({'dist_km': pos[-1] / 1000.0, 'splice_loss': 0.0, 'reflection': 0.0,
               'is_reflective': False})
    return {'name': name, 'trace': tr.astype(np.float32), 'pos': pos,
            'length': float(pos[-1]), 'events': ev, 'pulse_samples': pulse_samples,
            'timestamp': 1700000000, 'serial_number': 'X'}

def pairs_of(files):
    return [{'a': files[i]['name'], 'b': files[j]['name'], 'p_dup': 0.37}
            for i in range(len(files)) for j in range(i + 1, len(files))]

out = {}
rng = np.random.default_rng(3)
losses = rng.normal(0.09, 0.09, 40)
files = [mk('F%04d' % (k + 1), losses[k], rng) for k in range(40)]
files.append(mk('F9001', losses[2], rng))                 # F0003 shot again
pr = pairs_of(files)
s = RS._near_splice(files, pr)
out['usable'] = s['usable']
out['note_usable'] = s['note'].startswith('Near splice: usable')
out['offset'] = s.get('offset_m')
out['noise'] = s.get('noise_db')
out['theory'] = 0.015 * float(np.sqrt(DZ / 35.0 + DZ / 400.0))
by = {(p['a'], p['b']): p for p in pr}
out['twin_sd'] = by[('F0003', 'F9001')]['splice_diff_sd']
i, j = int(np.argmin(losses)), int(np.argmax(losses))
a, b = sorted(['F%04d' % (i + 1), 'F%04d' % (j + 1)])
out['far_sd'] = by[(a, b)]['splice_diff_sd']
out['far_err_db'] = abs(by[(a, b)]['splice_diff_db'] - float(abs(losses[i] - losses[j])))
out['p_dup_untouched'] = all(p['p_dup'] == 0.37 for p in pr)
out['same_pct_zero'] = RS._near_splice_same_pct(s, 0.0)
out['same_pct_huge'] = RS._near_splice_same_pct(s, 10.0)

flat = [mk('N%04d' % (k + 1), 0.0, rng) for k in range(30)]
prf = pairs_of(flat)
sf = RS._near_splice(flat, prf)
out['flat_usable'] = sf['usable']
out['flat_note'] = sf['note']
out['flat_keys'] = any('splice_diff_sd' in p for p in prf)

tray = [mk('T%04d' % (k + 1), float(rng.normal(0.6, 0.1)), rng, connector_step=True)
        for k in range(20)]
prt = pairs_of(tray)
st = RS._near_splice(tray, prt)
out['tray_usable'] = st['usable']
out['tray_keys'] = any('splice_diff_sd' in p for p in prt)

longp = [mk('L%04d' % (k + 1), losses[k], rng, pulse_samples=400.0) for k in range(20)]
sl = RS._near_splice(longp, pairs_of(longp))
out['long_usable'] = sl['usable']
out['long_note'] = sl['note']

few = RS._near_splice(files[:4], [])
out['few_usable'] = few['usable']
print(json.dumps(out))
"""


def test_measures_the_splice_and_its_noise():
    r = _run(_SCRIPT)
    assert r["usable"] is True and r["note_usable"] is True
    assert abs(r["offset"] - 55.0) <= 4.0, r["offset"]
    # noise read off identical windows at splice-free stretches, not guessed
    assert 0.5 < r["noise"] / r["theory"] < 1.6, (r["noise"], r["theory"])
    # the most different pair is measured to within a few noise units
    assert r["far_err_db"] < 0.01, r["far_err_db"]
    assert r["far_sd"] > 20.0, r["far_sd"]


def test_the_same_fibre_shot_again_agrees():
    r = _run(_SCRIPT)
    assert r["twin_sd"] < 4.0, r["twin_sd"]


def test_it_never_touches_the_verdict():
    r = _run(_SCRIPT)
    assert r["p_dup_untouched"] is True
    assert r["same_pct_zero"] == 100.0
    assert r["same_pct_huge"] == 0.0


def test_abstains_where_there_is_no_shared_splice():
    r = _run(_SCRIPT)
    assert r["flat_usable"] is False
    assert r["flat_note"].startswith("Near splice: NOT USABLE")
    assert r["flat_keys"] is False, "an abstaining folder must attach nothing to pairs"
    assert r["few_usable"] is False


def test_never_measures_a_connector():
    # Reel + jumper + panel port: the step past the first connector is the port
    # mating.  Anchoring on the panel port leaves nothing to measure past it.
    r = _run(_SCRIPT)
    assert r["tray_usable"] is False
    assert r["tray_keys"] is False


def test_a_long_pulse_abstains():
    r = _run(_SCRIPT)
    assert r["long_usable"] is False
    assert "pulse" in r["long_note"]


def test_source_locks_the_safeguards():
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    i = src.index("def _near_splice(files, pairs):")
    body = src[i:src.index("\ndef ", i + 10)]
    for use in ("['p_dup']", "'p_dup'", "p_dup =", "p_dup["):
        assert use not in body, "the near-splice check must never touch the verdict"
    assert "_mating_port_event(ev)" in src[src.index("def _near_splice_prep("):i], (
        "anchor on the panel PORT, or a reel+jumper tray measures a connector")
    assert "a connector, not a splice" in body
    assert "near_splice = _near_splice(files, pairs)" in src
    assert "if _ns.get('usable'):\n        ws = wb.create_sheet('Near splice')" in src
    # The pairs it lists as duplicates must be exactly the Confirmed duplicates
    # sheet's (> 0.5).  Demoted pairs sit AT 0.5 (LEN_CAP): on the Goodland Finals
    # a >= test listed 57 of them on a folder with zero confirmed duplicates.
    assert "[p for p in pairs if p['p_dup'] > 0.5]" in src
    assert "if p.get('p_dup', 0.0) > 0.5:\n                listed.append((p, 'confirmed duplicate'))" in src
    assert ">= 0.5:\n                listed.append" not in src
