"""The wide-LSA grey measurement must not read losses out of the noise floor.

`measure_grey_loss_from_sor` masked its samples with `0.5 < v < 63.5`.  That is
a SATURATION cap, not a liveness test: a fiber's noise floor sits around
62-63 dB, just under the cap, so every sample past a break survives the mask
and `np.polyfit` fits it as happily as it fits backscatter.  The caller gets a
confident-looking number measured out of noise.

Swept across the dead 55 km of SUI↔EMR F908 (a real break at 21.36 km, +22.7 dB
straight into the floor): 212 positions, ALL returned a number, 66% cleared the
0.160 report threshold, range -0.574 to +0.730 dB.  Those are the readings that
put phantom losses on the dead side of a broken fiber.

The gate is trace-based on purpose.  Gating on the fiber's own stored
end-of-fiber would be easier and would be wrong: on this very span the firmware
writes `0E` end-of-fiber on high-loss points that still carry tens of km of
live glass (F369 reads `0E` at 27.155 km and has 49 km of clean backscatter and
the far-end Fresnel past it).  Trusting that marker would drop real
measurements on precisely the damaged fibers the report exists to find.

Backscatter is a straight line with shot noise on it; the noise floor is not a
line at all.  Residual scatter about the window's own fit separates them by an
order of magnitude — measured over 2 642 live windows across four 1152-fiber
sets (SUI↔EMR both ways, Miller↔Elmdale both ways): p50 0.017, p99 0.30, max
1.19 dB.  Over 328 windows past confirmed breaks: min 1.51, p50 1.98.
LIVENESS_MAX_RESID_DB sits in that gap, biased toward the live side — a false
"dead" verdict silently drops a real cell, a false "live" one only leaves the
old behaviour in place.

Splice Report's sor_reader is its own copy (3-engine isolation), so this runs
in a clean subprocess with only that folder on sys.path.
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"


def _run(body):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import numpy as np\n"
              "import sor_reader324802a as S\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    out = p.stdout.strip().splitlines()
    assert out and out[-1] == "OK", p.stdout


def test_threshold_sits_between_measured_live_and_dead_windows():
    """Guard the constant itself against a well-meaning tweak."""
    _run("""
        # Live windows topped out at 1.19 dB, dead windows bottomed out at
        # 1.51 dB, across 2642 + 328 measured windows.  Anything outside that
        # gap either drops real cells or lets the noise back through.
        assert 1.19 < S.LIVENESS_MAX_RESID_DB < 1.51, S.LIVENESS_MAX_RESID_DB
        print('OK')
    """)


def test_residual_helper_separates_a_line_from_noise():
    """Backscatter fits; the noise floor does not."""
    _run("""
        rng = np.random.default_rng(7)
        x = np.arange(2000).astype(float)

        # Backscatter: a sloped line (0.19 dB/km) plus shot noise.
        y = 30.0 + 0.00048 * x + rng.normal(0, 0.02, x.size)
        c = np.polyfit(x, y, 1)
        live = S._win_residual(x, y, c)
        assert live < S.LIVENESS_MAX_RESID_DB, live

        # Noise floor: flat, ~62 dB, scattered by ~2 dB.
        y = 62.0 + rng.normal(0, 2.0, x.size)
        c = np.polyfit(x, y, 1)
        dead = S._win_residual(x, y, c)
        assert dead > S.LIVENESS_MAX_RESID_DB, dead

        assert dead > live * 10, (live, dead)

        # A degenerate window is never reported as dead — the sample-count
        # guards upstream own that case, and returning a large residual here
        # would turn "too few samples" into a silent liveness rejection.
        assert S._win_residual(np.array([1.0]), np.array([5.0]), [0.0, 5.0]) == 0.0
        print('OK')
    """)


def test_synthetic_break_is_measured_before_and_refused_after():
    """End to end through measure_grey_loss_from_sor on a built trace.

    Live glass for 20 km, then a hard break into a 62 dB floor. Positions in
    the glass still measure; positions past the break return None instead of a
    number fitted to noise.
    """
    _run("""
        rng = np.random.default_rng(11)
        # Derive the sample pitch exactly the way the function does, or the
        # km->index mapping drifts and the "noise" probes land back in glass.
        SP_S = 5e-08
        res_m = 299_792_458.0 * SP_S / 2.0 / 1.46820
        n = 33000
        x = np.arange(n).astype(float)
        brk = int(20_000.0 / res_m)          # break at 20 km

        trace = 30.0 + 0.00048 * x + rng.normal(0, 0.02, n)
        # Noise floor.  N(60.5, 2.2) is chosen to land where the REAL dead
        # windows land once the 63.5 saturation mask has taken its bite:
        # ~91% of samples retained, post-mask residual ~1.90 dB, against the
        # 1.51-2.69 dB measured past confirmed breaks.  Drawing it too close
        # to the cap (e.g. N(62, 2)) is not more realistic — it is the
        # clipping artifact, and it flatters the gate by truncating the very
        # scatter the gate keys on.
        trace[brk:] = 60.5 + rng.normal(0, 2.2, n - brk)
        trace = trace.astype(np.float64)

        sor = {
            'trace': trace,
            'exfo_sampling_period': SP_S,
            'events': [{'dist_km': 0.0, 'splice_loss': 0.0, 'reflection': -50.0,
                        'is_end': False, 'is_reflective': True,
                        'type': '1F9999LS', 'time_of_travel': 0}],
        }

        # In the glass, well clear of both the launch and the break.
        for km in (8.0, 12.0, 14.0):
            v = S.measure_grey_loss_from_sor(sor, km)
            assert v is not None, ('live position refused', km)
            assert abs(v) < 0.10, (km, v)

        # Past the break there is nothing to measure.  Stay clear of the step
        # itself so this asserts on the noise floor, not on the edge.
        refused = 0
        for km in (26.0, 32.0, 40.0, 48.0, 56.0, 64.0):
            v = S.measure_grey_loss_from_sor(sor, km)
            assert v is None, ('noise floor returned a number', km, v)
            refused += 1
        assert refused == 6, refused
        print('OK')
    """)


def test_gate_moves_no_real_number_on_healthy_fixture_fibers():
    """The gate closes a hazard; it may not move a single value on live glass.

    Measures every position on the fixture fibers twice — once with the gate
    live, once with the threshold raised out of reach, which is exactly the
    pre-gate code path — and requires the two to agree bit for bit.
    """
    fixture_a = REPO_ROOT / "desktop" / "tests" / "fixtures" / "span_A"
    sors = sorted(str(p) for p in fixture_a.glob("*.sor"))
    assert sors, f"no fixture SOR files under {fixture_a}"
    _run(f"""
        sors = {sors!r}
        checked = 0
        for path in sors:
            d = S.parse_sor_full(path, trim=False)
            ends = [e['dist_km'] for e in d['events'] if e.get('is_end')]
            assert ends, path
            eol = min(ends)
            km = 2.0
            while km < eol - 2.0:
                gated = S.measure_grey_loss_from_sor(d, km)
                S.LIVENESS_MAX_RESID_DB = float('inf')      # pre-gate behaviour
                try:
                    ungated = S.measure_grey_loss_from_sor(d, km)
                finally:
                    S.LIVENESS_MAX_RESID_DB = 1.30
                assert gated == ungated, (path, km, gated, ungated)
                if gated is not None:
                    checked += 1
                km += 1.0
        assert checked > 20, checked
        print('OK')
    """)
