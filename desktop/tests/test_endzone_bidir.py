"""End-zone bidirectional measurement (the WSC↔SUI Splice 12 miss).

Splice 12 on WSC↔SUI sits at 63.9675 km — 80 m before the 64.05 km far-end
connector.  From the A side that leaves no room for EXFO's LSA geometry (the
after-window's own start, P + event length, already lands past the end), so
_grey_loss read None, no bidirectional average was ever formed, and 21 fibers
the reviewer had to add back by hand simply vanished.  The mirror position is
80 m PAST the B direction's launch, where the tight re-measure gate reads the
connector's recovery tail and confidently refuted 14 real events.

Four mechanisms are pinned here:

  1  measure_endzone_grey_from_sor — an end-anchored, shared-slope two-line
     LSA that fits whatever glass exists between the launch connector and the
     far-end connector, wired in as _grey_loss's fallback.
  2  LAUNCH_STEP_GUARD_KM — the tight re-measure read is UNMEASURABLE (None,
     fail open) that close to the launch, never a refutation; paired with
     _in_cable_end_zone so a fail-open can't ship a raw single-direction cell
     off a dead-zone-biased stored loss.
  3  FastReporter parity — a measurable-but-flat silent side means DROP (the
     bidir average decides), not "ship the loud side raw as (A)".
  4  _clears_threshold — flag on the value the report PRINTS, so a 0.1595
     bidir that renders ".160" flags at a 0.160 threshold.

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
RUNNER_SRC = (SPLICEREPORT_DIR / "run_splicereport.py").read_text(encoding="utf-8")


def _run(body: str, imports: str = "import splicereportmatchexfo as E\n",
         synth: bool = False):
    """Run `body` in a clean child interpreter; it must print OK last.

    SYNTH_TRACE and `body` are dedented SEPARATELY — they carry different
    indentation depths, and a joint dedent would leave the body indented
    inside synth() as dead code past its `return` (exits 0, prints nothing)."""
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n" + imports)
    src = header
    if synth:
        src += textwrap.dedent(SYNTH_TRACE)
    src += textwrap.dedent(body)
    p = subprocess.run([sys.executable, "-c", src],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    out = p.stdout.strip().splitlines()
    assert out and out[-1] == "OK", p.stdout or p.stderr


# A synthetic SOR-shaped trace: cumulative loss rising with distance (the SOR
# convention), a real step at the splice, and only `tail_m` of glass left
# before the far-end connector — the WSC↔SUI geometry in miniature.
SYNTH_TRACE = """
    import numpy as np

    RES_M = 2.55                       # m/sample (275 ns acquisition)
    SP = RES_M * 2.0 * 1.47 / 299792458.0

    def synth(step_db, splice_km, eol_km, launch_dead_km=0.046, slope=0.19,
              noise=0.0, seed=1):
        n = int(eol_km * 1000.0 / RES_M) + 400
        x_km = np.arange(n) * RES_M / 1000.0
        y = 36.0 + slope * x_km
        y[x_km >= splice_km] += step_db
        # launch connector: a reflection dip that decays to nothing by the
        # dead-zone edge (= where EXFO's launch marker-end sits)
        dip = x_km < launch_dead_km
        y[dip] -= 2.5 * (1.0 - x_km[dip] / launch_dead_km)
        # far-end connector: a big reflection past the end event
        y[x_km >= eol_km] -= 20.0
        if noise:
            y = y + np.random.RandomState(seed).normal(0, noise, n)
        evs = [{'dist_km': 0.0, 'splice_loss': 0.0, 'type': '1F', 'is_end': False,
                'is_reflective': True, 'time_of_travel': 0,
                'tot_start_curr': 0,
                'tot_end_curr': int(launch_dead_km * 1000.0 * 1.47 / 0.02998),
                'tot_end_prev': 0, 'tot_start_next': 0},
               {'dist_km': eol_km, 'splice_loss': 0.0, 'type': '1E',
                'is_end': True, 'is_reflective': True,
                'time_of_travel': int(eol_km * 1000.0 * 1.47 / 0.02998)}]
        return {'_source': 'sor', 'trace': y, 'events': evs,
                'exfo_sampling_period': SP, '_trace_offset_km': 0.0,
                'num_points': n, 'wavelength': 1550.0}
