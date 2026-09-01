"""The .trc lineage must not call a short folder "every file is the same fiber".

`report.py` grew the four-regime classifier from `report_sor.py`, but never
received the two guards `report_sor.py` added afterwards
(`_ALLDUPS_MIN_SPAN_M` 2026-07, `_ALLDUPS_MIN_HIGHR_FRAC` 2026-07-31).  So the
original `bulk_r >= 0.7 and bulk_sigma < 0.10` stood here unguarded, and
all_dups then applies a WIDENED 0.85-0.95 r-ramp to RAW r - every pair above
0.95 is reported at p_dup 1.0.

Measured 2026-08-31 on two real 31 m folders, 12 files each, same instrument
(FTBx-730C-SM2-OPM-EA sn 870995), same 1552.9 nm, same 5 ns pulse, acquired
38 minutes apart:

    retruetest    ONE fiber shot 12x, 66 REAL duplicates
                  bulk_r 0.9654   bulk_sigma 0.0601   61/66 at r >= 0.95
    LSC1->LSC6    12 DIFFERENT fibers, ZERO duplicates
                  bulk_r 0.9621   bulk_sigma 0.0571   58/66 at r >= 0.95

Both fire the trigger and bulk_r separates them by 0.003.  The folder with no
duplicates in it would report 58 of its 66 pairs at p_dup 1.0.  That is the
bug: the rule cannot tell one fiber shot twelve times from twelve fibers, so
its confident verdict was luck rather than detection.

BEHAVIOUR CHANGE, stated rather than buried: `newbeta` (12 .trc, 31.4 m) goes
from 66 of 66 flagged to 0 of 66.  Those 66 pairs really are the same fiber,
so this is recall given up - but the identical verdict was being produced for
a folder containing no duplicates, so the 66 was never evidence.  Measured
ripple over every .trc folder on disk: newbeta is the ONLY one that moves.
TEST DUPE (18 files, 67.5 km) keeps all six of its known duplicates.

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


_CLASSIFY_SCRIPT = r"""
import sys, json
sys.path.insert(0, sys.argv[1])
import numpy as np
import report as R

def folder(n, bulk_r, bulk_sigma, length_m, frac_high_r=1.0):
    '''A batch whose upper triangle has the requested medians, and the
    requested FRACTION of pairs at r >= 0.95.'''
    rm = np.full((n, n), float(bulk_r))
    sm = np.full((n, n), float(bulk_sigma))
    iu = np.triu_indices(n, k=1)
    m = len(iu[0])
    n_hi = int(round(frac_high_r * m))
    vals = np.full(m, 0.10)          # well below 0.95
    vals[:n_hi] = max(float(bulk_r), 0.96) if bulk_r >= 0.95 else 0.96
    # keep the MEDIAN at bulk_r regardless of the high-r fraction
    if n_hi < m:
        vals[n_hi:] = float(bulk_r) if frac_high_r < 0.5 else 0.10
    if frac_high_r >= 0.5:
        vals[n_hi:] = float(bulk_r)
    for k, (i, j) in enumerate(zip(*iu)):
        rm[i, j] = rm[j, i] = vals[k]
    np.fill_diagonal(rm, 1.0)
    # force the medians the classifier will read
    med_r = float(np.median(rm[iu]))
    files = [{'wl': {1550: {'length_m': float(length_m)}}} for _ in range(n)]
    batch = {1550: {'sigma_matrix': sm, 'r_matrix': rm}}
    reg, bs, br = R._classify_regime_multiwl(files, batch, [1550])
    return {'regime': reg, 'bulk_sigma': round(bs, 4), 'bulk_r': round(br, 4),
            'median_r_seen': round(med_r, 4)}

out = {}
out['consts'] = [R._ALLDUPS_MIN_SPAN_M, R._ALLDUPS_MIN_HIGHR_FRAC]

