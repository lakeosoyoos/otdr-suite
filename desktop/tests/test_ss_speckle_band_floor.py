"""The fingerprint band must start past the launch hardware.

_SPECKLE_WINDOWS puts the band at 2%-60% of the interior window, which is
span-relative; the interior itself starts at a FIXED 500 m.  On a long span the
2% offset clears a launch reel by itself (2% of 64 km is 1.3 km).  On a short
span it does not, and the reel's panel connector lands inside the band.  That
connector is the same hardware in every shot of a session, so its residual
correlates at r ~ 1.0 between DIFFERENT fibres and swamps the glass: on
Goodland->Monument (4,993 m over a 1,005 m reel) it took the folder null to
p99 0.93, the confirm bar to 2.79 -- above the 1.0 a Pearson r can reach -- and
the run reported "confirmation impossible in this acquisition class" when the
real fault was the window.

Two rules here:

  1. A shared reflector inside the LAUNCH ZONE (span-relative) pushes the band
     start past it, plus a pulse-scaled dead-zone guard.
  2. A shared reflector past that zone is REPORTED, never removed: clearing it
     costs more glass than it keeps, and on the one trusted set with that
     geometry (RDR4RDR5, port at 1,036 m of 2,064 m, six known re-shoot pairs)
     clearing it recovers nothing -- the six true pairs read 0.12 and below
     against a 0.24 bar.  At 5 ns over 1 km of trunk there is no fingerprint to
     uncover, so arming the gate there would separate nothing.

Every folder whose band already clears its launch stays byte-identical, which
is what keeps the long spans -- the ones where the gate actually fires -- out
of this change.

Namespace isolation rule: the engine is only exercised through subprocesses.
"""
from __future__ import annotations

import json
import subprocess
import sys

from conftest import SECRETSAUCE_DIR, REPO_ROOT


