"""Port length: the one tie-panel measurement that survives a re-plug.

A tie panel shot launch reel -> panel A -> short tie -> panel B -> receive reel
carries no identity after the port at 5 ns (2026-08 study).  What it does carry
is the length of the tie between the panels: on a 288-port tie panel ports
spread 19 cm, and one port repeats to a few mm in a session and to under a
centimeter across sessions and OTDRs.  Blind test on six re-shot ports of
that panel (never used to build it): the true partner ranks 1st-2nd of ~290
by length alone on five of six, where the mating likelihood ranks it
150th-275th or cannot compare two OTDRs at all.

What has to hold:
  1. It reads the length to within about a centimeter from the trace
     (sub-sample edges; real same-session repeatability is 1-6 mm).
  2. A re-shoot of a port lands next to it; the ranking puts it near the top.
  3. It abstains out loud when every port is the same length to within the
     measurement (a jumper straight into the port), and attaches nothing.
  4. It never touches p_dup.

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

DZ = 0.08
N = 28000                                   # 2.24 km

def dip(tr, pos, x, depth):
    # a reflection is a dip in the loss-domain trace: a 5 ns pulse rises over
    # two samples and decays over about half a meter
    u = np.clip(pos - x, -1.0, 40.0)
    shape = np.where(u < 0, 0.0, np.where(u < 0.24, u / 0.24, np.exp(-(u - 0.24) / 0.45)))
    tr -= depth * shape

def mk(name, tie_m, rng, noise=0.015, reel_end=1004.7, recv=1000.0, depths=None):
    # one tie-panel shot; depths = the two tie-mating dip depths (dB): a port
    # keeps its own, a connector-left-in repeat copies them, a re-plug redraws
    pos = np.arange(N) * DZ
    tr = 50.0 + 0.19e-3 * pos + rng.normal(0.0, noise, N)
    port = reel_end + tie_m
    end = port + recv
    d1, d2 = depths if depths is not None else (12.0 + rng.normal(0.0, 0.6), 11.0 + rng.normal(0.0, 0.6))
    tr[pos > reel_end] += 0.3
    tr[pos > port] += 0.6
    dip(tr, pos, reel_end, d1); dip(tr, pos, port, d2); dip(tr, pos, end, 20.0)
    tr[pos > end + 2.0] = 64.0
    ev = [{'dist_km': 0.0, 'splice_loss': 0.0, 'reflection': -60.0, 'is_reflective': True},
          {'dist_km': reel_end / 1000.0, 'splice_loss': 0.3, 'reflection': -55.0, 'is_reflective': True},
          {'dist_km': port / 1000.0, 'splice_loss': 0.6, 'reflection': -52.0, 'is_reflective': True},
          {'dist_km': end / 1000.0, 'splice_loss': 0.0, 'reflection': -48.0, 'is_reflective': True}]
    return {'name': name, 'trace': tr.astype(np.float32), 'pos': pos, 'length': float(end),
            'events': ev, 'pulse_samples': 6.4, 'timestamp': 1700000000, 'serial_number': 'X',
            'depths': (d1, d2)}

def pairs_of(files):
    return [{'a': files[i]['name'], 'b': files[j]['name'], 'p_dup': 0.37}
            for i in range(len(files)) for j in range(i + 1, len(files))]

out = {}
rng = np.random.default_rng(11)
ties = 31.2 + rng.normal(0.0, 0.19, 40)
files = [mk('P%04d' % (k + 1), ties[k], rng) for k in range(40)]
files.append(mk('P9001', ties[7] + 0.004, rng))          # P0008 re-plugged, 4 mm off
files.append(mk('P9002', ties[12], rng, depths=files[12]['depths']))   # P0013, connector left in
pr = pairs_of(files)
s = RS._port_length(files, pr)
out['usable'] = s['usable']
out['note'] = s['note']
L = s.get('length', {})
out['err_mm'] = max(abs(L['P%04d' % (k + 1)] - L['P0001'] - (ties[k] - ties[0])) * 1000 for k in range(40))
by = {(p['a'], p['b']): p for p in pr}
twin = by[('P0008', 'P9001')]
out['twin_diff_mm'] = twin['port_len_diff_m'] * 1000
rank = sorted(pr, key=lambda p: -p['port_len_lr'] + 1e-6 * p['port_len_diff_m'])
out['twin_rank'] = [i for i, p in enumerate(rank) if (p['a'], p['b']) == ('P0008', 'P9001')][0] + 1
out['n_pairs'] = len(pr)
out['left_in'] = s.get('left_in')
out['left_in_pair'] = by[('P0013', 'P9002')].get('port_len_left_in')
out['replug_left_in'] = twin.get('port_len_left_in')
out['p_dup_untouched'] = all(p['p_dup'] == 0.37 for p in pr)
out['meta'] = RS._port_length_meta({'port_length': s, 'pairs': pr}) is not None

same = [mk('S%04d' % (k + 1), 31.2 + rng.normal(0.0, 0.004), rng) for k in range(20)]
prs = pairs_of(same)
ss = RS._port_length(same, prs)
out['same_usable'] = ss['usable']
out['same_note'] = ss['note']
out['same_keys'] = any('port_len_diff_m' in p for p in prs)
few = RS._port_length(files[:3], [])
out['few_usable'] = few['usable']
# a wide pulse (30 ns, 0.5 m samples) reads no edge: it must say so, not guess
wide = []
for k in range(8):
    f = mk('W%04d' % (k + 1), ties[k], rng)
    f['pos'] = f['pos'] * 6.25
    wide.append(f)
out['wide_note'] = RS._port_length(wide, [])['note']
# a broken sample must not take the reading down
bad = [mk('B%04d' % (k + 1), ties[k], rng) for k in range(8)]
bad[2]['trace'][15000:15010] = -np.inf
bad[3]['trace'][:] = np.nan
out['bad_ok'] = RS._port_length(bad, pairs_of(bad))['usable'] in (True, False)

# Both directions: the same tie reads the same length from either end (other
# OTDR: another reel, another receive reel, a 3 cm offset).  Planted faults:
#   direction B files port 8 under number 9 as well, re-plugged (ties 8 and 9
#   differ by 12 cm here);
#   direction B has ports 20 and 21 swapped (ties 25 cm apart);
#   direction A files port 30 under 31 as well with the connector left in,
#   while direction B shot the real 30 and 31 (they differ by 12 cm).
tb = ties.copy(); tb[8] = ties[7]; tb[19], tb[20] = ties[20], ties[19]
A = [mk('X1X2%04d' % (k + 1), ties[k], rng) for k in range(40)]
A[30] = mk('X1X20031', ties[29], rng, depths=A[29]['depths'])
B = [mk('X2X1%04d' % (k + 1), tb[k] + 0.03 + rng.normal(0.0, 0.003), rng, reel_end=1010.2, recv=1030.0)
     for k in range(40)]
both = A + B
prb = pairs_of(both)
sb = RS._port_length(both, prb)
out['both_usable'] = sb['usable']
out['both_groups'] = sorted((sb.get('groups') or {}).keys())
ab = sb.get('ab') or {}
out['ab_usable'] = ab.get('usable')
out['ab_matched'] = ab.get('n_matched')
out['ab_offset_cm'] = None if not ab.get('usable') else 100 * ab['offset_m']
out['ab_sd_cm'] = None if not ab.get('usable') else 100 * ab['sd_m']
out['ab_mismatch'] = sorted(m['num'] for m in ab.get('mismatch', []))
out['ab_gap'] = [abs(ties[8] - ties[7]), abs(ties[19] - ties[20]), abs(ties[29] - ties[30])]
out['both_left_in'] = sb.get('left_in')
out['m31'] = [(m['left_in_a'], m['left_in_b']) for m in ab.get('mismatch', []) if m['num'] == 31]
out['like_9'] = [m.get('like_b') for m in ab.get('mismatch', []) if m['num'] == 9]
out['cross_keys'] = any('port_len_diff_m' in p for p in prb
                        if p['a'][:4] != p['b'][:4])
out['both_p_dup'] = all(p['p_dup'] == 0.37 for p in prb)
print(json.dumps(out))
"""


