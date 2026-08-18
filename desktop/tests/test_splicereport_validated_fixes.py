"""Regression tests for three trace-validated Splice Report engine fixes.

These came out of the retrospective trace-validation against the human reports
(the lsa_verify prototype under splice-tune):

  FIX 1  single-direction emit — recover a real miss (Ontario↔Boise / Seattle
         F111) where A shows a clear loss but B is FLAT.  The bidir /2 average
         falls under REBURN_THRESHOLD, so the engine used to drop the event.
  FIX 2  borderline band — surface a threshold-edge reburn (~0.150-0.175) with
         an additive review marker without changing flagging or counts.
  FIX 3  dataset-provenance guard — warn when the A-set and B-set came from
         DIFFERENT acquisitions (the Elmdale-Miller trap).

HARD RULE — namespace isolation
-------------------------------
The Splice Report engine ships its OWN sor_reader324802a.py that collides with
the viewer's copy, so this test process must NEVER import splicereportmatchexfo
directly.  Every behavioural check runs the engine in a CLEAN child subprocess
with only SPLICEREPORT_DIR on sys.path (the same isolation conftest.run_splicereport
relies on), plus static-source guards that pin each fix.  Synthetic fiber dicts
mimic the parsed-SOR structure so we can drive precise A-clear / B-flat and
threshold-edge scenarios the 24-fiber fixture doesn't naturally produce.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

from conftest import (SPLICEREPORT_DIR, run_splicereport,
                      FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR)


def _run_engine_snippet(body: str):
    """Run `body` in a clean child interpreter with the splice engine importable.
    `body` should print 'OK' on success (and may raise/SystemExit on failure).
    `body` is dedented to column 0 before concatenation (so callers can keep
    a readable indented triple-quoted block)."""
    header = (
        "import sys\n"
        f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
        "import splicereportmatchexfo as E\n"
    )
    snippet = header + textwrap.dedent(body)
    p = subprocess.run([sys.executable, "-c", snippet],
                       capture_output=True, text=True)
    assert p.returncode == 0, (
        f"subprocess exited {p.returncode}\n"
        f"stdout:\n{p.stdout}\nstderr:\n{p.stderr}"
    )
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


# ───────────────────────── shared synthetic-fiber helper ─────────────────────
# Emitted into the subprocess: builds a minimal parsed-SOR-shaped fiber dict.
# Indented 8 spaces to share a common prefix with the test bodies so a single
# textwrap.dedent over the concatenation lands everything at column 0.
_FIBER_HELPER = """
        def _fiber(events_spec, eol_km=70.0, wavelength=1550.0):
            # events_spec: list of (dist_km, splice_loss, type, reflection)
            evs = []
            for (dk, sl, ty, refl) in events_spec:
                evs.append({'dist_km': dk, 'splice_loss': sl, 'type': ty,
                            'reflection': refl, 'is_end': False})
            evs.append({'dist_km': eol_km, 'splice_loss': 0.0, 'type': '1E',
                        'reflection': -40.0, 'is_end': True})
            return {'_source': 'sor', 'wavelength': wavelength,
                    'events': sorted(evs, key=lambda e: e['dist_km'])}
