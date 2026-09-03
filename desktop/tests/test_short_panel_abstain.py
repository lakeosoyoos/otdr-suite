"""short_panel must abstain: its r-ramp separated nothing, in either direction.

`report_sor.py` carried this claim in the regime's own branch:

    # Standard production ramp - true same-fiber re-shoots in a short
    # panel still produce r >= 0.95. With sigma-outlier disabled below,
    # the r-tier is the entire detector for this regime.

Measured 2026-08-31 on two 31 m folders, same instrument (FTBx-730C-SM2-OPM-EA
sn 870995), same 1552.9 nm, same 5 ns pulse, acquired 38 minutes apart.  RAW r
is what this ramp reads:

    retruetest    ONE fiber x12, 66 REAL dups    p50 0.9642  min 0.9423  max 0.9874
    LSC1->LSC6    288 DIFFERENT fibers, 0 dups   p50 0.9618  min 0.9177  max 0.9926

The different-fiber MAXIMUM is ABOVE the true-fiber maximum, and 16,376
different-fiber pairs sit above the true-pair median.  There is no threshold
on this axis that keeps duplicates on one side of it.

What the ramp actually produced, measured on every short panel on disk:

    folder                     >=0.99   >=0.50   >=0.10   truth
    LSC1->LSC6      31 m            0    7,588   32,732   ZERO duplicates
    REUB PTL5 A     31 m            0       34    3,357   none known
    BETA LFY E DW   62 m            0        0        0   none known
    BETA ORN W SW   62 m            0        0        0   none known
    Cle Elum E 144f 68 m            0        0        0   Yupana list
    Cle Elum W 144f 68 m            0        0        0   Yupana list

Zero true positives anywhere - including both trays that carry a known
duplicate list - against 7,588 cells at >=0.50 on a folder proven to hold no
duplicates.  After the change every one of those columns reads 0.

WHY ABSTENTION AND NOT A DIFFERENT RAMP.  At these spans the same-fiber and
different-fiber distributions are NESTED, not shifted: 0/66 at zero false
positives in ten configurations against a matched same-instrument null, and
0/48 on a zero-confound control.  A retuned threshold would be fitting noise.

CAVEAT, recorded rather than glossed: each Cle Elum tray was run alone, so a
Yupana duplicate pairing fibers ACROSS trays would not have both members in
the run.  The LSC result is the airtight one - a folder with no duplicates in
it produced 7,588 cells at >=0.50.

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


_RAMP_SCRIPT = r"""
import sys, json, inspect, re
sys.path.insert(0, sys.argv[1])
import report_sor as RS

src = inspect.getsource(RS._analyze_sor)
_k = src.index('R_LO, R_HI = 0.999, 0.9999')      # anchor on the ramp itself
_k = src.rindex("if regime == 'tie_panel':", 0, _k)
i = src.rindex('\n', 0, _k) + 1          # keep the line's own indentation
block = src[i:src.index('_R_SPAN =', _k)]

# Rebuild the ramp table exactly as the function assigns it, per regime.
def ramp_for(regime):
    ns = {'regime': regime, 'R_LO': 'UNSET', 'R_HI': 'UNSET'}
    # execute just the if/elif chain, dedented to module level
    import textwrap
    chain = textwrap.dedent(block)
    exec(compile(chain, '<chain>', 'exec'), {}, ns)
    return ns['R_LO'], ns['R_HI']

out = {}
for r in ('tie_panel', 'all_dups', 'short_panel', 'production'):
    lo, hi = ramp_for(r)
    out[r] = [lo, hi]

# The abstaining branch must make _r_to_p return 0 for EVERY r, including
# values that would score 1.0 under any ordinary ramp.
def r_to_p(R_LO, R_HI, r):
    _R_SPAN = None if R_LO is None else R_HI - R_LO
    if R_LO is None:
        return 0.0
    if r is None:
        return 0.0
    if r >= R_HI:
        return 1.0
    if r <= R_LO:
        return 0.0
    return float((r - R_LO) / _R_SPAN)

lo, hi = out['short_panel']
out['short_panel_scores'] = [r_to_p(lo, hi, v)
                             for v in (None, -1.0, 0.0, 0.9423, 0.9874,
                                       0.9926, 0.99999, 1.0)]
plo, phi = out['production']
out['production_scores'] = [r_to_p(plo, phi, v)
                            for v in (0.94, 0.95, 0.97, 0.99, 1.0)]
print(json.dumps(out))
"""


def test_short_panel_abstains():
    """No ramp at all, not a wider or narrower one."""
    out = _run(_RAMP_SCRIPT)
    assert out["short_panel"] == [None, None], out["short_panel"]


def test_abstention_scores_zero_for_every_r():
    """Including 0.9926 - the highest DIFFERENT-fiber r measured - and 1.0.
    A regime that abstains must not have a value that sneaks through."""
    out = _run(_RAMP_SCRIPT)
    assert out["short_panel_scores"] == [0.0] * 8, out["short_panel_scores"]


def test_every_other_regime_is_untouched():
    """The change must be confined to short_panel.  These three ramps carry
    the long-span verdicts and are frozen."""
    out = _run(_RAMP_SCRIPT)
    assert out["tie_panel"] == [0.999, 0.9999], out["tie_panel"]
    assert out["all_dups"] == [0.85, 0.95], out["all_dups"]
    assert out["production"] == [0.95, 0.99], out["production"]
    assert out["production_scores"] == [0.0, 0.0, 0.5, 1.0, 1.0], (
        out["production_scores"])


def test_the_false_claim_is_gone():
    """The sentence that justified the ramp.  If it comes back, so does the
    ramp it was defending."""
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    assert "true same-fiber re-shoots in a short\n        # panel still produce r ≥ 0.95" not in src
    assert "the r-tier is the entire detector for this regime" not in src


def test_the_refuting_measurement_is_recorded():
    """The two folders, both maxima, and the yield table.  Whoever reinstates
    a ramp here should have to read what the last one produced."""
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    i = src.index("elif regime == 'short_panel':")
    block = src[i:i + 3600]
    for marker in ("retruetest", "LSC1->LSC6", "0.9874", "0.9926",
                   "7,588", "Cle Elum", "NESTED"):
        assert marker in block, f"missing evidence: {marker}"


def test_the_guard_is_in_the_scoring_helper_not_just_the_table():
    """R_LO = None has to be handled where the score is computed, or the
    abstaining regime raises a TypeError on the first pair instead."""
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    i = src.index("_R_SPAN = None if R_LO is None else R_HI - R_LO")
    block = src[i:i + 500]
    assert "if R_LO is None:" in block
    assert block.index("if R_LO is None:") < block.index("if r is None:"), (
        "the abstention check must come first; r >= R_HI on None would raise")
