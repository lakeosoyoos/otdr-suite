"""One union window, because MAX across sub-windows is the worst combiner.

`_SPECKLE_WINDOWS` carved the interior into three fractional slices and
`_speckle_pair_r` reduced them with MAX.  MAX is the most permissive
combiner, and that permissiveness is paid for by the NULL: a known-different
pair gets to take the best of k draws, while a true pair only needs one
window to agree.  The null therefore rises faster than the true minimum.

Measured on EMVSUI Long against 54,285 known-different pairs,
margin = (true minimum) - (null maximum):

    k=1  single window                                      +0.242   4/4
    k=2  MAX +0.075   MEAN +0.101   MEDIAN +0.101           all 4/4
    k=3  MAX +0.007   MEAN +0.116   MEDIAN -0.035        MEDIAN 3/4
    k=5  MAX -0.090   MEAN +0.020   MEDIAN -0.080           MAX 1/4

At the shipped k=3 the MAX combiner had spent almost the whole margin: the
gate was one window away from the null.

The union stops at 0.60 deliberately.  Widening it to the whole interior
pulls in the far end where SNR is gone - null p50 +0.5686, null max +0.9714,
harness down to 1/4 at zero false positives with 564 of them.  The 2-60%
placement is load-bearing, and test_the_union_must_not_reach_the_far_end
holds it.

MEASURED RIPPLE - the two folders on disk carrying long-span candidates,
four-way isolated against the shipped engine:

    Mecca 576f     shipped   99:0  2 demoted  498/504 at 0.5  null p99 0.067
                   union     99:0  2 demoted  498/504 at 0.5  null p99 0.036
    Niland 576f    shipped   99:0  2 demoted  359/360 0.8505  null p99 0.072
                   union     99:0  2 demoted  359/360 0.8505  null p99 0.037

Every verdict identical; the folder null nearly halved.  That is margin the
gate did not have, and this change spends none of it.

WHAT IS NOT HERE, ON PURPOSE.  The same sweep also recommended narrowing the
high-pass cap 21 -> 15 and adding a +-2.5-sample sub-sample alignment.  Both
were built and measured on these folders:

    hp 21 -> 15    moved NOTHING on either folder, null unchanged at 0.036 /
                   0.037.  Its benefit is EMVSUI-measured only, and it would
                   change the width on every long-pulse folder, so it is not
                   carried on an unmeasured benefit.
    alignment      moves verdicts on its own: 498/504 to 1.0 in BOTH
                   directions, 418/424 to 1.0, and speckle demotions from
                   2 to 0 on both folders.  That is a judgement call about a
                   pair whose status is formally UNKNOWN (PR #129), not a
                   free margin gain, so it is held separately.

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


_WIN_SCRIPT = r"""
import sys, json
sys.path.insert(0, sys.argv[1])
import numpy as np
from report_sor import (_SPECKLE_WINDOWS, _SPECKLE_HP_WIDTH,
                        _speckle_windows, _speckle_pair_r,
                        _SPECKLE_MIN_SAMPLES)

out = {'windows': [list(w) for w in _SPECKLE_WINDOWS],
       'hp_cap': _SPECKLE_HP_WIDTH,
       'n_windows': len(_SPECKLE_WINDOWS)}

# A long folder: one window must now cover what three used to, so the union
# has to yield MORE samples than any single old slice did.
dz, L = 2.5524, 78576.0
n_s = int(L / dz) + 400
pos = np.arange(n_s) * dz
rng = np.random.default_rng(11)
f = {'pos': pos, 'trace': (rng.standard_normal(n_s) * 0.05).astype(np.float32)}
r = _speckle_windows(f, 500.0, 78322.0, 21)
out['union_len'] = int(len(r['win'][0][2]))
out['old_slice_len'] = int(round(out['union_len'] / 3))

# Identical traces still correlate at 1.0; unrelated ones do not.
g = {'pos': pos, 'trace': f['trace'].copy()}
h = {'pos': pos,
     'trace': (np.random.default_rng(12).standard_normal(n_s) * 0.05).astype(np.float32)}
out['self_r'] = round(_speckle_pair_r(r, _speckle_windows(g, 500.0, 78322.0, 21)), 6)
out['other_r'] = round(_speckle_pair_r(r, _speckle_windows(h, 500.0, 78322.0, 21)), 4)
print(json.dumps(out))
"""


def test_there_is_exactly_one_window():
    out = _run(_WIN_SCRIPT)
    assert out["n_windows"] == 1, out["windows"]
    assert out["windows"] == [[0.02, 0.60]], out["windows"]


def test_the_union_must_not_reach_the_far_end():
    """0.60, not 1.0.  The far end has no SNR left and taking it collapses
    the harness to 1/4 with 564 false positives."""
    out = _run(_WIN_SCRIPT)
    assert out["windows"][0][1] == 0.60, out["windows"]
    assert out["windows"][0][1] < 0.95, "the union reached the far end"


def test_the_high_pass_cap_is_unchanged():
    """The sweep also suggested 15, but it moved nothing on either folder
    with candidates, and it would change the width on every long-pulse
    folder.  Not carried on an unmeasured benefit."""
    out = _run(_WIN_SCRIPT)
    assert out["hp_cap"] == 21


def test_the_union_is_longer_than_the_slices_it_replaces():
    """The margin comes from N: one window of 3x the samples has a null
    roughly 1/sqrt(3) as wide."""
    out = _run(_WIN_SCRIPT)
    assert out["union_len"] > 2.5 * out["old_slice_len"] * 0.9, out
    assert out["union_len"] >= 500, out["union_len"]


def test_the_statistic_still_behaves():
    """Same trace -> 1.0; unrelated traces -> near zero.  A window change is
    exactly the kind of edit that can silently break the metric."""
    out = _run(_WIN_SCRIPT)
    assert out["self_r"] >= 0.999999, out["self_r"]
    assert abs(out["other_r"]) < 0.1, out["other_r"]


def test_the_combiner_evidence_is_recorded():
    """The k-table is the whole argument.  Anyone re-splitting the window
    should have to read what MAX costs."""
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    i = src.index("\n_SPECKLE_WINDOWS = ((0.02, 0.60),)")
    block = src[max(0, i - 3000):i + 100]
    for marker in ("WORST combiner", "k=3", "+0.007", "0.9714",
                   "Mecca", "Niland", "0.067 -> 0.036"):
        assert marker in block, f"missing evidence: {marker}"


def test_alignment_is_not_in_this_change():
    """Sub-sample alignment moves verdicts and is held separately; if it
    lands here by accident this PR stops being verdict-neutral."""
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    assert "_SPECKLE_ALIGN_MAX" not in src
    assert "_speckle_align_r" not in src
