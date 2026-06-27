"""Regression / correctness gate for the helix (EFL) calibration tool.

This is the gate that proves the calibration tool *recovers a known excess
fiber length* and that its IOR guardrail *fires on a corrupted trace* — the two
things that, if they silently broke, would hand a tech a wrong cable-sheath
conversion factor.

How the known-truth fixtures are built
--------------------------------------
We do NOT forge SOR binaries here.  Instead we synthesize *parsed-trace
records* in the exact shape ``helixcal.sor_fields.read_trace_record`` returns
(events list + stored_ior + genparams + eof_km), then feed them through the
REAL ``helixcal.calibrate.calibrate`` math.  The events we build still flow
through the suite's own ``sor_reader324802a._interior_events`` filter (via
``_interior_event_distances_m``), so the production event-resolution path is
exercised, not bypassed.

Injecting a KNOWN EFL
---------------------
A true line  ``y_known = m_true * x_otdr + b_true``  is chosen with

    m_true = 1 / 1.025  ≈ 0.975610            (a 2.5 % excess fiber length)
    EFL%_true = (1/m_true - 1) * 100 = 2.5 %

For each closure we pick a known cable-sheath footage ``y`` and back out the
OTDR fiber distance the trace would have reported,
``x = (y - b_true) / m_true``, and place an interior event there.  The matching
anchor table carries the SAME ``y``.  Because the trace's x and the anchor's y
sit exactly on the injected line, ``calibrate`` must recover m_true / EFL%_true
to float precision (a real tolerance is asserted regardless).

The A and B traces of a fiber put their shared closures at the same OTDR x
(the synthetic span is symmetric), so the bidirectional A+B averaging path is
exercised and must not perturb the recovered factor.

IOR guardrail
-------------
One fiber's traces carry a stored IOR deliberately offset from the cohort by
far more than ``IOR_COHORT_TOL``.  The guardrail must (a) emit an ior_flag
naming that file and (b) hold ``ior_verified`` False even when an
``expected_ior`` matching the good cohort is supplied.
"""
from __future__ import annotations

import csv

import pytest

from conftest import REPO_ROOT  # noqa: F401 — puts REPO_ROOT on sys.path

from helixcal import anchors as anchors_mod
from helixcal.anchors import M_PER_FT
from helixcal.calibrate import (
    calibrate as run_calibrate,
    fiber_key_from_id,
    _efl_pct,
    IOR_COHORT_TOL,
)
from helixcal import report as report_mod


# ── Known truth injected into the synthetic span ────────────────────────
EFL_PCT_TRUE = 2.5                      # excess fiber length, percent
M_TRUE = 1.0 / (1.0 + EFL_PCT_TRUE / 100.0)   # = 1/1.025 ≈ 0.9756098
B_TRUE_M = 6.0                          # fixed launch/patch-cord offset, meters
GOOD_IOR = 1.46820                      # cohort / fiber-spec stored IOR
BAD_IOR = 1.45500                       # deliberately-wrong stored IOR (one fiber)

# Known cable-sheath footage (METERS) at each closure along the span.  These
# are the y-values; the trace's reported OTDR fiber distance x is derived from
# them via the injected line, so the fit's job is to invert it back to M_TRUE.
CLOSURE_KNOWN_M = [300.0, 1000.0, 1800.0, 2600.0, 3400.0, 4200.0]


# ── Synthetic parsed-trace record builders ──────────────────────────────
def _event(number, dist_km, *, reflective=False, end=False, splice_loss=0.05):
    """One KeyEvent dict in the suite's parser shape (sor_reader324802a)."""
    return {
        "number": number,
        "time_of_travel": 0,
        "dist_km": round(dist_km, 6),
        "splice_loss": splice_loss,
        "reflection": -50.0,
        "slope": 0.2,
        "type": ("1E" if end else ("1F" if reflective else "0F")),
        "is_reflective": reflective,
        "is_end": end,
    }


