"""Regression tests for the Secret Sauce regime fix (sandbox/ss-regime-fix).

Two boss-visible bugs, one fix package in secretsauce/report_sor.py:

  A. FALSE-POSITIVE FLOOD — a shared-glass tie panel (A-F West 145-288,
     266 files) routed to 'production' because its bulk_r stayed 0.05:
     shared-glass correlation tops out ~0.8 among port NEIGHBORS only, so
     the bulk_r / frac_high_r tie-panel triggers never fired and σ-outlier
     cascaded into 1 997 false positives at ≥99%.  Fixed by the additive
     distance-decay route: raw r that falls off with port distance is
     physically impossible for real file copies (copies don't care about
     port distance), so decay ⇒ shared path ⇒ tie_panel fingerprint
     extraction.  A second additive route catches very short common spans
     (< 2 km) whose interiors are launch+connector dominated.

  B. MISSED BYTE-IDENTICAL COPY — a literal file copy in a folder that
     routes to tie_panel had its shared signal cancelled by the median
     subtraction (with 2 copies among 3 files the median IS the copy, so
     both residuals go to exactly zero and r reads 0.0) and came back
     "no duplicates".  Fixed by the raw-identity short-circuit: raw
     interior σ ≤ 0.001 dB AND raw r ≥ 0.98 → p_dup = 1.0
     "CONFIRMED duplicate (identical)" regardless of regime.

Namespace isolation rule: the Secret Sauce engine is only ever exercised
through subprocesses (run_secretsauce / sys.executable -c), never imported
into the test process.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys

from conftest import SECRETSAUCE_DIR, FIXTURE_A_DIR, run_secretsauce


# ── Bug B end-to-end: byte-identical copy in a tie_panel-routed folder ──────

def _bugb_folder(tmp_path):
    """3-file folder [A, B, byte-copy-of-A] that deterministically routes
    tie_panel under the PRE-EXISTING rules (frac_high_r = 1/3 ≥ 0.30 while
    bulk_r = 0.689 < 0.7), and where the fingerprint median of 3 traces IS
    the copied trace — the exact mechanism that hid the copy before."""
    d = tmp_path / "bugb"
    d.mkdir()
    a = FIXTURE_A_DIR / "ELMMIL0001_1550.sor"
    b = FIXTURE_A_DIR / "ELMMIL0002_1550.sor"
    shutil.copy(a, d / a.name)
    shutil.copy(b, d / b.name)
    shutil.copy(a, d / "ELMMIL0009_1550.sor")     # byte-identical, new port
    return d


def test_byte_copy_in_tie_panel_folder_is_confirmed_identical(tmp_path):
    folder = _bugb_folder(tmp_path)
    rc, m, stderr = run_secretsauce(folder, tmp_path / "out", "pairs")
    assert rc == 0 and m and m.get("ok"), f"runner failed: {(stderr or '')[-800:]}"
    # The folder must route tie_panel via the PRE-EXISTING frac-high-r rule —
    # this test exercises the short-circuit surviving median subtraction,
    # not the new routing.
    assert "Regime: tie_panel" in (stderr or ""), (stderr or "")[-400:]
    flagged = [p for p in m["pairs"] if p["p_dup"] > 0.5]
    assert len(flagged) == 1, f"expected exactly the copy pair: {flagged}"
    p = flagged[0]
    assert {p["fileA"], p["fileB"]} == {"ELMMIL0001_1550", "ELMMIL0009_1550"}, p
    assert p["p_dup"] == 1.0, p
    assert p["verdict"] == "CONFIRMED duplicate (identical)", p
    assert p.get("raw_identical") is True, p
    assert m["n_flagged"] == 1, m["n_flagged"]


def test_non_copy_pairs_stay_unique_in_bugb_folder(tmp_path):
    """The short-circuit must not splash onto the two genuinely-different
    pairs in the same folder, and the raw_identical key must be ABSENT
    (not false) on them so unaffected manifests stay byte-stable."""
    folder = _bugb_folder(tmp_path)
    rc, m, stderr = run_secretsauce(folder, tmp_path / "out", "pairs")
    assert rc == 0 and m and m.get("ok"), f"runner failed: {(stderr or '')[-800:]}"
    others = [p for p in m["pairs"]
              if {p["fileA"], p["fileB"]} != {"ELMMIL0001_1550", "ELMMIL0009_1550"}]
    assert len(others) == 2
    for p in others:
        assert p["p_dup"] <= 0.5, p
        assert "raw_identical" not in p, p


def test_regime_log_line_backward_compatible(tmp_path):
    """The Regime: line keeps its original field layout; any new-route
    reason may only be APPENDED inside the parens."""
    folder = _bugb_folder(tmp_path)
    rc, m, stderr = run_secretsauce(folder, tmp_path / "out", "pairs")
    assert rc == 0 and m and m.get("ok")
    # NOTE: match the sigma loosely — Windows stderr capture re-encodes the
    # Unicode σ (cp1252 mojibake), so the literal char can't be asserted.
    pat = (r"Regime: \w+ \(bulk .{1,3}=\d+\.\d{4} dB, "
           r"bulk r=-?\d+\.\d{4}, frac high-r=\d+\.\d{2}")
    assert re.search(pat, stderr or ""), (
        "Regime: log line no longer matches the pre-fix format:\n"
        + "\n".join(l for l in (stderr or "").splitlines() if "Regime" in l))


def test_clean_fixture_folder_untouched(tmp_path):
    """A normal folder (4 distinct A-direction fibers) must not grow the
    raw_identical key nor the new verdict — its manifest stays exactly in
    the pre-fix vocabulary (production-ripple guard)."""
    d = tmp_path / "clean"
    d.mkdir()
    for src in FIXTURE_A_DIR.glob("*.sor"):
        shutil.copy(src, d / src.name)
    rc, m, stderr = run_secretsauce(d, tmp_path / "out", "pairs")
    assert rc == 0 and m and m.get("ok"), f"runner failed: {(stderr or '')[-800:]}"
    for p in m["pairs"]:
        assert "raw_identical" not in p, p
        assert p["verdict"] in ("CONFIRMED duplicate", "Likely duplicate",
                                "Possible duplicate", "Unique"), p


# ── Decay-detector + port-split unit behavior (engine subprocess) ───────────

_UNIT_SCRIPT = r"""
import sys, json
sys.path.insert(0, sys.argv[1])
import numpy as np
from report_sor import _neighbor_decay, _port_split

