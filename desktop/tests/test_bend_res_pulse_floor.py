"""Test-1's bend residual is floored at the run's pulse smear.

KANLAN (Lancaster<->Kansas City, 864 fibers, 116 km) was acquired with a
2500 ns pulse.  The far-end closure at Splice 19 is invisible to the A
direction on many fibers and is only seen from B, ~2.3 km from B's launch.
scan_b_events mirrors that B event into the A frame, and the mirror carries
the pulse's own placement error -- so the closure's OWN event arrived
100-580 m "off column" and Test 1's 150 m residual gate called four of them
BENDs (plus one more at Splice 4).

150 m is a 500-1000 ns number.  Measured on matched A<->B event pairs -- the
same physical event read once per direction, which is the cleanest available
estimate of placement error:

    span         pulse    smear    |dpos| p95   p99    max
    WSC<->SUI     500 ns    51 m       43 m    61 m   102 m
    KANLAN       2500 ns   255 m      168 m   230 m   592 m

The smear tracks the measured p99 on both spans, so it is the right floor:
an OTDR cannot resolve position finer than the pulse it fired, and a
residual inside that window says nothing about whether there are one or two
events there.

Same widen-never-narrow doctrine as _fold_km(): a panel override that RAISES
BEND_RES_BEND_M still wins.

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"


def _run(body):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_floor_is_inactive_on_a_short_pulse_span():
    """500 ns -> 51 m smear, below the 150 m constant: nothing changes.
    This is what keeps WSC<->SUI bit-for-bit."""
    _run("""
        E._RUN_PULSE_SMEAR_KM = 0.0511          # 500 ns
        assert abs(E._bend_res_bend_m() - 150.0) < 1e-9, E._bend_res_bend_m()

        E._RUN_PULSE_SMEAR_KM = 0.0             # unknown pulse -> legacy
        assert abs(E._bend_res_bend_m() - 150.0) < 1e-9, E._bend_res_bend_m()
        print("OK")
    """)


def test_floor_lifts_the_gate_on_a_2500ns_span():
    """2500 ns -> 255 m smear, which becomes the gate."""
    _run("""
        E._RUN_PULSE_SMEAR_KM = 0.2553          # 2500 ns
        got = E._bend_res_bend_m()
        assert abs(got - 255.3) < 0.5, got
        print("OK")
    """)


def test_panel_override_still_widens():
    """A raised BEND_RES_BEND_M outranks the floor -- widen, never narrow."""
    _run("""
        E._RUN_PULSE_SMEAR_KM = 0.2553
        E.BEND_RES_BEND_M = 400
        assert abs(E._bend_res_bend_m() - 400.0) < 1e-9, E._bend_res_bend_m()
        print("OK")
    """)


def test_mirror_sized_residual_is_not_a_bend_at_2500ns():
    """A candidate 190 m from the fiber's own predicted splice km is a BEND
    at 500 ns and NOT a bend at 2500 ns -- 190 m is inside a 2500 ns pulse's
    placement error, so it is one event read twice, not two events.

    The fiber's events sit exactly on the closures (an ideal 1:1 length
    model), so the length model predicts the splice AT the closure and the
    residual is the candidate's own offset.
    """
    _run("""
        closures = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
        fiber = {'events': [{'dist_km': c, 'splice_loss': 0.05,
                             'is_end': False, 'is_reflective': False}
                            for c in closures[:-1]]}
        fiber['events'].append({'dist_km': 70.0, 'splice_loss': 0.0,
                                'is_end': True, 'is_reflective': False})
        cand = 60.0 - 0.190                      # 190 m short of Splice 6

        # The model must fit and see the offset we think it sees.
        res, pred, nfit = E._perfiber_residual_m(fiber, closures, cand)
        assert nfit >= 3, nfit
        assert abs(res + 190.0) < 1.0, res

        def verdict():
            return E._is_bend_event(cand, 60.0, 0.130,
                                    fiber_events=fiber['events'],
                                    a_loss=0.13, b_loss=0.13,
                                    closure_kms=closures, fiber_data=fiber)

        # 500 ns: 190 m clears the 150 m gate, so Test 1 says BEND and hands
        # off to Test 2 (no raw trace here -> _narrow_lsa_loss returns None
        # -> conservative drop).  What matters is that the 2500 ns run must
        # not even reach Test 2.
        E._RUN_PULSE_SMEAR_KM = 0.0511
        short_pulse_residual_clears = abs(res) >= E._bend_res_bend_m()
        assert short_pulse_residual_clears

        # 2500 ns: the gate is 255 m, 190 m is inside it -> ambiguous ->
        # never a bend, whatever Test 2 would have said.
        E._RUN_PULSE_SMEAR_KM = 0.2553
        assert abs(res) < E._bend_res_bend_m()
        assert verdict() is False
        print("OK")
    """)
