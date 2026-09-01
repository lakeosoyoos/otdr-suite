"""Secret Sauce false-positive proofing (fix/ss-regime-hardening).

Boss's BKF↔DEL 80 km span (432 files per direction) printed 47 + 1
"duplicate" pairs.  All 48 were adjudicated FALSE POSITIVE.  Four
independent defects let them through, and this module pins the fix for
each one:

  1. ROUTER SELF-REFUTATION.  Both folders landed in the `all_dups`
     regime on hair margins (bulk σ 0.0979 / 0.0922 vs the < 0.10 gate,
     bulk r 0.8014 ≥ 0.7, 80 km span ≥ 15 km).  all_dups bypasses the
     σ-outlier tier and widens the r-ramp to 0.85→0.95, so an ordinary
     r ≈ 0.91 walked to "Likely duplicate".  The refuting evidence —
     frac_high_r = 0.00, i.e. NOT ONE pair in the folder is
     near-identical — was computed and printed on the Regime: line and
     never consulted.  The gate now requires frac_high_r >=
     _ALLDUPS_MIN_HIGHR_FRAC and routes PRODUCTION when the claim
     refutes itself.

  2. EVENTS GATE FAIL-OPEN.  _events_agree returned "agree" whenever
     fewer than 3 interior events were available.  BKFDEL028 and
     BKFDEL040 are the only 2 of 432 files with ≤ 2 interior events, and
     every one of the 47 flagged pairs contained one of them — the
     fail-open set exactly.  A table that is PRESENT but too thin now
     caps the pair.  (n_max == 0 — neither file carries a table at all —
     stays fail-open: that is an input-format property, and capping it
     switched the .json path off entirely.)

  3. TWIN GATE SCOPING.  The uniqueness twin gate ran in the production
     regime ONLY, so a regime misroute took it off the board at exactly
     the moment it was needed.  BKF twin ratios were 1.00-1.71 against a
     ≤ 0.5 requirement — it would have capped all 47.  It now runs in
     every regime.

  4. SPECKLE CONFIRM GATE (the class-closer).  Every pair still standing
     above the print threshold must show the same sub-pulse-width
     Rayleigh backscatter fingerprint.  DELBKF138↔162 slid under the
     10 mdB matched-splice mean-Δ gate at 9.86 mdB but reads r_hp = 0.019
     — different fibers.  Demote-only and fail-safe: unmeasurable never
     vetoes, and byte-identical copies confirm trivially at r_hp = 1.0.

HARD RULE — namespace isolation
-------------------------------
The Secret Sauce engine ships its own sor_reader324802a.py that collides
with the viewer's copy.  This test process NEVER imports it.  Everything
here is either a source-string lock or a `sys.executable -c` subprocess,
matching test_secretsauce_runner.py's established pattern.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys

from conftest import SECRETSAUCE_DIR, FIXTURE_A_DIR, run_secretsauce


def _sor_src() -> str:
    return (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")


def _report_src() -> str:
    return (SECRETSAUCE_DIR / "report.py").read_text(encoding="utf-8")


# ── 1. Router self-refutation ───────────────────────────────────────────────

def test_alldups_selfrefutation_constant():
    src = _sor_src()
    assert "_ALLDUPS_MIN_HIGHR_FRAC = 0.5" in src


def test_alldups_gate_consults_frac_high_r():
    """The all_dups gate must AND in the self-refutation term.  Locked as
    the literal continuation line so a future edit can't drop it while
    leaving the constant defined."""
    src = _sor_src()
    assert ("and min_L >= _ALLDUPS_MIN_SPAN_M\n"
            "            and frac_high_r >= _ALLDUPS_MIN_HIGHR_FRAC):") in src


def test_refuted_alldups_routes_production_not_tie_panel():
    """A refuted all_dups folder has bulk_r >= 0.7, so the tie_panel elif
    would otherwise claim it and disable the σ-outlier detector the folder
    actually needs.  The refuted branch must set production explicitly and
    sit ABOVE the tie_panel route."""
    src = _sor_src()
    assert "alldups_refuted = (bulk_r >= 0.7 and bulk_sigma < 0.10" in src
    assert "and frac_high_r < _ALLDUPS_MIN_HIGHR_FRAC)" in src
    i_ref = src.index("elif alldups_refuted:")
    i_tie = src.index("elif bulk_r >= 0.7 or frac_high_r >= 0.30:")
    assert i_ref < i_tie
    branch = src[i_ref:i_tie]
    assert "regime = 'production'" in branch
    assert "all_dups refuted: frac high-r" in branch      # printed + reported


def test_tie_panel_route_left_untouched():
    """DELIBERATE non-change: the same sanity is NOT applied to the
    tie_panel route.  tie_panel is the conservative destination
    (fingerprint extraction + the 0.999-0.9999 ramp + σ-outlier bypassed);
    demanding a high frac_high_r there would push folders into production,
    where σ-outlier is live.  Measured counter-example: the BKF+DEL
    combined 864-file folder routes tie_panel on bulk r 0.7256 with
    frac_high_r 0.00 and reports ZERO pairs."""
    src = _sor_src()
    assert "elif bulk_r >= 0.7 or frac_high_r >= 0.30:\n        regime = 'tie_panel'" in src
    i_tie = src.index("elif bulk_r >= 0.7 or frac_high_r >= 0.30:")
    assert "_ALLDUPS_MIN_HIGHR_FRAC" not in src[i_tie:i_tie + 200]
    assert "Leave tie_panel routing untouched." in src


# ── 2. Events-gate fail-safe ────────────────────────────────────────────────

_EVENTS_SCRIPT = r"""
import sys, json
sys.path.insert(0, sys.argv[1])
from report import _events_agree, _event_match_quality
out = {}
# BKFDEL028-shaped: table PRESENT on both sides but only 2 interior events
# on one of them -> unverifiable -> must NOT agree.
out['thin'] = _events_agree(2, 5, 2, 0.001)
out['thin_zero_match'] = _events_agree(0, 6, 1, 0.0)
# One-sided table (n_max > 0, n_min == 0) -> genuine disagreement -> cap.
out['one_sided'] = _events_agree(0, 5, 0, 0.0)
# NEITHER file has a table (.json exports) -> gate has no opinion -> agree.
out['no_tables'] = _events_agree(0, 0, 0, 0.0)
# Normal agree / disagree behaviour is unchanged.
out['agree'] = _events_agree(6, 6, 6, 0.002)
out['loss_mismatch'] = _events_agree(6, 6, 6, 0.050)
out['count_mismatch'] = _events_agree(4, 9, 6, 0.001)
# _event_match_quality must report a TRUTHFUL n_max when one side is empty.
ev = [{'dist_km': 1.0, 'splice_loss': 0.4},
      {'dist_km': 5.0, 'splice_loss': 0.1},
      {'dist_km': 9.0, 'splice_loss': 0.2}]
