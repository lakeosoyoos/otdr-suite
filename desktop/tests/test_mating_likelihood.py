"""Mating likelihood: a calibrated RANKING for folders the fingerprint cannot judge,
plus a per-folder confidence band.

The field duplicate on a short panel is two consecutive shots of one port with
the jumper never moved.  The glass is unmeasurable at 5 ns, but the connector
matings are identical between the two shots and differ between ports.  Measured
on retruetest (one jumper x12) vs LSC1->LSC6 (288 fibres, same instrument),
against the honest null of consecutive ports shot seconds apart: out-of-fold
AUC 0.988, 8 of the top 10 true, label shuffle 0.49, instrument-state-only
features 0.51.  The likelihood ratio per feature is a half-normal same-port
density (scale measured on the true pairs) over the FOLDER'S OWN quantile
density, so it self-calibrates to the folder's connector population.

Display only.  p_dup, regimes and verdicts do not move.  The manifest carries
'mating_p' / 'mating_lr' per pair and a 'confidence' band per folder
(additive keys).  Namespace isolation rule: engine only via subprocess.
"""
from __future__ import annotations

import json
import subprocess
import sys

from conftest import SECRETSAUCE_DIR, REPO_ROOT, run_secretsauce, mixed_fixture_dir


def _run(script: str):
    p = subprocess.run([sys.executable, "-c", script, str(SECRETSAUCE_DIR)],
                       capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr[-3000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


_SYNTH = r"""
import sys, json
sys.path.insert(0, sys.argv[1])
import numpy as np
import report_sor as RS

def ev(l0, r0, l1, r1, rE):
    return [{'splice_loss': l0, 'reflection': r0, 'dist_km': 0.0},
            {'splice_loss': l1, 'reflection': r1, 'dist_km': 0.0315},
            {'splice_loss': 0.0, 'reflection': rE, 'dist_km': 1.04}]

def folder(n, seed=1, plant=True):
    rng = np.random.default_rng(seed)
    files, t = [], 1_000_000
    for k in range(n):
        # different ports: connector values spread like LSC (sd ~0.06 dB loss, ~1.5 dB refl)
        files.append({'name': f'P{k:04d}', 'timestamp': t + 60 * k,
                      'events': ev(-0.17 + rng.normal(0, 0.06), -58 + rng.normal(0, 1.5),
                                   0.6 + rng.normal(0, 0.06), -58 + rng.normal(0, 1.5),
                                   -47.5 + rng.normal(0, 0.5))})
    if plant:
        # the same port shot twice without moving the jumper: matings identical
        # to within acquisition repeatability (retruetest never-unplugged scales)
        base = files[min(7, n - 1)]['events']
        files.append({'name': f'P{min(7, n - 1):04d}_again', 'timestamp': t + 60 * min(7, n - 1) + 35,
                      'events': ev(base[0]['splice_loss'] + 0.005, base[0]['reflection'] + 0.3,
                                   base[1]['splice_loss'] + 0.01, base[1]['reflection'] + 0.1,
                                   base[2]['reflection'] + 0.05)})
    return files

def pairs_of(files):
    out = []
    for i in range(len(files)):
        for j in range(i + 1, len(files)):
            out.append({'a': files[i]['name'], 'b': files[j]['name'], 'p_dup': 0.0, 'score': 0.1})
    return out
"""


def test_the_planted_same_mating_pair_ranks_first_and_the_ratio_is_calibrated_in_form():
    out = _run(_SYNTH + r"""
files = folder(60)
pairs = pairs_of(files)
summ = RS._mating_likelihood(files, pairs)
top = max(pairs, key=lambda p: p['mating_lr'])
lrs = sorted(p['mating_lr'] for p in pairs)
print(json.dumps({'top': [top['a'], top['b']], 'top_lr': top['mating_lr'], 'top_p': top['mating_p'],
                  'null_p99': lrs[int(0.99 * len(lrs))], 'n': summ['n_pairs'],
                  'features': summ['features'], 'prior': summ['prior'],
                  'all_in_unit': all(0.0 <= p['mating_p'] <= 1.0 for p in pairs),
                  'p_dup_untouched': all(p['p_dup'] == 0.0 for p in pairs)}))
""")
    assert set(out["top"]) == {"P0007", "P0007_again"}, out
    assert out["top_lr"] > 100, "a same-mating pair must read a large likelihood ratio"
    assert out["top_lr"] > 5 * out["null_p99"], "and stand clear of the folder's own 99th percentile"
    assert out["all_in_unit"] and out["p_dup_untouched"]
    assert out["features"] == ["dl0", "dl1", "dr1", "dr0", "drE"]
    assert abs(out["prior"] - 1.0 / out["n"]) < 1e-12, "stated prior: one duplicate pair per folder"
    # posterior consistency with the stated prior
    pi = out["prior"]
    expect = pi * out["top_lr"] / (pi * out["top_lr"] + 1 - pi)
    assert abs(out["top_p"] - expect) < 1e-9


def test_a_clean_folder_has_no_standout_and_small_folders_abstain():
    out = _run(_SYNTH + r"""
files = folder(60, seed=5, plant=False)
pairs = pairs_of(files)
RS._mating_likelihood(files, pairs)
lrs = sorted(p['mating_lr'] for p in pairs)
small = folder(6, plant=True); sp = pairs_of(small)
res = RS._mating_likelihood(small, sp)
print(json.dumps({'frac_ge_100': sum(1 for v in lrs if v >= 100) / len(lrs),
                  'frac_ge_10': sum(1 for v in lrs if v >= 10) / len(lrs),
                  'small_none': res is None and all(p['mating_lr'] is None for p in sp)}))
""")
    # A clean folder can hold a chance look-alike pair (the real LSC null had one
    # consecutive pair at 737x), so the property is a RATE, matching the
    # calibration: ratios >= 100 are rare, >= 10 uncommon.
    assert out["frac_ge_100"] < 0.02, out
    assert out["frac_ge_10"] < 0.10, out
    assert out["small_none"] is True, "under _MATING_MIN_PAIRS the folder has no density: abstain"


def test_missing_events_never_crash_and_read_neutral():
    out = _run(_SYNTH + r"""
files = folder(40)
files[3]['events'] = []                       # a file with no event table
files[4]['events'] = files[4]['events'][:1]   # launch only
pairs = pairs_of(files)
summ = RS._mating_likelihood(files, pairs)
bad = [p for p in pairs if 'P0003' in (p['a'], p['b'])]
print(json.dumps({'ok': summ is not None, 'neutral': all(abs(p['mating_lr'] - 1.0) < 1e-9 for p in bad)}))
""")
    assert out["ok"] and out["neutral"], "a pair with no features gets ratio 1.0 (no evidence), not a crash"


def test_confidence_band_follows_the_competence_ratio():
    out = _run(r"""
import sys, json
sys.path.insert(0, sys.argv[1])
import report_sor as RS
print(json.dumps({
    'hi': RS._confidence_band({'ratio': 3.7})['band'],
    'med': RS._confidence_band({'ratio': 1.2})['band'],
    'lo': RS._confidence_band({'ratio': 0.09})['band'],
    'unk': RS._confidence_band(None)['band'],
    'lo_note': RS._confidence_band({'ratio': 0.09})['note'],
    'ok': RS._COMPETENCE_OK_RATIO, 'marg': RS._COMPETENCE_MARGINAL_RATIO,
}))
""")
    assert (out["hi"], out["med"], out["lo"], out["unk"]) == ("High", "Medium", "Low", "Unknown")
    assert "not a verdict" in out["lo_note"] and "port log" in out["lo_note"]
    assert out["ok"] == 1.5 and out["marg"] == 1.0


def test_the_calibration_is_recorded_beside_the_constants():
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    i = src.index("_MATING_TRUE_SCALES = {")
    block = src[max(0, i - 3200):i + 600]
    for marker in ("0.988", "0.49", "0.51", "consecutive", "retruetest", "LSC1->LSC6",
                   "1.46%", "1.59%", "never routes a pair", "'dl0': 0.0177", "'dr1': 0.1734"):
        assert marker in block, f"calibration evidence missing: {marker}"
    assert "_MATING_ALPHA = 0.9" in src
    # display-only: the mating keys must never feed p_dup, regime or verdict code
    for line in src.splitlines():
        if "mating" not in line or line.strip().startswith("#"):
            continue
        assert not any(tok in line for tok in ("p_dup =", "p_dup[", "regime =", "verdict =", "p_dup_r")), (
            f"mating value used in a decision path: {line.strip()}")
    # appended LAST so existing sheet indices (Confirmed duplicates = 1, ...) hold
    assert src.index("wb.create_sheet('Mating likelihood')") > src.index("wb.create_sheet('Charts')")


def test_the_runner_emits_the_keys_and_sorts_ties_by_mating(tmp_path):
    src = (SECRETSAUCE_DIR / "run_secretsauce.py").read_text(encoding="utf-8")
    assert "-(d.get('mating_p') or 0.0)" in src
    assert "rec['mating_lr'] = round(float(pr['mating_lr']), 1)" in src
    assert "payload['confidence'] = confidence_all" in src
    folder = mixed_fixture_dir(tmp_path)
    rc, manifest, err = run_secretsauce(str(folder), str(tmp_path / "out"), fmt="pairs")
    assert rc == 0 and manifest and manifest.get("ok"), err[-2000:]
    assert "Detector confidence:" in err
    conf = manifest.get("confidence")
    assert isinstance(conf, list) and conf and conf[0]["band"] in ("High", "Medium", "Low", "Unknown")
    # 8 files = 28 pairs < _MATING_MIN_PAIRS: keys absent, contract intact
    assert all("mating_p" not in p for p in manifest["pairs"])
    assert "Mating likelihood:" not in err


def test_the_hub_shows_the_column_and_the_confidence_line():
    src = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    assert "def _render_confidence_caption(res):" in src
    assert src.count("_render_confidence_caption(res)") == 3, "def + both result modes"
    assert ">Mating</th>" in src and "m_txt" in src
    assert "not a verdict" in src
