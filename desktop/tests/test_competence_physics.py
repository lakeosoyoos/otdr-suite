"""Detector competence from physics, and the notification that carries it.

Whether a folder can carry a duplicate verdict is decided from the folder's
own noise and its own spread, plus one property of glass (the fingerprint
amplitude, 0.00344 dB, measured flat across 12 dB of received power):

    predicted same-fibre r = null p50 + REPRO x (fingerprint / sigma_band)^2
    ratio = predicted / confirm bar      >= 1.5 OK, >= 1.0 MARGINAL, else NOT MEASURED

Calibrated on every folder with known truth (see the block above
_FINGERPRINT_DB in report_sor.py): 3.7 and 2.4 where the engine finds its
duplicates (500 ns), 0.00 to 0.33 on every 5 ns and 10 ns folder including
the two that hold known duplicates the engine cannot find.

The verdict is DIAGNOSTIC: it prints, it lands on the sheet, the PDF and the
hub, and the runner's manifest carries it (only when not OK, so unaffected
manifests stay byte-stable).  It never routes a pair.

Namespace isolation rule: the engine is only exercised through subprocesses.
"""
from __future__ import annotations

import json
import subprocess
import sys

from conftest import SECRETSAUCE_DIR, REPO_ROOT, run_secretsauce, mixed_fixture_dir


