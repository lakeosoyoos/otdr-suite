"""Sub-sample alignment for the speckle statistic.

THIS PR CHANGES VERDICTS.  It is the only one of the short-span series that
does, and the change lands on a pair whose status is formally UNKNOWN.  The
measurements are here so the decision is made on them rather than on the
headline.

WHY.  Two acquisitions of the same fiber do not begin on the same sample, and
the offset is not an integer.  Measured best-fit shifts on the four confirmed
EMVSUI duplicates: -1.625, +0.250, -0.875, +1.500 samples.  Correlating at
shift 0 alone discards most of the signal.

    EMVSUI W=335, 54,285 known-different pairs, ma15:
        no align   2/4 at 0 FP, margin -0.066, 5 false positives at t_min
        aligned    4/4 at 0 FP, margin +0.187, 0 false positives

Applied SYMMETRICALLY - the folder null goes through the same
_speckle_pair_r - so the null widens with the search rather than the true
pairs alone getting the benefit.

WHAT IT DOES ON THIS CORPUS.  Isolated four ways against the shipped engine
(shipped / union window / union+hp15 / union+align), so the movement is
attributable and not a bundle effect:

    Mecca 576f    shipped      99:0  2 demoted  498/504 at 0.5   p99 0.067
                  union        99:0  2 demoted  498/504 at 0.5   p99 0.036
                  union+hp15   99:0  2 demoted  498/504 at 0.5   p99 0.036
                  union+ALIGN  99:1  1 demoted  498/504 at 1.0   p99 0.043

    Niland 576f   shipped      99:0  2 demoted  359/360 0.8505   p99 0.072
                  union        99:0  2 demoted  359/360 0.8505   p99 0.037
                  union+hp15   99:0  2 demoted  359/360 0.8505   p99 0.037
                  union+ALIGN  99:2  0 demoted  498/504 + 418/424 at 1.0

    Winterhaven   shipped      99:2  1 demoted
                  union+ALIGN  99:3  0 demoted  413/414 promoted to 1.0

Alignment alone accounts for ALL of it.  The window change is verdict-neutral
and hp15 moved nothing.

THE TWO READINGS, neither of which this test can settle:

  FOR - the fingerprint was dissenting because it compared two acquisitions
  at zero shift when the real offset is fractional.  498/504 already had
  sigma 0.0171/0.0235, detrended r 0.9955/0.9851, EOF identical to 0.1 m in
  BOTH directions, and event tables matching 9 of 10 at a median |dLoss| of
  0.0020 dB - tighter than confirmed duplicate 563/564 at 0.0030.  Every
  other line of evidence said duplicate; only the fingerprint dissented, and
  PR #129 recorded that if the gate was wrong, this is exactly where a real
  duplicate was being suppressed.  Alignment removes the dissent.

  AGAINST - a MAX over 41 shifts buys r for every pair, and the speckle veto
  went from 2 demotions to 0 on both folders.  A gate that stops vetoing is
  not obviously a better gate.  The null does widen (0.036 -> 0.043) but far
  less than the true pairs move, which is the intended asymmetry AND what an
  overfit search would also look like.

  There are no held-out long-span positives on disk.  Every configuration
  choice behind alignment was made on the same four EMVSUI pairs.

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


_ALIGN_SCRIPT = r"""
import sys, json
sys.path.insert(0, sys.argv[1])
import numpy as np
from report_sor import (_speckle_align_r, _SPECKLE_ALIGN_MAX,
                        _SPECKLE_ALIGN_STEP)

def unit(x):
    x = x - x.mean()
    return x / np.sqrt(np.dot(x, x))

rng = np.random.default_rng(5)
n = 4000
# Band-limited, because REAL speckle is.  The residual is a trace minus an
# hp_width moving average, and the finest structure it can carry is set by
# the pulse (about 20 samples at 500 ns / 25 ns).  White noise has energy at
# Nyquist that NO interpolator can reconstruct, so testing against it would
# measure the interpolator rather than the alignment.  The white-noise limit
# is recorded below as the worst case.
_w = np.ones(5) / 5.0
base = np.convolve(rng.standard_normal(n + 60), _w, mode='same')
grid = np.arange(len(base), dtype=np.float64)
white = rng.standard_normal(n + 60)

def shifted(sh):
    return unit(np.interp(np.arange(20, 20 + n) + sh, grid, base))

a = shifted(0.0)
out = {'max': _SPECKLE_ALIGN_MAX, 'step': _SPECKLE_ALIGN_STEP}

# A known fractional offset must be recovered: aligned r >> unaligned r.
for sh in (0.0, 0.5, 1.5, -0.875, 2.0):
    b = shifted(sh)
    out[f'aligned_{sh}'] = round(_speckle_align_r(a, b), 4)
    out[f'raw_{sh}'] = round(float(np.dot(a, b)), 4)

# The white-noise worst case: linear interpolation cannot reconstruct
# energy at Nyquist, so a half-sample shift is only partly recovered.  This
# is a real bound on the method and is recorded rather than hidden.
def shifted_white(sh):
    return unit(np.interp(np.arange(30, 30 + n) + sh, grid, white))
