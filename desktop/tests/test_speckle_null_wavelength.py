"""The speckle null must be built per WAVELENGTH, and cross-lambda pairs must
be unmeasurable rather than scored.

Rayleigh speckle is a wavelength-dependent interference pattern: the same
glass shot at a different lambda gives an unrelated fingerprint.  `_spk_null`
pooled every file in the folder regardless, which makes the null BIMODAL -
same-lambda pairs carry the real correlation, cross-lambda pairs sit near
zero - and a percentile of a bimodal distribution describes neither mode.

This is live, not hypothetical.  A walk of every folder on disk with >= 40
.sor files found 56 carrying more than one acquisition wavelength, including
production spans:

    1152   {1548.0, 1539.8}                  MILTOP / TOPMIL
    1152   {1539.8, 1554.8}                  MILELMsh
     864   {1550.4, 1554.8}                  LONGS
     576   {1547.1, 1549.0}                  Niland  /  Mecca {1554.7, 1555.7}
     576   {1552.8, 1554.7, 1548.0, 1547.1}  Winterhaven - four

Measured on LONGS, 1,770 known-different pairs from the engine's own 60-file
sample, at the engine's own high-pass width:

    pooled       p50 +0.0230   p99 +0.0780   3x bar 0.234
    wl 1550.4    p50 +0.0332   p99 +0.0992   3x bar 0.298
    wl 1554.8    p50 +0.0221   p99 +0.0702   3x bar 0.211

The pooled null is not merely inflated - it is wrong in BOTH directions, too
lax for 1550.4 and too strict for 1554.8.  Neither wavelength is judged
against what its own fibers actually do.

MEASURED RIPPLE: a no-op on every folder tested, because one wavelength
dominates each of them and the minority group falls back to the pooled null
rather than building one from too few files.

    LONGS   864f  2 wl   tie_panel   0/0/0                        identical
    MILTOP 1152f  2 wl   tie_panel   rescue fires, 329/330 0.9991 identical
    Mecca   576f  2 wl   production  2 candidates, 2 demoted      identical
                                     (498/504 stays demoted)
    Niland  576f  2 wl   production  3 cand, 2 demoted, 359/360   identical
                                     at 0.8505
    Winterhaven   4 wl   production  2 confirmed at 1.0, 1 demoted identical

LONGS is the folder where the per-lambda bars genuinely differ, and it has no
candidates for them to act on.  So this is a correctness fix that costs
nothing today and stops the next balanced two-lambda folder from being judged
against a number that describes neither of its wavelengths.

Namespace isolation rule: the engine is only exercised through subprocesses.
"""
from __future__ import annotations

import json
import subprocess
import sys

from conftest import SECRETSAUCE_DIR


def _run(script: str):
    p = subprocess.run([sys.executable, "-c", script, str(SECRETSAUCE_DIR)],
                       capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr[-3000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


_NULL_SCRIPT = r"""
import sys, json, inspect
sys.path.insert(0, sys.argv[1])
import numpy as np
import report_sor as RS

src = inspect.getsource(RS._analyze_sor)
out = {}

# The two helpers must exist inside _analyze_sor and be wired to every
# consumer; a bare _spk_null() left behind is a site still pooling.
out['has_wl_helper'] = 'def _spk_wl(' in src
out['has_pair_helper'] = 'def _spk_null_for_pair(' in src
out['bare_calls'] = src.count('_spk_null()')
out['pair_calls'] = src.count('_spk_null_for_pair(')
out['null_cache_is_dict'] = "'null_q': {}" in src

# Every place a bar is formed must come from the per-pair null.
out['bars'] = [l.strip() for l in src.splitlines()
               if '_SPECKLE_CONFIRM_NULL_MULT' in l and '=' in l]

# The veto must treat a non-comparable pair as unmeasurable (kept).
i = src.index("p['speckle_unmeasurable'] = True")
out['unmeas_guard'] = src[max(0, i - 260):i]

# The abstain test must use the pair's own null, not the pooled one.
out['abstain_uses_pair_null'] = 'if r_floor < p_nq:' in src
out['pooled_only_for_log'] = 'pooled value, for the run-log line only' in src
print(json.dumps(out))
"""


def test_both_helpers_exist():
    out = _run(_NULL_SCRIPT)
    assert out["has_wl_helper"] is True
    assert out["has_pair_helper"] is True
    assert out["null_cache_is_dict"] is True, (
        "the null cache must be keyed by wavelength, not a single value")


def test_no_consumer_still_asks_for_the_pooled_null_to_judge_with():
    """One bare call survives on purpose - the run-log line.  Any other is a
    site still judging every wavelength against one number."""
    out = _run(_NULL_SCRIPT)
    assert out["bare_calls"] == 0, (
        f"{out['bare_calls']} bare _spk_null() call(s) left; only the "
        "explicit _spk_null(None) for the log is allowed")
    assert out["pair_calls"] >= 4, out["pair_calls"]
    assert out["pooled_only_for_log"] is True


def test_every_confirm_bar_comes_from_the_pair_null():
    """Three sites form a bar: the sigma rescue, the twin-gate refutation and
    the veto.  If any of them still multiplies a pooled null, that site is
    unfixed."""
    out = _run(_NULL_SCRIPT)
    assert out["bars"], out["bars"]
    for line in out["bars"]:
        assert "_nq *" in line or "nq *" in line, line
    # and nothing forms a bar straight off a pooled call
    for line in out["bars"]:
        assert "_spk_null()" not in line, line


def test_a_cross_wavelength_pair_is_unmeasurable_not_vetoed():
    """Two different lambdas have unrelated speckle by physics, so the
    statistic reads ~0 for them.  Scoring that as evidence would let a
    cross-lambda pair be demoted - or refute the twin gate - for free.
    Fail-safe direction is KEEP."""
    out = _run(_NULL_SCRIPT)
    guard = out["unmeas_guard"]
    assert "not p_cmp" in guard, guard
    assert "p_nq is None" in guard, guard


def test_the_abstain_test_uses_the_pairs_own_null():
    out = _run(_NULL_SCRIPT)
    assert out["abstain_uses_pair_null"] is True


def test_a_file_with_no_wavelength_keeps_the_old_behaviour():
    """Fail-safe: an unreadable wavelength must not make the pair vanish from
    the gate, or one odd file silently disables it."""
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    i = src.index("def _spk_null_for_pair(")
    block = src[i:i + 1200]
    assert "if wa is None or wb is None:" in block
    assert "return _spk_null(None), True" in block


def test_too_few_same_wavelength_files_falls_back_rather_than_losing_the_gate():
    """A minority wavelength with under _SPECKLE_NULL_MIN_PAIRS pairs must
    fall back to the pooled null.  Dropping the gate instead would silently
    stop judging those pairs at all - which is how MILTOP's rescue would
    have been lost."""
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    i = src.index("def _spk_null_for_pair(")
    block = src[i:i + 1200]
    assert "nq if nq is not None else _spk_null(None)" in block, block


def test_the_measured_evidence_is_recorded():
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    i = src.index("def _spk_null(")
    block = src[i:i + 3000]
    for marker in ("56 folders", "LONGS", "1550.4", "1554.8",
                   "0.298", "0.211", "BIMODAL"):
        assert marker in block, f"missing evidence: {marker}"
