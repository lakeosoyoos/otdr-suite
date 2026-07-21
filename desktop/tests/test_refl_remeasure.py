"""Reflective re-measure gate (PLACHE F609 phantom, 4th stored-table-trust fix).

The B-side KeyEvents of CHEPLA0609 claims refl -66.4 dB @25.68 km; the
DataPts block in the same file is flat there (expected +31 mdB spike,
measured -2.7).  The mid-span reflective warning now confirms the spike
exists in the fiber's own trace before repeating any stored claim; it
suppresses only on a confident negative (unmeasurable -> keep).
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'splicereport'))
import numpy as np
import splicereportmatchexfo as E
import sor_reader324802a as SR

FIX = os.path.join(HERE, 'fixtures', 'refl')


def test_f609_phantom_suppressed():
    b = SR.parse_sor_full(os.path.join(FIX, 'CHEPLA0609_1550.sor'))
    assert E._reflective_spike_confirms(b, 25.682, -66.4) is False


def test_real_spike_confirms():
    # synthetic: clean slope + a planted 80 mdB spike at 20 km
    res = 5.1
    n = 8000
    y = 30.0 + 0.0002 * np.arange(n) * res   # ascending accumulated dB
    i = int(20_000 / res)
    y[i-3:i+3] += 0.080
    r = {'trace': y.tolist(), 'exfo_sampling_period': 5e-08,
         'events': [], 'exfo_calibration': {'NominalPulseWidth': 2500}}
    assert E._reflective_spike_confirms(r, 20.0, -60.0) is True


def test_unmeasurable_keeps_warning():
    r = {'trace': [30.0] * 100, 'events': []}
    assert E._reflective_spike_confirms(r, 50.0, -60.0) is True


def test_gate_wired_into_midspan_scan():
    src = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    i = src.index('MIDSPAN_REFL_WARN_DB:')
    assert '_reflective_spike_confirms(r, e[' in src[i:i+600]
    assert 'own-frame' in src        # findable position in the label
