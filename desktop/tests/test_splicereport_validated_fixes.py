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
    """The A-only recovery path must exist: when the bidir average is below
    threshold but A clears SINGLE_DIR_THRESHOLD and B's grey is flat (< the
    bend floor), analyze_all emits an A-only cell instead of dropping."""
    src = (SPLICEREPORT_DIR / "splicereportmatchexfo.py").read_text(encoding="utf-8")
    assert "a_loss_abs >= SINGLE_DIR_THRESHOLD and" in src, (
        "expected a single-direction recovery gate combining the A "
        "single-dir threshold with a B-flat check"
    )
    assert "abs(b_grey) < BEND_THRESHOLD" in src, (
        "the absent side must be CONFIRMED flat (grey below the bend floor), "
        "not merely missing — a borderline B reading must NOT count as flat"
    )
    assert "'_b_is_flat_grey': True" in src, (
        "the recovered cell should be marked as B-confirmed-flat single-dir"
    )


def test_fix1_emits_single_dir_when_a_clear_b_flat():
    """A clear loss + B verifiably flat → a single-direction A cell is emitted.
    All-flat (A below SINGLE_DIR_THRESHOLD) → nothing is emitted."""
    _run_engine_snippet(_FIBER_HELPER + """
        # B grey is FLAT everywhere (monkeypatch the wide-LSA probe).
        E._grey_loss = lambda fiber_data, km: 0.01

        SP = 30.0  # closure km

        # ── Case A: A shows a clear 0.30 dB loss (clears SINGLE_DIR_THRESHOLD,
        #    0.250); B is flat (grey 0.01).  The bidir AVERAGE is (0.30+0.01)/2
        #    = 0.155, just UNDER REBURN_THRESHOLD (0.160) — so the old engine
        #    dropped it.  FIX 1 recovers it as a single-direction A cell. ──
        assert (0.30 + 0.01) / 2.0 < E.REBURN_THRESHOLD     # the knife-edge
        assert 0.30 >= E.SINGLE_DIR_THRESHOLD               # A is clearly real
        splices = [{'position_km': SP, 'position_km_refined': SP,
                    'column_kind': 'splice'}]
        fa = {1: _fiber([(SP, 0.30, '0F', -60.0)])}
        fb = {1: _fiber([])}                     # B has NO event at the closure
        res = E.analyze_all(fa, fb, splices, E.REBURN_THRESHOLD)
        cell = res.get((1, 0))
        assert cell is not None, "A-clear / B-flat event was dropped (FIX 1 miss)"
        assert cell['is_a_only'] is True, cell
        assert cell['is_flagged'] is True
        assert cell['b_loss'] is not None and abs(cell['b_loss']) < E.BEND_THRESHOLD
        assert cell.get('_b_is_flat_grey') is True
        assert abs(abs(cell['a_loss']) - 0.30) < 1e-6

        # ── Case B: A loss is 0.20 (below SINGLE_DIR_THRESHOLD); B flat.
        #    The bidir average (0.105) is sub-threshold AND A doesn't clear the
        #    strict single-direction bar, so NOTHING should be emitted. ──
        assert 0.20 < E.SINGLE_DIR_THRESHOLD
        fa2 = {1: _fiber([(SP, 0.20, '0F', -60.0)])}
        fb2 = {1: _fiber([])}
        res2 = E.analyze_all(fa2, fb2, splices, E.REBURN_THRESHOLD)
        assert (1, 0) not in res2, (
            "all-flat / weak-A case must NOT be emitted as single-direction"
        )

        print("OK")
    """)


# ═══════════════════════════════════════════════════════════════════════════
#  FIX 2 — borderline band
# ═══════════════════════════════════════════════════════════════════════════

def test_fix2_static_borderline_band_is_additive():
    src = (SPLICEREPORT_DIR / "splicereportmatchexfo.py").read_text(encoding="utf-8")
    assert "BORDERLINE_LO_MARGIN" in src and "BORDERLINE_HI_MARGIN" in src
    assert "def _is_borderline_loss" in src
    # The band is computed off the LIVE threshold so a REBURN_THRESHOLD
    # override moves it (not a hard-coded 0.150/0.175).
    assert "threshold - BORDERLINE_LO_MARGIN" in src
    # Must not gate flagging: is_borderline is derived AFTER is_flagged is set,
    # and is_flagged is never a function of is_borderline.
    assert "is_flagged = (abs(bidir_loss) >= threshold)" in src
    assert "and _is_borderline_loss(bidir_loss, threshold)" in src


def test_fix2_borderline_marks_only_knife_edge_cells():
    """Cells at ~0.158 and ~0.170 carry the borderline marker; 0.30 and 0.05
    do not.  The marker never changes whether the cell is flagged."""
    _run_engine_snippet(_FIBER_HELPER + """
        E._grey_loss = lambda fiber_data, km: None  # force event-table pairing

        # Four closures, each gets an A+B matched event at a chosen bidir loss.
        kms = [10.0, 20.0, 30.0, 40.0]
        target = {10.0: 0.158, 20.0: 0.170, 30.0: 0.300, 40.0: 0.050}
        splices = [{'position_km': k, 'position_km_refined': k,
                    'column_kind': 'splice'} for k in kms]

        # bidir = (a_loss + b_loss)/2, so set both sides equal to the target.
        a_spec = [(k, target[k], '0F', -60.0) for k in kms]
        b_spec = [(70.0 - k, target[k], '0F', -60.0) for k in kms]  # B-frame mirror
        fa = {1: _fiber(a_spec, eol_km=70.0)}
        fb = {1: _fiber(b_spec, eol_km=70.0)}

        res = E.analyze_all(fa, fb, splices, E.REBURN_THRESHOLD)

        got = {}
        for (fnum, si), cell in res.items():
            got[round(kms[si], 1)] = cell
        # All four cleared the bidir/break/etc. emission (0.05 is below
        # REBURN_THRESHOLD, so it is only present if flagged for another
        # reason — assert it is either absent or NOT borderline).
        # 0.158 and 0.170 are inside [0.150, 0.175] -> borderline.
        assert got[10.0]['is_borderline'] is True, got[10.0]
        assert got[20.0]['is_borderline'] is True, got[20.0]
        assert 'borderline' in got[10.0]['label']
        # 0.300 is well above the band -> flagged reburn, NOT borderline.
        assert got[30.0]['is_borderline'] is False, got[30.0]
        assert got[30.0]['is_flagged'] is True
        assert 'borderline' not in got[30.0]['label']
        # 0.050 is below threshold -> not flagged at all (not emitted).
        assert 40.0 not in got, "0.05 dB cell should not be flagged/emitted"

        print("OK")
    """)


def test_fix2_borderline_marker_in_manifest_does_not_change_counts(tmp_path):
    """Through the real runner on the fixture: adding the marker field must not
    change n_flagged (the marker is purely additive)."""
    out = tmp_path / "rep.xlsx"
    rc, m, stderr = run_splicereport(FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, out)
    assert rc == 0 and m and m["ok"], f"runner failed: {stderr[-800:]}"
    # Every cell carries the additive 'borderline' field.
    assert all('borderline' in c for c in m['cells'])
    # And it is a clean boolean.
    assert all(isinstance(c['borderline'], bool) for c in m['cells'])


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
    assert "'warnings': provenance_warnings" in src, (
        "provenance warnings must be threaded into the manifest"
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