"""


# ═══════════════════════════════════════════════════════════════════════════
#  FIX 1 — end-zone / near-launch reconstruction
# ═══════════════════════════════════════════════════════════════════════════

def test_endzone_reader_function_exists_and_is_wired():
    """sor_reader must expose the end-anchored reconstruction and _grey_loss
    must fall back to it when the EXFO geometry can't fit."""
    assert "def measure_endzone_grey_from_sor(" in SOR_SRC, (
        "sor_reader324802a.py must define measure_endzone_grey_from_sor — the "
        "end-anchored silent-side reconstruction"
    )
    assert "measure_endzone_grey_from_sor" in ENGINE_SRC, (
        "the engine must import the end-zone reconstruction"
    )
    # It is a FALLBACK: the EXFO-exact windower still runs first and still wins.
    idx_silent = ENGINE_SRC.index("measure_silent_grey_from_sor(fiber_data, splice_km")
    idx_end = ENGINE_SRC.index("return measure_endzone_grey_from_sor(fiber_data, splice_km")
    assert idx_silent < idx_end, (
        "_grey_loss must try the EXFO-exact silent windower FIRST and only fall "
        "back to the end-zone reconstruction when it returns None"
    )
    for const in ("ENDZONE_REACH_KM", "ENDZONE_EOF_GUARD_KM",
                  "ENDZONE_DEAD_ZONE_KM", "ENDZONE_MIN_SLOPE_SAMPLES"):
        assert const in SOR_SRC, f"missing end-zone constant {const}"


def test_endzone_scope_is_limited_to_cable_ends():
    """The reconstruction must REFUSE to fire mid-span: everywhere else the
    calibrated EXFO geometry owns the answer and its None means 'don't trust
    this position'.  A widened scope would silently re-price every span."""
    assert "ENDZONE_REACH_KM = 0.50" in SOR_SRC.replace("  ", " ") or \
        "ENDZONE_REACH_KM          = 0.50" in SOR_SRC, (
        "ENDZONE_REACH_KM must stay at 0.50 km — the cable-end scope guard"
    )
    _run("""
        from sor_reader324802a import measure_endzone_grey_from_sor as MZ
        # 0.30 dB step 80 m before the far-end connector (the WSC↔SUI geometry)
        r = synth(0.30, 63.960, 64.044)
        got = MZ(r, 63.960)
        assert got is not None, "end-zone position must be measurable"
        assert abs(got - 0.30) < 0.02, got
        # 80 m PAST the launch connector (the mirrored B-direction geometry)
        r2 = synth(0.30, 0.092, 64.044)
        got2 = MZ(r2, 0.092)
        assert got2 is not None, "near-launch position must be measurable"
        assert abs(got2 - 0.30) < 0.02, got2
        # MID-SPAN: out of scope, must return None (EXFO geometry's job)
        r3 = synth(0.30, 32.0, 64.044)
        assert MZ(r3, 32.0) is None, "mid-span must stay out of scope"
        print("OK")
    """, imports="", synth=True)


def test_endzone_shares_the_slope_from_the_long_window():
    """The short side gets a LEVEL only.  A free two-line fit over the ~10-30
    samples that fit beside a cable end reads pure noise (measured ±0.35 dB on
    WSC↔SUI); the shared slope is what makes the reconstruction usable."""
    assert "slope = float(np.polyfit(long_x, long_y, 1)[0])" in SOR_SRC, (
        "the slope must come from the LONG window and be shared with the short "
        "one — an independent short-window slope fit is noise"
    )
    _run("""
        from sor_reader324802a import measure_endzone_grey_from_sor as MZ
        # With realistic trace noise the estimate must stay near the truth on
        # BOTH cable-end geometries (a free two-line fit does not).
        for seed in range(6):
            r = synth(0.30, 63.960, 64.044, noise=0.045, seed=seed)
            got = MZ(r, 63.960)
            assert got is not None and abs(got - 0.30) < 0.06, (seed, got)
        print("OK")
    """, imports="", synth=True)


def test_endzone_refuses_when_there_is_no_glass_left():
    """Not every end-zone position is measurable — a splice a few samples from
    the connector must read None, never a fabricated number.  (WSC↔SUI F1142:
    7.8 m of glass after the mirrored position.)"""
    _run("""
        from sor_reader324802a import measure_endzone_grey_from_sor as MZ
        r = synth(0.30, 64.036, 64.044)     # 8 m of glass left
        assert MZ(r, 64.036) is None, "must refuse when no window fits"
        print("OK")
    """, imports="", synth=True)


