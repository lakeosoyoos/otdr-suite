"""Pulse-smear floor on clustering/attribution radii (KANLAN repair splice).

One physical repair splice at ~110.2 km appeared as two "Bends @" columns:
the cross-fiber event scatter of a 2500 ns pulse (~255 m smear) straddled the
200 m off-splice cluster gap (measured population gap: 219 m).  The engine's
clustering and attribution radii are now FLOORED at the population's median
pulse smear — the instrument cannot separate events closer than the pulse, so
sub-smear gaps are scatter, not structure.  Floor is always-on: the panel's
"Bend fold distance" can widen radii, never narrow them below the smear.
Files with no readable pulse (or stages driven without discovery) keep the
exact legacy radii (smear 0.0).

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


def test_pulse_smear_units_and_fallbacks():
    """ns and seconds forms both read; corrupt values and missing blocks
    yield 0.0 (legacy radii)."""
    _run("""
        # 2500 ns -> ~255 m smear
        f_ns  = {1: {'exfo_calibration': {'NominalPulseWidth': 2500.0}}}
        # same pulse written in seconds by some firmware
        f_s   = {1: {'exfo_calibration': {'NominalPulseWidth': 2.5e-6}}}
        # corrupt / absent
        f_bad = {1: {'exfo_calibration': {'NominalPulseWidth': 1e9}}}
        f_no  = {1: {'exfo_calibration': None}, 2: {}}
        for fx, want in ((f_ns, 0.2553), (f_s, 0.2553)):
            got = E._pulse_smear_km(fx)
            assert abs(got - want) < 0.001, (fx, got)
        assert E._pulse_smear_km(f_bad) == 0.0
        assert E._pulse_smear_km(f_no) == 0.0
        assert E._pulse_smear_km({}) == 0.0
        print('OK')
    """)


def test_discovery_merges_subsmear_gap_with_pulse():
    """With a 2500 ns pulse the 219 m gap is sub-smear -> ONE closure.
    Synthetic 60-fiber cable, one closure whose event positions split into
    two populations 219 m apart (the KANLAN shape), plus EOF."""
    _run("""
        def ev(km, typ='0F9999LS', end=False):
            return {'dist_km': km, 'splice_loss': 0.05, 'type': typ,
                    'is_end': end, 'is_reflective': False}
        fibers = {}
        for f in range(1, 61):
            km = 50.000 if f <= 30 else 50.219      # 219 m population gap
            fibers[f] = {
                'events': [ev(km), ev(117.0, typ='1E9999LS', end=True)],
                'exfo_calibration': {'NominalPulseWidth': 2500.0},
            }
        sp = E.discover_splices(fibers)
        assert len(sp) == 1, sp
        assert sp[0]['count'] == 60, sp
        print('OK')
    """)


def test_discovery_without_pulse_keeps_legacy_split():
    """No calibration block -> smear 0.0 -> legacy 250 m gap splits...
    219 m < 250 m merges under legacy too, so use a 300 m gap: legacy
    splits it, and a 2500 ns pulse would not (255 m floor still < 300 m —
    both split).  The legacy-preservation claim is the no-pulse path."""
    _run("""
        def ev(km, typ='0F9999LS', end=False):
            return {'dist_km': km, 'splice_loss': 0.05, 'type': typ,
                    'is_end': end, 'is_reflective': False}
        fibers = {}
        for f in range(1, 61):
            km = 50.000 if f <= 30 else 50.300
            fibers[f] = {'events': [ev(km), ev(117.0, typ='1E9999LS', end=True)]}
        sp = E.discover_splices(fibers)
        assert E._RUN_PULSE_SMEAR_KM == 0.0
        assert len(sp) == 2, sp
        print('OK')
    """)


def test_fold_floor_always_on_vs_panel():
    """_fold_km(): default 0.200; pulse smear raises it; a panel setattr can
    widen further but cannot narrow below the smear."""
    _run("""
        assert abs(E._fold_km() - E.BEND_SPLICE_FOLD_KM) < 1e-9
        E._RUN_PULSE_SMEAR_KM = 0.2553
        assert abs(E._fold_km() - 0.2553) < 1e-9      # floor beats 0.200
        E.BEND_SPLICE_FOLD_KM = 0.500                  # panel widens
        assert abs(E._fold_km() - 0.500) < 1e-9
        E.BEND_SPLICE_FOLD_KM = 0.075                  # panel 'unchecked' legacy
        assert abs(E._fold_km() - 0.2553) < 1e-9      # cannot go sub-smear
        print('OK')
    """)


def test_offsplice_cluster_gap_floored():
    """Two bend clusters 219 m apart, far from any splice: legacy 200 m gap
    splits them into two columns; with the run smear at 255 m they are one
    column."""
    body = """
        splices = [{'position_km': 20.0, 'position_km_refined': 20.0,
                    'column_kind': 'splice'}]
        def bend(f, km):
            return {'fiber': f, 'splice_idx': 0, 'bidir_dist': km,
                    'bidir_loss': 0.11, 'is_bend': True, 'is_flagged': True,
                    'is_break': False, 'is_broke': False, 'is_ref': False}
        allr = {(f, 0): bend(f, 52.000 + i*0.004) for i, f in enumerate((1, 2, 3))}
        allr.update({(f, 0): bend(f, 52.219 + (f-10)*0.004) for f in (10, 11)})
        %s
        out, sp2 = E.split_offsplice_events_into_own_columns(
            dict(allr), [dict(s) for s in splices], total_span_km=100.0)
        bend_cols = [s for s in sp2 if s.get('column_kind') == 'bend']
        assert len(bend_cols) == %d, sp2
        print('OK')
    """
    _run(body % ("E._RUN_PULSE_SMEAR_KM = 0.0", 2))
    _run(body % ("E._RUN_PULSE_SMEAR_KM = 0.2553", 1))
