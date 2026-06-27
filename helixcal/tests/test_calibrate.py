"""calibrate core math: LSQ recovery, EFL, bidir averaging, IOR guardrail,
cross-fiber spread, AEN142 bands.

Synthetic anchors are built FROM each fixture's own interior-event positions,
so a known (m, b) is recoverable to float precision — this isolates the fit
math from any uncertainty in the real cable footage (which this span lacks).
"""

import csv

import pytest

from helixcal import sor_fields, anchors as anchors_mod, calibrate
from helixcal.calibrate import (
    calibrate as run_calibrate, fiber_key_from_id, _interior_event_distances_m,
    _ols, _efl_pct, _resolve_directions, CABLE_TYPE_BANDS,
)


# ── Pure-math unit tests (no SOR needed) ────────────────────────────────
def test_ols_exact_line():
    xs = [0.0, 1.0, 2.0, 3.0, 4.0]
    m_true, b_true = 0.975, 12.0
    ys = [m_true * x + b_true for x in xs]
    m, b, r2 = _ols(xs, ys)
    assert abs(m - m_true) < 1e-9
    assert abs(b - b_true) < 1e-9
    assert abs(r2 - 1.0) < 1e-12


def test_ols_underdetermined():
    assert _ols([1.0], [2.0]) == (None, None, None)


def test_efl_pct():
    # m = 0.975 -> EFL ~ 2.564 %
    assert abs(_efl_pct(0.975) - ((1 / 0.975 - 1) * 100)) < 1e-12
    assert _efl_pct(None) is None
    assert _efl_pct(0) is None


# ── Helpers to build synthetic anchors from real fixtures ───────────────
def _records(files):
    recs = [sor_fields.read_trace_record(f) for f in files]
    return [r for r in recs if r]


def _write_anchor_csv(path, rows):
    header = ("fiber_id", "anchor_type", "closure_name", "event_index",
              "approx_otdr_km", "known_distance", "units", "direction")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)


def test_direction_resolution(span_a_files, span_b_files):
    recs = _records(span_a_files) + _records(span_b_files)
    dirs = _resolve_directions(recs)
    a = [r for r in recs if dirs[id(r)] == "A"]
    b = [r for r in recs if dirs[id(r)] == "B"]
    assert len(a) == len(span_a_files)
    assert len(b) == len(span_b_files)
    # All A records share one prefix, all B another.
    a_pref = {r["genparams"]["cable_id"][:6].upper() for r in a}
    b_pref = {r["genparams"]["cable_id"][:6].upper() for r in b}
    assert len(a_pref) == 1 and len(b_pref) == 1
    assert a_pref != b_pref


def test_a_only_recovers_known_line(tmp_path, span_a_files):
    recs = _records(span_a_files)
    rec0 = recs[0]
    key = fiber_key_from_id(rec0["genparams"]["cable_id"])
    ia = _interior_event_distances_m(rec0["events"])
    assert len(ia) >= 3
    m_true, b_true = 0.976, 8.0
    M_PER_FT = anchors_mod.M_PER_FT
    rows = []
    for idx in (1, 3, 5):
        x = ia[idx]
        y_ft = (m_true * x + b_true) / M_PER_FT      # known footage in ft
        rows.append((key, "closure", f"S{idx}", idx, "", round(y_ft, 4),
                     "ft", "A"))
    p = tmp_path / "anchors.csv"
    _write_anchor_csv(p, rows)
    anchors = anchors_mod.load_anchors(str(p))

    res = run_calibrate(recs, anchors, cable_type="stranded_loose_tube",
                        expected_ior=1.47)
    # Exact recovery (A-only, no bidir averaging perturbation).
    assert abs(res.m - m_true) < 1e-4
    assert abs(res.b_m - b_true) < 1.0          # meters
    assert res.r2 > 0.9999
    assert abs(res.efl_pct - _efl_pct(m_true)) < 1e-2
    # Every fit anchor used the A direction only.
    assert all(a.direction_used == "A" for a in res.anchor_fits
               if a.fiber_key == key)


