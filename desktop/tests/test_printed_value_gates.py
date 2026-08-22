"""Threshold gates must adjudicate on the value the report PRINTS.

Every loss reaches the tech as a 3-decimal number.  A tech reading `.250`
cannot tell whether the engine saw 0.2495 or 0.2504, so a gate that decides
on the unrounded float produces verdicts the tech cannot reproduce — and two
cells printing the identical number get adjudicated differently.

While splice loss came from the Bellcore KeyEvents block that could not
happen: the stored value was an int16 in millidecibels, so the stored number
WAS the printed number and comparing the raw float was accidentally the same
thing.  PR #96 started reading EXFO's proprietary block at full float
precision (FastReporter's own source, 74.8% -> 100% agreement with EXFO's
exports) and the coincidence ended.

The bidirectional path was already correct via `_clears_threshold`; the
unidirectional gates were fixed in #96.  These are the three that were
deferred:

  SINGLE_DIR_THRESHOLD    >= gate, 4 sites  (A-only, B-only, B-fill, past-break)
  DIRTY_CONN_LOSS_GATE_DB >= gate, 1 site   (reflective-with-loss category)
  GHOST_REFL_MAX_LOSS_DB  <= gate, 2 sites  (A-side and B-side ghost filters)

Every check below EXECUTES the engine on a knife-edge value and asserts what
came out.  None of them reads the engine source: a test that greps for a
comparison keeps passing when the comparison is inverted.

Knife-edge values are chosen so the raw float and the printed value fall on
OPPOSITE sides of the gate, and each is asserted to be a knife edge before it
is used — otherwise the test could pass on a value that never exercised the
distinction.

HARD RULE — namespace isolation: the Splice Report engine ships its own
sor_reader324802a.py that collides with the viewer's copy, so this process
must never import splicereportmatchexfo directly.  Every check runs in a
clean child interpreter (same pattern as test_splicereport_validated_fixes).
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

from conftest import SPLICEREPORT_DIR


def _run_engine_snippet(body: str):
    """Run `body` in a clean child interpreter with the splice engine on the
    path.  `body` must print 'OK' as its last line."""
    header = (
        "import sys\n"
        f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
        "import splicereportmatchexfo as E\n"
    )
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, (
        f"subprocess exited {p.returncode}\n"
        f"stdout:\n{p.stdout}\nstderr:\n{p.stderr}"
    )
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


# Synthetic parsed-SOR fibers.  `is_reflective` is set from the Bellcore type
# so the reflective-only passes see what a real file would give them.
_FIBER_HELPER = """
        def _fiber(spec, eol_km=70.0, wavelength=1550.0):
            evs = [{'dist_km': dk, 'splice_loss': sl, 'type': ty,
                    'reflection': rf, 'is_end': False,
                    'is_reflective': ty.startswith(('1F', '2F'))}
                   for (dk, sl, ty, rf) in spec]
            evs.append({'dist_km': eol_km, 'splice_loss': 0.0, 'type': '1E',
                        'reflection': -40.0, 'is_end': True,
                        'is_reflective': True})
            return {'_source': 'sor', 'wavelength': wavelength,
                    'events': sorted(evs, key=lambda e: e['dist_km'])}

        # The real WSC<->SUI AUG fiber 443 value: EXFO's proprietary block
        # carries 0.24952510058942323 where KeyEvents stored a flat 0.250.
        # It PRINTS .250 and is BELOW 0.250 as a float — the exact cell that
        # moved in production when #96 landed (SINGLE_DIR 57 -> 56 True).
        ON_GATE_250 = 0.24952510058942323
        UNDER_250   = 0.2494        # prints .249
        ON_GATE_100 = 0.0995        # prints .100, raw < 0.10
        UNDER_100   = 0.0994        # prints .099
        ON_GATE_030 = 0.0304        # prints .030, raw > 0.030
        OVER_030    = 0.0306        # prints .031
