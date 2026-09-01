"""A folder approaching the all_dups sigma cliff must say so.

`bulk_r >= 0.7 and bulk_sigma < 0.10` is a hard step.  On one side a folder is
ordinary; on the other EVERY pair is judged by the widened 0.85-0.95 r-ramp,
and a 1,152-fiber span can report the large majority of its pairs as
duplicates.  Nothing warned when a folder approached it.

Measured 2026-09-01, the two closest real spans on disk:

    NEWELM json  1152f  26,092 m  bulk_r 0.9861  sigma 0.1256   0.0256 clear
    ELMNEW json  1152f   5,460 m  bulk_r 0.9867  sigma 0.1234   0.0234 clear

Both clear the bulk_r half of the trigger outright.  ELMNEW is additionally
blocked by the 15 km span floor PR #134 added; NEWELM, at 26 km, is not - it
is held out by sigma alone.  Neither floods today (0 pairs at >=99% on the
full verdict path); both are one small sigma shift from it.

WHAT THIS IS NOT.  It does not move the cliff, reroute anything, or change a
verdict.  Replacing `bulk_sigma < 0.10` would alter the regime router, which
decides everything downstream on every folder including the .sor spans techs
use daily - the highest-blast-radius change available in this engine - and
the case for making it is a folder that has actually tipped, which does not
exist yet.  This buys the warning without betting the router on it.

SELECTIVITY, measured over every folder whose bulk stats were taken this
session: fires on 2 of 12.  ELMMIL (r 0.0777), SANDUR (r 0.0963), DURSAN
(r 0.0340), MILELM (r 0.5028), TEST DUPE (r 0.6062) are all far from the
bulk_r half; newbeta, retruetest and LSC sit at sigma 0.057-0.060, outside
the margin on the other side.

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


_MARGIN_SCRIPT = r"""
import sys, json
sys.path.insert(0, sys.argv[1])
import report_sor as RS
import report as R

def kind(fn, r, s):
    n = fn(r, s)
    if n is None: return None
    return 'NEAR' if 'NEAR' in n else 'INSIDE'

CORPUS = [
 ("NEWELM",    0.9861, 0.1256), ("ELMNEW",     0.9867, 0.1234),
 ("ELMMIL",    0.0777, 0.0901), ("MILELM",     0.5028, 0.1059),
 ("SANDUR",    0.0963, 0.0857), ("DURSAN",     0.0340, 0.0850),
 ("newbeta",   0.9973, 0.0575), ("TEST DUPE",  0.6062, 0.0884),
 ("retruetest",0.9654, 0.0601), ("LSC",        0.9621, 0.0571),
 ("LONGS",     0.0000, 0.5000), ("MILTOP",     0.7214, 0.2000),
]
out = {}
out['corpus'] = {nm: kind(RS._regime_margin_note, r, s) for nm, r, s in CORPUS}
# the two lineages must agree pair-for-pair
out['lineages_agree'] = all(
    RS._regime_margin_note(r, s) == R._regime_margin_note(r, s)
    for _nm, r, s in CORPUS)
out['consts'] = [RS._ALLDUPS_SIGMA_CLIFF, RS._ALLDUPS_SIGMA_MARGIN,
                 R._ALLDUPS_SIGMA_CLIFF, R._ALLDUPS_SIGMA_MARGIN]
out['edges'] = {f'{r}/{s}': kind(RS._regime_margin_note, r, s) for r, s in
                ((0.9, 0.13), (0.9, 0.1301), (0.9, 0.07), (0.9, 0.0699),
                 (0.70, 0.11), (0.6999, 0.11), (0.9, 0.099), (0.9, 0.1000))}
out['none_safe'] = [RS._regime_margin_note(None, 0.1),
                    RS._regime_margin_note(0.9, None),
                    RS._regime_margin_note(None, None)]