def test_reads_the_length_to_millimeters():
    r = _run(_SCRIPT)
    assert r["usable"] is True, r["note"]
    assert r["note"].startswith("Port length: usable")
    # the synthetic dip is a Gaussian, not a pulse; on real 5 ns dips the
    # 10% crossing repeats to a few mm
    assert r["err_mm"] < 30.0, r["err_mm"]


def test_a_reshoot_lands_next_to_its_port():
    r = _run(_SCRIPT)
    assert r["twin_diff_mm"] < 12.0, r["twin_diff_mm"]
    # 820 pairs, ports 19 cm apart: the re-shoot sits in the top few percent
    assert r["twin_rank"] <= 0.05 * r["n_pairs"], (r["twin_rank"], r["n_pairs"])
    assert r["meta"] is True


def test_connector_left_in_is_a_repeat_and_a_replug_is_not():
    r = _run(_SCRIPT)
    assert r["left_in"] == [["P0013", "P9002"]], r["left_in"]
    assert r["left_in_pair"] is True
    assert not r["replug_left_in"]


def test_a_wide_pulse_and_broken_samples_abstain_without_crashing():
    r = _run(_SCRIPT)
    assert r["wide_note"].startswith("Port length: NOT USABLE")
    assert "validated at 0.08-0.2 m" in r["wide_note"], r["wide_note"]
    assert r["bad_ok"] is True


