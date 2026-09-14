"""A folder of traces PLUS one sidecar each must still load.

An iOLM job uploaded through EXFO Exchange puts a sidecar beside every
trace: identifiers, the element list, thresholds -- and no OtdrMeasurements
block, so it is not a loadable measurement.  Stage such a job with ONE
wavelength of .sor and the folder holds 432 of each; the loader's
"prefer JSON on a tie" rule hands the tie to the sidecars, every parse
fails, and the run dies with

    Loaded A=0 B=0 fibers -- both directions required.

with 432 perfectly readable .sor sitting right there.  Found on spans 19,
25 and 26 the moment their sidecars were downloaded (2026-09-13).

Counting files cannot tell a sidecar from an export without opening one.
Trying and falling back can, and costs nothing when the first choice was
right.
"""
from __future__ import annotations

import json
import shutil
import sys

from conftest import (
    run_splicereport, FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR,
    SPLICEREPORT_DIR,
)

sys.path.insert(0, str(SPLICEREPORT_DIR))


def _sidecar_beside_every_trace(src, dst):
    """Copy a fixture direction and drop an Exchange-style sidecar next to
    each trace, so the two counts tie."""
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for sor in sorted(src.glob("*.sor")):
        shutil.copy2(sor, dst / sor.name)
        (dst / (sor.stem + ".json")).write_text(json.dumps({
            "brief": {"Identifiers": [{"Name": "Cable ID", "Value": "X-Y-01"}],
                      "Measurement": {"Elements": []}}}), encoding="utf-8")
        n += 1
    return n


def test_a_sidecar_per_trace_still_loads_the_traces(tmp_path):
    a = tmp_path / "iOLM AB"
    b = tmp_path / "iOLM BA"
    n_a = _sidecar_beside_every_trace(FIXTURE_SPLICE_A_DIR, a)
    n_b = _sidecar_beside_every_trace(FIXTURE_SPLICE_B_DIR, b)
    assert n_a and n_b, "fixture folders must hold traces"
    assert len(list(a.glob("*.json"))) == len(list(a.glob("*.sor")))

    out = tmp_path / "report.xlsx"
    rc, man, err = run_splicereport(a, b, out)
    assert man and man.get("ok"), (rc, man, err[-600:])
    assert man["n_fibers"] > 0
    assert out.exists()


def test_the_traces_alone_still_load(tmp_path):
    """The control: no sidecars, nothing changed."""
    a = tmp_path / "A"
    b = tmp_path / "B"
    shutil.copytree(FIXTURE_SPLICE_A_DIR, a)
    shutil.copytree(FIXTURE_SPLICE_B_DIR, b)
    rc, man, err = run_splicereport(a, b, tmp_path / "r.xlsx")
    assert man and man.get("ok"), (rc, man, err[-600:])
    assert man["n_fibers"] > 0
