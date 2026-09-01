"""A real duplicate must not be zeroed just because the folder routed tie_panel.

Found by running both Secret Sauce lineages across 22 real spans (2026-08-30).
MILTOP (Miller->Topeka) clears the tie_panel trigger by 0.0214 - bulk_r 0.7214
against the >= 0.70 rule - and tie_panel discards the sigma-outlier result
wholesale, so:

    MILTOPls0329/0330   sigma 0.00985  p_sigma 0.9991  r 0.9965  ->  0.0000
    MILTOPls0830/0831   sigma 0.00941  p_sigma 0.9995  r 0.9964  ->  0.0000

Those sigmas sit beside EMVSUI 79/80 (0.0095), a confirmed duplicate.
Fingerprinted against a 1,770-pair known-different null on that folder
(p50 0.0310, p99 0.1066, MAX 0.2470), 329/330 reads **0.8243** - 3.3x the
maximum any different-fiber pair there reaches - with identical EOF.  It is a
real duplicate that the shipped engine reported as zero.  This is the same
failure PR #122 repaired on EMVSUI, reached by a different route.

THE FIX IS NARROW BY CONSTRUCTION.  A pair must be an EXTREME sigma outlier
(p_dup_sigma > _SIGMA_RESCUE_MIN) before its fingerprint is even measured, and
the candidate set is EMPTY on every other LONG sigma-bypassed folder on disk:

    A-F West   0   (the 1,997-false-positive panel)
    A-F East   0
    BKF<->DEL  0   (the 47-false-positive set)
    LAMBEY     0   (the 67-false-positive set)
    TULORO     0   (the 62,014-pair flood)
    ELMMIL sh  0
    MILTOP     2   <- the only candidates in the corpus

A LONG folder whose sigma bulk is genuinely cascading has no extreme outliers
left to rescue.  That is why this cannot re-open the cascades the regime exists
to stop, and it is measured rather than argued.

SHORT PANELS ARE A DIFFERENT STORY AND THE SCOPE MATTERS (measured 2026-08-31,
after this fix was written).  The 20 short panels on disk were never in the
list above and they are NOT empty - 132 to 6,081 candidates each, 2,953 on
BETA LFY East 144f DW Tray A-F, which is the historic flood number.  Nothing
is rescued there because the confirm bar, _SPECKLE_CONFIRM_NULL_MULT x the
folder's own null p99, exceeds 1.0 on every span class below 78 km, and a
Pearson r cannot reach 1.0.  Short-panel safety is therefore ARITHMETIC, not
emptiness, and repairing that bar without re-measuring those candidate sets is
how this path would inherit the flood.  See
test_the_emptiness_argument_is_scoped_to_LONG_folders.

830/831 is NOT rescued - fingerprint 0.1225 against a same-fiber floor of
0.3055 - so the bar is doing real work rather than waving both candidates
through.

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


_CONST_SCRIPT = r"""
import sys, json
sys.path.insert(0, sys.argv[1])
from report_sor import _SIGMA_RESCUE_MIN, _SPECKLE_CONFIRM_NULL_MULT
print(json.dumps({'rescue_min': _SIGMA_RESCUE_MIN,
                  'null_mult': _SPECKLE_CONFIRM_NULL_MULT}))