def _run(script: str, *args):
    p = subprocess.run([sys.executable, "-c", script, str(SECRETSAUCE_DIR), *args],
                       capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr[-3000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


# ── real traces: the repo's own 5 km / 10 ns fixtures carry the geometry ─────
# WSC_SUIsh_*.sor are 4,999 m shots over a 1 km launch reel whose connector
# reads at 1,002 m -- the same class as the folder that exposed this.
_FIXTURE_SCRIPT = r"""
import sys, json, glob, os
sys.path.insert(0, sys.argv[1])
import numpy as np
import report_sor as RS

files = [RS.load_sor_file(p) for p in sorted(glob.glob(os.path.join(sys.argv[2], '*.sor')))]
i_s = RS._LAUNCH_SKIP_M
i_e = min(f['length'] for f in files) - RS._END_BUFFER_M
hp = RS._speckle_hp_width(files)
floor, note = RS._speckle_band_floor(files, i_s, i_e)
out = {'n_files': len(files), 'floor': floor, 'note': note, 'hp': hp}
for key, bs in (('before', None), ('after', floor)):
    res = RS._speckle_band_residuals(files, i_s, i_e, hp, band_start_m=bs)
    null = [float(np.dot(res[i][1], res[j][1]))
            for i in range(len(res)) for j in range(i + 1, len(res))]
    out[key] = {'n_samples': int(res[0][3]),
                'sigma': round(float(np.median([r[2] for r in res])), 4),
                'p99': round(float(np.percentile(null, 99)), 3),
                'bar': round(float(RS._SPECKLE_CONFIRM_NULL_MULT
                                   * np.percentile(null, 99)), 3)}
print(json.dumps(out))
"""


def test_launch_reel_connector_no_longer_swamps_the_band():
    out = _run(_FIXTURE_SCRIPT,
               str(REPO_ROOT / "desktop" / "tests" / "fixtures" / "continuous"))
    assert out["n_files"] >= 3
    # The connector reads at 1,002 m; the band must start past it, and the
    # guard is 25 m or twenty pulse widths, whichever is larger.
    assert 1002 < out["floor"] < 1002 + 200, out["floor"]
    assert out["note"] is None

    # Before: different fibres read all but identical, and no correlation can
    # clear a bar above 1.0, so the gate is disabled while looking alive.
    assert out["before"]["p99"] > 0.80, out["before"]
    assert out["before"]["bar"] > 1.0, out["before"]

    # After: the baseline is the glass, and the bar is reachable.
    assert out["after"]["p99"] < 0.10, out["after"]
    assert out["after"]["bar"] < 1.0, out["after"]

    # The spike was inflating the measured noise band too, which is the
    # denominator of the fingerprint term.
    assert out["after"]["sigma"] < out["before"]["sigma"] / 2, out
    # Trimming must cost some band but leave plenty to measure in.
    assert out["after"]["n_samples"] > RS_MIN_SAMPLES_FLOOR
    assert out["after"]["n_samples"] < out["before"]["n_samples"]


RS_MIN_SAMPLES_FLOOR = 500      # mirrors _SPECKLE_MIN_SAMPLES


# ── synthetic geometries: what fires, what is reported, what is untouched ────
_SYNTH_SCRIPT = r"""
import sys, json
sys.path.insert(0, sys.argv[1])
import numpy as np
import report_sor as RS

def mk(span_m, event_m, n_files=6, with_event=None, dz=0.32, pulse_samples=3.2):
    n = int(span_m / dz)
    pos = np.arange(n) * dz
    out = []
    for k in range(n_files):
        ev = [{'dist_km': 0.0, 'reflection': -60.0, 'is_reflective': True,
               'splice_loss': 0.0},
              {'dist_km': span_m / 1000.0, 'reflection': 0.0,
               'is_reflective': False, 'splice_loss': 0.0}]
        if with_event is None or k < with_event:
            ev.insert(1, {'dist_km': event_m / 1000.0, 'reflection': -52.0,
                          'is_reflective': True, 'splice_loss': 0.3})
        out.append({'name': 'F%03d' % k, 'pos': pos,
                    'trace': np.zeros(n, dtype=np.float32),
                    'length': float(span_m), 'events': ev,
                    'pulse_samples': pulse_samples})
    return out

cases = {}
# a 1 km reel on a 5 km span: inside the band AND inside the launch zone
cases['short_span_reel'] = mk(4993.0, 1005.0)
# the same 1 km reel on a 64 km span: the 2% offset already clears it
cases['long_span_reel'] = mk(64000.0, 1005.0, dz=4.0, pulse_samples=12.5)
# a panel port at half of a 2 km span: inside the band, past the launch zone
cases['mid_span_port'] = mk(2064.0, 1036.0, dz=0.08, pulse_samples=6.4)
# the reel is there but only two of six files report it
cases['not_shared'] = mk(4993.0, 1005.0, with_event=2)

res = {}
for k, files in cases.items():
    i_s = RS._LAUNCH_SKIP_M
    i_e = min(f['length'] for f in files) - RS._END_BUFFER_M
    floor, note = RS._speckle_band_floor(files, i_s, i_e)
    res[k] = {'floor': floor, 'note': note}

# the guard scales with the pulse: same geometry, four times the pulse
wide = mk(4993.0, 1005.0, pulse_samples=3.2 * 8)
res['wide_pulse'] = {'floor': RS._speckle_band_floor(
    wide, RS._LAUNCH_SKIP_M, 4993.0 - RS._END_BUFFER_M)[0]}

# band_start_m=None must leave every index exactly where it was
f = mk(64000.0, 1005.0, n_files=1, dz=4.0)[0]
rng = np.random.default_rng(11)
f['trace'] = (rng.standard_normal(len(f['pos'])) * 0.05).astype(np.float32)
a = RS._speckle_windows(f, 500.0, 63000.0, hp_width=5)
b = RS._speckle_windows(f, 500.0, 63000.0, hp_width=5, band_start_m=None)
res['none_is_identical'] = bool(a['win'][0][0] == b['win'][0][0]
                               and a['win'][0][1] == b['win'][0][1]
                               and np.array_equal(a['win'][0][2], b['win'][0][2]))
c = RS._speckle_windows(f, 500.0, 63000.0, hp_width=5, band_start_m=2000.0)
res['floor_moves_i0'] = bool(c['win'][0][0] > a['win'][0][0])
print(json.dumps(res))
"""


def test_launch_zone_rule_fires_only_where_it_should():
    r = _run(_SYNTH_SCRIPT)

    # 1,005 m is 20% of a 4,993 m span: launch hardware, so the band moves.
    assert r["short_span_reel"]["floor"] is not None
    assert r["short_span_reel"]["floor"] > 1005.0
    assert r["short_span_reel"]["note"] is None

    # The same reel on a 64 km span sits below the band's own 2% offset, so
    # there is nothing to do and the folder stays byte-identical.
    assert r["long_span_reel"] == {"floor": None, "note": None}

    # A port at half the span is inside the band but is not launch gear.
    # Report it; do not spend half the glass clearing it.
    assert r["mid_span_port"]["floor"] is None
    assert "1,036 m" in r["mid_span_port"]["note"]

    # One file's stray reflector is not shared hardware.
    assert r["not_shared"] == {"floor": None, "note": None}


def test_guard_scales_with_the_pulse():
    r = _run(_SYNTH_SCRIPT)
    narrow = r["short_span_reel"]["floor"]
    wide = r["wide_pulse"]["floor"]
    assert wide > narrow, (narrow, wide)


def test_band_start_none_leaves_every_index_untouched():
    r = _run(_SYNTH_SCRIPT)
    assert r["none_is_identical"] is True
    assert r["floor_moves_i0"] is True


def test_source_locks_the_band_plumbing():
    src = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    # All three window builders must clamp the same way, or the gate, the
    # null and the competence diagnostic stop describing the same band.
    # The trailing paren keeps the definition line out of the count.
    assert src.count("_band_i0(band_start_m, dz))") == 3, (
        "every window builder must apply the same band floor")
    for call in ("band_start_m=_band_start",):
        assert src.count(call) >= 4, (
            "the folder's band floor must reach the gate, the null, the "
            "competence verdict and the window census")