def _x_otdr_m_for(y_known_m):
    """Invert the injected line: the OTDR fiber distance the trace would report
    (in the A frame) for a closure whose true cable-sheath distance is
    ``y_known_m``."""
    return (y_known_m - B_TRUE_M) / M_TRUE


# A-frame OTDR distances (m) of the closures, ascending, and the shared total
# span length.  The B trace is a PHYSICAL MIRROR of the A trace: the same
# closure measured from the far end sits at ``OTDR_SPAN_TOTAL_M - xA``, and both
# traces carry the same end-of-fiber distance.  This is what makes the bidir
# flip (``eof_b - x_b``) land back on the A-frame position exactly, so A and B
# agree and the recovered intercept b is the true launch offset (not an
# artifact of a mismatched EOF).
_CLOSURE_X_A_M = sorted(_x_otdr_m_for(y) for y in CLOSURE_KNOWN_M)
OTDR_SPAN_TOTAL_M = _CLOSURE_X_A_M[-1] + 500.0   # 500 m of fiber past last splice


def _make_record(cable_id, *, direction, stored_ior, location_a, location_b):
    """Build a synthetic trace record (read_trace_record shape).

    ``direction`` is 'A' or 'B'.  For 'A' the interior splice events sit at
    their A-frame OTDR distances.  For 'B' they sit at the mirrored distance
    ``OTDR_SPAN_TOTAL_M - xA`` (the same physical closures seen from the
    opposite launch).  Both directions place the end-of-fiber at
    ``OTDR_SPAN_TOTAL_M``.

    Event layout that survives sor_reader324802a._interior_events:
      * a launch 1F at dist 0            (dropped: dist == 0)
      * one 0F interior splice per closure
      * a far-end 1F connector           (dropped: last reflective non-end)
      * a 1E end-of-fiber                (dropped: is_end)
    """
    if direction == "A":
        interior_m = list(_CLOSURE_X_A_M)
    else:
        interior_m = sorted(OTDR_SPAN_TOTAL_M - x for x in _CLOSURE_X_A_M)
    interior_x_km = [x / 1000.0 for x in interior_m]

    events = [_event(1, 0.0, reflective=True)]   # launch connector
    num = 2
    for xk in interior_x_km:
        events.append(_event(num, xk))
        num += 1
    eof_km = OTDR_SPAN_TOTAL_M / 1000.0
    far_km = eof_km - 0.20                        # far-end connector before EOF
    events.append(_event(num, far_km, reflective=True))
    num += 1
    events.append(_event(num, eof_km, end=True))  # end-of-fiber

    return {
        "filename": f"{cable_id}.sor",
        "filepath": f"/synthetic/{cable_id}.sor",
        "events": events,
        "wavelength": 1550.0,
        "acq_range": None,
        "stored_ior": stored_ior,
        "derived_ior": stored_ior,
        "ior": stored_ior,
        "ior_source": "stored",
        "genparams": {
            "cable_id": cable_id,
            "location_a": location_a,
            "location_b": location_b,
        },
        "eof_km": eof_km,
    }


# Two-endpoint span naming so direction resolution pairs A and B traces.
LOC_A = "ELMHURST"
LOC_B = "MILLER"


def _span_records(fiber_nums, *, bad_ior_fiber=None):
    """Build A (ELMMIL###) and B (MILELM###) records for each fiber number.

    ``bad_ior_fiber`` (an int) gets BAD_IOR on both its traces; everyone else
    gets GOOD_IOR.
    """
    recs = []
    for n in fiber_nums:
        ior = BAD_IOR if n == bad_ior_fiber else GOOD_IOR
        suffix = f"{n:03d}"
        recs.append(_make_record(f"ELMMIL{suffix}", direction="A",
                                  stored_ior=ior,
                                  location_a=LOC_A, location_b=LOC_B))
        recs.append(_make_record(f"MILELM{suffix}", direction="B",
                                  stored_ior=ior,
                                  location_a=LOC_B, location_b=LOC_A))
    return recs


