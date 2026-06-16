"""Regression: a JSON acquisition missing 'Wavelength' (or sibling required
DataPoints keys) must NOT crash Secret Sauce's load_file / batch loader.

Bug: report.load_file hard-subscripted meas['Wavelength'] (and DataPoints
sub-keys) with no schema check after json.load, so one incomplete acquisition
threw KeyError and aborted the whole batch of a dozen files.

HARD RULE — namespace isolation
-------------------------------
Secret Sauce ships its OWN sor_reader324802a.py that collides with the viewer's
copy, so this test process must NEVER import report.py directly.  We exercise
load_file in a CLEAN child subprocess (the same isolation conftest.run_secretsauce
relies on), and back it with a static-source guard that pins the fix.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

from conftest import SECRETSAUCE_DIR


# ---------------------------------------------------------------------------
# 1. Static guard — load_file must not hard-subscript meas['Wavelength'].
#    The pre-fix source ran `int(meas['Wavelength'])` as the FIRST statement of
#    the OtdrMeasurements loop with no preceding check, so a missing key
#    KeyError'd immediately.  This asserts a "skip block missing required key"
#    guard now precedes that subscript — which FAILS on the unguarded old code
#    and PASSES once the guard is in place.
# ---------------------------------------------------------------------------
def test_load_file_guards_incomplete_blocks_before_subscript():
    src = (SECRETSAUCE_DIR / "report.py").read_text(encoding="utf-8")
    assert "meas['Wavelength']" in src or 'meas["Wavelength"]' in src, (
        "expected load_file to still reference Wavelength after guarding it"
    )
    # A per-block guard that skips acquisitions missing a required key must
    # appear, and it must come BEFORE the hard subscript it protects.
    guard_marker = "missing"
    subscript = "int(meas['Wavelength'])"
    assert guard_marker in src and "DataPoints" in src, (
        "expected a per-block 'missing required key' guard around DataPoints"
    )
    assert subscript in src, "expected the Wavelength subscript to remain"
    assert src.index(guard_marker) < src.index(subscript), (
        "the missing-key guard must precede int(meas['Wavelength']) — "
        "otherwise an incomplete acquisition still KeyErrors the whole batch"
    )


# ---------------------------------------------------------------------------
# 2. Behavioural reproduction in a clean subprocess: a file mixing one good
#    block and one Wavelength-less block keeps the good wavelength; a batch
#    containing an all-incomplete file skips it instead of aborting; a batch
#    of only incomplete files raises a clear, file-naming error.
# ---------------------------------------------------------------------------
def test_load_file_skips_incomplete_blocks_in_subprocess():
    snippet = textwrap.dedent(
        f"""
        import sys, os, json, base64, tempfile
        import numpy as np
        sys.path.insert(0, {str(SECRETSAUCE_DIR)!r})
        import report

        n = 4
        pts = base64.b64encode(np.array([1024]*n, dtype='<u2').tobytes()).decode()
        good = {{"Wavelength": "1550",
                 "DataPoints": {{"NumberOfPoints": n, "Resolution": "1.0",
                                 "FirstPointPosition": "0", "Points": pts}}}}
        bad = {{"DataPoints": {{"NumberOfPoints": n, "Resolution": "1.0",
                                "FirstPointPosition": "0", "Points": pts}}}}

        d = tempfile.mkdtemp()
        p_mixed = os.path.join(d, "MIX_0001.json")
        with open(p_mixed, "w") as f:
            json.dump({{"Measurement": {{"OtdrMeasurements": [good, bad]}}}}, f)
        p_allbad = os.path.join(d, "BAD_0002.json")
        with open(p_allbad, "w") as f:
            json.dump({{"Measurement": {{"OtdrMeasurements": [bad]}}}}, f)

        # (a) mixed file: good block survives, bad block skipped (no KeyError)
        r = report.load_file(p_mixed)
        assert sorted(r["wl"].keys()) == [1550], r["wl"].keys()

        # (b) all-bad file alone: clear ValueError that names the file
        try:
            report.load_file(p_allbad)
            raise SystemExit("FAIL: all-bad file did not raise")
        except ValueError as e:
            assert "BAD_0002.json" in str(e), e

        # (c) batch with one good + one all-bad: bad skipped, good loaded
        files = report._load_json_files([p_mixed, p_allbad])
        assert len(files) == 1, len(files)

        # (d) batch of only bad files: RuntimeError, not a silent empty result
        try:
            report._load_json_files([p_allbad])
            raise SystemExit("FAIL: all-bad batch did not raise")
        except RuntimeError as e:
            assert "BAD_0002.json" in str(e), e

        print("OK")
        """
    )
    p = subprocess.run([sys.executable, "-c", snippet],
                       capture_output=True, text=True)
    assert p.returncode == 0, (
        f"subprocess exited {p.returncode}\n"
        f"stdout:\n{p.stdout}\nstderr:\n{p.stderr}"
    )
    assert p.stdout.strip().endswith("OK"), p.stdout
