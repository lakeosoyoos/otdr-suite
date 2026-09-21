"""The end-zone geometry must not amplify a sub-sample input change.

The corrected-anchor reconstruction (test_endzone_anchor.py) reads one loss
off two OLS lines whose windows are pinned by four sample indices.  Three of
those indices used to be computed by truncating an ABSOLUTE kilometre 25,000
samples out, separately, and then subtracting the whole numbers — so the
leftovers of three unrelated truncations decided the window shape, and an
input change far too small to move any real boundary could still flip them.

That is not a theoretical worry.  Correcting the sample pitch from a
back-derived group index to the one the file states moved it 25 ppm, which at
64 km is 1.6 m against a 2.55 m sample.  On WSC↔SUI it reshaped the windows
on 563 of 1,152 fibers, moved 698 of the 820 grey A legs (median 6.7 mdB,
worst 63.2) and dropped fiber 1104 under the gate.  Nothing physical moved;
the arithmetic did.

The property these tests pin is the one that was missing:

  * a pitch change that does NOT move the cable end to a different sample
    must change the answer by NOTHING AT ALL — not a millidecibel, zero;
  * a pitch change that DOES move it must slide the whole geometry rigidly,
    keeping the mirror width and both window lengths exactly as they were;
  * an index that lands ON a whole sample must resolve to that sample, not
    to the one below it, because float arithmetic decides which side of the
    boundary "exactly 25120.0" falls on.

Namespace rule: the engine ships its own sor_reader324802a.py copy, so every
behavioural check runs in a clean child interpreter.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
SOR_SRC = (SPLICEREPORT_DIR / "sor_reader324802a.py").read_text(encoding="utf-8")


def _run(body: str):
    src = ("import sys\n"
           f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
           + textwrap.dedent(SCAFFOLD) + textwrap.dedent(body))
    p = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    out = p.stdout.strip().splitlines()
    assert out and out[-1] == "OK", p.stdout or p.stderr


# A far-end geometry with the real span's proportions: the closure 76 m before
# a 64 km cable end, the previous closure 4.5 km back, and enough seeded noise
# that a one-sample change to a short window is visible in the answer.
SCAFFOLD = """
    import numpy as np
    from sor_reader324802a import _endzone_mirror_grey as MG

    IOR   = 1.47
    RES_M = 2.549255595                  # the pitch the file states
    EOL   = 64.0398
    MIRROR_D  = 0.0765                   # closure, in the OTHER direction's frame
    MIRROR_SC = 0.0764776678              # its raw marker start
    MIRROR_EC = 0.1300120353              # its raw marker end
    PREV_EC   = 59.5123718708             # previous event's marker end
    STEP  = 0.21

    def synth(noise=0.02, seed=11):
        # built by SAMPLE, so the cable end is the last glass sample and the
        # connector's reflection starts after it -- SubCursorB = idx(EOL) runs
        # INCLUSIVE to that last glass sample.
        i_eol = int(round(EOL * 1000.0 / RES_M))
        n = i_eol + 300
        y = 36.0 + 0.19 * (np.arange(n) * RES_M / 1000.0)
        y[i_eol - int(round(MIRROR_D * 1000.0 / RES_M)):] += STEP
        y[i_eol + 1:] -= 20.0
        return y + np.random.RandomState(seed).normal(0, noise, n)

    TRACE = synth()

    def geom(res):
        \"\"\"The four indices the windows are pinned by, read back out.\"\"\"
        ns = lambda km: int(round(km * 1000.0 / res))
        ieol = int(round(EOL * 1000.0 / res))
        cur_a = ieol - ns(MIRROR_D)
        width = max(4, ns(MIRROR_EC - MIRROR_SC))
        sub_a = max(cur_a - ns(5001.6 / 1000.0), ieol - ns(EOL - PREV_EC))
        return ieol, cur_a, cur_a + width, sub_a

    def geom_old(res):
        \"\"\"The arithmetic this file exists to retire: each absolute km
        truncated on its own, then the whole numbers subtracted.\"\"\"
        idx = lambda km: int(km * 1000.0 / res)
        ieol = idx(EOL)
        cur_a = idx(EOL - MIRROR_D)
        width = max(4, idx(MIRROR_EC) - idx(MIRROR_SC))
        sub_a = max(cur_a - int(round(5001.6 / res)), idx(PREV_EC))
        return ieol, cur_a, cur_a + width, sub_a

    def loss(res, trace=None):
        return MG(TRACE if trace is None else trace, res, 0.0, EOL, PREV_EC,
                  MIRROR_D, MIRROR_SC, MIRROR_EC)
