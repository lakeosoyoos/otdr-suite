"""End-zone CORRECTED-ANCHOR geometry (EXFO's own cable-end cursors).

The end-zone reconstruction shipped in run 134 is our own construction: a
shared-slope two-line fit over whatever glass sits beside the cable end.  It
works, but it is anchored on the SILENT side's guessed position.  EXFO is not:
at a cable end it anchors on the LOUD (mirror) direction's stored event and
maps it through the end, and its after-window runs to the end event itself.

    CursorA    = idx(EOL - mirror_event.dist)     (mirror's own launch frame)
    CursorB    = CursorA + the mirror event's OWN width in samples
    SubCursorA = max(previous event's marker-end, CursorA - 5001.6 m)
    SubCursorB = idx(EOL)          exactly
    loss       = after_line(CursorB) - before_line(CursorB)

Calibrated on 918 FastReporter grey values across WSC↔SUI (train 1-864 /
test 865-1152, two different B units): median |Δ| 0.0180 → 0.0125 dB, and the
Splice-12 column goes from 23 TP / 1 FP / 4 misses to 26 / 2 / 1 against
FastReporter.  Re-measuring the 152 fibers that DO store the event, off their
own stored cursors, reproduces FastReporter to 0.0022 dB median — which is
what says the geometry itself (not a fitted fudge) is EXFO's.

Four choices in that recipe are load-bearing and each was measured against its
alternative; they are pinned individually below:

  * TRUNCATED km→sample indexing (rounding tested measurably worse)
  * CursorA anchored on the MIRROR event, not on the caller's position
  * CursorB = CursorA + the mirror event's own marker width
  * SubCursorB = idx(EOL) exactly (±1 sample degrades the median)
  * BOTH lines evaluated AT CursorB (not at CursorA, not at the position)

Namespace rule: the engine ships its own sor_reader324802a.py copy, so every
behavioural check runs in a clean child interpreter (static source guards
otherwise).
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
ENGINE_SRC = (SPLICEREPORT_DIR / "splicereportmatchexfo.py").read_text(encoding="utf-8")
SOR_SRC = (SPLICEREPORT_DIR / "sor_reader324802a.py").read_text(encoding="utf-8")


def _run(body: str, imports: str = ""):
    """Run `body` in a clean child interpreter; it must print OK last."""
    src = ("import sys\n"
           f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
           + imports + textwrap.dedent(SCAFFOLD) + textwrap.dedent(body))
    p = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    out = p.stdout.strip().splitlines()
    assert out and out[-1] == "OK", p.stdout or p.stderr


# A synthetic far-end geometry plus an INDEPENDENT implementation of the
# convention, so the assertions compare the shipping code against the written
# recipe rather than against itself.  The noise is seeded and non-trivial: with
# a noiseless straight line the before/after slopes would come out identical
# and every alternative cursor choice would score the same, which would make
# the "this variant is different" locks vacuous.
SCAFFOLD = """
    import numpy as np
    from sor_reader324802a import (measure_endzone_grey_from_sor as MZ,
                                   ENDZONE_HALF_WIN_M)

    IOR   = 1.47
    RES_M = 2.5493                       # m/sample (275 ns acquisition)
    SP    = RES_M * 2.0 * IOR / 299792458.0
    EOL   = 64.0517                      # far-end connector / end event
    STEP  = 0.30                         # the real loss at the closure
    # mirror event 75.5 m from the far end, in the OTHER direction's frame.
    # (EOL - MIRROR_D) * 1000 / RES_M lands on sample 25095.59, so truncating
    # and rounding pick DIFFERENT samples — that is what makes the indexing
    # lock below a real test rather than a coincidence.
    MIRROR_D  = 0.0755
    MIRROR_SC = 0.0755                   # its raw marker start
    MIRROR_EC = 0.13260                  # its raw marker end (22 samples wide)
    PREV_EC   = 59.5373                  # previous event's marker end

    def ridx(km):
        return int(km * 1000.0 / RES_M)

    def synth(noise=0.03, seed=7):
        n = ridx(EOL) + 300
        x = np.arange(n) * RES_M / 1000.0
        y = 36.0 + 0.19 * x
        y[x >= EOL - MIRROR_D] += STEP
        y[x >= EOL] -= 20.0              # far-end connector reflection dip
        y = y + np.random.RandomState(seed).normal(0, noise, n)
        evs = [{'dist_km': 0.0, 'splice_loss': 0.0, 'type': '1F', 'is_end': False,
                'is_reflective': True, 'time_of_travel': 0,
                'tot_start_curr': 0, 'tot_end_curr': int(0.0714 * 1000 * IOR / 0.02998),
                'tot_end_prev': 0, 'tot_start_next': 0},
               {'dist_km': 59.4960, 'splice_loss': 0.097, 'type': '0F',
                'is_end': False, 'is_reflective': False,
                'time_of_travel': int(59.4960 * 1000 * IOR / 0.02998),
                'tot_start_curr': int(59.4965 * 1000 * IOR / 0.02998),
                'tot_end_curr': int(PREV_EC * 1000 * IOR / 0.02998),
                'tot_end_prev': 0, 'tot_start_next': 0},
               {'dist_km': EOL, 'splice_loss': 0.0, 'type': '1E', 'is_end': True,
                'is_reflective': True,
                'time_of_travel': int(EOL * 1000 * IOR / 0.02998)}]
        return {'_source': 'sor', 'trace': y, 'events': evs,
                'exfo_sampling_period': SP, '_trace_offset_km': 0.0,
                'num_points': n, 'wavelength': 1550.0}

    def ols(y, lo, hi):
        x = np.arange(lo, hi + 1, dtype=float)
        return np.polyfit(x, y[lo:hi + 1].astype(float), 1)

    def reference(rec, cur_a=None, width=None, sub_b=None, eval_at=None,
                  round_idx=False):
        \"\"\"The convention, written out again from the docstring.\"\"\"
        y = rec['trace']
        f = round if round_idx else int
        ieol = f(EOL * 1000.0 / RES_M)
        if cur_a is None:
            cur_a = f((EOL - MIRROR_D) * 1000.0 / RES_M)
        if width is None:
            width = max(4, f(MIRROR_EC * 1000.0 / RES_M)
                           - f(MIRROR_SC * 1000.0 / RES_M))
        cur_b = cur_a + width
        if sub_b is None:
            sub_b = ieol
        sub_a = max(f(PREV_EC * 1000.0 / RES_M),
                    cur_a - int(round(ENDZONE_HALF_WIN_M / RES_M)))
        before = ols(y, sub_a, cur_a - 1)
        after = ols(y, cur_b, sub_b)
        at = cur_b if eval_at is None else eval_at
        return float(np.polyval(after, at) - np.polyval(before, at))

    def measured(rec, P=None):
        return MZ(rec, EOL - MIRROR_D if P is None else P,
                  mirror_dist_km=MIRROR_D,
                  mirror_marker_start_km=MIRROR_SC,
                  mirror_marker_end_km=MIRROR_EC)