"""


def test_the_bar_is_extreme_on_purpose():
    """0.99 is what makes the candidate set two pairs corpus-wide.  Lowering
    it is how the cascades get back in."""
    out = _run(_CONST_SCRIPT)
    assert out["rescue_min"] == 0.99
    assert out["null_mult"] == 3.0


def test_rescue_only_runs_in_a_sigma_bypassed_regime():
    """In production the sigma result is already used, so a rescue there would
    be a second, weaker path to the same verdict."""
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    i = src.index("n_sig_rescued = 0")
    block = src[i:i + 1800]
    assert "if regime in ('tie_panel', 'all_dups', 'short_panel'):" in block


def test_rescue_needs_the_fingerprint_to_clear_BOTH_bars():
    """Above what different fibers do in this folder AND at least what the
    same-fiber hypothesis predicts at this pair's own sigma.  Dropping either
    is how 830/831 (0.1225 against a 0.3055 floor) would come through."""
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    i = src.index("n_sig_rescued = 0")
    block = src[i:i + 1800]
    assert "_bar = _nq * _SPECKLE_CONFIRM_NULL_MULT" in block
    assert "if r_hp < _bar or r_hp < r_floor:" in block
    assert "continue" in block


def test_a_rescued_pair_still_faces_every_downstream_gate():
    """It is restored into p_dup_raw, BEFORE the length / events / twin /
    serial filters and the speckle veto - not written straight to p_dup."""
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    i_resc = src.index("n_sig_rescued = 0")
    i_phys = src.index("physical_violation = (length_violation")
    i_spk = src.index("speckle_violation = np.zeros(len(pairs), dtype=bool)")
    assert i_resc < i_phys < i_spk, "rescue must precede the gates"
    block = src[i_resc:i_resc + 1800]
    assert "p_dup_raw[i] = max(p_dup_raw[i], float(p_dup_sigma[i]))" in block
    assert "p_dup[i]" not in block, "must not bypass the gates by writing p_dup"


def test_the_rescue_reports_itself():
    """A silent promotion path inside a regime that is meant to suppress is
    exactly what nobody would think to look for."""
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    assert "Sigma rescue:" in src
    assert "confirmed by fingerprint" in src


def test_the_measured_immunity_is_recorded():
    """The safety argument is a measurement, not a claim.  If someone lowers
    the bar later, these numbers are what tells them what they are spending."""
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    i = src.index("# ── Fingerprint rescue from a sigma-bypassed regime")
    block = src[i:i + 4200]
    for marker in ("A-F West 0", "BKF<->DEL 0", "LAMBEY 0", "TULORO 0",
                   "MILTOPls0329/0330", "0.8243"):
        assert marker in block, f"missing calibration evidence: {marker}"


def test_the_emptiness_argument_is_scoped_to_LONG_folders():
    """The original comment said the candidate set is empty on every other
    sigma-bypassed folder on disk.  Measured 2026-08-31, that is true of the
    LONG ones and false of the short: the 20 short panels carry 132 to 6,081
    candidates each.  They are not rescued only because the confirm bar,
    _SPECKLE_CONFIRM_NULL_MULT x the folder's own null p99, exceeds 1.0 on
    every span class below 78 km - and a Pearson r cannot reach 1.0.

    That makes the short-panel safety ARITHMETIC, not emptiness.  This test
    exists so the distinction survives: the unqualified sentence is exactly
    what someone repairing the bar would lean on, and leaning on it is how
    this path inherits the BETA flood."""
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    i = src.index("# ── Fingerprint rescue from a sigma-bypassed regime")
    block = src[i:i + 4200]

    assert "every other LONG sigma-bypassed folder" in block, (
        "the emptiness claim must stay scoped to long folders")
    assert "empty on every other sigma-bypassed folder" not in src, (
        "the unqualified claim is back; short panels are not empty")

    # The numbers that make the scope checkable rather than asserted.
    for marker in ("132 to 6,081", "2,953", "EXCEEDS 1.0", "safe by ARITHMETIC"):
        assert marker in block, f"missing short-panel evidence: {marker}"

    # And the instruction to whoever repairs the bar.
    assert "re-measure" in block and "BEFORE lowering it" in block


_GATE_ORDER_SCRIPT = r"""
import sys, json, inspect
sys.path.insert(0, sys.argv[1])
import report_sor as R
src = inspect.getsource(R._analyze_sor)
out = {
  'rescue_after_sigma_bypass':
      src.index('p_dup_sigma_eff = np.zeros_like') < src.index('n_sig_rescued = 0'),
  'speckle_ctx_before_rescue':
      src.index('def _spk_null()') < src.index('n_sig_rescued = 0'),
  'rescue_before_events':
      src.index('n_sig_rescued = 0') < src.index('events_violation = np.zeros'),
}
print(json.dumps(out))
"""


def test_the_ordering_holds_at_runtime():
    """Source-slice asserts can pass on a file that no longer composes; check
    the real function body."""
    out = _run(_GATE_ORDER_SCRIPT)
    assert out["rescue_after_sigma_bypass"] is True
    assert out["speckle_ctx_before_rescue"] is True, (
        "the speckle context must be hoisted above the rescue or _spk_null is undefined")
    assert out["rescue_before_events"] is True
