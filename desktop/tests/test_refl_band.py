"""Mid-span reflectance: polarity-robust spike confirm + band ceiling.

Lumen Border (2026-07-23): real -77.6/-77.9 dB glints (LAMBEY F109 @5.19,
F133 @4.82) measure as -0.13 dB DIPS in accumulated-loss-ascending traces
— 20x noise, at exactly the claimed km — and the positive-only spike
confirm blindly refuted them, so the mid-span reflective detection was
blind on that whole trace-orientation class.  Plus Robert's band ask:
an optional ceiling so the pass can flag ONLY [warn floor, ceiling]
(e.g. -80..-40 isolates faint fusion glints).
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'splicereport'))

import splicereportmatchexfo as E  # noqa: E402

SP = 5e-08
M0 = SP * (299792458.0 / 1.468) / 2.0


def _rec(kind, km=5.0, mag=0.7, n=4000, seed=9):
    """Loss-ascending trace with a localized DIP ('dip'), a localized
    SPIKE ('spike'), or nothing ('flat') at km."""
    rng = np.random.RandomState(seed)
    x = np.arange(n)
    tr = 5.0 + 0.19 * (x * M0 / 1000.0) + rng.normal(0, 0.004, n)
    i = int(km * 1000 / M0)
    w = int(15 / M0) or 1
    if kind == 'dip':
        tr[i - w:i + w] -= mag
    elif kind == 'spike':
        tr[i - w:i + w] += mag
    # 50 ns pulse stored in SECONDS — matches the Lumen file class and
    # exercises the units normalization; keeps min_run at the short-pulse
    # floor so the narrow synthetic features are width-consistent.
    return {'trace': tr, 'exfo_sampling_period': SP, 'events': [],
            'exfo_calibration': {'NominalPulseWidth': 5e-08}}


def test_spike_confirm_accepts_dip_orientation():
    """Accumulated-loss traces draw the glint as a DIP — must confirm."""
    assert E._reflective_spike_confirms(_rec('dip'), 5.0, -50.0) is True


def test_spike_confirm_accepts_spike_orientation():
    """Power-descending traces draw it UP — must also confirm."""
    assert E._reflective_spike_confirms(_rec('spike'), 5.0, -50.0) is True


def test_spike_confirm_still_refutes_flat_glass():
    """PLACHE F609 class: table claims a reflection, glass is flat in
    BOTH signs — refutation power unchanged."""
    assert E._reflective_spike_confirms(_rec('flat'), 5.0, -50.0) is False


def test_band_ceiling_default_off_and_gate_wired():
    assert E.MIDSPAN_REFL_CEIL_DB == 0.0          # shipped behavior
    src = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    assert src.count('if MIDSPAN_REFL_CEIL_DB < 0 and refl > MIDSPAN_REFL_CEIL_DB:') == 1
    assert '_passes(dev) or _passes(-dev)' in src        # orientation-symmetric
    assert 'dev = dev - float(np.median(dev))' in src     # offset-artifact centering
    assert 'min_run = max(2, int(0.3 * pulse_m / res))' in src  # width discriminator


def test_panel_row_and_maps():
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    assert '"midspan_refl_ceiling"' in src or "'midspan_refl_ceiling'" in src
    assert '"midspan_refl_ceiling": "MIDSPAN_REFL_CEIL_DB"' in src
    assert '"midspan_refl_ceiling": 0.0' in src        # unticked = no ceiling
    # NOT pre-applied: shipped default keeps no ceiling
    apply_block = src.split('OTDR_DEFAULT_APPLY = ', 1)[1].split('}', 1)[0]
    assert 'midspan_refl_ceiling' not in apply_block


def test_pulse_width_units_normalized():
    """Some firmware writes NominalPulseWidth in SECONDS (5e-08 = 50 ns).
    Treated as ns, the expected-spike floor computed to ~39 dB and refuted
    EVERY mid-span reflective on the file class.  Both unit spellings must
    behave identically."""
    r_sec = _rec('dip', mag=0.13)
    r_ns = _rec('dip', mag=0.13)
    r_ns['exfo_calibration'] = {'NominalPulseWidth': 50}
    assert E._reflective_spike_confirms(r_sec, 5.0, -77.6) is True
    assert E._reflective_spike_confirms(r_ns, 5.0, -77.6) is True


def test_echo_guard_geometry_candidate_scale():
    """Echo position test runs at the CANDIDATE's scale: |cand - n*k| <=
    tol.  A candidate 1.17 km from any parent multiple must NOT be called
    an echo (the old /n form had n*tol slop and ate it)."""
    parents = [(1.006, -45.0)]
    assert E._is_likely_echo(5.19, -77.6, parents) is False
    # true echo geometry still fires: candidate at 2*parent, weaker
    assert E._is_likely_echo(2.012, -77.6, parents) is True


def test_uni_band_off_by_default():
    assert E.UNI_REFL_FLOOR_DB == 0.0 and E.UNI_REFL_CEIL_DB == 0.0
    assert E.uni_find_reflective_events({1: {'events': []}}, 10.0) == []


def test_uni_band_flags_confirmed_glint(monkeypatch):
    monkeypatch.setattr(E, 'UNI_REFL_FLOOR_DB', -80.0)
    monkeypatch.setattr(E, 'UNI_REFL_CEIL_DB', -40.0)
    monkeypatch.setattr(E, '_reflective_spike_confirms', lambda r, km, refl: True)
    fibers = {7: {'events': [
        {'dist_km': 5.0, 'reflection': -77.6, 'is_reflective': True,
         'is_end': False, 'splice_loss': 0.0},
        {'dist_km': 6.0, 'reflection': -30.0, 'is_reflective': True,   # above ceiling
         'is_end': False, 'splice_loss': 0.0},
        {'dist_km': 7.0, 'reflection': -85.0, 'is_reflective': True,   # below floor
         'is_end': False, 'splice_loss': 0.0},
        {'dist_km': 10.5, 'reflection': -40.0, 'is_reflective': True,
         'is_end': True, 'splice_loss': 0.0},
    ], '_trace_offset_km': 0.0}}
    out = E.uni_find_reflective_events(fibers, 10.5)
    assert [(e['fiber'], e['position_km']) for e in out] == [(7, 5.0)]
    cols = E.uni_cluster_reflective(out)
    assert len(cols) == 1 and cols[0]['kind'] == 'reflective'
    assert cols[0]['refl_members'] == {7: -77.6}


def test_uni_band_requires_trace_confirm(monkeypatch):
    monkeypatch.setattr(E, 'UNI_REFL_FLOOR_DB', -80.0)
    monkeypatch.setattr(E, '_reflective_spike_confirms', lambda r, km, refl: False)
    fibers = {7: {'events': [
        {'dist_km': 5.0, 'reflection': -77.6, 'is_reflective': True,
         'is_end': False, 'splice_loss': 0.0}], '_trace_offset_km': 0.0}}
    assert E.uni_find_reflective_events(fibers, 10.5) == []
