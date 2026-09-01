"""The multi-wavelength engine must compute pair sigma the way report_sor does.

`_compute_pair_metrics_batch_multiwl` used the UNCENTERED variance identity:

    var(A - B) = m2a + m2b - 2C - (mean_a - mean_b)^2

`report_sor._compute_pair_metrics_batch` used the same form until 2026-07-23,
when it cost 67 false positives.  On raw ~46 dB trace levels that identity
subtracts ~2000-magnitude terms to extract a variance of ~1e-4, and in float32
the trace quantization alone (~5.5e-6 dB/sample at 46 dB) puts ~2.6e-4 of
error into the cross term.  True pair sigma 0.0094 collapsed to 0.0000, worst
on large-injection-offset pairs, and the sigma-outlier tier confirmed the
artifacts as duplicates (Lumen Border LAM/BEY).  The fix there was to
mean-center the rows first.  This lineage never received it.

THIS CHANGE IS A MEASURED NO-OP ON EVERY FOLDER ON DISK, and the reason is
worth stating precisely: both loaders emit float64.

    NEWELM   .json  float64  46.1 dB      ELMNEW   .json  float64  45.5 dB
    SANDUR   .json  float64  38.3 dB      newbeta  .trc   float64  50.5 dB
    TEST DUPE .trc  float64  45.4 dB      ELMHURST .trc   float64  40.0 dB

Median sigma is unchanged to 4 decimal places on all of them, but the change
is NOT a pure no-op even in float64 - on ~46 dB levels the uncentered form
still loses enough significance to move the most injection-offset pairs:

    SANDUR      4 of 19,900 pairs move   max delta p_dup 7.01e-03
    TEST DUPE   3 of     153 pairs move  max delta p_dup 6.29e-03
    the other four folders: byte-identical

No verdict moves.  Every changed pair sits at least 6.8x its own delta from
the nearest tier boundary (0.1 / 0.5 / 0.99); the one closest to a boundary,
VERSLK003|VERSLK015 at 0.999756, moves 2.13e-06.  TEST DUPE keeps all six of
its known duplicates.  The centred value is the correct one in every case.

So this is not a bug fix on current data - it is parity, a small correction,
and defence.  The float64-ness is a property of the LOADERS, nothing
in this engine enforces it, and the failure it prevents is silent: a sigma of
0.0000 does not look like an error, it looks like a perfect duplicate.

Namespace isolation rule: the engine is only exercised through subprocesses.
"""
from __future__ import annotations

import json
import subprocess
import sys

from conftest import SECRETSAUCE_DIR


