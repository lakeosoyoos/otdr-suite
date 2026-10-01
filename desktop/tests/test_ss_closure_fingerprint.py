"""The splice loss at every closure the cable passes: glass a re-plug cannot change.

The near splice reads one splice behind the panel.  A cable also passes
closures along its length and every fiber is spliced in each, so every fiber
carries its own loss at every closure.  A 10 km test span (18 shots at 5 and
10 ns, 13-18 re-shoot 1-6) has two closures; read with the near splice they
rank the six true pairs exactly top 6 of 153 on both pulse widths, where the
current engine flags none.  On a 1152-fiber 64 km job (11 closures) 25 pairs
agree at every closure to ~0.005 dB and the next pair sits at chi2 52; on an
864-fiber 97 km job (14 closures) none of 372,816 pairs agree.

What has to hold:

  1. It finds the closures itself, from the spread of the step across fibers,
     and measures each with noise from the SAME windows at closure-free spots.
  2. The same fiber shot again agrees; different fibers do not.
  3. It says how many pairs would agree by chance, so a short span with two
     closures is not read as a verdict.
  4. It never measures a connector (reflective at the closure): a re-plug
     changes that.
  5. It abstains out loud on a span with no closure or no room for one, and
     attaches nothing to pairs when it does.
  6. It never touches p_dup.

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

DZ = 1.0
N = 26000                                     # 26 km
CLOSURES = (4000.0, 9800.0, 13600.0, 18400.0, 23200.0)

def mk(name, losses, rng, noise=0.03, closures=CLOSURES, refl_at=None, n=N):
    pos = np.arange(n) * DZ
    tr = 20.0 + 0.19e-3 * pos + rng.normal(0.0, noise, n)
    tr[pos < 8.0] -= 3.0
    ev = [{'dist_km': 0.0, 'splice_loss': 0.0, 'reflection': -60.0, 'is_reflective': True}]
    for c, l in zip(closures, losses):
        # the splice sits anywhere in the tray, +-15 m of the closure
        s = c + rng.uniform(-15.0, 15.0)
        tr[pos > s] += l
        ev.append({'dist_km': s / 1000.0, 'splice_loss': float(l),
                   'reflection': (-45.0 if refl_at is not None and c == refl_at else 0.0),
                   'is_reflective': bool(refl_at is not None and c == refl_at)})
    ev.append({'dist_km': pos[-1] / 1000.0, 'splice_loss': 0.0, 'reflection': 0.0,
               'is_reflective': False})
    return {'name': name, 'trace': tr.astype(np.float32), 'pos': pos,
            'length': float(pos[-1]), 'events': ev, 'pulse_samples': 3.0,
            'timestamp': 1700000000, 'serial_number': 'X'}

def pairs_of(files):
    return [{'a': files[i]['name'], 'b': files[j]['name'], 'p_dup': 0.37}
            for i in range(len(files)) for j in range(i + 1, len(files))]

out = {}
rng = np.random.default_rng(7)
L = rng.normal(0.03, 0.07, (40, 5))
files = [mk('F%04d' % (k + 1), L[k], rng) for k in range(40)]
files.append(mk('F9001', L[4], rng))                      # F0005 shot again
files.append(mk('F9002', L[17], rng))                     # F0018 shot again
pr = pairs_of(files)
s = RS._closure_fingerprint(files, pr)
out['usable'] = s['usable']
out['note'] = s['note']
out['at'] = [c['at_m'] for c in s.get('closures', [])]
out['noise'] = [c['noise_db'] for c in s.get('closures', [])]
out['theory'] = 0.03 * float(np.sqrt(8.0 * DZ / 1150.0))
by = {(p['a'], p['b']): p for p in pr}

def cq(st, p):
    # every pair's reading lives in the folder's arrays, not on the pair
    return RS._closure_pair(st, p['a'], p['b']) or {}
q1, q2 = cq(s, by[('F0005', 'F9001')]), cq(s, by[('F0018', 'F9002')])
out['twin1'] = [q1.get(k) for k in ('closure_p', 'closure_max_sd', 'closure_k')]
out['twin2'] = [q2.get(k) for k in ('closure_p', 'closure_max_sd')]
ranked = sorted(pr, key=lambda p: -cq(s, p).get('closure_p', -1))
out['top2'] = sorted([(p['a'], p['b']) for p in ranked[:2]])
out['n_match'] = s.get('n_match')
out['expected'] = s.get('expected_chance')
out['p_dup_untouched'] = all(p['p_dup'] == 0.37 for p in pr)
t1 = by[('F0005', 'F9001')]
out['twin1_level'] = t1.get('closure_level')
out['twin1_mutual'] = t1.get('closure_mutual')
out['twin1_reason'] = RS._closure_reason(t1, s['n_readings'])
oth = by[('F0001', 'F0002')]
out['other_level'] = cq(s, oth).get('closure_level')
out['levels_main'] = sorted((p['a'], p['b']) for p in pr if cq(s, p).get('closure_level'))
# only the pairs the tab lists carry closure keys (memory: see _closure_fingerprint)
out['keyed'] = sum('closure_p' in p for p in pr)
out['listed'] = len(s['ranked'])
out['listed_first'] = (s['ranked'][0]['a'], s['ranked'][0]['b'])
out['chance_main'] = s.get('chance_likely')
out['summary_main'] = s.get('summary')
out['chance_line_main'] = s.get('chance_line')
out['meta'] = RS._closure_meta({'closure_fp': s, 'pairs': pr}) is not None

# a closure that is a connector (reflective on every fiber) is not used
conn = [mk('C%04d' % (k + 1), L[k], rng, refl_at=9800.0) for k in range(30)]
sc = RS._closure_fingerprint(conn, pairs_of(conn))
out['conn_at'] = [c['at_m'] for c in sc.get('closures', [])]
out['conn_dropped'] = [d['why'] for d in sc.get('dropped', [])]

# two closures, many fibers, no duplicates: agreement is expected by chance
L2 = rng.normal(0.03, 0.05, (60, 2))
two = [mk('T%04d' % (k + 1), L2[k], rng, noise=0.06, closures=(5000.0, 12000.0), n=20000)
       for k in range(60)]
st = RS._closure_fingerprint(two, pairs_of(two))
out['two_usable'] = st['usable']
out['two_same'] = st.get('n_same_glass')
out['two_likely'] = st.get('n_likely')
out['two_chance'] = st.get('chance_likely')
out['two_same_ok'] = st.get('same_ok')
out['two_summary'] = st.get('summary')
out['two_chance_line'] = st.get('chance_line')
out['two_match'] = st.get('n_match')
out['two_expected'] = st.get('expected_chance')

# Same-color fibers of different tubes share part of every one-way splice
# loss (a 432-fiber job): 4 tubes x 12 colors, color part 0.06 dB, own part
# small enough that cousins agree.  Many agree; none is the same glass, because
# each has several partners about as good.  (A re-shoot inside such a color
# group cannot be certified either: its cousins come just as close.  On that
# job 67 pairs agree, 0 are the same glass.)
col = rng.normal(0.0, 0.06, (12, 5))
cz = []
for tube in range(4):
    for c in range(12):
        cz.append(mk('K%04d' % (tube * 12 + c + 1), col[c] + rng.normal(0.0, 0.002, 5), rng,
                     noise=0.02))
prc = pairs_of(cz)
scz = RS._closure_fingerprint(cz, prc)
out['cz_match'] = scz.get('n_match')
out['cz_same'] = scz.get('n_same_glass')
out['cz_expected'] = scz.get('expected_chance')
out['cz_summary'] = scz.get('summary')
out['twin1_same'] = q1.get('closure_same_glass')
out['twin2_same'] = q2.get('closure_same_glass')
out['n_same'] = s.get('n_same_glass')

# Many readings (8 closures), same-color look-alikes and no repeats: pairs
# that are each other's best match but only 2-10x clear of the runner-up are
# what look-alikes do there (the 432-fiber job, 16 readings: 12 pairs at
# 2.8-5.1x, while 37 of 37 known repeats on long jobs stood 11-80x clear).
C8 = tuple(2500.0 + 2900.0 * i for i in range(8))
col8 = rng.normal(0.0, 0.06, (12, 8))
lk = []
for tube in range(4):
    for c in range(12):
        lk.append(mk('Q%04d' % (tube * 12 + c + 1), col8[c] + rng.normal(0.0, 0.004, 8), rng,
                     noise=0.02, closures=C8, n=26000))
prq = pairs_of(lk)
sq = RS._closure_fingerprint(lk, prq)
out['lk_many'] = sq.get('many_readings')
out['lk_same'] = sq.get('n_same_glass')
out['lk_likely'] = sq.get('n_likely')
out['lk_summary'] = sq.get('summary')
out['lk_reason'] = [RS._closure_reason(p, sq['n_readings']) for p in sq['ranked']
                    if p.get('closure_level') == 'likely'][:1]

flat = [mk('N%04d' % (k + 1), (0.0, 0.0, 0.0), rng) for k in range(20)]
prf = pairs_of(flat)
sf = RS._closure_fingerprint(flat, prf)
out['flat_usable'] = sf['usable']
out['flat_note'] = sf['note']
out['flat_keys'] = any('closure_p' in p for p in prf)

short = [mk('S%04d' % (k + 1), (), rng, closures=(), n=1500) for k in range(20)]
ss = RS._closure_fingerprint(short, pairs_of(short))
out['short_usable'] = ss['usable']
out['short_note'] = ss['note']

few = RS._closure_fingerprint(files[:4], [])
out['few_usable'] = few['usable']
print(json.dumps(out))
"""