out['q_one_sided'] = list(_event_match_quality(ev, []))
out['q_both_empty'] = list(_event_match_quality([], []))
print(json.dumps(out))
"""


def _run(script: str, *args: str):
    p = subprocess.run([sys.executable, "-c", script, str(SECRETSAUCE_DIR), *args],
                       capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_events_gate_thin_table_does_not_agree():
    out = _run(_EVENTS_SCRIPT)
    assert out["thin"] is False, "event-poor pair still gets a free pass"
    assert out["thin_zero_match"] is False
    assert out["one_sided"] is False


def test_events_gate_still_fails_open_with_no_tables_at_all():
    """n_max == 0 (neither file carries events, e.g. the .json exports)
    must keep failing OPEN — capping there switches the detector off for a
    whole input class instead of withholding a free pass."""
    out = _run(_EVENTS_SCRIPT)
    assert out["no_tables"] is True


def test_events_gate_normal_behaviour_unchanged():
    out = _run(_EVENTS_SCRIPT)
    assert out["agree"] is True
    assert out["loss_mismatch"] is False
    assert out["count_mismatch"] is False


def test_event_match_quality_reports_truthful_n_max():
    out = _run(_EVENTS_SCRIPT)
    # one-sided: 3 events vs 0 -> n_match 0, n_max 3, n_min 0
    assert out["q_one_sided"][:3] == [0, 3, 0], out["q_one_sided"]
    assert out["q_both_empty"][:3] == [0, 0, 0], out["q_both_empty"]


def test_events_gate_docstring_pins_the_case():
    src = _report_src()
    assert "BKFDEL028 and BKFDEL040" in src
    assert "return False  # table present but too thin to check — cap" in src


# ── 3. Twin gate in ALL regimes ─────────────────────────────────────────────

def test_twin_gate_is_not_regime_scoped():
    src = _sor_src()
    i = src.index("uniq_violation = np.zeros(len(pairs), dtype=bool)")
    block = src[i:i + 900]
    assert "if regime ==" not in block, "twin gate is regime-scoped again"
    assert "Ksz = sigma_matrix.shape[0]" in block
    assert "_UNIQ_TWIN_RATIO * min(nb_i, nb_j)" in block


def test_twin_gate_comment_records_the_scoping_fix():
    src = _sor_src()
    assert "Uniqueness (twin) gate — ALL regimes" in src


# ── 4. Speckle confirm gate ─────────────────────────────────────────────────

def test_speckle_constants():
    src = _sor_src()
    assert "_SPECKLE_HP_WIDTH = 21" in src
    # ONE union window since 2026-08-31.  MAX across three sub-windows was
    # the worst available combiner: at k=3 it had spent almost the entire
    # margin (+0.007 of +0.242) because the null takes the best of k draws
    # while a true pair needs only one window.  Verdict-neutral on every
    # folder with candidates; the folder null nearly halved.
    assert "_SPECKLE_WINDOWS = ((0.02, 0.60),)" in src
    assert "_SPECKLE_MIN_SAMPLES = 500" in src
    assert "_SPECKLE_DZ_TOL = 1e-6" in src
    assert "_SPECKLE_FLOOR_MARGIN = 3.0" in src
    assert "_SPECKLE_NULL_FILES = 60" in src
    assert "_SPECKLE_NULL_PCT = 99.0" in src
    assert "_SPECKLE_NULL_MIN_PAIRS = 100" in src


def test_speckle_has_no_fixed_confirm_threshold():
    """A fixed confirm threshold is the trap this gate was rebuilt to
    avoid: a genuine re-shoot's speckle r falls off as s²/(s²+σ²/2), so
    any threshold high enough to reject the BKF false positives also
    rejects real duplicates at the same σ.  Measured white-noise controls
    on real files: BKF σ 0.0396 -> 0.077, CHEPLA σ 0.0098 -> 0.091."""
    src = _sor_src()
    assert "_SPECKLE_CONFIRM_MIN_R" not in src
    assert "NO FIXED CONFIRM THRESHOLD" in src


def test_speckle_gate_is_wired_and_demote_only():
    src = _sor_src()
    assert "speckle_violation = np.zeros(len(pairs), dtype=bool)" in src
    # candidates only (>0.5), so cost is O(files in candidate pairs)
    assert "cand = [i for i in range(len(pairs)) if p_dup[i] > 0.5]" in src
    # demote-only: np.minimum against the borderline cap, never a raise
    assert ("p_dup = np.where(speckle_violation, np.minimum(p_dup, LEN_CAP), p_dup)"
            in src)
    # unmeasurable is recorded and NOT vetoed
    assert "p['speckle_unmeasurable'] = True" in src
    # ...nor is an inconclusive pair (statistic can't separate at this σ)
    assert "p['speckle_abstain'] = True" in src
    # the veto needs BOTH the competence test and the margin test
    assert "if r_floor < null_q:" in src
    assert "if r_hp <= r_floor / _SPECKLE_FLOOR_MARGIN:" in src
    # ...and the raw-identity short-circuit still runs last, so a literal
    # copy can never be capped by this gate.
    assert src.index("speckle_violation") < src.index("raw_ident_mask")


def test_speckle_null_sample_is_deterministic():
    """The folder null must not depend on an RNG — two runs of the same
    folder have to produce the same verdicts."""
    src = _sor_src()
    i = src.index("null_res = [_speckle_windows(f, interior_start")
    block = src[i - 400:i + 400]
    assert "files[::step]" in block
    assert "random" not in block.lower()


_SPECKLE_SCRIPT = r"""
import sys, json
sys.path.insert(0, sys.argv[1])
import numpy as np
import report_sor as RS