def _run(script: str):
    p = subprocess.run([sys.executable, "-c", script, str(SECRETSAUCE_DIR)],
                       capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr[-3000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


_SIGMA_SCRIPT = r"""
import sys, json
sys.path.insert(0, sys.argv[1])
import numpy as np

def uncentred(M, N):
    m1 = M.mean(axis=1)
    m2 = (M.astype(np.float64) ** 2).mean(axis=1)
    C = (M.astype(np.float64) @ M.astype(np.float64).T) / float(N)
    var_ij = m2[:, None] + m2[None, :] - 2.0 * C - (m1[:, None] - m1[None, :]) ** 2
    return np.sqrt(np.maximum(var_ij, 0.0))

def centred(M, N):
    M64 = M.astype(np.float64)
    M0 = M64 - M64.mean(axis=1, keepdims=True)
    v = (M0 ** 2).mean(axis=1)
    C0 = (M0 @ M0.T) / float(N)
    var_ij = v[:, None] + v[None, :] - 2.0 * C0
    return np.sqrt(np.maximum(var_ij, 0.0))

out = {}
rng = np.random.default_rng(4)
N = 20000
# Two traces at REAL levels (~46 dB, 6 dB of slope) whose true pair sigma is
# ~0.0094 - the same value the Lumen pairs carried.  The injection-level
# OFFSET between them is what drives the cancellation, so it is swept.
sweep = []
for off in (0.6, 5.0, 15.0, 30.0, 45.0):
    base = 46.0 - np.linspace(0, 6.0, N) + rng.standard_normal(N) * 0.02
    other = base + off + rng.standard_normal(N) * 0.0094
    M64 = np.stack([base, other])
    M32 = M64.astype(np.float32)
    sweep.append({'offset': off,
                  'true': float(centred(M64, N)[0, 1]),
                  'cen32': float(centred(M32, N)[0, 1]),
                  'unc32': float(uncentred(M32, N)[0, 1])})
out['sweep'] = sweep
out['unc_f64'] = float(uncentred(np.stack([base, other]), N)[0, 1])
out['cen_f64'] = float(centred(np.stack([base, other]), N)[0, 1])

# And the engine must be using the centred form.
src = open(f"{sys.argv[1]}/report.py", encoding='utf-8').read()
i = src.index('def _compute_pair_metrics_batch_multiwl(')
body = src[i:i + 6000]
out['engine_centred'] = 'M0 = M64 - M64.mean(axis=1, keepdims=True)' in body
out['engine_uncentred_gone'] = '- (m1[:, None] - m1[None, :]) ** 2' not in body
print(json.dumps(out))
"""


def test_the_engine_uses_the_centred_form():
    out = _run(_SIGMA_SCRIPT)
    assert out["engine_centred"] is True
    assert out["engine_uncentred_gone"] is True, (
        "the uncentered identity is back in the multiwl engine")


def test_both_lineages_now_agree():
    """The two engines must not compute the same quantity two ways.  That
    divergence is what let the .sor side be fixed in July while this one kept
    the form that caused the flood."""
    sor = (SECRETSAUCE_DIR / "report_sor.py").read_text(encoding="utf-8")
    multi = (SECRETSAUCE_DIR / "report.py").read_text(encoding="utf-8")
    marker = "M0 = M64 - M64.mean(axis=1, keepdims=True)"
    assert marker in sor, "report_sor lost its centring"
    assert marker in multi, "report.py did not receive it"
    for line in ("v = (M0 ** 2).mean(axis=1)",
                 "C0 = (M0 @ M0.T) / float(N)",
                 "var_ij = v[:, None] + v[None, :] - 2.0 * C0"):
        assert line in sor and line in multi, line


def test_on_float64_the_two_forms_agree():
    """Which is why this change moves nothing on any folder on disk - both
    loaders emit float64.  If that ever stops being true, the next test is
    what happens."""
    out = _run(_SIGMA_SCRIPT)
    assert abs(out["unc_f64"] - out["cen_f64"]) < 1e-6, (
        out["unc_f64"], out["cen_f64"])
    assert 0.008 < out["cen_f64"] < 0.011, out["cen_f64"]


def test_float32_is_where_the_uncentred_form_breaks():
    """The reason the change ships despite being a no-op today.

    Same traces, same true sigma (~0.0094 - the Lumen value), only the dtype
    and the injection-level OFFSET change.  Measured:

        offset dB       true  f32 centred  f32 uncentred
               0.6   0.009395     0.009395       0.009183
               5.0   0.009456     0.009456       0.010150
              15.0   0.009353     0.009353       0.000000   <-- collapse
              30.0   0.009345     0.009345       0.015070
              45.0   0.009449     0.009449       0.025297

    The centred form recovers the true value at every offset.  The uncentered
    one wanders, and at 15 dB it returns EXACTLY ZERO - which is the recorded
    Lumen signature: "true sigma .0094 collapsed to 0.0000, worst on
    large-injection-offset pairs".  A sigma of 0.0000 does not read as an
    error, it reads as a perfect duplicate, which is how 67 artifacts were
    confirmed."""
    out = _run(_SIGMA_SCRIPT)
    sweep = out["sweep"]
    assert len(sweep) == 5, sweep

    for row in sweep:
        assert abs(row["cen32"] - row["true"]) < 1e-6, (
            f"centred form must survive float32 at offset {row['offset']}: "
            f"{row['cen32']} vs {row['true']}")

    # The uncentered form must be demonstrably worse, and must produce the
    # silent zero at least once across the sweep.
    worst = max(abs(r["unc32"] - r["true"]) for r in sweep)
    assert worst > 0.005, f"cancellation not reproduced; worst error {worst}"
    zeros = [r for r in sweep if r["unc32"] == 0.0]
    assert zeros, (
        "the 0.0000 collapse is the signature this test exists to preserve; "
        f"sweep={sweep}")
    assert zeros[0]["true"] > 0.008, zeros[0]


def test_the_measurement_and_the_reason_are_recorded():
    """A no-op with no rationale is the kind of change someone reverts."""
    src = (SECRETSAUCE_DIR / "report.py").read_text(encoding="utf-8")
    i = src.index("def _compute_pair_metrics_batch_multiwl(")
    block = src[i:i + 6000]
    for marker in ("67 false positives", "catastrophic cancellation",
                   "float64", "Lumen Border",
                   # the measured ripple, so "no-op" is never claimed loosely
                   "7.01e-03", "NO VERDICT MOVES", "6.8x"):
        assert marker in block, f"missing rationale: {marker}"
