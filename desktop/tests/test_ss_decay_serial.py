"""Regression tests for the EMVSUI missed-duplicate package.

Reported 2026-08-29: Secret Sauce called EMVSUI 79/80 a CONFIRMED duplicate
when only those two files were in the folder and "Unique" when all 1152
were.  The whole-folder run flagged ZERO of 662,976 pairs at any tier.

Two independent defects, one per fix below.

  1. REGIME MISROUTE — `_neighbor_decay` reads a drop in raw r between
     near-port and far-port pairs as evidence of shared launch glass, and
     re-routes the folder to tie_panel, which bypasses sigma-outlier
     entirely.  EMVSUI was shot by TWO OTDRs interleaved across the port
     range over five days, so the far bucket filled with cross-instrument
     pairs that decorrelate for reasons unrelated to port distance.  Split
     out: port distance costs 0.069 of the drop, instrument costs 0.472.
     Within one OTDR the rule does not fire at all.  Fixed by making the
     decay buckets same-instrument, the way they were already same-prefix.

  2. EVENTS GATE — `_events_agree` averaged |Δloss| over every matched
     event including the launch-reel mating, which is genuinely re-made
     between acquisitions and so legitimately differs.  On pairs shot hours
     apart that one event carried the mean over the 10 mdB threshold and
     capped confirmed duplicates at 0.5 (EMVSUI 563/564 mean 0.0214 vs
     median 0.0030; 296/308 mean 0.0238 vs median 0.0020).  It also counted
     unmatched events sitting at the firmware's own detection threshold
     against the pair.  Fixed by gating on the MEDIAN and by dropping
     sub-flicker unmatched events from the count denominator.

Namespace isolation rule: the Secret Sauce engine is only ever exercised
through subprocesses, never imported into the test process.
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


# ── 1. Decay detector must not mix instruments ─────────────────────────────

_DECAY_SCRIPT = r"""
import sys, json
sys.path.insert(0, sys.argv[1])
import numpy as np
from report_sor import _neighbor_decay