"""

# ═══════════════════════════════════════════════════════════════════
#  Gate 2 — DIRTY_CONN_LOSS_GATE_DB  (>=, one site)
# ═══════════════════════════════════════════════════════════════════

def test_dirty_connector_gate_adjudicates_on_the_printed_loss():
    """_is_dirty_connector decides whether a reflective event ALSO carries a
    real loss step.  The cell prints that loss at 3 dp beside the category, so
    two events printing .100 must land in the same category.

    Category-only: this never changes is_flagged and never decorates the label
    (no diagnosis in the report), but it does reach the tech through the hub
    grid's manifest.  `>=` gate, so it can only gain."""
    _run_engine_snippet(_FIBER_HELPER + """
        THR = E.DIRTY_CONN_LOSS_GATE_DB
        assert THR == 0.10, THR
        assert ON_GATE_100 < THR and E._format_loss(ON_GATE_100) == '.100'
        assert UNDER_100   < THR and E._format_loss(UNDER_100)   == '.099'
        KM, REFL = 10.0, -40.0            # past the launch, stronger than -55

        assert E._is_dirty_connector(KM, REFL, ON_GATE_100) is True, \\
            "a loss that PRINTS .100 against a .100 gate must count as a loss step"
        assert E._is_dirty_connector(KM, REFL, UNDER_100) is False, \\
            "a loss that prints .099 must not"
        # Everything else about the predicate is unchanged.
        assert E._is_dirty_connector(KM, REFL, None) is False
        assert E._is_dirty_connector(KM, REFL, ON_GATE_100, is_end=True) is False
        assert E._is_dirty_connector(0.01, REFL, ON_GATE_100) is False, \\
            "launch-connector exclusion"
        assert E._is_dirty_connector(KM, -70.0, ON_GATE_100) is False, \\
            "reflectance gate"
        assert E._is_dirty_connector(KM, REFL, -ON_GATE_100) is True, \\
            "|loss| gate: a printed -.100 step is still a loss step"

        # End to end: the category actually reaches a cell.  The downstream
        # event at 45 km is what stops the reflective from reading as a
        # BREAK, which would win the event_source before this rule is asked.
        E._local_step_confirms = lambda fd, e: True
        splices = [{'position_km': 30.0, 'position_km_refined': 30.0,
                    'column_kind': 'splice'}]

        def _src(v):
            fa = {1: _fiber([(30.0, v, '1F9999LS', REFL),
                             (45.0, 0.05, '0F', -60.0)])}
            fb = {1: _fiber([(70.0 - 30.0, v, '1F9999LS', REFL),
                             (70.0 - 45.0, 0.05, '0F', -60.0)])}
            cell = E.analyze_all(fa, fb, splices, E.REBURN_THRESHOLD).get((1, 0))
            assert cell is not None and cell['is_break'] is False, cell
            return cell['event_source']

        assert _src(ON_GATE_100) == 'dirty_connector', \
            "a reflective printing .100 of loss carries a real loss step"
        assert _src(UNDER_100) != 'dirty_connector', \
            "a reflective printing .099 does not"
        print("OK")
    """)

# ═══════════════════════════════════════════════════════════════════
#  One implementation of the rounding
# ═══════════════════════════════════════════════════════════════════