rng = np.random.RandomState(5)
N = 30000
dz = 2.5524451709576352                 # BKF sample spacing
pos = np.arange(N) * dz
base = 46.0 - 1.8e-4 * pos              # attenuation slope
# Rayleigh speckle: fine-scale, frozen into the glass, per-fiber.
sp1 = rng.normal(0, 0.008, N)
sp2 = rng.normal(0, 0.008, N)           # a DIFFERENT fiber

def mk(trace):
    return {'name': 'x', 'trace': trace.astype(np.float32), 'pos': pos}

lo, hi = 500.0, pos[-1] - 200.0
def W(tr):
    return RS._speckle_windows(mk(tr), lo, hi)

out = {}
# (a) byte-identical trace -> confirms trivially
a = base + sp1
out['identical'] = RS._speckle_pair_r(W(a), W(a.copy()))
out['identical_floor'] = RS._speckle_same_fiber_floor(W(a), W(a.copy()), 0.0)
# (b) same fiber re-shot: same speckle + independent shot noise
for tag, nz in (('2mdb', 0.002), ('10mdb', 0.010), ('30mdb', 0.030)):
    b_ = base + sp1 + rng.normal(0, nz, N)
    sig = float(np.std((a - b_) - (a - b_).mean()))
    out['reshoot_' + tag] = RS._speckle_pair_r(W(a), W(b_))
    out['floor_' + tag] = RS._speckle_same_fiber_floor(W(a), W(b_), sig)