# The measured shape of BOTH 31 m folders: 12 files, high bulk r, low sigma.
out['short_31m'] = folder(12, 0.9654, 0.0601, 31.5)
out['short_62m'] = folder(12, 0.9600, 0.0570, 62.0)
# A short PANEL keeps its own route.
out['short_panel'] = folder(60, 0.9600, 0.0570, 31.0)
# A genuine long all-duplicates folder must still be claimed.
out['long_alldups'] = folder(12, 0.9800, 0.0500, 80000.0, frac_high_r=1.0)
# Long, high bulk r, but self-refuting: almost no pairs near-identical.
out['long_refuted'] = folder(12, 0.7500, 0.0500, 80000.0, frac_high_r=0.0)
# Exactly at the span boundary, and one metre under it.
out['at_span_floor'] = folder(12, 0.9800, 0.0500, 15000.0, frac_high_r=1.0)
out['under_span_floor'] = folder(12, 0.9800, 0.0500, 14999.0, frac_high_r=1.0)
# Ordinary production folder is untouched.
out['production'] = folder(12, 0.3000, 0.4000, 80000.0, frac_high_r=0.0)
print(json.dumps(out))
"""


def test_a_31m_folder_is_no_longer_claimed_as_all_dups():
    """The measured case.  Both the true folder and the zero-duplicate folder
    land here, so all_dups must claim neither."""
    out = _run(_CLASSIFY_SCRIPT)
    assert out["short_31m"]["regime"] != "all_dups", out["short_31m"]
    assert out["short_62m"]["regime"] != "all_dups", out["short_62m"]


def test_a_short_panel_still_routes_short_panel():
    """The guards must not steal folders from the short_panel route."""
    out = _run(_CLASSIFY_SCRIPT)
    assert out["short_panel"]["regime"] == "short_panel", out["short_panel"]


def test_a_genuine_long_all_duplicates_folder_is_still_claimed():
    """The regime still exists and still fires where bulk_r means something."""
    out = _run(_CLASSIFY_SCRIPT)
    assert out["long_alldups"]["regime"] == "all_dups", out["long_alldups"]


def test_the_span_floor_is_inclusive():
    """15,000 m clears; 14,999 does not.  An off-by-one here silently changes
    which folders get the widened ramp."""
    out = _run(_CLASSIFY_SCRIPT)
    assert out["at_span_floor"]["regime"] == "all_dups", out["at_span_floor"]
    assert out["under_span_floor"]["regime"] != "all_dups", out["under_span_floor"]


def test_a_self_refuted_claim_routes_production_not_tie_panel():
    """bulk_r >= 0.7 would otherwise hand it to tie_panel, which bypasses the
    sigma-outlier that a long span of unique fibers actually needs.  This is
    the BKF<->DEL failure, and it must land the same way on both lineages."""
    out = _run(_CLASSIFY_SCRIPT)
    assert out["long_refuted"]["regime"] == "production", out["long_refuted"]


def test_production_is_untouched():
    out = _run(_CLASSIFY_SCRIPT)
    assert out["production"]["regime"] == "production", out["production"]


def test_the_two_lineages_agree_on_the_guard_values():
    """Drift between the lineages IS the bug being fixed.  They are kept
    namespace-isolated, so each carries its own copy; this is what stops the
    copies diverging again."""
    out = _run(_CLASSIFY_SCRIPT)
    assert out["consts"] == [15000.0, 0.5], out["consts"]

    sor = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    assert "_ALLDUPS_MIN_SPAN_M = 15000.0" in sor
    assert "_ALLDUPS_MIN_HIGHR_FRAC = 0.5" in sor


def test_the_unguarded_rule_is_gone_from_the_trc_lineage():
    """The exact line that shipped.  A refactor that reinstates it puts the
    58-false-positive verdict straight back."""
    src = (SECRETSAUCE_DIR / "report.py").read_text(encoding="utf-8")
    i = src.index("def _classify_regime_multiwl(")
    body = src[i:i + 6000]
    assert "if bulk_r >= 0.7 and bulk_sigma < 0.10:\n        regime = 'all_dups'" not in body, (
        "the unguarded all_dups rule is back")
    assert "_ALLDUPS_MIN_SPAN_M" in body and "_ALLDUPS_MIN_HIGHR_FRAC" in body


def test_the_measured_evidence_is_recorded_beside_the_fix():
    """Both folders, both verdicts and the behaviour change.  Whoever weakens
    this later should have to read what it costs."""
    src = (SECRETSAUCE_DIR / "report.py").read_text(encoding="utf-8")
    i = src.index("def _classify_regime_multiwl(")
    body = src[i:i + 6000]
    for marker in ("retruetest", "LSC1->LSC6", "0.9654", "0.9621",
                   "58/66", "61/66", "newbeta"):
        assert marker in body, f"missing evidence: {marker}"