def test_bidirectional_pairs_and_averages(tmp_path, span_a_files, span_b_files):
    recs = _records(span_a_files) + _records(span_b_files)
    # Anchor located by approx_otdr_km so BOTH directions can snap to the same
    # physical closure by position (the bidir alignment path).
    rec_a = next(r for r in _records(span_a_files))
    ia = _interior_event_distances_m(rec_a["events"])
    key = fiber_key_from_id(rec_a["genparams"]["cable_id"])
    M_PER_FT = anchors_mod.M_PER_FT
    m_true, b_true = 0.975, 5.0
    rows = []
    for idx in (2, 4):
        x = ia[idx]
        y_ft = (m_true * x + b_true) / M_PER_FT
        rows.append((key, "closure", f"S{idx}", "", round(x / 1000.0, 4),
                     round(y_ft, 4), "ft", "both"))
    p = tmp_path / "anchors.csv"
    _write_anchor_csv(p, rows)
    anchors = anchors_mod.load_anchors(str(p))

    res = run_calibrate(recs, anchors, cable_type="stranded_loose_tube",
                        expected_ior=1.47)
    # At least one anchor for this fiber resolved both directions (A+B) and
    # was averaged; the fit stays in the stranded loose-tube band.
    used = [a.direction_used for a in res.anchor_fits if a.fiber_key == key]
    assert "A+B" in used
    assert 0.96 < res.m < 0.99


def test_ior_guardrail_label_flips(tmp_path, span_a_files):
    recs = _records(span_a_files)
    rec0 = recs[0]
    key = fiber_key_from_id(rec0["genparams"]["cable_id"])
    ia = _interior_event_distances_m(rec0["events"])
    M_PER_FT = anchors_mod.M_PER_FT
    rows = [(key, "closure", f"S{i}", i, "",
             round((0.975 * ia[i] + 5.0) / M_PER_FT, 4), "ft", "A")
            for i in (1, 3, 5)]
    p = tmp_path / "anchors.csv"
    _write_anchor_csv(p, rows)
    anchors = anchors_mod.load_anchors(str(p))

    # Without expected_ior -> not independently verified.
    res_unv = run_calibrate(recs, anchors, cable_type="stranded_loose_tube")
    assert res_unv.ior_verified is False
    assert "not independently verified" in res_unv.ior_label

    # With matching expected_ior -> verified.
    res_v = run_calibrate(recs, anchors, cable_type="stranded_loose_tube",
                          expected_ior=1.47)
    assert res_v.ior_verified is True
    assert "verified" in res_v.ior_label

    # With a WRONG expected_ior -> flagged + not verified.
    res_w = run_calibrate(recs, anchors, cable_type="stranded_loose_tube",
                          expected_ior=1.460)
    assert res_w.ior_verified is False
    assert res_w.ior_flags


def test_aen142_band_warning(tmp_path, span_a_files):
    recs = _records(span_a_files)
    rec0 = recs[0]
    key = fiber_key_from_id(rec0["genparams"]["cable_id"])
    ia = _interior_event_distances_m(rec0["events"])
    M_PER_FT = anchors_mod.M_PER_FT
    # Build a line with m WAY out of any AEN142 band.
    m_bad = 1.20
    rows = [(key, "closure", f"S{i}", i, "",
             round((m_bad * ia[i] + 5.0) / M_PER_FT, 4), "ft", "A")
            for i in (1, 3, 5)]
    p = tmp_path / "anchors.csv"
    _write_anchor_csv(p, rows)
    anchors = anchors_mod.load_anchors(str(p))

    res = run_calibrate(recs, anchors, cable_type="stranded_loose_tube")
    assert res.band_verdict.startswith("WARNING")
    assert any("OUTSIDE" in w for w in res.warnings)