out = {}
# The EMVSUI shape: 60 ports, ONE prefix, TWO OTDRs interleaved in blocks.
# Within an instrument r is flat (no shared-glass decay at all).  Across
# instruments r is low.  Near pairs are mostly same-instrument, far pairs
# are mostly cross-instrument, so the POOLED far median collapses and the
# folder looks like a tie panel when it is nothing of the kind.
names = ['EMVSUI%04d' % i for i in range(1, 61)]
K = len(names)
# Blocks of 10 ports alternate between the two boxes.
serial = ['1723356' if (i // 10) % 2 == 0 else '1876271' for i in range(K)]
r = np.empty((K, K))
for i in range(K):
    for j in range(K):
        r[i, j] = 0.75 if serial[i] == serial[j] else 0.20
np.fill_diagonal(r, 1.0)

out['pooled'] = _neighbor_decay(names, r)                    # serials ignored
out['per_instrument'] = _neighbor_decay(names, r, serial)    # serials honoured

# A genuine shared-glass panel shot entirely on ONE box must still fire.
names2 = ['PANEL%04d' % i for i in range(1, 61)]
one_box = ['1723356'] * 60
r2 = np.full((60, 60), 0.05)
for i in range(60):
    for j in range(60):
        if abs(i - j) <= 3:
            r2[i, j] = 0.80
np.fill_diagonal(r2, 1.0)
out['real_panel'] = _neighbor_decay(names2, r2, one_box)

# Missing serials fail OPEN: identical to passing none at all.
half_blank = [None] * 60
out['all_blank'] = _neighbor_decay(names, r, half_blank)
out['no_serials'] = _neighbor_decay(names, r, None)
print(json.dumps(out))
"""


def test_decay_pooled_far_bucket_is_the_bug():
    """Without instrument separation the synthetic EMVSUI folder trips the
    0.30 drop even though NO bucket has any port-distance structure."""
    out = _run(_DECAY_SCRIPT)
    near, far, _, _ = out["pooled"]
    assert near - far >= 0.30, (near, far)


def test_decay_is_quiet_once_buckets_are_same_instrument():
    """Same matrix, same names, serials honoured: the drop disappears, so
    the folder stays in production and sigma-outlier keeps working."""
    out = _run(_DECAY_SCRIPT)
    near, far, n_near, n_far = out["per_instrument"]
    assert abs(near - far) < 1e-9, out["per_instrument"]
    assert near - far < 0.30
    assert n_near >= 10 and n_far >= 10, "buckets must still be populated"


def test_decay_still_fires_on_a_real_single_instrument_panel():
    """The rule exists to catch shared-glass tie panels (A-F West, 1,997
    false positives).  Those are shot on one box, so the fix must leave
    them firing exactly as before."""
    out = _run(_DECAY_SCRIPT)
    near, far, _, _ = out["real_panel"]
    assert abs(near - 0.80) < 1e-9 and abs(far - 0.05) < 1e-9, out["real_panel"]
    assert near - far >= 0.30


def test_decay_missing_serial_fails_open():
    """An unknown instrument is not evidence — a folder whose files carry
    no serial must behave exactly as it did before this change."""
    out = _run(_DECAY_SCRIPT)
    assert out["all_blank"] == out["no_serials"], (out["all_blank"],
                                                   out["no_serials"])
    assert out["no_serials"][0] - out["no_serials"][1] >= 0.30


# ── 2. Events gate: median, and flicker-tolerant counting ──────────────────

_GATE_SCRIPT = r"""
import sys, json
sys.path.insert(0, sys.argv[1])
from report import _events_agree, _event_match_quality, _EVENT_FLICKER_DB

out = {'floor': _EVENT_FLICKER_DB}

# EMVSUI 563/564 shape: a long span whose splices all agree to a few mdB,
# with ONE big disagreement at the launch-reel mating (re-made between two
# shots 6.9 h apart).  The mean is carried over the threshold by that one
# event; the median is not.
launch = [0.170]
body = [0.003, 0.002, 0.005, 0.001, 0.003, 0.001, 0.001, 0.007]
vals = launch + body
mean = sum(vals) / len(vals)
srt = sorted(vals)
median = srt[len(srt) // 2] if len(srt) % 2 else (srt[len(srt)//2-1]+srt[len(srt)//2])/2
out['mean'] = mean
out['median'] = median
out['old_rule'] = _events_agree(9, 9, 9, mean)
out['new_rule'] = _events_agree(9, 9, 9, mean, median_dloss_db=median,
                                n_max_significant=9)

# A pair that disagrees BROADLY (different fibers) must still cap: the
# median is high too, so moving off the mean does not weaken the gate.
out['broad_mismatch'] = _events_agree(9, 9, 9, 0.070,
                                      median_dloss_db=0.071,
                                      n_max_significant=9)

# Count agreement: an unmatched event BELOW the flicker floor is detection
# noise and must not cap; one ABOVE it is a real disagreement and must.
base = [{'dist_km': 1.0, 'splice_loss': 0.40},
        {'dist_km': 5.0, 'splice_loss': 0.10},
        {'dist_km': 9.0, 'splice_loss': 0.20},
        {'dist_km': 13.0, 'splice_loss': 0.15}]
small = base + [{'dist_km': 7.0, 'splice_loss': 0.02}]
big = base + [{'dist_km': 7.0, 'splice_loss': 0.30}]
for tag, other in (('small', small), ('big', big)):
    q = _event_match_quality(base, other)
    out['q_' + tag] = list(q)
    out['agree_' + tag] = _events_agree(q[0], q[1], q[2], q[3],
                                        median_dloss_db=q[5],
                                        n_max_significant=q[6])

# Backward compatibility: the 4-argument form is byte-for-byte the old rule.
out['compat'] = [_events_agree(2, 5, 2, 0.001), _events_agree(0, 6, 1, 0.0),
                 _events_agree(0, 5, 0, 0.0), _events_agree(0, 0, 0, 0.0),
                 _events_agree(6, 6, 6, 0.002), _events_agree(6, 6, 6, 0.050),
                 _events_agree(4, 9, 6, 0.001)]
print(json.dumps(out))
"""


def test_launch_connector_no_longer_caps_a_duplicate():
    out = _run(_GATE_SCRIPT)
    assert out["mean"] > 0.010, "fixture must reproduce the over-threshold mean"
    assert out["median"] <= 0.010
    assert out["old_rule"] is False, "the mean rule is what capped 563/564"
    assert out["new_rule"] is True


def test_broadly_disagreeing_tables_still_cap():
    """Moving to the median must not weaken the gate on pairs whose event
    tables disagree everywhere rather than at one connector."""
    out = _run(_GATE_SCRIPT)
    assert out["broad_mismatch"] is False


def test_sub_threshold_unmatched_event_is_flicker_not_evidence():
    out = _run(_GATE_SCRIPT)
    n_match, n_max, _, _, _, _, n_sig = out["q_small"]
    assert (n_match, n_max, n_sig) == (4, 5, 4), out["q_small"]
    assert out["agree_small"] is True


def test_significant_unmatched_event_still_counts_against_the_pair():
    out = _run(_GATE_SCRIPT)
    n_match, n_max, _, _, _, _, n_sig = out["q_big"]
    assert (n_match, n_max, n_sig) == (4, 5, 5), out["q_big"]
    assert out["agree_big"] is False


def test_four_argument_form_is_unchanged():
    """Every existing caller and the calibration behind it keeps the old
    mean/n_max behaviour when the robust statistics aren't supplied."""
    out = _run(_GATE_SCRIPT)
    assert out["compat"] == [False, False, False, True, True, False, False]


# ── 3. Source locks ────────────────────────────────────────────────────────

def test_source_locks_decay_and_gate():
    sor = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    rep = (SECRETSAUCE_DIR / "report.py").read_text(encoding="utf-8")

    # The decay call must pass serials — dropping the argument silently
    # restores the misroute.
    assert "_neighbor_decay(names_raw, r_raw, serials_raw)" in sor
    assert "eligible = upper & same_pref & both_ports & same_ser" in sor

    # The flicker floor is calibrated; pin it.
    assert "_EVENT_FLICKER_DB = 0.040" in rep

    # Both engines must hand the robust statistics to the gate.
    assert sor.count("median_dloss_db=median_dloss") == 1
    assert rep.count("median_dloss_db=median_dloss") == 1
    assert sor.count("n_max_significant=n_max_sig") == 1
    assert rep.count("n_max_significant=n_max_sig") == 1

    # The threshold itself did NOT move — only the statistic fed to it.
    assert "loss_thresh_db=0.010" in rep
    assert "frac_thresh=0.85" in rep


# ── 3b. Twin gate refuted by the fingerprint ───────────────────────────────

def test_twin_refutation_requires_both_conditions():
    """The refutation must demand BOTH that the pair fingerprints far above
    the folder's different-fiber null AND that the specific rival which
    raised the objection fingerprints AT the null.  Dropping either turns
    it into a way to re-open ribbon-ladder false positives."""
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    i = src.index("n_uniq_refuted = 0")
    block = src[i:i + 1600]
    # condition 1 — the pair itself must clear the bar
    assert "bar = nq * _SPECKLE_CONFIRM_NULL_MULT" in block
    assert "if r_pair is None or r_pair < bar:" in block
    # condition 2 — the rival must NOT
    assert "if r_rival is None or r_rival >= nq:" in block
    # and only ever for pairs whose ONLY objection was the twin gate
    assert ("if uniq_violation[i] and not (length_violation[i]" in block
            and "or events_violation[i]" in block
            and "or serial_violation[i])" in block)


def test_twin_refutation_constant_is_pinned():
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    assert "_SPECKLE_CONFIRM_NULL_MULT = 3.0" in src
    assert "_UNIQ_TWIN_RATIO = 0.5" in src, "twin ratio itself must NOT move"


def test_twin_refutation_reports_itself():
    """A silent promotion path is unauditable — the run log must say when
    it fired, the way the speckle gate always logs itself."""
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    assert "objection(s) refuted by the fingerprint" in src


def test_speckle_null_is_built_once_and_shared():
    """The twin refutation and the speckle gate must read the SAME folder
    null; two independently-sampled nulls could disagree with each other.

    The null is now keyed by WAVELENGTH (speckle is a lambda-dependent
    interference pattern, so one pooled null describes neither mode of a
    two-lambda folder).  The property this test protects is unchanged: ONE
    sampling site, ONE cache, every consumer reading it."""
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    assert src.count("np.percentile(null_vals") == 1, "null sampled twice"
    assert "_spk['null_q'][key] = float(np.percentile(null_vals," in src, (
        "the per-wavelength cache must be the only place the null is stored")
    # The run-log line still reports the pooled value explicitly.
    assert "null_q = _spk_null(None)" in src
    # ...and every JUDGING site goes through the per-pair helper.
    assert src.count("_spk_null()") == 0, "a consumer still pools wavelengths"
