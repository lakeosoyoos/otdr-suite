"""Pins for the Parker/Stephens (Zayo Dallas to Patricia Segment 2)
FastReporter comparison fixes: #315, #318, #320.

#315  run_splicereport.py Pass 0 stamps r['_span_side'] ('a' / 'b') as the
      engine's own main() does, so _fr_pick_terminal can tell A from B and
      picks A's terminal instead of min(end_a, end_b).
#318  scan_b_events measures a small B reading (under 0.75 x gate) at a
      closure when A has NO stored event there; it still skips it when A has
      its own event there, and in bend/damage columns.
#320  (D) scan_b_events no longer skips a fiber whose B record has no end
      marker.  (E) _trace_noise_db / _no_end_leg_is_noise refuse a
      silent-side leg read in trace noise on a record with no end marker.

Synthetic records only (no customer traces).  Engine runs in a clean
subprocess (3-engine sor_reader isolation).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT, FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"


def _run(body, args=()):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body),
                        *map(str, args)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    out = p.stdout.strip().splitlines()
    assert out and out[-1] == "OK", p.stdout + p.stderr


# ── #315 ──────────────────────────────────────────────────────────────────

def test_315_runner_pass0_stamps_span_side(tmp_path):
    """Drive the real runner main() on the fixture spans and look at the
    records analyze_all receives: every A record 'a', every B record 'b'."""
    _run("""
        import runpy, json
        seen = {}
        _orig = E.analyze_all
        def _capture(fa, fb, *a, **k):
            seen['a'] = sorted({str(r.get('_span_side')) for r in fa.values()})
            seen['b'] = sorted({str(r.get('_span_side')) for r in fb.values()})
            return _orig(fa, fb, *a, **k)
        E.analyze_all = _capture
        import run_splicereport as R
        sys.argv = ['run_splicereport.py', '--dir-a', sys.argv[1],
                    '--dir-b', sys.argv[2], '--out', sys.argv[3],
                    '--site-a', 'A', '--site-b', 'B']
        R.main()
        # The runner routes progress text to stderr; print on the real stdout.
        sys.stdout = sys.__stdout__
        assert seen, 'analyze_all was never reached'
        assert seen['a'] == ['a'], seen
        assert seen['b'] == ['b'], seen
        print('OK')
    """, args=(FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, tmp_path / "o.xlsx"))


def test_315_pick_terminal_uses_a_geometry_when_sides_are_stamped():
    """With sides stamped, the terminal nearest A's physical far end wins even
    when it is the larger one; with no stamp it falls back to min()."""
    _run("""
        E._cable_far_end_raw_m = lambda rec: 95000.0
        silent = {'_span_side': 'b'}
        loud = {'_span_side': 'a', '_trace_offset_km': 0.0}
        # t_s (B's) = 94950, t_l (A's) = 95010 -> A's is nearer 95000.
        got = E._fr_pick_terminal(silent, loud, 94950.0, 95010.0)
        assert got == 95010.0, got
        got = E._fr_pick_terminal({}, {}, 94950.0, 95010.0)
        assert got == 94950.0, got
        print('OK')
    """)


# ── #318 ──────────────────────────────────────────────────────────────────

_SMALL_B_SETUP = """
    SPAN = 100.0
    COL = 60.0
    def recs(a_has_event, kind='splice'):
        splices = [{'position_km': COL, 'column_kind': kind, 'count': 24}]
        a_ev = [{'dist_km': SPAN, 'splice_loss': 0.0, 'is_end': True,
                 'type': '1E'}]
        if a_has_event:
            a_ev.insert(0, {'dist_km': COL, 'splice_loss': 0.30,
                            'is_end': False, 'type': '0F'})
        fa = {5: {'_fnum': 5, 'events': a_ev}}
        fb = {5: {'_fnum': 5, 'events': [
            {'dist_km': SPAN - COL, 'splice_loss': 0.06, 'is_end': False,
             'type': '0F'},
            {'dist_km': SPAN, 'splice_loss': 0.0, 'is_end': True,
             'type': '1E'}]}}
        return splices, fa, fb
    # Fixed A leg: the Zayo Seg 2 entry closure read .25 to .38 on A.
    E._grey_loss = lambda fd, km, mirror=None, twin=None: 0.30 if fd else None
    E._phase2_loss = lambda rec, ev: ev['splice_loss']
    assert 0.06 < E.REBURN_THRESHOLD * 0.75