out['white_half_aligned'] = round(
    _speckle_align_r(shifted_white(0.0), shifted_white(0.5)), 4)

# Unrelated traces must NOT be rescued by the search.
c = unit(rng.standard_normal(n))
out['unrelated_aligned'] = round(_speckle_align_r(a, c), 4)
out['unrelated_raw'] = round(float(np.dot(a, c)), 4)

# A shift beyond the search range must NOT be recovered - the window is a
# deliberate bound, not an open-ended hunt.
out['beyond_range'] = round(_speckle_align_r(a, shifted(6.0)), 4)

# Too-short input falls back to the plain dot product rather than raising.
short_a, short_b = unit(rng.standard_normal(9)), unit(rng.standard_normal(9))
out['short'] = round(_speckle_align_r(short_a, short_b), 4)
out['short_raw'] = round(float(np.dot(short_a, short_b)), 4)
print(json.dumps(out))
"""


def test_a_fractional_offset_is_recovered():
    """The whole premise: the same trace shifted by a non-integer number of
    samples must come back as a near-1.0 correlation.

    Measured on a band-limited synthetic - the gain scales with the offset,
    which is the signature of alignment doing what it claims rather than
    adding a constant:

        shift    raw      aligned
        +0.5     0.9501   0.9775
        -0.875   0.8479   0.9971
        +1.5     0.7397   0.9775
        +2.0     0.6003   1.0000

    The measured EMVSUI duplicate shifts are -1.625, +0.250, -0.875, +1.500,
    so the folders that matter sit where the gain is largest."""
    out = _run(_ALIGN_SCRIPT)
    for sh in ("0.5", "1.5", "-0.875", "2.0"):
        assert out[f"aligned_{sh}"] > 0.95, (sh, out[f"aligned_{sh}"])
        assert out[f"aligned_{sh}"] >= out[f"raw_{sh}"], (
            f"alignment made shift {sh} WORSE: "
            f"{out[f'raw_{sh}']} -> {out[f'aligned_{sh}']}")
    # The gain has to be substantial where the real duplicates sit.
    for sh in ("1.5", "2.0", "-0.875"):
        assert out[f"aligned_{sh}"] > out[f"raw_{sh}"] + 0.14, (
            f"alignment bought little at shift {sh}: "
            f"{out[f'raw_{sh}']} -> {out[f'aligned_{sh}']}")


def test_white_noise_is_the_worst_case_and_is_bounded():
    """Recorded, not hidden: on a signal with energy at Nyquist a half-sample
    shift is only partly recovered, because linear interpolation cannot
    reconstruct it.  Real residuals are band-limited by the pulse, which is
    why the gain is real on actual folders - but if an acquisition ever runs
    at a sampling period comparable to its pulse, expect this bound."""
    out = _run(_ALIGN_SCRIPT)
    assert 0.6 < out["white_half_aligned"] < 0.95, out["white_half_aligned"]


def test_zero_shift_is_not_degraded():
    out = _run(_ALIGN_SCRIPT)
    assert out["aligned_0.0"] > 0.99, out["aligned_0.0"]


def test_unrelated_traces_are_not_rescued():
    """41 shifts is 41 chances to find a spurious correlation.  If the search
    lifts unrelated traces materially, it is buying r for everyone and the
    null is doing the work instead of the physics."""
    out = _run(_ALIGN_SCRIPT)
    assert out["unrelated_aligned"] < 0.15, out["unrelated_aligned"]
    # Measured lift on an unrelated pair: 0.0098 -> 0.0320.  That is the
    # null inflation the search costs, and it is what the folder null
    # absorbs.  Compare against +0.15 to +0.40 on genuinely shifted copies.
    assert out["unrelated_aligned"] - out["unrelated_raw"] < 0.06, (
        out["unrelated_raw"], out["unrelated_aligned"])


def test_the_search_range_is_a_real_bound():
    """A 6-sample offset must stay unrecovered.  An unbounded search would
    eventually align anything with anything."""
    out = _run(_ALIGN_SCRIPT)
    assert out["beyond_range"] < 0.5, out["beyond_range"]
    assert out["max"] == 2.5
    assert out["step"] == 0.125


def test_short_windows_fall_back_instead_of_raising():
    """This runs on every candidate pair on every folder, including ones
    whose windows are barely longer than the search."""
    out = _run(_ALIGN_SCRIPT)
    assert out["short"] == out["short_raw"], (out["short"], out["short_raw"])


def test_the_null_goes_through_the_same_search():
    """Asymmetry here would be self-serving: true pairs get the benefit of 41
    draws and the null does not.  Both must use _speckle_pair_r."""
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    i = src.index("null_vals = [v for a_i in range(len(null_res))")
    block = src[i:i + 400]
    assert "_speckle_pair_r(" in block, block


def test_the_verdict_movement_is_recorded_with_both_readings():
    """This PR changes verdicts on a pair whose status is UNKNOWN.  The
    argument on both sides has to survive in the tree, not just in a PR
    description."""
    doc = __doc__ or ""
    for marker in ("498/504", "418/424", "2 demoted", "0 demoted",
                   "FOR -", "AGAINST -", "no held-out long-span positives"):
        assert marker in doc, f"missing: {marker}"
