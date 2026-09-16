"""The viewer draws its trace on the file's OWN origin, not a guessed one.

THE BUG: `_load_trace_cached` placed sample 0 with `_sor_first_pos_m`, a hunt
for the trace minimum in samples 10..500 that was meant to find "the
just-past-launch position".  On a long-pulse acquisition the receiver is still
recovering from the port reflection there, the minimum lands 60+ samples in,
and the whole curve is drawn that far to the LEFT of its events.  Measured
on ROM<->TUC (5.1 m/sample): every reflective event's peak sat ~306 m BEFORE
its marker.  The tech saw a splice signature and its marker not lined up.

THE RULE: the trace and the KeyEvents share the OTDR's digitizer clock from
the port.  Sample 0 sits at the file's stored acquisition offset (FxdParams,
0 on every production file), so dist_km[i] = i * dx_km.  The Splice Report
engine's EXFO-exact LSA already indexes the raw trace as km / res_m for the
same reason (test_splicereport_exfo_lsa).

Behavioural anchor: a reflective event is marked at the start of its rise, so
its peak lands at or a few samples AFTER the marker.  On the committed span_A
fixtures the guessed origin put 12% of peaks inside [0, 12] samples of the
marker; the stored origin puts 98% there.
"""
from __future__ import annotations

import numpy as np
import pytest

from conftest import import_trace_server, FIXTURE_A_DIR, FIXTURE_B_DIR, VIEWER_DIR

ts = import_trace_server()


@pytest.fixture(scope="module")
def dirs_set():
    ts.set_dirs(str(FIXTURE_A_DIR), str(FIXTURE_B_DIR))
    return ts


def test_trace_server_does_not_guess_the_origin():
    src = (VIEWER_DIR / "trace_server.py").read_text(encoding="utf-8")
    assert "first_pos_m = _sor_first_pos_m(" not in src, (
        "the viewer must place sample 0 from the file's stored acquisition "
        "offset, not from the first-500-sample minimum hunt"
    )
    assert "fxd_acq_offset" in src


def test_axis_starts_at_stored_acquisition_offset(dirs_set):
    """Every fixture stores acq_offset 0, so the axis is exactly i * dx."""
    for fiber, _ in ts.list_fibers(str(FIXTURE_A_DIR)):
        t = ts.load_trace("a", fiber)
        assert t is not None
        assert t["first_pos_km"] == 0.0
        xs = np.asarray(t["dist_km"][:2000], dtype=np.float64)
        expect = np.arange(len(xs)) * t["dx_km"]
        assert np.allclose(xs, expect, atol=1e-4), fiber


def test_reflective_peaks_land_just_after_their_markers(dirs_set):
    hits = 0
    total = 0
    for fiber, _ in ts.list_fibers(str(FIXTURE_A_DIR)):
        t = ts.load_trace("a", fiber)
        xs = np.asarray(t["dist_km"], dtype=np.float64)
        ys = np.asarray(t["trace_db"], dtype=np.float64)
        for e in t["events"]:
            if not e["is_reflective"] or e["is_end"] or e["dist_km"] < 0.5:
                continue
            i = int(np.searchsorted(xs, e["dist_km"]))
            lo, hi = i - 40, i + 40
            if lo < 0 or hi > len(ys):
                continue
            peak = int(np.argmax(ys[lo:hi])) - 40
            total += 1
            hits += 0 <= peak <= 12
    assert total >= 4, total
    rate = hits / total
    assert rate >= 0.85, f"{hits}/{total} reflective peaks within 12 samples after their marker"