# (c) different fiber, identical macro-structure (same slope, same splices)
d_ = base + sp2
sig_d = float(np.std((a - d_) - (a - d_).mean()))
out['different'] = RS._speckle_pair_r(W(a), W(d_))
out['different_floor'] = RS._speckle_same_fiber_floor(W(a), W(d_), sig_d)
# (d) UNMEASURABLE cases must return None (fail-safe, never a veto)
flat = np.full(N, 40.0)
out['flat'] = RS._speckle_pair_r(W(a), W(flat))
short = {'name': 's', 'trace': (base[:300] + sp1[:300]).astype(np.float32),
         'pos': pos[:300]}
out['too_short'] = RS._speckle_pair_r(W(a), RS._speckle_windows(short, lo, hi))
other_grid = {'name': 'g', 'trace': (base + sp1).astype(np.float32),
              'pos': np.arange(N) * (dz * 1.5)}
out['grid_mismatch'] = RS._speckle_pair_r(
    W(a), RS._speckle_windows(other_grid, lo, hi))
out['none_side'] = RS._speckle_pair_r(W(a), None)
out['floor_unmeasurable'] = RS._speckle_same_fiber_floor(W(a), None, 0.01)
out['margin'] = RS._SPECKLE_FLOOR_MARGIN
print(json.dumps(out))
"""


def test_speckle_statistic_separates_same_fiber_from_different():
    out = _run(_SPECKLE_SCRIPT)
    m = out["margin"]
    assert out["identical"] > 0.999, out["identical"]
    assert out["reshoot_2mdb"] > 0.9, out["reshoot_2mdb"]
    assert out["different"] < 0.05, out["different"]
    # Every same-fiber re-shoot must sit ABOVE its own floor/margin, i.e.
    # never vetoed — including the 30 mdB shot where the raw r is low.
    for tag in ("2mdb", "10mdb", "30mdb"):
        r, fl = out["reshoot_" + tag], out["floor_" + tag]
        assert r > fl / m, (tag, r, fl)
    # ...and the different-fiber pair must sit BELOW floor/margin, i.e.
    # vetoable once the folder null clears.
    assert out["different"] <= out["different_floor"] / m, out


def test_speckle_same_fiber_floor_tracks_sigma():
    """The floor is the LOWEST value the same-fiber hypothesis can give at
    a pair's own σ, so it must fall as σ grows and be 1.0 at σ = 0."""
    out = _run(_SPECKLE_SCRIPT)
    assert out["identical_floor"] > 0.999, out["identical_floor"]
    assert (out["floor_2mdb"] > out["floor_10mdb"] > out["floor_30mdb"]), out


