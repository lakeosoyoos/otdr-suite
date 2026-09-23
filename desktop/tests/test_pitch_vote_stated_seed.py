"""The marker vote for the sample pitch must not hand back its own seed.

`exfo_res_m` is pinned by snapping each FastReporter marker Length to a
whole number of samples.  The snap only corrects the seed while the seed is
within half a sample across the marker, n < 1 / (2 x error).  The seed was
c x SamplingPeriod / (2 x 1.4682); a file stating 1.4700 is 1225 ppm away,
so every marker longer than ~400 samples kept the seed's error.

At 0.78 ns sampling (the 5 ns tie-panel shots, 8 cm a sample) every marker
is 600-13,000 samples, so the vote returned the seed on SNARCAAH 1 East/West
(1,152 files, 2026-09-23) and Dinwiddie ILA1-6: 1,212 ppm off.  The stated
pitch is the right one there -- each reflective peak sits a constant
pulse-rise after its marker at 1.04 km and at 2.10 km alike -- and it feeds
the engine's FR-exact loss windows (measure_fr_exact_loss) as well as the
Viewer's x axis.
"""
from __future__ import annotations

import glob
import importlib.util
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
PANEL = os.path.join(HERE, 'fixtures', 'panelmint')
_C = 299_792_458.0


def _reader(sub):
    spec = importlib.util.spec_from_file_location(
        f'pitchseed_{sub}', os.path.join(ROOT, sub, 'sor_reader324802a.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_FILES = sorted(glob.glob(os.path.join(PANEL, '*', '*.sor')))


@pytest.mark.parametrize('sub', ['splicereport', 'viewer'])
def test_fine_sampled_panel_shot_pins_the_stated_pitch(sub):
    assert _FILES, 'fixture missing'
    R = _reader(sub)
    for p in _FILES:
        r = R.parse_sor_full(p, trim=False)
        sp, ior = r['exfo_sampling_period'], r['ior']
        assert abs(sp - 7.8125e-10) < 1e-15, 'premise: 0.78 ns sampling'
        assert abs(ior - 1.47) < 1e-9, 'premise: the file states 1.4700'
        stated = _C * sp / 2.0 / ior
        ppm = abs(r['exfo_res_m'] - stated) / stated * 1e6
        assert ppm < 0.5, f'{os.path.basename(p)}: vote {ppm:.1f} ppm off the stated pitch'