def test_printed_value_rounding_has_one_implementation():
    """_printed_loss is the single copy of 'the number the report prints'.
    _clears_threshold is the single copy of the abs()-`>=` gate built on it.
    Asserted by behaviour: every gate above must agree with _printed_loss on
    the knife-edge values, and _clears_threshold must agree with the two
    `>=` gates."""
    _run_engine_snippet(_FIBER_HELPER + """
        assert E._printed_loss(ON_GATE_250) == 0.250
        assert E._printed_loss(UNDER_250)   == 0.249
        assert E._printed_loss(ON_GATE_100) == 0.100
        assert E._printed_loss(ON_GATE_030) == 0.030
        assert E._printed_loss(-ON_GATE_250) == -0.250, "sign preserved"
        assert E._printed_loss(None) is None

        # _clears_threshold is that rounding + the abs() >= comparison.
        assert E._clears_threshold(ON_GATE_250, E.SINGLE_DIR_THRESHOLD) is True
        assert E._clears_threshold(UNDER_250,   E.SINGLE_DIR_THRESHOLD) is False
        assert E._clears_threshold(ON_GATE_100, E.DIRTY_CONN_LOSS_GATE_DB) is True
        assert E._clears_threshold(UNDER_100,   E.DIRTY_CONN_LOSS_GATE_DB) is False
        assert E._clears_threshold(-ON_GATE_100, E.DIRTY_CONN_LOSS_GATE_DB) is True
        assert E._clears_threshold(None, 0.160) is False
        # The dirty-connector gate IS _clears_threshold, so they cannot drift.
        for v in (UNDER_100, ON_GATE_100, 0.2, -0.2, None):
            assert (E._is_dirty_connector(10.0, -40.0, v)
                    == E._clears_threshold(v, E.DIRTY_CONN_LOSS_GATE_DB)), v
        print("OK")
    """)


#  Gate 3 — GHOST_REFL_MAX_LOSS_DB  (<=, two sites)
# ═══════════════════════════════════════════════════════════════════

def test_ghost_reflection_gate_adjudicates_on_the_printed_loss():
    """scan_bidir_ghost_reflections looks for a reflective event carrying
    essentially NO loss that mirrors in the opposite direction.  Both the
    A-side and the B-side filter compare that loss against
    GHOST_REFL_MAX_LOSS_DB, and the report prints the same number at 3 dp.

    Direction: this is a `<=` gate, so it moves the OPPOSITE way to the two
    above — a raw 0.0304 that prints .030 is now ADMITTED as a ghost
    candidate.  Cells can be added.  Nothing is ever excluded, because a raw
    value at or below 0.030 always prints at or below .030.

    Each side is exercised on its own, so inverting either filter alone is
    caught."""
    _run_engine_snippet(_FIBER_HELPER + """
        THR = E.GHOST_REFL_MAX_LOSS_DB
        assert THR == 0.030, THR
        assert ON_GATE_030 > THR and E._format_loss(ON_GATE_030) == '.030'
        assert OVER_030    > THR and E._format_loss(OVER_030)    == '.031'
        SPAN, GK = 70.0, 25.0             # ghost far from the only closure
        splices = [{'position_km': 10.0, 'position_km_refined': 10.0,
                    'column_kind': 'splice'}]

        def ghost(a_loss, b_loss):
            fa = {1: _fiber([(GK, a_loss, '1F9999LS', -55.0)])}
            fb = {1: _fiber([(SPAN - GK, b_loss, '1F9999LS', -55.0)])}
            return E.scan_bidir_ghost_reflections(fa, fb, splices, {}, SPAN)

        # Baseline: a true zero-loss ghost is found either way.
        assert (1, 0) in ghost(0.0, 0.0), "the plain ghost case must still fire"

        # A-side filter alone.
        got = ghost(ON_GATE_030, 0.0)
        assert (1, 0) in got, (
            "A side prints .030 against a .030 ceiling — must be admitted")
        assert got[(1, 0)]['event_source'] == 'ref_bidir_ghost'
        assert ghost(OVER_030, 0.0) == {}, "A side prints .031 — has loss"

        # B-side filter alone.
        assert (1, 0) in ghost(0.0, ON_GATE_030), (
            "B side prints .030 against a .030 ceiling — must be admitted")
        assert ghost(0.0, OVER_030) == {}, "B side prints .031 — has loss"

        # Both sides at once (the census case).
        assert (1, 0) in ghost(ON_GATE_030, ON_GATE_030)
        print("OK")
    """)
