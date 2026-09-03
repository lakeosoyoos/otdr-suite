"""Regression tests for the per-folder Rayleigh-speckle high-pass width.

Mecca<->Niland 498/504 was queried in the field - could it be a tie-panel or
short-shot artifact? - and this file originally recorded that question as a
verdict.  It is not one: whether 498/504 is a duplicate is UNKNOWN.  What IS
measured is that the filter width was wrong for that acquisition.  Root cause:
`_SPECKLE_HP_WIDTH` was a fixed 21 SAMPLES, tuned on 500 ns / 25 ns
acquisitions where 21 happens to equal one pulse width.  The pulse expressed
in samples varies 15x across the acquisitions on disk:

    275 ns / 25 ns    = 11      (Mecca<->Niland)
    500 ns / 25 ns    = 20      (EMVSUI, SUIEMV, ELMMIL)
    2500 ns / 50 ns   = 50      (SEANOR, SANDUR)
    10 ns / 3.125 ns  = 3.2     (short shots)
    5 ns / 0.78 ns    = 6.4     (A-F West)

Run WIDER than the pulse and splice steps survive the moving average, land in
the residual as a same-sign spike in every fiber, and inflate the folder null.
On Mecca<->Niland the null read 0.201 (vs 0.086 on EMVSUI), which put the
gate into ABSTAIN on 498/504 and let it print.  Narrowed to the pulse the
null reads 0.072 and the pair is vetoed, in BOTH directions independently.

That demotion is a CONSEQUENCE of the width fix, not evidence for it.  Every
measure except the fingerprint points at 498/504 being real (sigma 0.0171,
identical EOF, a flat residual across 61 km, event tables matching 9 of 10 at
a median |dloss| of 0.0020 dB).  The tests below therefore assert the WIDTH
behaviour, not that 498/504 is false.

The width now only ever NARROWS, never widens past the calibrated 21, and
declines to narrow at all on a folder whose files disagree about the
acquisition - see test_mixed_acquisition_folder_is_not_narrowed for why that
guard is load-bearing rather than decorative.

Namespace isolation rule: the Secret Sauce engine is only ever exercised
through subprocesses, never imported into the test process.
"""
from __future__ import annotations

import json
import subprocess
import sys

from conftest import SECRETSAUCE_DIR