"""


# ═══════════════════════════════════════════════════════════════════════════
#  Source locks — the API and the five load-bearing choices
# ═══════════════════════════════════════════════════════════════════════════

def test_reader_exposes_the_corrected_anchor_api():
    """The reconstruction has to be TOLD the loud side's event; the parameters
    are optional so every existing caller keeps its behaviour."""
    assert "def measure_endzone_grey_from_sor(sor_data, position_km, ior=None," in SOR_SRC
    for p in ("mirror_dist_km=None", "mirror_marker_start_km=None",
              "mirror_marker_end_km=None"):
        assert p in SOR_SRC, f"missing optional parameter {p}"
    assert "def _endzone_mirror_grey(" in SOR_SRC
    assert "ENDZONE_HALF_WIN_M        = 5001.6" in SOR_SRC, (
        "SubCursorA's half-window is EXFO's 5.0016 km, same as the mid-span "
        "geometry — not a new tunable"
    )


def test_engine_threads_the_loud_side_event_into_the_reconstruction():
    """Both bidirectional grey paths know which direction stored the event;
    that event is the anchor, so both must pass it through."""
    assert "def _mirror_anchor(" in ENGINE_SRC
    assert "def _grey_loss(fiber_data, splice_km, mirror=None):" in ENGINE_SRC, (
        "the mirror must ride in as an OPTIONAL argument — callers that have "
        "no loud-side event keep the geometry they had"
    )
    assert "b_grey = _grey_loss(rb, b_frame_km,\n" in ENGINE_SRC
    assert "mirror=_mirror_anchor(r, ea))" in ENGINE_SRC, (
        "analyze_all measures the B side at an A-stored closure — `ea` is the "
        "mirror there"
    )
    assert "mirror=_mirror_anchor(rb, e))" in ENGINE_SRC, (
        "scan_b_events measures the A side at a B-stored closure — `e` is the "
        "mirror there (this is the WSC↔SUI Splice 12 path)"
    )
    assert "mirror_dist_km=m[0]" in ENGINE_SRC


def test_indexing_is_truncated_not_rounded():
    """km→sample is int(), never round().  Measured on 918 FastReporter grey
    values: rounding is worse, and it is worse structurally (EXFO's cursors
    are sample counts, not nearest-sample lookups)."""
    body = SOR_SRC[SOR_SRC.index("def _endzone_mirror_grey("):
                   SOR_SRC.index("def _endzone_prev_marker_end_km(")]
    assert "return int((km + off) * 1000.0 / res_m)" in body
    assert "return int(km * 1000.0 / res_m)" in body
    assert "round(" not in body.replace("int(round(ENDZONE_HALF_WIN_M / res_m))", ""), (
        "the only rounding allowed in the corrected-anchor geometry is the "
        "fixed 5001.6 m half-window length"
    )


def test_cursor_geometry_is_the_calibrated_one():
    body = SOR_SRC[SOR_SRC.index("def _endzone_mirror_grey("):
                   SOR_SRC.index("def _endzone_prev_marker_end_km(")]
    assert "cur_a = idx(eol_km - float(mirror_dist_km))" in body, (
        "CursorA is the MIRROR event mapped through the cable end"
    )
    assert "ridx(float(mirror_end_km)) - ridx(float(mirror_start_km))" in body, (
        "CursorB - CursorA is the mirror event's OWN marker width"
    )
    assert "after = _endzone_ols(trace, cur_b, ieol)" in body, (
        "SubCursorB is idx(EOL) exactly — the after-window runs INCLUSIVE to "
        "the end event"
    )
    assert "np.polyval(after, cur_b) - np.polyval(before, cur_b)" in body, (
        "both lines are read AT CursorB — do not 'tidy' this to CursorA or to "
        "the event position"
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Behaviour — the shipping code must equal the written recipe, and must NOT
#  equal any of the alternatives that were tested and rejected
# ═══════════════════════════════════════════════════════════════════════════

def test_matches_an_independent_implementation_of_the_convention():
    _run("""
        rec = synth()
        got = measured(rec)
        assert got is not None, "the far-end mirror geometry must measure"
        want = reference(rec)
        assert abs(got - want) < 1e-9, (got, want)
        # ... and it is a real reading of the planted step, not a constant
        assert abs(got - STEP) < 0.05, got
        print("OK")
    """)


def test_rejected_variants_give_different_answers():
    """Guards the locks above from becoming decorative: each alternative must
    actually move the number, otherwise the test proves nothing."""
    _run("""
        rec = synth()
        got = measured(rec)
        alts = {
            'rounded indexing':      reference(rec, round_idx=True),
            'SubCursorB = EOL - 1':  reference(rec, sub_b=int(EOL * 1000.0 / RES_M) - 1),
            'SubCursorB = EOL + 1':  reference(rec, sub_b=int(EOL * 1000.0 / RES_M) + 1),
            'evaluated at CursorA':  reference(rec, eval_at=int((EOL - MIRROR_D) * 1000.0 / RES_M)),
            'fixed 17-sample width': reference(rec, width=17),
            'anchor off by 4':       reference(rec, cur_a=int((EOL - MIRROR_D) * 1000.0 / RES_M) + 4),
        }
        for name, v in alts.items():
            assert abs(got - v) > 1e-6, (name, got, v)
        print("OK")
    """)


def test_anchor_comes_from_the_mirror_not_from_the_caller_position():
    """The whole point: the silent side does not know where the closure is to
    better than a sample or two, but the loud side stored it exactly.  Moving
    the caller's position (inside the agreement tolerance) must NOT move the
    answer; dropping the mirror must."""
    _run("""
        rec = synth()
        base = measured(rec)
        for dp in (-0.02, -0.005, 0.005, 0.02):
            assert abs(measured(rec, P=EOL - MIRROR_D + dp) - base) < 1e-9, dp
        plain = MZ(rec, EOL - MIRROR_D)          # no mirror -> shipped geometry
        assert plain is not None
        assert abs(plain - base) > 1e-6, "the two geometries must be distinct"
        print("OK")
    """)


def test_falls_back_when_the_mirror_cannot_be_trusted():
    """Fail SAFE, not silent: a mirror that disagrees with the caller's
    position, or one that resolves to the LAUNCH end (where SubCursorB = EOL
    would swallow the whole cable), drops back to the shipped geometry rather
    than inventing a number."""
    _run("""
        rec = synth()
        plain = MZ(rec, EOL - MIRROR_D)
        # mirror 1.2 km away from the caller's position -> not this closure
        far = MZ(rec, EOL - MIRROR_D, mirror_dist_km=MIRROR_D + 1.2,
                 mirror_marker_start_km=MIRROR_SC, mirror_marker_end_km=MIRROR_EC)
        assert far == plain, (far, plain)
        # a mirror that maps to the LAUNCH end is out of the calibrated scope
        near_launch = MZ(rec, 0.0765, mirror_dist_km=EOL - 0.0765,
                         mirror_marker_start_km=MIRROR_SC,
                         mirror_marker_end_km=MIRROR_EC)
        assert near_launch == MZ(rec, 0.0765), "near-launch keeps the old path"
        print("OK")
    """)


def test_mid_span_and_bad_frames_are_still_refused():
    """The corrected anchor rides INSIDE the existing scope guards — it must
    not widen them.  Mid-span stays the EXFO windower's job, and a trace whose
    far-end connector is not at idx(EOL) is still refused outright."""
    _run("""
        import numpy as np
        rec = synth()
        assert MZ(rec, 32.0, mirror_dist_km=EOL - 32.0,
                  mirror_marker_start_km=MIRROR_SC,
                  mirror_marker_end_km=MIRROR_EC) is None, "mid-span stays out of scope"
        bad = dict(rec)
        shift = int(1.0376 * 1000.0 / RES_M)
        bad['trace'] = np.concatenate([np.full(shift, 36.0), rec['trace']])
        assert measured(bad) is None, (
            "a frame the trace disagrees with must be refused, mirror or not")
        print("OK")
    """)