"""


# ═══════════════════════════════════════════════════════════════════════════
#  Source lock — one absolute index, everything else a distance
# ═══════════════════════════════════════════════════════════════════════════

def _body():
    return SOR_SRC[SOR_SRC.index("def _endzone_mirror_grey("):
                   SOR_SRC.index("def _endzone_prev_marker_end_km(")]


def test_only_the_cable_end_is_an_absolute_index():
    """Every other cursor is a DISTANCE back from it, converted once.  Two
    truncated absolute kilometres subtracted from each other is a different
    number from one converted difference, and only the second is stable."""
    body = _body()
    assert "def nsamp(km):" in body, (
        "the conversion km->samples must take a DISTANCE, so the call sites "
        "read as distances"
    )
    assert "ieol = int(round((float(eol_km) + off) * 1000.0 / res_m))" in body
    assert "cur_a = ieol - nsamp(mirror_dist_km)" in body, (
        "CursorA is the mirror event's distance back from the cable end"
    )
    assert "nsamp(float(mirror_end_km) - float(mirror_start_km))" in body, (
        "the mirror width is ONE conversion of the marker span, not the "
        "difference of two absolute indices"
    )
    assert "ieol - nsamp(float(eol_km) + off\n" in body, (
        "the SubCursorA clamp is the previous marker's distance back from the "
        "cable end"
    )
    # no absolute kilometre may be converted anywhere else
    for banned in ("int(km * 1000.0 / res_m)", "int((km + off) * 1000.0 / res_m)"):
        assert banned not in body, f"absolute-km truncation is back: {banned}"


def test_the_cable_end_is_a_nearest_sample_lookup():
    """The stated pitch puts the cable end ON a sample (1152/1152 WSC↔SUI
    traces land within 0.05 of a whole one), so flooring it lands a sample
    early.  Pinned because it used to be int() and the comment above it
    explains why that was right on the old, 25 ppm long, pitch."""
    body = _body()
    assert "int(round((float(eol_km) + off)" in body
    assert "INDEXING (corrected 2026-09-21)" in SOR_SRC, (
        "the block comment must keep saying WHY truncation measured better "
        "before, or someone will re-derive the old pitch and revert this"
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Behaviour — the missing property
# ═══════════════════════════════════════════════════════════════════════════

def test_a_pitch_change_that_moves_no_sample_moves_nothing():
    """The headline.  Sweep the pitch across +/-30 ppm — 1.9 m at 64 km, well
    under one 2.55 m sample — and every reading that shares the cable end's
    sample must be BIT-identical.  Not 'within a millidecibel': identical."""
    _run("""
        by_end = {}
        for ppm in range(-30, 31):
            res = RES_M * (1.0 + ppm * 1e-6)
            v = loss(res)
            assert v is not None, ppm
            by_end.setdefault(geom(res)[0], []).append((ppm, v))
        assert len(by_end) >= 2, (
            "the sweep must actually cross a sample boundary, or this proves "
            "nothing: " + repr(sorted(by_end)))
        for ieol, rows in by_end.items():
            vals = {round(v, 12) for _, v in rows}
            assert len(vals) == 1, (ieol, sorted(rows)[:4], vals)
        print("OK")
    """)


def test_crossing_a_sample_slides_the_windows_rigidly():
    """When the cable end does land on the next sample the geometry follows it
    whole: the mirror width and both OLS window lengths are what they were.
    Only the placement moves — never the shape."""
    _run("""
        shapes = set()
        for ppm in range(-60, 61):
            res = RES_M * (1.0 + ppm * 1e-6)
            ieol, cur_a, cur_b, sub_a = geom(res)
            shapes.add((cur_b - cur_a,          # mirror width
                        ieol - cur_b,           # after-window length
                        cur_a - sub_a))         # before-window length
            assert cur_a - sub_a >= 30, (ppm, cur_a - sub_a)
        assert len(shapes) == 1, sorted(shapes)
        # ... and the slide tracks the pitch: 30 ppm is 0.75 of a sample at
        # 64 km, so no 30 ppm step may move the cable end more than one.
        ends = {p: geom(RES_M * (1.0 + p * 1e-6))[0] for p in range(-60, 61)}
        for p in range(-60, 31):
            assert abs(ends[p + 30] - ends[p]) <= 1, (p, ends[p], ends[p + 30])
        # The retired arithmetic does NOT hold the shape -- without this the
        # assertion above would pass on a fixture that never stressed it.
        old_shapes = {(cb - ca, ie - cb, ca - sa)
                      for ie, ca, cb, sa in
                      (geom_old(RES_M * (1.0 + p * 1e-6)) for p in range(-60, 61))}
        assert len(old_shapes) > 1, (
            "the sweep no longer stresses the old arithmetic, so it proves "
            "nothing: " + repr(sorted(old_shapes)))
        print("OK")
    """)


def test_an_index_on_a_whole_sample_resolves_to_that_sample():
    """float arithmetic decides which side of 'exactly 25120.0' you land on.
    Truncation turns that coin flip into a whole sample of window; a nearest-
    sample lookup does not notice it at all."""
    _run("""
        # a pitch that puts the cable end exactly on sample 25121
        res = EOL * 1000.0 / 25121.0
        assert geom(res)[0] == 25121
        base = loss(res)
        assert base is not None
        for wobble in (-4e-16, -1e-16, 1e-16, 4e-16):
            res_w = res * (1.0 + wobble)
            assert geom(res_w)[0] == 25121, wobble
            assert loss(res_w) == base, (wobble, loss(res_w), base)
        print("OK")
    """)


def test_the_reading_is_still_the_planted_step():
    """Stability is worthless if the number stopped being a measurement: the
    reconstruction must still read the loss that was planted in the trace."""
    _run("""
        v = loss(RES_M)
        assert v is not None
        assert abs(v - STEP) < 0.05, (v, STEP)
        print("OK")
    """)