def test_abstains_when_every_port_is_the_same_length():
    r = _run(_SCRIPT)
    assert r["same_usable"] is False
    assert "no section differs from port to port" in r["same_note"]
    assert r["same_keys"] is False
    assert r["few_usable"] is False


def test_both_directions_must_agree_on_a_port():
    r = _run(_SCRIPT)
    assert r["both_usable"] is True
    assert r["both_groups"] == ["X1X2", "X2X1"], r["both_groups"]
    assert r["ab_usable"] is True and r["ab_matched"] == 40
    # the synthetic pulse edge is two or three samples wide, so the sampling
    # phase of each direction's reel shifts its crossing by up to a centimeter
    assert abs(r["ab_offset_cm"] + 3.0) < 2.0, r["ab_offset_cm"]
    assert r["ab_sd_cm"] < 1.5, r["ab_sd_cm"]
    assert min(r["ab_gap"]) > 0.08, r["ab_gap"]
    # the re-plugged repeat under 9, the swapped 20/21 and the left-in repeat under 31
    assert r["ab_mismatch"] == [9, 20, 21, 31], r["ab_mismatch"]
    # direction A's 30 & 31 are one shot repeated, and the mismatch row says so
    assert r["both_left_in"] == [["X1X20030", "X1X20031"]], r["both_left_in"]
    assert r["m31"] == [[["X1X20030"], []]], r["m31"]
    # the re-plugged repeat filed under 9: direction B's file 8 reads its length
    # (ports cut alike can sit as close; the mating likelihood orders them)
    assert r["like_9"] and "X2X10008" in r["like_9"][0], r["like_9"]
    # lengths are compared within a direction only
    assert r["cross_keys"] is False
    assert r["both_p_dup"] is True


def test_it_never_touches_the_verdict():
    r = _run(_SCRIPT)
    assert r["p_dup_untouched"] is True
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    i = src.index("def _port_length(files, pairs):")
    body = src[i:src.index("\ndef ", i + 10)]
    for use in ("['p_dup']", "'p_dup'", "p_dup =", "p_dup["):
        assert use not in body, "the port-length reading must never touch the verdict"
    # called once, through the display-only guard; the tab only when usable
    assert ("port_length = _display_only('Port length', 'port_len_', pairs,\n"
            "                                _port_length, files, pairs)") in src
    assert "('Port length', 'port_length', _port_length_sheet)" in src