def test_endzone_refuses_a_frame_the_trace_disagrees_with():
    """The km→sample mapping's launch offset comes from the EVENT LIST and can
    disagree with the trace.  Span 3's "Mecca V2" export is the proof: byte-
    identical trace to V1, but the event list was start/stop-picked so the
    offset reads 0 while the data block still carries a 1.04 km pre-launch
    lead.  Indexed with offset 0 the near-launch window measures 1.04 km INTO
    the fiber (F284: +0.131 dB of frame error vs a correctly framed -0.003)
    and manufactures flags at the end column.  The far-end connector's
    reflection anchors the check; no reflection at idx(EOL) → refuse."""
    assert "def _endzone_frame_ok(" in SOR_SRC
    assert "_endzone_frame_ok(tr, idx(eol))" in SOR_SRC, (
        "the reconstruction must validate its frame before measuring"
    )
    _run("""
        from sor_reader324802a import measure_endzone_grey_from_sor as MZ
        import numpy as np
        r = synth(0.30, 0.092, 64.044)
        assert MZ(r, 0.092) is not None, "correctly framed trace must measure"
        # Same trace, but the events claim a frame 1.04 km off (the V2 shape):
        # every window slides 408 samples and the far-end anchor is gone.
        lead = 1.0376
        shift = int(lead * 1000.0 / RES_M)
        r2 = dict(r)
        r2['trace'] = np.concatenate([np.full(shift, 36.0), r['trace']])
        assert MZ(r2, 0.092) is None, (
            "a trace whose far-end connector is NOT at idx(EOL) must be refused")
        print("OK")
    """, imports="", synth=True)


# ═══════════════════════════════════════════════════════════════════════════
#  FIX 2 — launch-proximity guard + its pairing clause
# ═══════════════════════════════════════════════════════════════════════════

def test_launch_step_guard_constant_and_placement():
    assert "LAUNCH_STEP_GUARD_KM  = 0.150" in ENGINE_SRC, (
        "LAUNCH_STEP_GUARD_KM must exist and stay at 0.150 km"
    )
    assert "if dist_km < LAUNCH_STEP_GUARD_KM:\n        return None" in ENGINE_SRC, (
        "_local_step_from_event must return None (unmeasurable) inside the "
        "launch guard instead of measuring the connector's recovery tail"
    )
    assert "if raw_km < LAUNCH_STEP_GUARD_KM:" in ENGINE_SRC, (
        "the raw-frame position must be guarded too (untrimmed files)"
    )


def test_launch_guard_fails_open_never_refutes():
    """A real 0.30 dB step 92 m past the launch must not be REFUTED.  Before
    the guard the tight read returned ~0.00 there and killed 14 real WSC↔SUI
    events; now it is unmeasurable, and unmeasurable keeps the flag."""
    _run("""
        r = synth(0.30, 0.092, 64.044)
        r['_raw_events'] = r['events']
        evt = {'dist_km': 0.092, 'splice_loss': 0.30, 'type': '0F',
               'is_end': False, 'time_of_travel': 4500}
        assert E._local_step_from_event(r, evt) is None, (
            "inside the launch guard the tight read must be unmeasurable")
        assert E._local_step_confirms(r, evt) is True, (
            "unmeasurable must FAIL OPEN — never refute a real near-launch event")
        # Well past the guard the gate still works normally.
        r2 = synth(0.30, 32.0, 64.044)
        r2['_raw_events'] = r2['events']
        evt2 = {'dist_km': 32.0, 'splice_loss': 0.30, 'type': '0F',
                'is_end': False, 'time_of_travel': 1}
        assert E._local_step_from_event(r2, evt2) is not None, (
            "mid-span events must still be measured")
        print("OK")
    """, synth=True)


def test_cable_end_zone_blocks_raw_single_direction_flags():
    """The pairing clause: where the re-measure gate is blind, a raw stored
    loss must not flag on its own.  WSC↔SUI's B direction stores a median
    0.197 dB (up to 0.369) 80 m past its launch across 1055 fibers the
    reviewer left unflagged — 5 such cells shipped as '(B)' until this gate."""
    assert "def _in_cable_end_zone(" in ENGINE_SRC
    assert ENGINE_SRC.count("not _in_cable_end_zone(") == 2, (
        "both raw single-direction fallbacks (A-only and B-only) must stand "
        "down inside the cable-end zone"
    )
    _run("""
        r = synth(0.30, 0.092, 64.044)
        assert E._in_cable_end_zone(r, 0.092) is True, "near launch"
        assert E._in_cable_end_zone(r, 63.990) is True, "near far end"
        assert E._in_cable_end_zone(r, 32.0) is False, "mid-span is fine"
        print("OK")
    """, synth=True)


# ═══════════════════════════════════════════════════════════════════════════
#  FIX 3 — FastReporter parity on the A-only recovery
# ═══════════════════════════════════════════════════════════════════════════