def test_finds_the_closures_and_their_noise():
    r = _run(_SCRIPT)
    assert r["usable"] is True, r["note"]
    assert r["note"].startswith("Closure fingerprint: usable")
    assert len(r["at"]) == 5, r["at"]
    for got, want in zip(r["at"], (4000.0, 9800.0, 13600.0, 18400.0, 23200.0)):
        assert abs(got - want) <= 25.0, r["at"]
    for n in r["noise"]:
        assert 0.4 < n / r["theory"] < 2.5, (r["noise"], r["theory"])


def test_the_same_fiber_shot_again_agrees_and_ranks_first():
    r = _run(_SCRIPT)
    p1, sd1, k1 = r["twin1"]
    p2, sd2 = r["twin2"]
    assert k1 == 5
    assert p1 > 0.001 and sd1 < 4.0, r["twin1"]
    assert p2 > 0.001 and sd2 < 4.0, r["twin2"]
    assert r["top2"] == [["F0005", "F9001"], ["F0018", "F9002"]], r["top2"]
    # five well-separated closures: chance agreement is rare
    assert r["expected"] < 0.5, r["expected"]
    assert 2 <= r["n_match"] <= 4, r["n_match"]
    assert r["meta"] is True


def test_only_the_listed_pairs_carry_closure_keys():
    # A dozen keys on each of 662,976 pairs took 700 MB on a 1152-fiber job:
    # every pair's reading stays in the folder's arrays (read through
    # _closure_pair) and only the pairs the tab and the manifest list carry keys.
    r = _run(_SCRIPT)
    assert r["keyed"] == r["listed"] == 50, (r["keyed"], r["listed"])
    assert tuple(r["listed_first"]) in {("F0005", "F9001"), ("F0018", "F9002")}, r["listed_first"]