out['near_text'] = RS._regime_margin_note(0.9861, 0.1256)
out['inside_text'] = RS._regime_margin_note(0.9861, 0.0900)
print(json.dumps(out))
"""


def test_it_fires_on_the_two_folders_that_are_actually_near():
    out = _run(_MARGIN_SCRIPT)
    c = out["corpus"]
    assert c["NEWELM"] == "NEAR", c["NEWELM"]
    assert c["ELMNEW"] == "NEAR", c["ELMNEW"]


def test_it_stays_quiet_on_everything_else():
    """A warning that fires on ordinary folders is noise, and noise is how a
    real warning gets ignored."""
    out = _run(_MARGIN_SCRIPT)
    c = out["corpus"]
    quiet = [k for k, v in c.items() if v is None]
    assert len(quiet) == 10, c
    for name in ("ELMMIL", "MILELM", "SANDUR", "DURSAN", "newbeta",
                 "TEST DUPE", "retruetest", "LSC", "LONGS", "MILTOP"):
        assert c[name] is None, (name, c[name])


def test_the_bulk_r_half_of_the_trigger_must_also_be_met():
    """sigma near 0.10 is harmless if bulk_r is nowhere near 0.70 - that is
    why ELMMIL (sigma 0.0901, INSIDE the cutoff) does not warn."""
    out = _run(_MARGIN_SCRIPT)
    assert out["edges"]["0.7/0.11"] == "NEAR"
    assert out["edges"]["0.6999/0.11"] is None
    assert out["corpus"]["ELMMIL"] is None


def test_the_margin_is_symmetric_and_exact():
    out = _run(_MARGIN_SCRIPT)
    e = out["edges"]
    assert e["0.9/0.13"] == "NEAR" and e["0.9/0.1301"] is None
    assert e["0.9/0.07"] == "INSIDE" and e["0.9/0.0699"] is None
    # the cutoff itself is on the NEAR side: 0.10 is not < 0.10
    assert e["0.9/0.099"] == "INSIDE"
    assert e["0.9/0.1"] == "NEAR"


def test_it_says_which_side_of_the_cliff():
    """'near it' and 'over it by a hair' need different responses, so they
    must not share a message."""
    out = _run(_MARGIN_SCRIPT)
    assert "NEAR the all_dups cliff" in out["near_text"]
    assert "0.0256" in out["near_text"] and "0.9861" in out["near_text"]
    assert "widened 0.85-0.95 ramp" in out["near_text"]
    assert "INSIDE the all_dups cliff" in out["inside_text"]


def test_it_never_raises_on_missing_stats():
    out = _run(_MARGIN_SCRIPT)
    assert out["none_safe"] == [None, None, None]


def test_both_lineages_carry_the_same_rule():
    """The divergence between these two engines is the defect that produced
    this whole series; a warning present in only one is the same mistake."""
    out = _run(_MARGIN_SCRIPT)
    assert out["lineages_agree"] is True
    assert out["consts"] == [0.10, 0.03, 0.10, 0.03], out["consts"]


def test_it_reports_and_never_decides():
    """The note must not reach any branch that sets a regime or a p_dup."""
    for fname in ("report_sor.py", "report.py"):
        src = (SECRETSAUCE_DIR / fname).read_text(encoding="utf-8")
        for line in src.splitlines():
            if "regime_margin" not in line and "_regime_margin_note" not in line:
                continue
            s = line.strip()
            ok = (s.startswith("#") or s.startswith("def ")
                  or "print(" in s or "rows.append" in s
                  or s.startswith("regime_margin =") or s.startswith("_margin =")
                  or s.startswith("if regime_margin") or s.startswith("if _margin")
                  or "'regime_margin':" in s or "analysis.get('regime_margin')" in s
                  or "analysis['regime_margin']" in s
                  or "regime_margin=" in s or "regime_margin)" in s
                  or "return all_pairs, regime, regime_margin" in s
                  or "regime_margin = _build_pairs_multiwl" in s
                  or "all_pairs, regime, regime_margin" in s)
            assert ok, f"{fname}: margin note used in a decision path: {s}"


def test_the_margin_is_threaded_not_recomputed_downstream():
    """In tie_panel mode the pair list carries FINGERPRINT-EXTRACTED r, so a
    renderer recomputing bulk r from it would warn against a different number
    than the router actually saw."""
    src = (SECRETSAUCE_DIR / "report.py").read_text(encoding="utf-8")
    assert "return all_pairs, regime, regime_margin" in src
    assert "regime_margin=None" in src, "renderer must accept it"
    assert src.count("regime_margin=regime_margin") == 2, (
        "both xlsx call sites must pass it")
    assert "FINGERPRINT-EXTRACTED r" in src, "the reason must survive"