def test_speckle_unmeasurable_never_vetoes():
    """Every unmeasurable shape returns None, and None is what the caller
    treats as 'confirm by default' — matching the splicereport
    re-measure-gate convention."""
    out = _run(_SPECKLE_SCRIPT)
    assert out["flat"] is None
    assert out["too_short"] is None
    assert out["grid_mismatch"] is None
    assert out["none_side"] is None
    assert out["floor_unmeasurable"] is None


# ── End-to-end smoke: a byte-identical copy still confirms ──────────────────

def _copy_folder(tmp_path):
    """3-file folder [A, B, byte-copy-of-A] built from the tracked ELMMIL
    fixtures — the same construction test_ss_regime_fix.py uses."""
    d = tmp_path / "sshard"
    d.mkdir()
    a = FIXTURE_A_DIR / "ELMMIL0001_1550.sor"
    b = FIXTURE_A_DIR / "ELMMIL0002_1550.sor"
    shutil.copy(a, d / a.name)
    shutil.copy(b, d / b.name)
    shutil.copy(a, d / "ELMMIL0009_1550.sor")
    return d


def test_byte_copy_survives_every_new_gate(tmp_path):
    """The twin gate now runs in all regimes and the speckle gate runs on
    every candidate pair — neither may hide a literal file copy."""
    folder = _copy_folder(tmp_path)
    rc, m, stderr = run_secretsauce(folder, tmp_path / "out", "pairs")
    assert rc == 0 and m and m.get("ok"), f"runner failed: {(stderr or '')[-800:]}"
    flagged = [p for p in m["pairs"] if p["p_dup"] > 0.5]
    assert len(flagged) == 1, flagged
    p = flagged[0]
    assert {p["fileA"], p["fileB"]} == {"ELMMIL0001_1550", "ELMMIL0009_1550"}, p
    assert p["p_dup"] == 1.0 and p.get("raw_identical") is True, p


def test_speckle_gate_reports_itself_on_stdout(tmp_path):
    """The gate has to be auditable from a run log: one line naming the
    candidate count, the demotions, and the unmeasurable (kept) count."""
    folder = _copy_folder(tmp_path)
    out_dir = tmp_path / "out"
    runner = SECRETSAUCE_DIR / "run_secretsauce.py"
    p = subprocess.run([sys.executable, str(runner), "--folder", str(folder),
                        "--out-dir", str(out_dir), "--format", "pairs"],
                       capture_output=True, text=True, timeout=600)
    assert p.returncode == 0, (p.stderr or "")[-1500:]
    blob = (p.stdout or "") + (p.stderr or "")
    line = [l for l in blob.splitlines() if l.startswith("Speckle gate:")]
    assert line, "no Speckle gate line in the run log"
    assert "candidate pair(s)" in line[0]
    assert "inconclusive (kept)" in line[0] and "unmeasurable (kept)" in line[0]
    assert "0 demoted" in line[0], line[0]      # the copy is not demoted