def test_same_glass_needs_a_partner_nothing_else_comes_near():
    r = _run(_SCRIPT)
    # a re-shoot of an independent fiber is the same glass
    assert r["twin1_same"] is True and r["twin2_same"] is True
    assert r["n_same"] == 2, r["n_same"]
    # same-color cousins agree, far above what independent closures predict,
    # and not one of them is called the same glass
    assert r["cz_match"] > 20, r["cz_match"]
    assert r["cz_expected"] < 0.2 * r["cz_match"], (r["cz_match"], r["cz_expected"])
    assert r["cz_same"] == 0, r["cz_same"]
    # nothing found: the top line says so, with no counts and no chance clause
    assert "No pair is likely the same fiber." in r["cz_summary"], r["cz_summary"]
    assert "by chance" not in r["cz_summary"], r["cz_summary"]


def test_a_tech_can_read_which_pairs_are_the_same_fiber():
    # The Closures tab's two levels (2026-09-30): LIKELY THE SAME FIBER when each
    # file is the other's best match, they agree as one fiber shot twice and the
    # runner-up is 2x worse; THE SAME FIBER at 10x, where the folder can make it.
    r = _run(_SCRIPT)
    assert r["twin1_level"] == "same", r["twin1_level"]
    assert r["twin1_mutual"] is True
    assert "each is the other's best match" in r["twin1_reason"], r["twin1_reason"]
    assert r["other_level"] is None
    assert r["levels_main"] == [["F0005", "F9001"], ["F0018", "F9002"]], r["levels_main"]
    assert r["chance_main"] < 0.1, r["chance_main"]
    assert "2 pair(s) the same fiber" in r["summary_main"], r["summary_main"]
    # the chance count is on the tab, not in the top line
    assert "by chance" not in r["summary_main"], r["summary_main"]
    assert "almost no pair would reach this list by chance" in r["chance_line_main"], r["chance_line_main"]