def test_cross_fiber_outlier_flag():
    # Drive the cross-fiber spread logic directly with a realistic cohort:
    # 10 tight fibers + 1 clear outlier.
    from helixcal.calibrate import FiberFit, _mean, _std, XFIBER_SIGMA, XFIBER_ABS_M
    ms = [0.9750, 0.9752, 0.9748, 0.9751, 0.9749,
          0.9753, 0.9747, 0.9750, 0.9752, 0.9748, 0.9950]
    mu, sd = _mean(ms), _std(ms)
    flagged = [i for i, m in enumerate(ms)
               if sd > 0 and abs(m - mu) > XFIBER_SIGMA * sd
               and abs(m - mu) > XFIBER_ABS_M]
    assert flagged == [10]  # only the 0.9950 fiber


def test_no_anchors_resolved_warns(tmp_path, span_a_files):
    recs = _records(span_a_files)
    # An anchor for a fiber that doesn't exist resolves to nothing.
    p = tmp_path / "a.csv"
    _write_anchor_csv(p, [("999", "closure", "S", 1, "", 1000.0, "ft", "A")])
    anchors = anchors_mod.load_anchors(str(p))
    res = run_calibrate(recs, anchors, cable_type="stranded_loose_tube")
    assert res.n_anchors == 0
    assert any("no anchors resolved" in w for w in res.warnings)


# ── Cable-type resolution inside calibrate (auto/manual/default) ────────
def _anchor_rows_for(rec, m_true=0.975, b_true=5.0):
    key = fiber_key_from_id(rec["genparams"]["cable_id"])
    ia = _interior_event_distances_m(rec["events"])
    M_PER_FT = anchors_mod.M_PER_FT
    return [(key, "closure", f"S{i}", i, "",
             round((m_true * ia[i] + b_true) / M_PER_FT, 4), "ft", "A")
            for i in (1, 3, 5)]


def test_calibrate_cable_type_default_fallback(tmp_path, span_a_files):
    # No manual cable_type; the fixtures' GenParams carry no cable code, so
    # calibrate auto-detect fails and falls back to the cable_db default with
    # source='default' (the real HOWESPAN→LANCASTER behavior).
    recs = _records(span_a_files)
    p = tmp_path / "a.csv"
    _write_anchor_csv(p, _anchor_rows_for(recs[0]))
    anchors = anchors_mod.load_anchors(str(p))
    res = run_calibrate(recs, anchors)  # cable_type defaults to None -> auto
    from helixcal.cable_db import DEFAULT_CABLE_TYPE
    assert res.cable_type == DEFAULT_CABLE_TYPE
    assert res.cable_type_source == "default"
    assert res.band is not None  # default still has a sanity band
    assert "(default)" in res.band_verdict


def test_calibrate_manual_cable_type_overrides(tmp_path, span_a_files):
    recs = _records(span_a_files)
    p = tmp_path / "a.csv"
    _write_anchor_csv(p, _anchor_rows_for(recs[0]))
    anchors = anchors_mod.load_anchors(str(p))
    res = run_calibrate(recs, anchors, cable_type="central_tube")
    assert res.cable_type == "central_tube"
    assert res.cable_type_source == "manual"
    # m≈0.975 is OUTSIDE the central-tube band 0.99–1.00 -> band warns.
    assert res.band_verdict.startswith("WARNING")
    assert any("OUTSIDE" in w for w in res.warnings)


def test_calibrate_autodetect_from_synth_genparams(tmp_path, span_a_files):
    # Inject a cable_code into the first record's genparams so auto-detect
    # fires and source becomes 'genparams'.
    recs = _records(span_a_files)
    recs[0]["genparams"] = dict(recs[0]["genparams"])
    recs[0]["genparams"]["cable_code"] = "144F SLT ALTOS"
    p = tmp_path / "a.csv"
    _write_anchor_csv(p, _anchor_rows_for(recs[0]))
    anchors = anchors_mod.load_anchors(str(p))
    res = run_calibrate(recs, anchors)  # no manual type -> auto from GenParams
    assert res.cable_type == "stranded_loose_tube"
    assert res.cable_type_source == "genparams"