def test_flat_silent_side_drops_instead_of_shipping_raw_a():
    """When the silent side IS measurable and flat, the bidirectional average
    decides — the loud side is not shipped raw as '(A)'.  The reviewer deleted
    exactly those cells: WSC↔SUI 456 (A .294, b_grey .011 → .152) and 826
    (A .259, b_grey .021 → .140).  Supersedes the F111 flat-other-side
    recovery for measurable-flat cases."""
    assert "abs(b_grey) < BEND_THRESHOLD" not in ENGINE_SRC, (
        "the flat-b_grey raw-A recovery must be gone — a measured flat B side "
        "is EVIDENCE of a small bidir loss, not a licence to report A alone"
    )
    assert "'_b_is_flat_grey': True" not in ENGINE_SRC
    assert "FastReporter drops it, so we drop it" in ENGINE_SRC, (
        "keep the comment trail explaining what this supersedes"
    )
    _run("""
        def _fiber(evs, eol=70.0):
            out = [{'dist_km': d, 'splice_loss': l, 'type': t,
                    'reflection': -60.0, 'is_end': False} for (d, l, t) in evs]
            out.append({'dist_km': eol, 'splice_loss': 0.0, 'type': '1E',
                        'reflection': -40.0, 'is_end': True})
            return {'_source': 'sor', 'wavelength': 1550.0,
                    'events': sorted(out, key=lambda e: e['dist_km'])}

        SP = 30.0
        splices = [{'position_km': SP, 'position_km_refined': SP,
                    'column_kind': 'splice'}]

        # Silent side MEASURABLE and flat -> average (0.155) is below the
        # 0.160 threshold -> DROP (FastReporter parity).
        E._grey_loss = lambda fd, km, mirror=None, twin=None: 0.01
        fa = {1: _fiber([(SP, 0.30, '0F')])}
        fb = {1: _fiber([])}
        res = E.analyze_all(fa, fb, splices, E.REBURN_THRESHOLD)
        assert (1, 0) not in res, (
            "a measurable, flat silent side must DROP the cell, not ship raw A")

        # Silent side UNMEASURABLE -> the raw-A single-direction cell survives
        # (stricter SINGLE_DIR_THRESHOLD still applies).
        E._grey_loss = lambda fd, km, mirror=None, twin=None: None
        E._local_step_confirms = lambda fd, e: True
        res2 = E.analyze_all(fa, fb, splices, E.REBURN_THRESHOLD)
        cell = res2.get((1, 0))
        assert cell is not None, "unmeasurable silent side must still ship (A)"
        assert cell['is_a_only'] is True, cell
        print("OK")
    """)


# ═══════════════════════════════════════════════════════════════════════════
#  FIX 4 — flag on the value the report PRINTS
# ═══════════════════════════════════════════════════════════════════════════

def test_clears_threshold_uses_the_displayed_value():
    """0.1595 renders '.160' via _format_loss; against a 0.160 threshold the
    boss flags it, so the engine must too.  WSC↔SUI 637@Splice 7 (A .187 /
    B .132) and 1067@Splice 2 (A .172 / B .147) are both exactly 0.1595."""
    assert "def _clears_threshold(" in ENGINE_SRC
    assert "abs(bidir_loss) >= threshold" not in ENGINE_SRC, (
        "the A+B flag gate must go through _clears_threshold"
    )
    assert ENGINE_SRC.count("_clears_threshold(") >= 5, (
        "all four bidirectional flag gates must use the display-rounded compare"
    )
    _run("""
        thr = 0.160
        assert E._format_loss(0.1595) == '.160'
        assert E._clears_threshold(0.1595, thr) is True, "prints .160 -> flags"
        assert E._clears_threshold(round((0.187 + 0.132) / 2.0, 4), thr) is True
        assert E._clears_threshold(round((0.172 + 0.147) / 2.0, 4), thr) is True
        assert E._clears_threshold(0.1594, thr) is False, "prints .159 -> no flag"
        assert E._clears_threshold(-0.1595, thr) is True, "magnitude, not sign"
        assert E._clears_threshold(None, thr) is False
        # Panel semantics: a raised threshold still moves the whole cutoff.
        assert E._clears_threshold(0.1595, 0.250) is False
        assert E._clears_threshold(0.2497, 0.250) is True
        print("OK")
    """)


def test_manifest_category_matches_the_flag_gate():
    """A 0.1595 bidir flags AND prints '.160', so the hub grid must file it as
    a reburn — not in the generic 'event' bucket."""
    assert "round(float(loss), 3) >= 0.160" in RUNNER_SRC, (
        "run_splicereport._category must round to the report's own 3-decimal "
        "display before comparing, like the engine's flag gate"
    )
