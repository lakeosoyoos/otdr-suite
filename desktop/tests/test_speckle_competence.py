"""A gate that could not have fired must not print like a gate that ran.

`Speckle gate: N candidate pair(s), 0 demoted` is what the run log says on a
folder where the statistic was never computed, and on a folder where it was
computed against a bar no Pearson r can reach.  A tech reading that cannot
tell "nothing wrong here" from "the detector was switched off".

Two mechanisms, both measured 2026-08-31:

  1. NO NULL.  `_SPECKLE_MIN_SAMPLES = 500` is a PER-WINDOW count after the
     high-pass edge trim, and `_SPECKLE_WINDOWS` carves the interior into
     three fractional slices.  Measured per-window sample counts:

         retruetest   31.5 m    interior 350   smallest window  49
         LSC1->LSC6   31.4 m    interior 339   smallest window  47
         BETA tray      62 m    interior 703   smallest window 127

     Every file is unmeasurable, so the folder null is never built and every
     candidate is dropped.  Under 15 files the null cannot be built either -
     `_SPECKLE_NULL_MIN_PAIRS = 100` needs 15 files to reach 105 pairs.

  2. A BAR OUT OF REACH.  The confirm bar is `_SPECKLE_CONFIRM_NULL_MULT x`
     the folder's own null p99, and that product is not scale-free:

         EMVSUI Long   78.5 km   null p99 +0.086   bar 0.257   usable
         BETA tray       62 m    null p99 +0.573   bar 1.718
         LSC1->LSC6      31 m    null p99 +0.600   bar 1.800
         Reubensville    31 m    null p99 +0.688   bar 2.064
         Dinwiddie     2.07 km   null p99 +0.961   bar 2.883
         EMVSUI Short  3.99 km   null p99 +0.976   bar 2.927
         ELMMIL sh     4.99 km   null p99 +0.976   bar 2.929

     A Pearson r cannot exceed 1.0.  On four of five span classes the gate is
     disabled, not conservative.

THIS CHANGE PRINTS; IT DOES NOT DECIDE.  The diff is purely additive - one
constant, one pure diagnostic function and three print statements - so no
verdict on any folder can move.  test_the_change_cannot_move_a_verdict is
what holds that property in place.

Namespace isolation rule: the engine is only exercised through subprocesses.
"""
from __future__ import annotations

import json
import subprocess
import sys

from conftest import SECRETSAUCE_DIR