def _run(script: str):
    p = subprocess.run([sys.executable, "-c", script, str(SECRETSAUCE_DIR)],
                       capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


_WIDTH_SCRIPT = r"""
import sys, json
sys.path.insert(0, sys.argv[1])
from report_sor import (_speckle_hp_width, _SPECKLE_HP_WIDTH,
                        _SPECKLE_HP_WIDTH_MIN)

def folder(*pulses):
    out = []
    for p in pulses:
        out += [{'pulse_samples': p}] * 5
    return out

out = {'cap': _SPECKLE_HP_WIDTH, 'floor': _SPECKLE_HP_WIDTH_MIN}
# Real acquisitions measured on disk.
out['nilmec_275ns'] = _speckle_hp_width(folder(11.0))
out['emvsui_500ns'] = _speckle_hp_width(folder(20.0))
out['seanor_2500ns'] = _speckle_hp_width(folder(50.0))
out['shortshot_10ns'] = _speckle_hp_width(folder(3.2))
out['afwest_5ns'] = _speckle_hp_width(folder(6.4))
# Fail-safe: a file that cannot report its pulse keeps the calibrated width.
out['missing'] = _speckle_hp_width([{'pulse_samples': None}] * 5)
out['partial'] = _speckle_hp_width(folder(11.0) + [{'pulse_samples': None}])
# Acquisition-uniformity guard.
out['mixed'] = _speckle_hp_width(folder(11.0, 20.0))
out['within_tolerance'] = _speckle_hp_width(folder(11.0, 11.02))
# The kernel must be odd at every width it can return.
out['odd'] = [_speckle_hp_width(folder(float(v))) % 2
              for v in range(3, 40)]
print(json.dumps(out))
"""


def test_width_narrows_to_the_pulse_when_the_pulse_is_short():
    """Mecca<->Niland is the folder the fix targets: 275 ns at 25 ns
    sampling is 11 samples, so the shipped 21 ran 1.9 pulse widths wide."""
    out = _run(_WIDTH_SCRIPT)
    assert out["nilmec_275ns"] == 11, out


def test_width_never_widens_past_the_calibrated_cap():
    """The cap is what keeps the long-pulse folders on the width their
    thresholds were calibrated with.  Matching SEANOR to its 50-sample pulse
    would take its null from 0.117 to 0.363, and SANDUR's to 0.493 - the
    same failure in reverse."""
    out = _run(_WIDTH_SCRIPT)
    assert out["cap"] == 21
    assert out["emvsui_500ns"] == 21, "20 samples must not narrow below the cap"
    assert out["seanor_2500ns"] == 21, "long pulse must stay at the cap"


def test_width_floors_so_the_filter_stays_meaningful():
    out = _run(_WIDTH_SCRIPT)
    assert out["floor"] == 5
    assert out["shortshot_10ns"] == 5
    assert out["afwest_5ns"] == 7


def test_width_is_always_odd():
    """A moving average kernel has to be odd or the residual is shifted by
    half a sample against the trace it came from."""
    out = _run(_WIDTH_SCRIPT)
    assert set(out["odd"]) == {1}, out["odd"]


def test_unknown_pulse_keeps_the_calibrated_width():
    """Fail-safe direction: a wider filter only ever inflates the null, and
    a higher null makes the gate abstain rather than veto."""
    out = _run(_WIDTH_SCRIPT)
    assert out["missing"] == 21
    assert out["partial"] == 21, "one unreadable file must not narrow the folder"


def test_mixed_acquisition_folder_is_not_narrowed():
    """Load-bearing guard, not decoration.  Secret Sauce runs ONE report over
    whatever folder is uploaded, and 275 ns and 500 ns EXFO acquisitions
    share a bit-identical sample spacing (dz = 2.552445171 m), so
    _SPECKLE_DZ_TOL considers their traces comparable and the existing grid
    guard does NOT catch the mismatch.  Without this the filter width would
    be decided by which file happened to sort first."""
    out = _run(_WIDTH_SCRIPT)
    assert out["mixed"] == 21, "a mixed-acquisition folder must not narrow"
    assert out["within_tolerance"] == 11, "ordinary jitter must still narrow"


_PLUMB_SCRIPT = r"""
import sys, json, inspect
sys.path.insert(0, sys.argv[1])
import numpy as np
from report_sor import _speckle_windows, _SPECKLE_HP_WIDTH

out = {}
sig = inspect.signature(_speckle_windows)
out['params'] = list(sig.parameters)
out['default_is_none'] = sig.parameters['hp_width'].default is None

# A synthetic trace long enough to window: broadband noise on a slope.
n = 40000
rng = np.random.default_rng(7)
pos = np.arange(n) * 2.5524
trace = (rng.standard_normal(n) * 0.05 - pos * 2e-5).astype(np.float32)
f = {'trace': trace, 'pos': pos}
lo, hi = 500.0, 90000.0

# residual = x - moving_average(x, w).  At w -> 1 the average IS the trace
# and the residual vanishes; the wider the average, the more of the signal
# survives into the residual.  So amplitude must fall monotonically as the
# width shrinks -- which is precisely why a too-wide filter lets splice
# structure through into what the gate treats as speckle.
amps = []
for w in (21, 11, 5):
    r = _speckle_windows(f, lo, hi, hp_width=w)
    amps.append(round(float(r['win'][0][3]), 6))
out['amps'] = amps
# Omitting the argument must reproduce the calibrated width exactly.
a = _speckle_windows(f, lo, hi)
b = _speckle_windows(f, lo, hi, hp_width=_SPECKLE_HP_WIDTH)
out['default_matches_cap'] = bool(np.array_equal(a['win'][0][2], b['win'][0][2]))
print(json.dumps(out))
"""


def test_windows_honour_the_width_argument():
    out = _run(_PLUMB_SCRIPT)
    assert out["params"] == ["f", "interior_start", "interior_end", "hp_width"]
    assert out["default_is_none"] is True
    a21, a11, a5 = out["amps"]
    assert a21 > a11 > a5, (
        f"wider high-pass must let more through, which is the leak: {out['amps']}")
    assert out["default_matches_cap"] is True


def test_source_locks_the_width_plumbing():
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")

    assert "_SPECKLE_HP_WIDTH = 21" in src
    assert "_SPECKLE_HP_WIDTH_MIN = 5" in src

    # Every file must carry its own pulse, or the resolver silently
    # fail-safes to the cap on every folder and the fix is inert.
    assert "'pulse_samples': pulse_samples," in src
    assert "CalibratedPulseWidth" in src

    # Both consumers must use the folder width; the null and the pair
    # measurements have to be taken through the same filter.
    # null, cache, and the competence diagnostic (test_competence_physics.py)
    # all measure through the same filter, or the competence verdict would
    # judge a band the gate never used.
    assert src.count("hp_width=_hp_w") == 3, "null, cache and competence must share the width"
    assert "_hp_w = _speckle_hp_width(files)" in src

    # The uniformity guard must survive refactors.
    i = src.index("def _speckle_hp_width(")
    block = src[i:i + 2200]
    assert "if hi - lo > 0.01 * hi:" in block, "acquisition-uniformity guard gone"
    assert "return _SPECKLE_HP_WIDTH" in block

    # A narrowed folder must say so in the run log, the way the gate does.
    assert "Speckle high-pass:" in src