out = {}
# Shared-glass panel: 60 ports, one prefix, r decays with port distance.
names = ['PANEL%04d' % i for i in range(1, 61)]
K = len(names)
r = np.full((K, K), 0.05)
for i in range(K):
    for j in range(K):
        if abs(i - j) <= 3:
            r[i, j] = 0.80
np.fill_diagonal(r, 1.0)
out['decay'] = _neighbor_decay(names, r)
# Flat correlation structure: no decay signal.
r2 = np.full((K, K), 0.5)
np.fill_diagonal(r2, 1.0)
out['flat'] = _neighbor_decay(names, r2)
# Small folder: not enough far pairs to judge -> None.
names3 = ['PANEL%04d' % i for i in range(1, 6)]
out['toofew'] = _neighbor_decay(names3, np.full((5, 5), 0.9))
# Cross-prefix pairs are excluded: two directions, ports overlap.
names4 = (['ABCXYZ%04d' % i for i in range(1, 61)]
          + ['XYZABC%04d' % i for i in range(1, 61)])
r4 = np.full((120, 120), 0.9)       # cross block high, but never counted
r4[:60, :60] = 0.5
r4[60:, 60:] = 0.5
out['crosspref'] = _neighbor_decay(names4, r4)
out['split'] = [_port_split('ELMMIL0001_1550'),
                _port_split('BCK1BCK60145'),
                _port_split('NOPORT')]
print(json.dumps(out))
"""


def _run_unit_script():
    p = subprocess.run([sys.executable, "-c", _UNIT_SCRIPT, str(SECRETSAUCE_DIR)],
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-1000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_neighbor_decay_and_port_split_units():
    out = _run_unit_script()

    # Decay structure detected: near ~0.80, far ~0.05, both buckets populated.
    near, far, n_near, n_far = out["decay"]
    assert abs(near - 0.80) < 1e-9 and abs(far - 0.05) < 1e-9, out["decay"]
    assert n_near == 174 and n_far == 465, out["decay"]
    assert near - far >= 0.30   # would route tie_panel

    # Flat structure: detector returns numbers but no drop → no re-route.
    fnear, ffar, _, _ = out["flat"]
    assert abs(fnear - ffar) < 1e-9, out["flat"]

    # Small folders can't trip the rule.
    assert out["toofew"] is None

    # Cross-prefix (direction) pairs are never mixed into the gap stats:
    # within each direction r is flat 0.5, so no decay is reported even
    # though cross-direction r is high.
    cnear, cfar, _, _ = out["crosspref"]
    assert abs(cnear - cfar) < 1e-9, out["crosspref"]

    # Port extraction mirrors run_secretsauce._extract_fiber_num.
    assert out["split"][0] == ["ELMMIL", 1]
    assert out["split"][1] == ["BCK1BCK6", 145]
    assert out["split"][2] == ["NOPORT", None]


# ── Source locks on the regime rules ────────────────────────────────────────

def test_source_locks_regime_rules():
    """Pin the calibrated thresholds and the additive-only routing so a
    future edit can't silently move them (they were calibrated against
    A-F West / SANDUR / SEANOR / ELMMIL real spans on 2026-07-14)."""
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")

    # Raw-identity short-circuit thresholds.
    assert "_RAW_IDENT_R = 0.98" in src
    assert "_RAW_IDENT_SIGMA_DB = 0.001" in src

    # Decay detector thresholds.
    assert "_DECAY_NEAR_GAP = 3" in src
    assert "_DECAY_FAR_GAP = 30" in src
    assert "_DECAY_MIN_PAIRS = 10" in src
    assert "_DECAY_MIN_DROP = 0.30" in src
    assert "_SHORT_COMMON_SPAN_M = 2000.0" in src

    # The three PRE-EXISTING regime rules are untouched, in order.
    # (2026-07-21: the all_dups gate gained a span floor — see
    # test_ss_alldups_span.py for its own locks; order lock kept here.)
    i_all = src.index("if (bulk_r >= 0.7 and bulk_sigma < 0.10")
    i_short = src.index("elif min_L < 200 and len(files) >= 50:")
    i_tie = src.index("elif bulk_r >= 0.7 or frac_high_r >= 0.30:")
    assert i_all < i_short < i_tie

    # The new routes live in the else (would-have-been-production) branch
    # ONLY — additive routing, production stays the default.
    else_block = src[i_tie:src.index("_reason_sfx")]
    assert "neighbor-decay: near r" in else_block
    assert "short common span:" in else_block

    # The short-circuit is applied AFTER the physical filters, raising only.
    # Composition grew on 2026-07-23 (Lumen Border FP fix): uniqueness twin
    # gate + different-OTDR gate joined the physical filters.
    i_phys = src.index("physical_violation = (length_violation | events_violation")
    i_ident = src.index("raw_ident_mask")
    assert i_phys < i_ident

    # Runner-side verdict override exists.
    runner = (SECRETSAUCE_DIR / "run_secretsauce.py").read_text(encoding="utf-8")
    assert "CONFIRMED duplicate (identical)" in runner