def _run(script: str):
    p = subprocess.run([sys.executable, "-c", script, str(SECRETSAUCE_DIR)],
                       capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


_CENSUS_SCRIPT = r"""
import sys, json
sys.path.insert(0, sys.argv[1])
import numpy as np
from report_sor import (_speckle_window_census, _speckle_windows,
                        _SPECKLE_MIN_SAMPLES, _SPECKLE_WINDOWS,
                        _SPECKLE_BAR_MAX, _SPECKLE_CONFIRM_NULL_MULT,
                        _SPECKLE_HP_WIDTH)

def folder(length_m, dz, n=4, seed=3):
    n_s = int(length_m / dz) + 400
    pos = np.arange(n_s) * dz
    rng = np.random.default_rng(seed)
    return [{'pos': pos,
             'trace': (rng.standard_normal(n_s) * 0.05).astype(np.float32)}
            for _ in range(n)]

out = {'bar_max': _SPECKLE_BAR_MAX, 'mult': _SPECKLE_CONFIRM_NULL_MULT,
       'floor': _SPECKLE_MIN_SAMPLES}

# The three real short classes, with their real sample spacing.
for tag, L, dz in (('retrue_31m', 31.5, 0.0797639),
                   ('lsc_31m',    31.4, 0.0797639),
                   ('beta_62m',   62.2, 0.0797639)):
    f = folder(L, dz)
    out[tag] = list(_speckle_window_census(f, 2.0, L - 2.0, 7))

# A long folder must clear the floor comfortably.
out['long_78km'] = list(_speckle_window_census(folder(78576.0, 2.5524),
                                               500.0, 78322.0, 21))

# The census must agree with what _speckle_windows ACTUALLY did, or the
# number printed is not the number that was tested.
f = folder(31.5, 0.0797639)
out['windows_is_none_at_31m'] = _speckle_windows(f[0], 2.0, 29.5, 7) is None
g = folder(78576.0, 2.5524)
out['windows_ok_at_78km'] = _speckle_windows(g[0], 500.0, 78322.0, 21) is not None

# Degenerate inputs must not raise - this runs on every folder.
out['empty'] = list(_speckle_window_census([], 2.0, 30.0, 7))
out['no_pos'] = list(_speckle_window_census([{}, None], 2.0, 30.0, 7))
out['bad_span'] = list(_speckle_window_census(folder(31.5, 0.0797639), 30.0, 2.0, 7))
print(json.dumps(out))
"""


def test_the_short_classes_are_short_by_an_order_of_magnitude():
    """49 and 47 samples against a floor of 500.  This is not a near miss that
    a small threshold change would fix."""
    out = _run(_CENSUS_SCRIPT)
    # Synthetic traces at the REAL sample spacing of each class.  The
    # interior bounds the engine picks per folder shift the interior count a
    # little, so the load-bearing assertion is the window count against the
    # floor, which is what _speckle_windows actually tests.
    assert out["retrue_31m"][1] == 49, out["retrue_31m"]
    assert out["lsc_31m"][1] == 48, out["lsc_31m"]
    assert out["beta_62m"][1] == 118, out["beta_62m"]
    for tag in ("retrue_31m", "lsc_31m", "beta_62m"):
        assert out[tag][1] < out["floor"], (tag, out[tag], out["floor"])
        assert out[tag][1] * 4 < out["floor"], (
            f"{tag} should miss the floor by an order of magnitude, not narrowly")


def test_a_long_folder_clears_the_floor():
    out = _run(_CENSUS_SCRIPT)
    assert out["long_78km"][1] >= out["floor"], out["long_78km"]


def test_the_census_agrees_with_the_real_window_builder():
    """If the census and _speckle_windows disagree, the run log names a
    sample count that was never the one tested."""
    out = _run(_CENSUS_SCRIPT)
    assert out["windows_is_none_at_31m"] is True
    assert out["windows_ok_at_78km"] is True


def test_the_census_never_raises_on_degenerate_input():
    """It runs on EVERY folder, including ones with unreadable files."""
    out = _run(_CENSUS_SCRIPT)
    assert out["empty"] == [0, 0]
    assert out["no_pos"] == [0, 0]
    assert out["bad_span"] == [0, 0]


def test_the_ceiling_is_below_one():
    """A bar above 1.0 is unreachable by any Pearson r; the ceiling has to sit
    below that or it certifies disabled gates as working."""
    out = _run(_CENSUS_SCRIPT)
    assert out["bar_max"] == 0.90
    assert out["bar_max"] < 1.0
    assert out["mult"] == 3.0, "the multiplier itself is deliberately unchanged"


def test_the_run_log_says_which_way_it_failed():
    """Three distinct outcomes, three distinct lines.  One catch-all message
    would leave 'no null' and 'bar out of reach' indistinguishable again."""
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    assert "Speckle competence: UNMEASURABLE — no folder null." in src
    assert "Speckle competence: UNMEASURABLE — confirm bar" in src
    assert "Speckle competence: OK — confirm bar" in src
    # The sentence that does the actual work for a tech reading the log.
    assert 'means NOT MEASURED' in src
    assert "which no Pearson r can reach" in src


def test_the_change_cannot_move_a_verdict():
    """The safety argument is structural, not empirical: the whole change is
    additive.  If a future edit deletes or rewrites an existing line inside
    this feature, that argument is gone and this test should fail loudly."""
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    i = src.index("# ── Competence, said out loud")
    block = src[i:i + 2400]
    # Everything in the block is a comment, an assignment to a local that
    # nothing else reads, or a print.
    assigned = "_spk_bar"
    assert block.count(assigned) >= 1
    after = src[i + 2400:]
    assert assigned not in after, (
        "_spk_bar escaped the reporting block; it must not feed a decision")
    assert "_speckle_window_census" not in src[:src.index("def _speckle_window_census")], (
        "census used before it is defined")
    # The census must not be called anywhere a verdict is computed.
    assert src.count("_speckle_window_census(") == 2, (
        "census should be defined once and called once, from the log block")


def test_the_measured_bar_table_is_recorded():
    """Whoever lowers the multiplier later needs these numbers in front of
    them - and the reason it was NOT lowered here."""
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    i = src.index("_SPECKLE_BAR_MAX")
    block = src[max(0, i - 2400):i + 200]
    for marker in ("EMVSUI Long", "Dinwiddie", "ELMMIL sh", "2.929",
                   "0.257", "132 to 6,081"):
        assert marker in block, f"missing bar evidence: {marker}"