def test_few_readings_never_claim_the_same_fiber():
    # 60 unrelated fibers, two closures, no duplicates: pairs reach 'likely' by
    # chance, and the tab must say so and never print 'the same fiber'.
    r = _run(_SCRIPT)
    assert r["two_same_ok"] is False
    assert r["two_same"] == 0, r["two_same"]
    assert r["two_likely"] <= 1.5 * r["two_chance"], (r["two_likely"], r["two_chance"])
    assert "as many as were found" in r["two_chance_line"], r["two_chance_line"]
    assert "by chance" not in r["two_summary"], r["two_summary"]
    # the top line names only what was found: no zero count, no 'same fiber'
    assert "pair(s) the same fiber" not in r["two_summary"], r["two_summary"]
    assert "0 pair(s)" not in r["two_summary"], r["two_summary"]


def test_many_readings_call_a_weak_best_match_a_look_alike_not_a_repeat():
    r = _run(_SCRIPT)
    assert r["lk_many"] is True
    assert r["lk_same"] == 0, r["lk_same"]
    assert r["lk_likely"] >= 1, r["lk_likely"]
    assert "alike but not as clear as a repeat" in r["lk_summary"], r["lk_summary"]
    assert "likely the same fiber" not in r["lk_summary"], r["lk_summary"]
    assert "same color in different tubes" in r["lk_summary"], r["lk_summary"]
    assert "normally stands at least 10 times clear" in r["lk_reason"][0], r["lk_reason"]


def test_says_when_agreement_is_expected_by_chance():
    # Two closures and 1,770 pairs of different fibers: many agree, and the
    # engine must say that about as many are expected by chance.
    r = _run(_SCRIPT)
    assert r["two_usable"] is True
    assert r["two_match"] > 5, r["two_match"]
    assert 0.4 < r["two_expected"] / r["two_match"] < 2.5, (r["two_match"], r["two_expected"])


def test_never_measures_a_connector():
    r = _run(_SCRIPT)
    assert all(abs(a - 9800.0) > 100.0 for a in r["conn_at"]), r["conn_at"]
    assert any("connector" in w for w in r["conn_dropped"]), r["conn_dropped"]


def test_abstains_out_loud():
    r = _run(_SCRIPT)
    assert r["flat_usable"] is False
    assert r["flat_note"].startswith("Closure fingerprint: NOT USABLE")
    assert r["flat_keys"] is False, "an abstaining folder must attach nothing to pairs"
    assert r["short_usable"] is False
    assert "leaves no room for a closure" in r["short_note"], r["short_note"]
    assert r["few_usable"] is False


def test_it_never_touches_the_verdict():
    r = _run(_SCRIPT)
    assert r["p_dup_untouched"] is True
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    i = src.index("def _closure_fingerprint(files, pairs, near_splice=None):")
    body = src[i:src.index("\ndef ", i + 10)]
    for use in ("['p_dup']", "'p_dup'", "p_dup =", "p_dup["):
        assert use not in body, "the closure check must never touch the verdict"
    # called once, through the display-only guard; the tab only when usable
    assert ("closure_fp = _display_only('Closure fingerprint', 'closure_', pairs,\n"
            "                               _closure_fingerprint, files, pairs, near_splice)") in src
    assert "('Closures', 'closure_fp', _closures_sheet)" in src
    assert "Pairs Most Likely to Be the Same Fiber" in src