def _run(script: str, *args):
    p = subprocess.run([sys.executable, "-c", script, str(SECRETSAUCE_DIR), *args],
                       capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr[-3000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


_SYNTH = r"""
import sys, json
sys.path.insert(0, sys.argv[1])
import numpy as np
import report_sor as RS

def synth(n_fibres, shots, L, dz, sigma_n, f_amp, pulse_samples, seed=7,
          wl=1550.0, common=0.0):
    rng = np.random.default_rng(seed)
    n = int(L / dz) + 400
    pos = np.arange(n) * dz
    # Fingerprint and common mode are planted IN-BAND (white): the engine's
    # _FINGERPRINT_DB is the amplitude measured AFTER its own high-pass, so a
    # pulse-smoothed plant would be filtered away and understate itself.
    c = rng.standard_normal(n) * common
    files = []
    for k in range(n_fibres):
        g = rng.standard_normal(n) * f_amp
        for s in range(shots):
            tr = 50.0 + 0.0002 * pos + c + g + rng.standard_normal(n) * sigma_n
            files.append({'name': f'F{k:04d}_{s}', 'trace': tr.astype(np.float32),
                          'pos': pos, 'length': float(L), 'pulse_samples': pulse_samples,
                          'wavelength': wl, 'fib': k})
    return files

def window(files):
    min_L = RS._robust_common_span([f['length'] for f in files])[0]
    i_s, i_e = RS._LAUNCH_SKIP_M, min_L - RS._END_BUFFER_M
    if i_e - i_s < 100:
        i_s = max(2.0, min_L * 0.05); i_e = max(i_s + 2.0, min_L * 0.95)
    return min_L, i_s, i_e

def comp(files):
    min_L, i_s, i_e = window(files)
    return RS._speckle_competence(files, i_s, i_e,
                                  hp_width=RS._speckle_hp_width(files), span_m=min_L)
"""


def test_a_long_pulse_folder_is_competent_and_a_short_pulse_one_is_not():
    out = _run(_SYNTH + r"""
out = {}
# 500 ns class: 2.55 m sampling, noise 0.0025 dB, fingerprint 0.00344 dB
long = synth(60, 1, 78000.0, 2.55, 0.0025, RS._FINGERPRINT_DB, 20)
d = comp(long); out['long'] = d
# 5 ns class: 0.08 m sampling, noise 0.03 dB, same glass fingerprint, 2 km
short = synth(60, 1, 2000.0, 0.08, 0.03, RS._FINGERPRINT_DB, 6)
d = comp(short); out['short'] = d
# 31 m jumper at 5 ns
jump = synth(60, 1, 31.5, 0.08, 0.016, RS._FINGERPRINT_DB, 6)
d = comp(jump); out['jumper'] = d
print(json.dumps(out))
""")
    L, S, J = out["long"], out["short"], out["jumper"]
    assert L["status"] == "OK" and L["ratio"] >= 1.5, L
    assert L["remedy"] == "none needed" and L["what_it_takes"] == ""
    assert S["status"] == "NOT MEASURED" and S["ratio"] < 1.0, S
    assert J["status"] == "NOT MEASURED" and J["ratio"] < 1.0, J
    for d in (L, S, J):
        for k in ("pred_same_r", "bar", "null_p50", "null_p99", "sigma_band_db",
                  "fingerprint_term", "pulse_ns", "span_m", "message", "remedy"):
            assert k in d, k
        assert 0.0 <= d["fingerprint_term"] <= 1.0
    # the fingerprint term is the physics: same glass, more noise, smaller term
    assert L["fingerprint_term"] > 0.4 > S["fingerprint_term"]
    assert "NOT MEASURED" in S["message"] and "not that there are no duplicates" in S["message"]


def test_the_prediction_brackets_a_genuine_re_shoot():
    """Two shots of one synthetic fibre must read at least what the rule
    predicts (REPRO 0.7 is deliberately conservative) and no more than the
    perfect-reproduction value."""
    out = _run(_SYNTH + r"""
files = synth(60, 1, 78000.0, 2.55, 0.0025, RS._FINGERPRINT_DB, 20)
d = comp(files)
min_L, i_s, i_e = window(files)
w = RS._speckle_hp_width(files)
pair = synth(1, 2, 78000.0, 2.55, 0.0025, RS._FINGERPRINT_DB, 20, seed=99)
ra, rb = RS._speckle_band_residuals(pair, i_s, i_e, w)
r = float(np.dot(ra[1], rb[1]))
print(json.dumps({'pred': d['pred_same_r'], 'perfect': d['null_p50'] + d['fingerprint_term'],
                  'measured': r, 'bar': d['bar']}))
""")
    assert out["pred"] <= out["measured"] + 0.05, out
    assert out["measured"] <= out["perfect"] + 0.10, out
    assert out["measured"] > out["bar"], "a real re-shoot must clear the bar where the rule says OK"


def test_what_it_would_take_names_a_pulse_or_says_impossible():
    out = _run(_SYNTH + r"""
out = {}
# white null, noise-limited: a wider pulse is the remedy
short = synth(60, 1, 2000.0, 0.08, 0.03, RS._FINGERPRINT_DB, 6)
out['white'] = comp(short)
# shared instrument structure dominates the spread: no pulse helps
cm = synth(60, 1, 2000.0, 0.08, 0.03, RS._FINGERPRINT_DB, 6, common=0.05)
out['common'] = comp(cm)
print(json.dumps(out))
""")
    W, C = out["white"], out["common"]
    assert W["status"] == "NOT MEASURED"
    assert W["remedy"] == "pulse", W
    assert W["pulse_need_ns"] > W["pulse_ns"]
    assert "shoot at about" in W["what_it_takes"]
    assert W["min_span_m"] is None or W["min_span_m"] > W["span_m"]
    assert C["status"] == "NOT MEASURED"
    assert C["null_p50"] > 0.33, "the planted common mode must dominate the null"
    assert C["remedy"] == "impossible", C
    assert "impossible" in C["what_it_takes"]
    assert C["pulse_need_ns"] is None and C["min_span_m"] is None


def test_deterministic_and_cheap_on_a_large_folder():
    out = _run(_SYNTH + r"""
import time
files = synth(600, 1, 5000.0, 0.32, 0.04, RS._FINGERPRINT_DB, 10)
t = time.time(); a = comp(files); dt = time.time() - t
b = comp(files)
print(json.dumps({'same': a == b, 'n_files': a['n_files'], 'n_sampled': a['n_sampled'],
                  'dt': dt}))
""")
    assert out["same"] is True, "no RNG anywhere: the verdict must be reproducible"
    assert out["n_files"] == 600 and out["n_sampled"] <= 60
    assert out["dt"] < 5.0


def test_a_mixed_wavelength_folder_is_judged_at_its_dominant_lambda():
    out = _run(_SYNTH + r"""
a = synth(40, 1, 5000.0, 0.32, 0.04, RS._FINGERPRINT_DB, 10, wl=1550.0)
b = synth(5, 1, 5000.0, 0.32, 0.04, RS._FINGERPRINT_DB, 10, wl=1310.0, seed=3)
d = comp(a + b)
print(json.dumps({'n_files': d['n_files'], 'n_sampled': d['n_sampled']}))
""")
    assert out["n_sampled"] == 40, out


def test_too_few_files_gives_no_verdict_rather_than_a_wrong_one():
    out = _run(_SYNTH + r"""
two = synth(2, 1, 5000.0, 0.32, 0.04, RS._FINGERPRINT_DB, 10)
d = comp(two)
print(json.dumps({'none': d is None}))
""")
    assert out["none"] is True


# ── The notification: one story in the log, the sheet, the PDF, the manifest, the hub ──

def test_the_engine_records_the_verdict_and_the_calibration():
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    assert "_FINGERPRINT_DB = 0.00344" in src
    assert "_FINGERPRINT_REPRO = 0.70" in src
    i = src.index("_FINGERPRINT_DB = 0.00344")
    block = src[max(0, i - 3000):i]
    for marker in ("EMVSUI Long", "Dinwiddie", "EMVSUI Short", "retruetest",
                   "3.7", "0.24", "0.00", "4/4 found", "0/48 found", "0/66 found"):
        assert marker in block, f"calibration evidence missing: {marker}"
    # the physics verdict is computed on every folder, before the old branches
    assert "competence_detail = _speckle_competence(" in src
    assert "'competence_detail': competence_detail," in src
    assert "rows.append(('What it would take', _cdet['what_it_takes']))" in src
    assert "{competence_block}" in src and "_competence_section_html(" in src


def test_the_pdf_notice_is_empty_when_ok_and_escaped_when_not():
    out = _run(r"""
import sys, json
sys.path.insert(0, sys.argv[1])
import report_sor as RS
print(json.dumps({
    'none': RS._competence_section_html(None),
    'ok': RS._competence_section_html({'status': 'OK', 'message': 'fine'}),
    'bad': RS._competence_section_html({'status': 'NOT MEASURED',
                                        'message': 'Shot at 5 ns <b>x</b>',
                                        'what_it_takes': 'shoot at about 40 ns'}),
}))
""")
    assert out["none"] == "" and out["ok"] == ""
    assert "Duplicate detection NOT MEASURED." in out["bad"]
    assert "&lt;b&gt;x&lt;/b&gt;" in out["bad"], "message text must be escaped"
    assert "shoot at about 40 ns" in out["bad"]


def test_the_trc_lineage_reports_through_the_same_channel():
    out = _run(r"""
import sys, json
sys.path.insert(0, sys.argv[1])
import report as R
short = [{'wl': {1550: {'length_m': 31.4}}} for _ in range(12)]
long_ = [{'wl': {1550: {'length_m': 80000.0}}} for _ in range(12)]
m1, m2, m3 = {}, {}, None
R._competence_meta(short, [1550], m1)
R._competence_meta(long_, [1550], m2)
R._competence_meta(short, [1550], m3)      # None meta: no-op, no crash
print(json.dumps({'short': m1, 'long': m2}))
""")
    c = out["short"]["competence"]
    assert c["status"] == "NOT MEASURED" and c["lineage"] == "multiwl"
    assert "NOT MEASURED" in c["message"] and c["what_it_takes"]
    assert out["long"] == {}, "a measurable span adds nothing to the manifest"


def test_the_runner_manifest_carries_it_only_when_not_ok(tmp_path):
    """Additive contract: the key exists only when a lineage said it could
    not measure, and then it is a list of dicts the hub can render."""
    src = (SECRETSAUCE_DIR / "run_secretsauce.py").read_text(encoding="utf-8")
    assert src.count("competence_all = []") == 2
    assert "payload['competence'] = competence_all" in src
    assert "run_trc_xlsx_bytes(stage, 'Secret Sauce', meta=meta)" in src
    assert "run_json_xlsx_bytes(stage, 'Secret Sauce', meta=meta)" in src
    folder = mixed_fixture_dir(tmp_path)
    rc, manifest, err = run_secretsauce(str(folder), str(tmp_path / "out"), fmt="xlsx")
    assert rc == 0 and manifest and manifest.get("ok"), err[-2000:]
    assert "Speckle competence:" in err, "the run log must always say what it decided"
    if "competence" in manifest:
        assert isinstance(manifest["competence"], list)
        for c in manifest["competence"]:
            assert c["status"] != "OK"
            assert c["message"] and "remedy" in c or c.get("lineage") == "multiwl"


def test_the_hub_shows_the_banner_in_both_result_modes():
    src = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    assert "def _render_competence_banner(res):" in src
    # the def line plus exactly two call sites
    assert src.count("_render_competence_banner(res)") == 3, (
        "pairs mode and the Excel/PDF result must both show it")
    i = src.index("def _render_competence_banner(res):")
    body = src[i:i + 1200]
    assert "st.error if status == 'NOT MEASURED' else st.warning" in body
    assert "c.get('status') in (None, 'OK')" in body, "OK folders show nothing"
    assert "what_it_takes" in body