"""


# ═══════════════════════════════════════════════════════════════════════════
#  FIX 1 — single-direction emit
# ═══════════════════════════════════════════════════════════════════════════

def test_fix1_static_single_direction_recovery_present():
    """The A-only single-direction path must exist — and, since the WSC↔SUI
    review, must fire ONLY when the silent side is truly UNMEASURABLE.

    SUPERSEDED (2026-07-31, fix/endzone-bidir): the original FIX 1 also
    recovered a raw-A "(A)" cell when b_grey was MEASURABLE and flat.  That
    contradicts the FastReporter north star — FastReporter averages the two
    sides and applies the same 0.160 cutoff — and the boss's review deleted
    exactly those cells (456 @Splice 3: A .294, b_grey .011 → .152;
    826 @Splice 5: A .259, b_grey .021 → .140).  A measured flat B side is
    EVIDENCE the bidirectional loss is small.  The signed-loss gate and the
    stricter SINGLE_DIR_THRESHOLD (the parts that were right) are unchanged;
    see desktop/tests/test_endzone_bidir.py for the replacement behaviour."""
    src = (SPLICEREPORT_DIR / "splicereportmatchexfo.py").read_text(encoding="utf-8")
    assert "ea['splice_loss'] >= SINGLE_DIR_THRESHOLD and" in src, (
        "expected a single-direction gate on a POSITIVE A loss clearing the "
        "single-dir threshold — gate on the SIGNED loss, not abs(), so a "
        "negative-A gainer can't masquerade as a loss"
    )
    assert "abs(b_grey) < BEND_THRESHOLD" not in src, (
        "the measurable-but-flat b_grey raw-A recovery must be GONE — when the "
        "silent side is measurable the bidirectional average decides"
    )
    assert "'_b_is_flat_grey': True" not in src, (
        "the B-confirmed-flat single-dir cell no longer exists"
    )


def test_fix1_emits_single_dir_only_when_b_is_unmeasurable():
    """Silent side MEASURABLE (even if flat) → the bidir average decides.
    Silent side UNMEASURABLE → the raw-A single-direction cell still ships.
    A gainer never ships either way."""
    _run_engine_snippet(_FIBER_HELPER + """
        SP = 30.0  # closure km
        splices = [{'position_km': SP, 'position_km_refined': SP,
                    'column_kind': 'splice'}]

        # ── Case A: A shows a clear 0.30 dB loss (clears SINGLE_DIR_THRESHOLD,
        #    0.250); B's grey is MEASURABLE and flat (0.01).  The bidir average
        #    is (0.30+0.01)/2 = 0.155, under REBURN_THRESHOLD — FastReporter
        #    drops it, so we drop it (the boss deleted these cells by hand). ──
        E._grey_loss = lambda fiber_data, km, mirror=None, twin=None: 0.01
        assert (0.30 + 0.01) / 2.0 < E.REBURN_THRESHOLD     # the knife-edge
        assert 0.30 >= E.SINGLE_DIR_THRESHOLD               # A is clearly real
        fa = {1: _fiber([(SP, 0.30, '0F', -60.0)])}
        fb = {1: _fiber([])}                     # B has NO event at the closure
        res = E.analyze_all(fa, fb, splices, E.REBURN_THRESHOLD)
        assert (1, 0) not in res, (
            "a MEASURABLE flat silent side must let the bidir average decide, "
            "not ship the loud side raw as (A)"
        )

        # ── Case A2: same event, but the silent side is UNMEASURABLE.  Now
        #    there is no average to form and the conservative raw-A
        #    single-direction cell is the only honest output. ──
        E._grey_loss = lambda fiber_data, km, mirror=None, twin=None: None
        E._local_step_confirms = lambda fiber_data, e: True
        res_a2 = E.analyze_all(fa, fb, splices, E.REBURN_THRESHOLD)
        cell = res_a2.get((1, 0))
        assert cell is not None, "unmeasurable silent side must still ship (A)"
        assert cell['is_a_only'] is True, cell
        assert cell['is_flagged'] is True
        assert cell['b_loss'] is None, cell
        assert abs(abs(cell['a_loss']) - 0.30) < 1e-6

        # ── Case B: A loss is 0.20 (below SINGLE_DIR_THRESHOLD), B unmeasurable.
        #    A doesn't clear the strict single-direction bar → nothing. ──
        assert 0.20 < E.SINGLE_DIR_THRESHOLD
        fa2 = {1: _fiber([(SP, 0.20, '0F', -60.0)])}
        fb2 = {1: _fiber([])}
        res2 = E.analyze_all(fa2, fb2, splices, E.REBURN_THRESHOLD)
        assert (1, 0) not in res2, (
            "all-flat / weak-A case must NOT be emitted as single-direction"
        )

        # ── Case C: A reads a 0.30 dB GAINER (signed loss -0.30).  |loss|
        #    clears 0.250 but it is NOT a real loss — the positive-loss gate
        #    must REJECT it (a gainer must never surface as a single-dir loss). ──
        fa3 = {1: _fiber([(SP, -0.30, '0F', -60.0)])}
        fb3 = {1: _fiber([])}
        res3 = E.analyze_all(fa3, fb3, splices, E.REBURN_THRESHOLD)
        assert (1, 0) not in res3, (
            "a negative-A gainer (|loss| >= threshold but signed < 0) must NOT "
            "be emitted as a single-direction loss"
        )

        print("OK")
    """)


def test_fix1b_static_bside_single_dir_gates_use_signed_loss():
    """Symmetry guard for FIX 1: the B-side single-direction gates (B-fill and
    the no-JSON B-only fallback) must gate on the SIGNED loss too, so a B-side
    gainer can't surface as a single-dir loss — the same leak FIX 1 closed on A.
    Pins the rewrite and prevents a regression back to the abs()-gate form."""
    src = (SPLICEREPORT_DIR / "splicereportmatchexfo.py").read_text(encoding="utf-8")
    # B-only fallback gate: signed, not abs.
    assert "if (b_loss_signed >= SINGLE_DIR_THRESHOLD and" in src, (
        "B-only fallback must gate on the signed loss (b_loss_signed)"
    )
    assert "if b_loss_abs >= SINGLE_DIR_THRESHOLD" not in src, (
        "the abs()-based B-only single-dir gate must be gone (a gainer leak)"
    )
    # B-fill gate: the loss value fed to the >= SINGLE_DIR_THRESHOLD check is the
    # signed splice_loss, not abs(splice_loss).
    assert "b_loss_val = b_evt['splice_loss']" in src
    assert "b_loss_val = abs(b_evt['splice_loss'])" not in src, (
        "the B-fill single-dir value must be the signed loss, not abs()"
    )


def test_borderline_band_removed_is_disabled_noop():
    """REVERSED (boss's workflow): there is NO loss borderline band.  The reburn
    threshold (settings-panel REBURN_THRESHOLD, typically 0.16) is a HARD cutoff.
    _is_borderline_loss is a disabled no-op that returns False for EVERY input —
    including losses that used to sit inside the old ~0.150-0.175 review band."""
    _run_engine_snippet("""
        thr = E.REBURN_THRESHOLD
        # Old-band values, threshold edges, and a near-threshold gainer:
        for x in (0.150, 0.155, 0.158, 0.160, 0.165, 0.170, 0.175,
                  -0.155, None, 0.30, 0.05):
            assert E._is_borderline_loss(x, thr) is False, (x, "must never be borderline")
        # The dead band constants are gone from the module namespace.
        assert not hasattr(E, 'BORDERLINE_LO_MARGIN')
        assert not hasattr(E, 'BORDERLINE_HI_MARGIN')
        print("OK")
    """)


# ═══════════════════════════════════════════════════════════════════════════
#  Hard reburn threshold — no borderline / review band (FIX 2 reversed)
# ═══════════════════════════════════════════════════════════════════════════

def test_hard_threshold_flagging_preserved_static():
    """Flagging stays a HARD threshold call and is never a function of the
    (now disabled) borderline tier: the band comparison is removed but the
    `is_flagged = _clears_threshold(bidir_loss, threshold) ...` rule is intact.

    (The comparison moved behind _clears_threshold on 2026-07-31 so the gate
    reads the same 3-decimal value the cell PRINTS — still one hard cutoff at
    the same threshold, no band; see test_endzone_bidir.py.)"""
    src = (SPLICEREPORT_DIR / "splicereportmatchexfo.py").read_text(encoding="utf-8")
    # The hard-threshold flagging rule is the load-bearing invariant.
    assert "is_flagged = (_clears_threshold(bidir_loss, threshold)" in src
    # The band is gone: no live comparison, no margin constants.
    assert "BORDERLINE_LO_MARGIN" not in src and "BORDERLINE_HI_MARGIN" not in src
    assert "<= bidir_loss\n" not in src, "the active borderline band comparison must be removed"


def test_no_cell_is_ever_borderline_hard_threshold():
    """A near-threshold sub-threshold loss (0.158, 0.170) is NOT surfaced —
    no borderline tier, no 'borderline' label, not flagged, not emitted.  A
    genuine reburn (0.300) still flags; a clean loss (0.05) stays absent."""
    _run_engine_snippet(_FIBER_HELPER + """
        E._grey_loss = lambda fiber_data, km, mirror=None, twin=None: None  # force event-table pairing

        kms = [10.0, 20.0, 30.0, 40.0]
        target = {10.0: 0.158, 20.0: 0.170, 30.0: 0.300, 40.0: 0.050}
        splices = [{'position_km': k, 'position_km_refined': k,
                    'column_kind': 'splice'} for k in kms]

        a_spec = [(k, target[k], '0F', -60.0) for k in kms]
        b_spec = [(70.0 - k, target[k], '0F', -60.0) for k in kms]  # B-frame mirror
        fa = {1: _fiber(a_spec, eol_km=70.0)}
        fb = {1: _fiber(b_spec, eol_km=70.0)}

        res = E.analyze_all(fa, fb, splices, E.REBURN_THRESHOLD)

        got = {}
        for (fnum, si), cell in res.items():
            got[round(kms[si], 1)] = cell
        # No cell anywhere carries the borderline marker.
        for cell in res.values():
            assert cell.get('is_borderline', False) is False, cell
            assert 'borderline' not in cell.get('label', '')
        # Hard threshold = 0.16.  0.158 is sub-threshold and not a bend -> NOT
        # flagged; with no review tier it is not surfaced for a human at all.
        assert got.get(10.0, {}).get('is_flagged', False) is False, got.get(10.0)
        # 0.170 and 0.300 are >= threshold -> flagged reburn (never 'borderline').
        assert got[20.0]['is_flagged'] is True, got.get(20.0)
        assert got[30.0]['is_flagged'] is True, got.get(30.0)
        # 0.050 is below threshold -> not flagged at all (not emitted).
        assert 40.0 not in got, "0.05 dB cell should not be flagged/emitted"

        print("OK")
    """)


def test_fix2_n_flagged_counts_only_flagged_not_borderline(tmp_path):
    """Through the real runner on the fixture: n_flagged must count ONLY cells
    with is_flagged True (the REAL invariant — len(cells) would let a
    borderline-only / dead-zone cell inflate the flagged count).  Also exposes
    n_borderline and the per-cell is_flagged field."""
    out = tmp_path / "rep.xlsx"
    rc, m, stderr = run_splicereport(FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, out)
    assert rc == 0 and m and m["ok"], f"runner failed: {stderr[-800:]}"
    # Every cell carries the additive 'borderline' + 'is_flagged' booleans.
    assert all('borderline' in c and isinstance(c['borderline'], bool) for c in m['cells'])
    assert all('is_flagged' in c and isinstance(c['is_flagged'], bool) for c in m['cells'])
    # THE invariant: n_flagged is the is_flagged count, NOT len(cells), so a
    # borderline-only (is_flagged=False) review cell can never inflate it.
    assert m['n_flagged'] == sum(1 for c in m['cells'] if c['is_flagged'])
    assert m['n_flagged'] <= len(m['cells'])
    # The band is removed: n_borderline is always 0 and no cell is borderline.
    assert 'n_borderline' in m and isinstance(m['n_borderline'], int)
    assert m['n_borderline'] == 0
    assert all(c['borderline'] is False for c in m['cells'])


# ═══════════════════════════════════════════════════════════════════════════
#  FIX 3 — dataset-provenance guard
# ═══════════════════════════════════════════════════════════════════════════

def test_fix3_static_provenance_guard_present():
    src = (SPLICEREPORT_DIR / "run_splicereport.py").read_text(encoding="utf-8")
    assert "def _provenance_warnings" in src
    assert "PROV_EOL_TOL_KM" in src and "PROV_CLOSURE_TOL" in src
    assert "_nominal_wavelength_nm" in src, (
        "wavelength must be compared on the nominal band, not the exact "
        "per-trace λ (EXFO jitters a few nm)"
    )
    # The manifest line grew the GenParams identity warnings (rescue-only
    # fiber-identity feature); provenance warnings must STILL be threaded in.
    assert "'warnings': identity_warnings + provenance_warnings" in src, (
        "provenance (and identity) warnings must be threaded into the manifest"
    )


def test_fix3_matched_pair_passes_clean(tmp_path):
    """The matched ELMMIL/MILELM fixture (same cable, both ends) must NOT
    trip the guard — empty warnings, ok report.  Guards against false alarms
    from EXFO's per-trace wavelength jitter (1548.0 vs 1539.8 nm)."""
    out = tmp_path / "rep.xlsx"
    rc, m, stderr = run_splicereport(FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, out)
    assert rc == 0 and m and m["ok"], f"runner failed: {stderr[-800:]}"
    assert m.get("warnings") == [], (
        f"matched pair tripped the provenance guard: {m.get('warnings')}"
    )


def test_fix3_mismatched_pair_triggers_guard():
    """A mismatched A/B (different EOL span, different wavelength, different
    closure count) trips the guard with clear WARNINGs; a matched pair stays
    clean.  Exercises _provenance_warnings directly in a clean subprocess."""
    _run_engine_snippet(_FIBER_HELPER + """
        import importlib.util, os
        # Import the runner module in this clean namespace (engine on path).
        runner_path = os.path.join({SPLICEREPORT_DIR!r}, "run_splicereport.py")
        spec = importlib.util.spec_from_file_location("rsr", runner_path)
        rsr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rsr)

        # ── Matched pair: same EOL, same nominal λ, same closure layout. ──
        # 20 fibers each (>= MIN_POP_SPLICE) with two real closures.
        def cable(eol, wl, closures):
            d = {}
            for fnum in range(1, 21):
                evs = [(c, 0.05, '0F', -60.0) for c in closures]
                d[fnum] = _fiber(evs, eol_km=eol, wavelength=wl)
            return d

        fa = cable(70.0, 1548.0, [15.0, 30.0, 45.0, 60.0])   # 4 closures; λ jitter low
        fb = cable(70.05, 1539.8, [15.0, 30.0, 45.0, 60.0])  # matched 4 closures; λ jitter high
        warns = rsr._provenance_warnings(E, fa, fb)
        assert warns == [], ("matched pair (nominal 1550 both, ~70 km both, "
                             "2 closures both) must be clean, got: %r" % warns)

        # ── Mismatch: B is a different cable — shorter span, 1310 nm, 1 closure.
        fb_bad = cable(40.0, 1310.0, [25.0])
        warns2 = rsr._provenance_warnings(E, fa, fb_bad)
        joined = " | ".join(warns2)
        assert warns2, "mismatched pair did not trip the guard"
        assert "EOL" in joined, joined          # 70 vs 40 km
        assert "wavelength" in joined, joined   # 1550 vs 1310 nm
        assert "closures" in joined, joined     # 4 vs 1 (diff 3 > PROV_CLOSURE_TOL)

        print("OK")
    """.replace("{SPLICEREPORT_DIR!r}", repr(str(SPLICEREPORT_DIR))))


# ═══════════════════════════════════════════════════════════════════════════
#  HELIX FIX — EOF-anchored far-end fold (HOWLAN @108.8 holdout)
# ═══════════════════════════════════════════════════════════════════════════

def test_eof_anchored_far_end_fold():
    """A helix-shifted LAST-closure splice on a SHORT-reading fiber folds via the
    end-of-fiber anchor (the HOWLAN @108.8 holdout the boss identified as a
    helix-shifted Splice 1), WITHOUT folding a real far-end bend, and stays inert
    on a fiber that reads normal.

    All three far events sit 500-1000 m off the LINEAR per-fiber prediction, so
    the linear gate rejects every one — any fold here is the EOF anchor's doing,
    exactly the far-end non-linearity gap it was added to close."""
    _run_engine_snippet(_FIBER_HELPER + """
        SPLICES  = [10.0, 30.0, 50.0, 70.0, 109.3]   # last closure = 109.3 km
        CONS_EOF = 117.3                              # consensus end-of-fiber
        INNER    = [(10.0,0.1,'0F',-60.0),(30.0,0.1,'0F',-60.0),
                    (50.0,0.1,'0F',-60.0),(70.0,0.1,'0F',-60.0)]

        # (1) SHORT fiber (end 116.9 = 0.40 km short): its last splice reads short
        #     too, landing at 108.8 ~= 116.9-(117.3-109.3).  Linear model predicts
        #     ~109.3 -> rejects (500 m).  EOF anchor folds it.
        # (truthiness, not 'is True/False' — the linear path returns a numpy bool)
        short = _fiber(INNER + [(108.8,0.05,'0F',-60.0)], eol_km=116.9)
        assert E._event_explained_as_splice(1, 108.8, SPLICES, {1: short}, consensus_eof=CONS_EOF), "short fiber last-closure splice must fold via the EOF anchor"

        # (2) SAME short fiber, far event 600 m off the EOF prediction (a genuine
        #     far-end bend) -> must STAY flagged.
        bend = _fiber(INNER + [(108.3,0.05,'0F',-60.0)], eol_km=116.9)
        assert not E._event_explained_as_splice(1, 108.3, SPLICES, {1: bend}, consensus_eof=CONS_EOF), "far-end event 600 m off the EOF prediction must NOT fold"

        # (3) NORMAL fiber (end 117.3, reads on-consensus): EOF anchor stays inert
        #     (read-short below the helix gate) -> event stays flagged.
        normal = _fiber(INNER + [(108.8,0.05,'0F',-60.0)], eol_km=117.3)
        assert not E._event_explained_as_splice(1, 108.8, SPLICES, {1: normal}, consensus_eof=CONS_EOF), "EOF anchor must stay inert on a fiber that does not read short"
        print("OK")
    """)
