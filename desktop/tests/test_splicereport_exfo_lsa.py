"""Stage-1 regression: our marker-based LSA must reproduce EXFO's grey numbers.

North star (boss directive): the OTDR software must reproduce EXFO FastReporter's
numbers.  EXFO computes a non-reflective event's grey/splice loss with a two-line
extrapolated LSA, and it STORES its own per-event fit-window cursors in the SOR
KeyEvents block (tot_end_prev / tot_start_curr / tot_end_curr / tot_start_next).
`measure_grey_loss_from_sor_event` recomputes the loss from the raw trace using
those stored cursors, so it should match the event-table `splice_loss` exactly.

THE BUG (Stage 1 fix): the index mapping subtracted the pre-launch `first_pos_m`
offset, which shifted the fit windows ~1 km off the event.  Markers and the raw
trace share the OTDR's digitizer clock, so the correct index is simply
km / res_m with NO offset.  Empirically (Seattle, 2207 events): WITH the offset
median |err| 0.034 dB / 10% within 0.01; WITHOUT it median 0.002 dB / 91% within
0.01.  This test pins the no-offset frame and the EXFO-match on committed fixtures.

HARD RULE — namespace isolation (same as test_splicereport_validated_fixes):
the engine ships its OWN sor_reader324802a.py, so the behavioural check runs in a
CLEAN child subprocess with only SPLICEREPORT_DIR on sys.path; this process only
does static-source guards.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

from conftest import (SPLICEREPORT_DIR,
                      FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR)


def _run_engine_snippet(body: str):
    header = (
        "import sys\n"
        f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
        "import splicereportmatchexfo as E\n"
    )
    snippet = header + textwrap.dedent(body)
    p = subprocess.run([sys.executable, "-c", snippet],
                       capture_output=True, text=True)
    assert p.returncode == 0, (
        f"subprocess exited {p.returncode}\n"
        f"stdout:\n{p.stdout}\nstderr:\n{p.stderr}"
    )
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_stage1_static_marker_lsa_drops_launch_offset():
    """The marker-based EXFO-exact LSA must index markers AND the splice point
    in the untrimmed digitizer-clock frame (km / res_m) with NO pre-launch
    first_pos_m offset — applying that offset is the bug that made it disagree
    with EXFO's event-table value."""
    src = (SPLICEREPORT_DIR / "sor_reader324802a.py").read_text(encoding="utf-8")
    assert "splice_idx = event['dist_km'] * 1000.0 / res_m" in src, (
        "marker-based splice_idx must use the no-offset frame (km/res_m), not "
        "(km*1000 - first_pos_m)/res_m"
    )
    assert "return int(km_val * 1000.0 / res_m)" in src, (
        "km_to_idx must map the event's stored markers with no first_pos_m offset"
    )
    # Guard against silent reintroduction of the offset in the marker function.
    assert "(km_val * 1000.0 - first_pos_m)" not in src, (
        "the launch-offset marker mapping must be gone from sor_reader"
    )


def test_stage1_marker_lsa_reproduces_exfo_event_table():
    """On the committed MILELM fixtures, recomputing each non-reflective event's
    loss from EXFO's own stored cursors must reproduce the event-table
    splice_loss.  Pre-fix this was median ~0.034 dB / ~10% within 0.01 (fails);
    post-fix it is ~0.003 dB / >80% within 0.01 (passes)."""
    _run_engine_snippet(f"""
        import sor_reader324802a as S
        import glob, statistics
        errs = []
        for folder in ({str(FIXTURE_SPLICE_A_DIR)!r}, {str(FIXTURE_SPLICE_B_DIR)!r}):
            for fpath in sorted(glob.glob(folder + "/*.sor")):
                d = S.parse_sor_full(fpath, trim=False)
                if d is None:
                    continue
                d['_source'] = 'sor'
                d['events'] = E._normalize_untrimmed_events(d['events'])
                for e in d['events']:
                    if e.get('is_end') or e['dist_km'] < 1.0 or e.get('is_reflective'):
                        continue
                    v = S.measure_grey_loss_from_sor_event(d, e)
                    if v is not None:
                        errs.append(abs(v - e['splice_loss']))
        errs.sort()
        n = len(errs)
        assert n > 200, "too few events to validate: %d" % n
        median = statistics.median(errs)
        w01 = sum(1 for x in errs if x <= 0.01) / n
        w02 = sum(1 for x in errs if x <= 0.02) / n
        # Conservative thresholds: achieved ~0.0028 / 82%% / 98%% on fixtures;
        # the buggy (offset) version scored ~0.034 / 10%% and fails all three.
        assert median < 0.006, "marker-LSA median %.4f dB — not on EXFO's ruler" % median
        assert w01 >= 0.70, "only %.0f%% within 0.01 dB of EXFO" % (100 * w01)
        assert w02 >= 0.92, "only %.0f%% within 0.02 dB of EXFO" % (100 * w02)
        print("median=%.5f w01=%.2f w02=%.2f n=%d" % (median, w01, w02, n))
        print("OK")
    """)