"""


def test_318_small_b_reading_measured_when_a_has_no_event():
    _run(_SMALL_B_SETUP + """
    splices, fa, fb = recs(a_has_event=False)
    res = E.scan_b_events(fa, fb, splices, E.REBURN_THRESHOLD, {}, SPAN)
    cell = res.get((5, 0))
    assert cell is not None, res
    assert abs(cell['bidir_loss'] - 0.18) < 1e-3, cell
    assert cell['event_source'] == 'bidir_grey_a', cell
    print('OK')
    """)


def test_318_small_b_reading_still_skipped_when_a_has_its_own_event():
    _run(_SMALL_B_SETUP + """
    splices, fa, fb = recs(a_has_event=True)
    res = E.scan_b_events(fa, fb, splices, E.REBURN_THRESHOLD, {}, SPAN)
    assert res == {}, res
    print('OK')
    """)


def test_318_small_b_reading_still_skipped_in_bend_and_damage_columns():
    _run(_SMALL_B_SETUP + """
    for kind in ('bend', 'damage'):
        splices, fa, fb = recs(a_has_event=False, kind=kind)
        res = E.scan_b_events(fa, fb, splices, E.REBURN_THRESHOLD, {}, SPAN)
        assert res == {}, (kind, res)
    print('OK')
    """)


# ── #320 D ────────────────────────────────────────────────────────────────

def test_320d_b_record_without_end_marker_is_scanned():
    """Fiber 734: A ends, B carries a 4.7 dB step and no end marker."""
    _run("""
        SPAN = 100.0
        COL = 60.0
        splices = [{'position_km': COL, 'column_kind': 'splice', 'count': 24}]
        fa = {734: {'_fnum': 734, 'events': [
            {'dist_km': SPAN, 'splice_loss': 0.0, 'is_end': True,
             'type': '1E'}]}}
        fb = {734: {'_fnum': 734, 'events': [
            {'dist_km': SPAN - COL, 'splice_loss': 0.40, 'is_end': False,
             'type': '0F'}]}}
        E._grey_loss = lambda fd, km, mirror=None, twin=None: 0.10 if fd else None
        E._phase2_loss = lambda rec, ev: ev['splice_loss']
        res = E.scan_b_events(fa, fb, splices, E.REBURN_THRESHOLD, {}, SPAN)
        cell = res.get((734, 0))
        assert cell is not None, res
        assert abs(cell['bidir_dist'] - COL) < 1e-6, cell
        assert abs(cell['bidir_loss'] - 0.25) < 1e-3, cell
        print('OK')
    """)


# ── #320 E ────────────────────────────────────────────────────────────────

_TRACE_SETUP = """
    import numpy as np
    N = 20000                      # 20 km at 1 m
    km = np.arange(N) / 1000.0
    clean = np.round((-0.2 * km) * 1024).astype(int)        # 0.2 dB/km
    rng = np.random.default_rng(0)
    noisy = np.round(rng.normal(0.0, 2.0, N) * 1024).astype(int)
    END = [{'dist_km': 20.0, 'splice_loss': 0.0, 'is_end': True, 'type': '1E'}]
    MID = [{'dist_km': 5.0, 'splice_loss': 0.1, 'is_end': False, 'type': '0F'}]
    def rec(raw, events):
        return {'exfo_raw': raw, 'exfo_res_m': 1.0, 'events': events}
"""


def test_320e_trace_noise_db_clean_vs_noise():
    _run(_TRACE_SETUP + """
    n_clean = E._trace_noise_db(rec(clean, MID), 10.0)
    n_noise = E._trace_noise_db(rec(noisy, MID), 10.0)
    assert n_clean is not None and n_clean < 0.05, n_clean
    assert n_noise is not None and n_noise > E.NO_END_LEG_NOISE_DB, n_noise
    assert E._trace_noise_db({'events': MID}, 10.0) is None
    print('OK')
    """)


def test_320e_no_end_leg_is_noise():
    _run(_TRACE_SETUP + """
    # Has an end marker: never refused, even on noise.
    assert E._no_end_leg_is_noise(rec(noisy, MID + END), 10.0) is False
    # No end marker, noise at km: refused.
    assert E._no_end_leg_is_noise(rec(noisy, MID), 10.0) is True
    # No end marker, healthy trace: kept.
    assert E._no_end_leg_is_noise(rec(clean, MID), 10.0) is False
    print('OK')
    """)