def _write_anchor_csv(path, fiber_nums):
    """One closure anchor per fiber per CLOSURE_KNOWN_M, located by event_index
    in the A frame, carrying the KNOWN cable-sheath footage (in feet, so the
    ft→m normalization path is exercised), direction 'both' for bidir averaging.
    """
    header = ("fiber_id", "anchor_type", "closure_name", "event_index",
              "approx_otdr_km", "known_distance", "units", "direction")
    rows = []
    for n in fiber_nums:
        key = fiber_key_from_id(f"ELMMIL{n:03d}")
        for idx, y_m in enumerate(sorted(CLOSURE_KNOWN_M)):
            y_ft = y_m / M_PER_FT
            rows.append((key, "closure", f"S{idx}", idx, "",
                         round(y_ft, 6), "ft", "both"))
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


# ── The regression gate ─────────────────────────────────────────────────
def test_recovers_injected_2p5pct_efl(tmp_path):
    """End-to-end: a synthetic span with a TRUE 2.5 % EFL must be recovered
    by the calibration tool, with a high R², a separately-reported offset b,
    and a PASS verdict against the stranded-loose-tube AEN142 band."""
    fiber_nums = [1, 2, 3, 4]
    recs = _span_records(fiber_nums)
    anchor_csv = tmp_path / "anchors.csv"
    _write_anchor_csv(anchor_csv, fiber_nums)
    anchors = anchors_mod.load_anchors(str(anchor_csv))

    res = run_calibrate(recs, anchors,
                        cable_type="stranded_loose_tube",
                        expected_ior=GOOD_IOR)

    # --- factor + EFL recovery (the headline assertion) ---
    assert res.m is not None
    assert abs(res.m - M_TRUE) < 1e-3, f"m={res.m} not ~{M_TRUE}"
    assert abs(res.efl_pct - EFL_PCT_TRUE) < 0.05, \
        f"EFL%={res.efl_pct} not ~{EFL_PCT_TRUE}"
    assert abs(res.efl_pct - _efl_pct(M_TRUE)) < 0.05

    # --- offset reported SEPARATELY (not folded into m) ---
    # The synthetic A/B traces are exact physical mirrors, so the bidir flip
    # lands back on the A frame and the true launch offset is recovered cleanly.
    assert res.b_m is not None
    assert abs(res.b_m - B_TRUE_M) < 0.5, f"b={res.b_m} not ~{B_TRUE_M} m"
    # Per-anchor residuals are essentially zero (anchors lie on the line).
    assert all(abs(a.residual_m) < 0.1 for a in res.anchor_fits)

    # --- goodness of fit ---
    assert res.r2 is not None and res.r2 > 0.9999, f"R²={res.r2}"

    # --- bidirectional averaging actually fired (A+B used) ---
    assert any(a.direction_used == "A+B" for a in res.anchor_fits), \
        "expected at least one A+B (bidir-averaged) anchor"

    # --- cross-fiber consistency: all fibers share one tight EFL ---
    assert res.fiber_m_std is not None and res.fiber_m_std < 1e-3
    assert not res.outlier_fibers
    for f in res.fiber_fits:
        if f.m is not None:
            assert abs(f.m - M_TRUE) < 1e-3

    # --- AEN142 band sanity: 0.9756 is inside stranded loose-tube 0.97–0.98 ---
    assert res.band_verdict.startswith("PASS"), res.band_verdict

    # --- IOR independently verified (good cohort + matching spec) ---
    assert res.ior_verified is True
    assert not res.ior_flags
    assert "verified" in res.ior_label

    # --- the report renders without error (house-style workbook) ---
    out = tmp_path / "report.xlsx"
    report_mod.write_report(res, str(out))
    assert out.exists() and out.stat().st_size > 0


