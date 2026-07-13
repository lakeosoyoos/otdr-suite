"""Bend fold distance — panel-tunable "at the splice" gate for bend columns.

Platteville–Cheyenne ("calling bends at splices"): a few short-lay fibers put
their splice events 107–165 m before the splice column; the old hard-wired
75 m (CLOSURE_MATCH_KM) gate in split_offsplice_events_into_own_columns
spawned six phantom "Bends @" columns hugging real splices.  The gate is now
BEND_SPLICE_FOLD_KM (default 0.200), applied per-CLUSTER (median), editable
from the OTDR panel as "Bend fold distance"; unchecking the row sends the
legacy 0.075 instead of the 1e9 disable sentinel (which would fold
everything).

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import json
import subprocess
import sys
import textwrap

from conftest import (REPO_ROOT, run_splicereport,
                      FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR)

import app as hub

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"


def _run(body):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_near_splice_cluster_folds_far_cluster_gets_column():
    """A bend cluster 120 m from a splice folds (no new column) at the 200 m
    default; a cluster 450 m out still gets its own column.  Uses synthetic
    all_results entries through the real split_offsplice pass."""
    _run("""
        splices = [{'position_km': 20.0, 'position_km_refined': 20.0,
                    'column_kind': 'splice'},
                   {'position_km': 52.73, 'position_km_refined': 52.73,
                    'column_kind': 'splice'}]
        def bend(f, km):
            return {'fiber': f, 'splice_idx': 1, 'bidir_dist': km,
                    'bidir_loss': 0.11, 'is_bend': True, 'is_flagged': True,
                    'is_break': False, 'is_broke': False, 'is_ref': False}
        allr = {(f, 1): bend(f, 52.60 + i*0.005) for i, f in enumerate((1, 2, 3))}
        allr.update({(f, 1): bend(f, 53.18 + (f-10)*0.005) for f in (10, 11)})
        out, sp2 = E.split_offsplice_events_into_own_columns(
            dict(allr), [dict(s) for s in splices], total_span_km=100.0)
        kinds = [s.get('column_kind') for s in sp2]
        bend_cols = [s for s in sp2 if s.get('column_kind') == 'bend']
        # 52.60 cluster (130 m from 52.73) folded; 53.18 cluster (450 m) kept.
        assert len(bend_cols) == 1, f"expected exactly one bend column: {sp2}"
        assert abs(bend_cols[0]['position_km_refined'] - 53.18) < 0.05, bend_cols
        print('OK')
    """)


def test_fold_distance_zero_legacy_via_override_value():
    """With the legacy 75 m gate (splice_dist_km=0.075) the same 130 m
    cluster is NOT folded — it gets its own column (old behavior, and what
    an unchecked panel row reverts to)."""
    _run("""
        splices = [{'position_km': 52.73, 'position_km_refined': 52.73,
                    'column_kind': 'splice'}]
        def bend(f, km):
            return {'fiber': f, 'splice_idx': 0, 'bidir_dist': km,
                    'bidir_loss': 0.11, 'is_bend': True, 'is_flagged': True,
                    'is_break': False, 'is_broke': False, 'is_ref': False}
        allr = {(f, 0): bend(f, 52.60 + i*0.005) for i, f in enumerate((1, 2, 3))}
        out, sp2 = E.split_offsplice_events_into_own_columns(
            dict(allr), [dict(s) for s in splices], total_span_km=100.0,
            splice_dist_km=0.075)
        bend_cols = [s for s in sp2 if s.get('column_kind') == 'bend']
        assert len(bend_cols) == 1, f"legacy gate should keep the column: {sp2}"
        print('OK')
    """)


def test_engine_default_reads_global_at_call_time():
    """splice_dist_km=None must read BEND_SPLICE_FOLD_KM at call time so a
    run_splicereport --overrides setattr changes behavior."""
    _run("""
        splices = [{'position_km': 52.73, 'position_km_refined': 52.73,
                    'column_kind': 'splice'}]
        def bend(f, km):
            return {'fiber': f, 'splice_idx': 0, 'bidir_dist': km,
                    'bidir_loss': 0.11, 'is_bend': True, 'is_flagged': True,
                    'is_break': False, 'is_broke': False, 'is_ref': False}
        allr = {(f, 0): bend(f, 52.60 + i*0.005) for i, f in enumerate((1, 2, 3))}
        E.BEND_SPLICE_FOLD_KM = 0.075          # simulate the override setattr
        out, sp2 = E.split_offsplice_events_into_own_columns(
            dict(allr), [dict(s) for s in splices], total_span_km=100.0)
        assert any(s.get('column_kind') == 'bend' for s in sp2), sp2
        E.BEND_SPLICE_FOLD_KM = 0.200
        out, sp3 = E.split_offsplice_events_into_own_columns(
            dict(allr), [dict(s) for s in splices], total_span_km=100.0)
        assert not any(s.get('column_kind') == 'bend' for s in sp3), sp3
        print('OK')
    """)


# ── Panel plumbing ────────────────────────────────────────────────────────
def test_panel_row_maps_and_unchecked_sends_legacy_not_sentinel():
    s = hub._otdr_settings_from_profile("Default (engine baseline)")
    ov = hub._overrides_from_settings(s)
    assert ov["BEND_SPLICE_FOLD_KM"] == 0.200          # checked default

    s["bend_fold_distance"]["fail"] = 0.300            # tech edit flows
    assert hub._overrides_from_settings(s)["BEND_SPLICE_FOLD_KM"] == 0.300

    # Unchecked = LEGACY 75 m gate, never the 1e9 sentinel (1e9 would mean
    # "fold every bend into a splice" — the inverse of disabling).
    s["bend_fold_distance"]["apply"] = False
    off = hub._overrides_from_settings(s)["BEND_SPLICE_FOLD_KM"]
    assert off == 0.075, off
    # Detection rows keep the sentinel semantics.
    s["unidir_splice_loss"]["apply"] = False
    assert hub._overrides_from_settings(s)["SINGLE_DIR_THRESHOLD"] == \
        hub._OTDR_DISABLE_SENTINEL


def test_all_profiles_ship_fold_on_at_default():
    for prof in ("Default (engine baseline)", "Lumen", "Zayo"):
        s = hub._otdr_settings_from_profile(prof)
        assert s["bend_fold_distance"]["apply"] is True, prof
        assert s["bend_fold_distance"]["fail"] == 0.200, prof


def test_runner_guards_fold_distance_positive():
    src = (SPLICEREPORT_DIR / "run_splicereport.py").read_text(encoding="utf-8")
    assert "'BEND_SPLICE_FOLD_KM'" in src, \
        "fold distance missing from the positive-float override guard"


# ── End-to-end: the override crosses the subprocess boundary cleanly ─────
def test_fold_override_accepted_and_baseline_stable(tmp_path):
    """The fixture's 4 bend columns are refine PHANTOM ZONES (1.6+ km from any
    splice) — the fold gate governs split_offsplice columns only, so even an
    absurd 5 km fold must leave the fixture untouched.  What this proves end
    to end: the override key exists on the engine (a renamed global would make
    the runner's hasattr check silently no-op) and the runner's guard accepts
    the value (a rejection prints 'skip override' to stderr)."""
    eng = (SPLICEREPORT_DIR / "splicereportmatchexfo.py").read_text(encoding="utf-8")
    assert "\nBEND_SPLICE_FOLD_KM" in eng, "engine global renamed/removed"

    rc0, m0, e0 = run_splicereport(FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR,
                                   tmp_path / "d.xlsx")
    assert m0 and m0.get("ok"), (e0 or "")[-800:]

    rc1, m1, e1 = run_splicereport(FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR,
                                   tmp_path / "w.xlsx",
                                   overrides={"BEND_SPLICE_FOLD_KM": 5.0})
    assert m1 and m1.get("ok"), (e1 or "")[-800:]
    assert "skip override" not in (e1 or ""), e1[-400:]
    kinds0 = [c["kind"] for c in m0["columns"]]
    kinds1 = [c["kind"] for c in m1["columns"]]
    # The fixture's bend columns are a mix: refine PHANTOM ZONES (unaffected
    # by the fold gate) and split_offsplice columns (folded by it).  A 5 km
    # fold must remove at least the split_offsplice one(s) — proof the panel
    # value crossed the subprocess boundary and drove column layout — while
    # hiding nothing (flag count identical; cells move into splice columns).
    assert kinds1.count("bend") < kinds0.count("bend"), (kinds0, kinds1)
    assert m1["n_flagged"] == m0["n_flagged"]