def test_efl_recovered_single_direction_too(tmp_path):
    """Sanity: even with A traces only (no bidir averaging), the same injected
    2.5 % EFL is recovered — proves the averaging isn't masking a frame bug."""
    fiber_nums = [1, 2, 3]
    # Keep only the A (ELMMIL) traces.
    recs = [r for r in _span_records(fiber_nums)
            if r["genparams"]["cable_id"].startswith("ELMMIL")]
    anchor_csv = tmp_path / "anchors_a.csv"

    # Re-emit the anchors as A-direction only.
    header = ("fiber_id", "anchor_type", "closure_name", "event_index",
              "approx_otdr_km", "known_distance", "units", "direction")
    with open(anchor_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for n in fiber_nums:
            key = fiber_key_from_id(f"ELMMIL{n:03d}")
            for idx, y_m in enumerate(sorted(CLOSURE_KNOWN_M)):
                w.writerow((key, "closure", f"S{idx}", idx, "",
                            round(y_m / M_PER_FT, 6), "ft", "A"))
    anchors = anchors_mod.load_anchors(str(anchor_csv))

    res = run_calibrate(recs, anchors, cable_type="stranded_loose_tube",
                        expected_ior=GOOD_IOR)
    assert all(a.direction_used == "A" for a in res.anchor_fits)
    assert abs(res.m - M_TRUE) < 1e-4
    assert abs(res.efl_pct - EFL_PCT_TRUE) < 0.01
    assert res.r2 > 0.9999


def test_wrong_stored_ior_is_flagged(tmp_path):
    """The IOR guardrail must FLAG a trace whose stored IOR is corrupted, and
    must NOT mark the run independently-verified — because a wrong IOR silently
    corrupts the recovered factor (~0.1 % IOR error ≈ the whole helix effect)."""
    fiber_nums = [1, 2, 3, 4]
    bad_fiber = 3
    recs = _span_records(fiber_nums, bad_ior_fiber=bad_fiber)
    anchor_csv = tmp_path / "anchors.csv"
    _write_anchor_csv(anchor_csv, fiber_nums)
    anchors = anchors_mod.load_anchors(str(anchor_csv))

    # Supply an expected spec IOR matching the GOOD cohort.  The bad fiber's
    # traces deviate from BOTH the cohort median and the spec, so they flag.
    res = run_calibrate(recs, anchors, cable_type="stranded_loose_tube",
                        expected_ior=GOOD_IOR)

    # The corrupted IOR is well outside the guardrail tolerance.
    assert abs(BAD_IOR - GOOD_IOR) > IOR_COHORT_TOL

    # (a) at least one ior_flag, naming the bad fiber's file(s).
    assert res.ior_flags, "expected the guardrail to flag the bad-IOR trace"
    bad_suffix = f"{bad_fiber:03d}"
    assert any(bad_suffix in msg for msg in res.ior_flags), \
        f"no ior_flag named fiber {bad_suffix}: {res.ior_flags}"

    # (b) the run is NOT independently verified while a trace is flagged.
    assert res.ior_verified is False
    assert "not independently verified" in res.ior_label


def test_clean_cohort_not_flagged(tmp_path):
    """Control: with every stored IOR equal to the cohort/spec, the guardrail
    stays silent and the run is marked verified (guards against a guardrail
    that flags everything)."""
    fiber_nums = [1, 2, 3, 4]
    recs = _span_records(fiber_nums)            # all GOOD_IOR
    anchor_csv = tmp_path / "anchors.csv"
    _write_anchor_csv(anchor_csv, fiber_nums)
    anchors = anchors_mod.load_anchors(str(anchor_csv))

    res = run_calibrate(recs, anchors, cable_type="stranded_loose_tube",
                        expected_ior=GOOD_IOR)
    assert not res.ior_flags
    assert res.ior_verified is True
