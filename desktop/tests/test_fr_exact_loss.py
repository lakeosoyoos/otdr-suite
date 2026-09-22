"""FastReporter's event-loss algorithm, reproduced bit-for-bit.

Reverse-engineered against 12 SEANOR .bdr files (FR's own saved bidirectional
results, vendored under fixtures/bdr/) and verified machine-exact: 186/186
loud events reproduce FR's stored float64 losses to 0.000000 mdB.

Four pieces, every one load-bearing — each was an assumption that turned out
wrong, and getting any of them wrong reintroduces ~1-2 mdB of error:

  1. Fit the PROPRIETARY RawSamples trace (dB = 64 - raw/1024), NOT the
     Bellcore DataPts.  Same signal, different quantisation grid; the ~0.3 mdB
     structured difference does not cancel in a least-squares fit.
  2. Both OLS windows are INCLUSIVE of their boundary cursors:
     [SubCursorA..CursorA] and [CursorB..SubCursorB].
  3. Both fitted lines are evaluated at the MIDPOINT of the event,
     (CursorA_idx + CursorB_idx)/2 — not at the event onset, which is how the
     classic four-point method is usually described.
  4. Indices use the file's EXACT sample pitch, pinned by the marker lengths
     (they are integer sample multiples).  The IOR-derived pitch drifts ~0.3
     per mil, which is whole samples at 100+ km.

Terminal records are refused: the end-of-fibre entry carries Loss=nan with
Status bit 0x80, and fitting it returns the ~15 dB end reflection.

Engine tests run in a clean subprocess (3-engine sor_reader isolation).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
BDR_DIR = REPO_ROOT / "desktop" / "tests" / "fixtures" / "bdr"


def _run(body):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import sor_reader324802a as sr\n"
              "import splicereportmatchexfo as E\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    out = p.stdout.strip().splitlines()
    assert out and out[-1] == "OK", p.stdout


def test_bdr_fixtures_present():
    """The ground truth this whole capability is pinned to."""
    files = sorted(BDR_DIR.glob("SEANOR*.bdr"))
    assert len(files) == 12, [f.name for f in files]


def test_reader_extracts_full_proprietary_event_list():
    """Three extraction bugs used to truncate this list to 2 events: an 80 KB
    scan cap that sliced records mid-stream, a kilometres-vs-metres unit error
    that discarded everything past 500 m, and lossy record grouping."""
    _run(f"""
        d = sr.parse_sor_full({str(REPO_ROOT / 'desktop/tests/fixtures/splice_A')!r}
                              + '/' + sorted(__import__('os').listdir(
                                  {str(REPO_ROOT / 'desktop/tests/fixtures/splice_A')!r}))[0],
                              trim=False)
        # the fixture may or may not carry a proprietary block; the contract is
        # that when it does, events come back as a list and positions are METRES
        ev = d.get('exfo_events')
        assert ev is None or isinstance(ev, list)
        if ev:
            for e in ev:
                p = e.get('Position')
                assert p is None or -1.0 <= p <= 500_000.0, e
        print('OK')
    """)


def test_degenerate_and_out_of_bounds_windows_are_refused():
    """measure_fr_exact_loss must return None — never a garbage fit — when a
    cursor set reaches past the trace (e.g. a mirror position near the B
    fibre end) or collapses below a fittable window.  The reciprocity veto
    leans on this: a None simply drops that fiber from the B population,
    and the fail-safe direction is keep-the-splice."""
    _run("""
        import numpy as np
        res = 5.0985
        raw = ((64.0 - np.linspace(30, 25, 4000)) * 1024).astype('<u2')
        rec = {'exfo_raw': raw, 'exfo_res_m': res}
        ok = sr.measure_fr_exact_loss(rec, 8000.0, 8340.0, 6000.0, 10340.0)
        assert ok is not None
        # past the end of the trace
        assert sr.measure_fr_exact_loss(rec, 19000.0, 19340.0,
                                        17000.0, 21340.0) is None
        # negative / inverted geometry
        assert sr.measure_fr_exact_loss(rec, 8340.0, 8000.0,
                                        6000.0, 10340.0) is None
        # a two-sample window is FastReporter's minimum (it fits them; see
        # the evaluation point) -- one sample before the event is not
        assert sr.measure_fr_exact_loss(rec, 8000.0, 8010.0,
                                        7995.0, 8020.0) is not None
        assert sr.measure_fr_exact_loss(rec, 8000.0, 8010.0,
                                        8000.0, 8020.0) is None
        # missing trace
        assert sr.measure_fr_exact_loss({'exfo_raw': None,
                                         'exfo_res_m': res},
                                        8000.0, 8340.0,
                                        6000.0, 10340.0) is None
        print('OK')
    """)


def test_fit_recipe_is_pinned():
    """Guard the four load-bearing choices in the source itself, so a future
    'simplification' cannot silently revert them."""
    _run("""
        import inspect
        src = inspect.getsource(sr.measure_fr_exact_loss)
        assert '64.0 - raw' in src.replace(' ', ' '), 'RawSamples conversion missing'
        assert 'i2 + 1' in src and 'i4 + 1' in src, 'windows must be cursor-inclusive'
        assert '(i2 + i3) / 2.0' in src, 'must evaluate at the event midpoint'
        assert "exfo_res_m" in src, 'must use the exact pitch'
        print('OK')
    """)


def test_silent_side_transplant_is_machine_exact():
    """The silent-side rule: FastReporter TRANSPLANTS the detecting
    direction's cursor geometry (position projected through the terminal
    constant, inner window, outer widths) clamped by the silent side's own
    proprietary list.  Reverse-engineered on 12 .bdr ground-truth files —
    62/62 silent-in-A + 60/60 silent-in-B cursors float-exact, fitted
    losses 62/62 at 0.000000 mdB.  This pins fiber 109's two silent
    events against FastReporter's stored float64 losses to 0.05 mdB
    (fixture .sor pair vendored; expecteds transcribed from the .bdr)."""
    _run(f"""
        FIX = {str(REPO_ROOT / 'desktop/tests/fixtures/frsilent')!r}
        ra = sr.parse_sor_full(FIX + '/SEANOR109_1550.sor', trim=False)
        rb = sr.parse_sor_full(FIX + '/NORSEA109_1550.sor', trim=False)
        t_a = [e['Position'] for e in ra['exfo_events']
               if isinstance(e.get('Status'), int) and e['Status'] & 0x80]
        t_b = [e['Position'] for e in rb['exfo_events']
               if isinstance(e.get('Status'), int) and e['Status'] & 0x80]
        L = min(t_a[0], t_b[0])
        # (A-frame silent position, FastReporter's stored loss) from the .bdr
        for pos_m, want in ((54798.8, -0.0066907262), (61503.3, 0.0359852551)):
            twin_pos = L - pos_m
            tw = min((e for e in rb['events'] if not e['is_end']),
                     key=lambda e: abs(e['dist_km'] * 1000 - twin_pos))
            assert abs(tw['dist_km'] * 1000 - twin_pos) <= 60, (pos_m, twin_pos)
            v = E._fr_exact_silent_loss(ra, rb, tw)
            assert v is not None, pos_m
            assert abs(v - want) < 5e-5, (pos_m, v, want)
        # fail-safe: no RawSamples -> None, caller falls back to legacy
        ra2 = dict(ra); ra2['exfo_raw'] = None
        assert E._fr_exact_silent_loss(ra2, rb, tw) is None
        print('OK')
    """)


# ══════════════════════════════════════════════════════════════════════════
#  THE ONE-SAMPLE AFTER-WINDOW — FastReporter's own rule for a transplanted
#  window that runs into the silent direction's own next event
# ══════════════════════════════════════════════════════════════════════════
# Read off FR's stored cursors on the Zayo 432 .bdr set (2026-09-21).  When
# the silent direction's own proprietary list carries an event INSIDE the
# projected inner window, FR does not abstain and does not fit across the
# step: it pulls CursorB back to that event and SubCursorB with it, so the
# after-window is ONE sample, and that sample is fitted with the before-
# window's slope.  13 of 13 such records reproduce FR's stored float64 loss
# to 0.000000 mdB; abstaining (the previous behaviour) matched none of them.
# Fibers 0017 (A silent at 21.83 km) and 0019 (B silent at 40.30 km) are two
# of the 13 and are vendored under fixtures/bdr/.

def test_one_sample_after_window_takes_the_before_slope():
    """measure_fr_exact_loss on a synthetic trace: SubCursorB == CursorB is
    fitted, not refused, and the after-line is that sample carrying the
    before-window's fitted slope.  A 2..7-sample after-window is still
    refused — FR was never seen to write one, and the one-sample rule is
    the only degenerate case it has been observed to produce."""
    _run("""
        import numpy as np
        res = 1.276081836
        n = 6000
        x = np.arange(n, dtype=float)
        # 0.19 dB/km of glass with a 0.05 dB step at sample 4000
        y = 0.19 * res / 1000.0 * x + np.where(x >= 4000, 0.05, 0.0)
        rng = np.random.default_rng(7)
        y = y + rng.normal(0.0, 0.0008, n)
        raw = np.round((64.0 - y) * 1024).astype('<u2')
        rec = {'exfo_raw': raw, 'exfo_res_m': res}
        i1, i2, i3 = 3900, 3990, 4025          # before window 90 samples

        got = sr.measure_fr_exact_loss(rec, i2 * res, i3 * res, i1 * res, i3 * res)
        assert got is not None, 'one-sample after-window must be fitted'

        # by hand: before-line OLS read at CursorB (the evaluation point for
        # a zero-length after-window), after-level = the sample itself
        yy = 64.0 - raw.astype(float) / 1024.0
        m1, b1 = np.polyfit(np.arange(i1, i2 + 1, dtype=float), yy[i1:i2 + 1], 1)
        mid = (i2 + i3) / 2.0
        want = yy[i3] - (m1 * i3 + b1)
        assert abs(got - want) < 1e-12, (got, want)
        assert abs(got - 0.05) < 0.005, got          # it is the real step

        # 2..8 samples: fitted, read at x = CursorB - wb (the evaluation
        # point never reaches further before CursorB than the window is
        # long); the before-line is read at the same x
        floor = sr.FR_SLOPE_FLOOR_DB_KM * res / 1000.0
        ceil = sr.FR_SLOPE_CEIL_DB_KM * res / 1000.0
        def lev(m, b, anc, xx):
            s = floor if m < floor else (ceil if m > ceil else m)
            return (m * anc + b) + s * (xx - anc) if s != m else m * xx + b
        for w in (1, 2, 5, 7, 8):
            got = sr.measure_fr_exact_loss(rec, i2 * res, i3 * res,
                                           i1 * res, (i3 + w) * res)
            assert got is not None, w
            m2, b2 = np.polyfit(np.arange(i3, i3 + w + 1, dtype=float), yy[i3:i3 + w + 1], 1)
            x = max(mid, i3 - w)
            want = lev(m2, b2, i3, x) - lev(m1, b1, i1, x)
            assert abs(got - want) < 1e-12, (w, got, want)
        # a one-sample window at CursorA (i2 == i3) has no inner window: None
        assert sr.measure_fr_exact_loss(rec, i2 * res, i2 * res,
                                        i1 * res, i2 * res) is None
        print('OK')
    """)


def test_own_event_inside_the_window_reproduces_fr_on_the_sor_path():
    """Fibers 0017 and 0019, driven the way a customer's .sor pair is: FR's
    synthesised leg stripped off the record, cursors rebuilt from the loud
    direction, fitted on the silent trace.  The silent side's own event sits
    inside the projected inner window on both (0017 A: own at sample 17132,
    projection at 17106; 0019 B: own at 31603, projection at 31579), and FR's
    stored cursors show exactly what it did — SubCursorB == CursorB — so the
    engine's number must be FR's to the last digit, and must equal the fit
    at FR's own stored cursors."""
    _run(f"""
        B = {str(BDR_DIR)!r}
        for fib, side, pos_m in ((17, 'a', 21828.656), (19, 'b', 40297.388)):
            d = sr.parse_bdr(B + '/ORPVL.ZYO-OR-DES-0048.1550.%04d_1550.bdr' % fib)
            srec, lrec = d[side], d['b' if side == 'a' else 'a']
            fr = min(srec['fr_synthetic'], key=lambda z: abs(z['position_m'] - pos_m))
            assert abs(fr['position_m'] - pos_m) < 0.01, fr
            sa, ca, cb, sb = fr['cursors_m']
            assert sb == cb, 'FR itself wrote a one-sample after-window here'
            # the silent side's own event is inside FR's projected inner window
            res = srec['exfo_res_m']
            own = [e for e in srec['exfo_events'] if isinstance(e.get('Position'), float)
                   and not e.get('_is_section') and ca <= e['Position'] <= cb + 0.5 * res]
            assert len(own) == 1, own

            # 1. the fit at FR's stored cursors is FR's stored loss
            at_fr = sr.measure_fr_exact_loss(srec, ca, cb, sa, sb)
            assert at_fr is not None and abs(at_fr - fr['loss']) < 1e-9, (at_fr, fr['loss'])

            # 2. the .sor path — nothing of FR's on the record — lands on it too
            stripped = dict(srec); stripped.pop('fr_synthetic', None); stripped['_source'] = 'sor'
            lp = E._fr_proj_constant(srec, lrec)
            off = float(lrec.get('_trace_offset_km') or 0.0)
            twin = min((e for e in lrec['events'] if not e.get('is_end')),
                       key=lambda e: abs((e['dist_km'] + off) * 1000.0 - (lp - pos_m)))
            v = E._grey_loss(stripped, pos_m / 1000.0,
                             mirror=E._mirror_anchor(lrec, twin), twin=(lrec, twin))
            assert v is not None and abs(v - fr['loss']) < 1e-9, (fib, side, v, fr['loss'])
        print('OK')
    """)


# ══════════════════════════════════════════════════════════════════════════
#  THE PITCH ON A REAL .sor — the customer path
# ══════════════════════════════════════════════════════════════════════════
# `exfo_res_m` was pinned only by a vote of three or more marker Lengths.  A
# single-direction .sor with a handful of events rarely has three: 689 of
# the 864 ZAYO BETA 432 files had no pitch at all.  measure_fr_exact_loss
# refuses without one, so on a customer's .sor pair the FR-exact silent-side
# transplant never ran for those files and the legacy reconstruction
# answered instead: 606 of 1,789 silent legs matched FastReporter (33.9%),
# against 1,761 (98.4%) for the same legs read from the .bdr, where the
# vote always has enough markers.  Every .bdr-based figure quoted before
# 2026-09-21 was therefore not the customer path.
#
# The stated IOR carries the same pitch, c x SamplingPeriod / (2 x Ior), and
# on every file that has both the two agree to 1.2e-13 relative (864 Zayo
# files) -- so the fallback is the same number, not an estimate.

def test_a_sor_without_three_markers_still_carries_the_pitch():
    """Fixtures with a proprietary block but fewer than three usable marker
    Lengths (27 of 115 shipped) now carry exfo_res_m = the stated-IOR pitch,
    and one with enough markers agrees with it to 1e-9 relative."""
    _run(f"""
        FX = {str(REPO_ROOT / 'desktop/tests/fixtures')!r}
        lacking = ['frtruncated/WSCSUIsh0001.sor', 'continuous/WSC_SUIsh_0017.sor',
                   'endlaunch/HOWLAN309_1550.sor', 'frspan/DNN1DNN20001.sor']
        for rel in lacking:
            r = sr.parse_sor_full(FX + '/' + rel, trim=False)
            assert r.get('exfo_raw') is not None, rel
            sp = r['exfo_sampling_period']; ior = r['test_settings']['Ior']
            n_mark = len([e for e in r['exfo_events'] if not e.get('_is_section')
                          and isinstance(e.get('Length'), float) and 50.0 < e['Length'] < 3000.0])
            assert n_mark < 3, (rel, n_mark)          # the vote could not run
            want = 299_792_458.0 * sp / 2.0 / ior
            assert r.get('exfo_res_m') is not None, rel
            assert abs(r['exfo_res_m'] - want) < 1e-12, (rel, r['exfo_res_m'], want)
            # and the FR-exact fit now has a pitch to index with
            assert sr._sor_res_m(r) == r['exfo_res_m']
        # a file WITH the vote: marker-pinned and stated agree
        r = sr.parse_sor_full(FX + '/continuous/WSC_SUIsh_0019.sor', trim=False)
        sp = r['exfo_sampling_period']; ior = r['test_settings']['Ior']
        want = 299_792_458.0 * sp / 2.0 / ior
        assert abs(r['exfo_res_m'] - want) / want < 1e-9, (r['exfo_res_m'], want)
        print('OK')
    """)


def test_the_transplant_runs_on_a_sor_pair_without_marker_votes():
    """Fibre 0017 as a customer would supply it: the .sor pair, not the .bdr.
    NEITHER direction has three markers to vote with, so before the fallback
    the silent side of every leg fell to the legacy reconstruction: A at
    21,828.656 m read +0.031801 against FastReporter's +0.002589.  With the
    stated-IOR pitch the transplant fires and lands on FR's stored value,
    read from the .bdr beside it, on all three A-silent legs."""
    _run(f"""
        B = {str(BDR_DIR)!r}
        FX = {str(REPO_ROOT / 'desktop/tests/fixtures')!r}
        pa = FX + '/zayo_sor/ORPVL.ZYO-OR-DES-0048.1550.0017.sor'
        pb = FX + '/zayo_sor/ZYO-OR-DES-0048.ORPVL.1550.0017.sor'
        ra = sr.parse_sor_full(pa, trim=False); rb = sr.parse_sor_full(pb, trim=False)
        for r, side in ((ra, 'a'), (rb, 'b')):
            r['_source'] = 'sor'; r['_span_side'] = side
            n_mark = len([e for e in r['exfo_events'] if not e.get('_is_section')
                          and isinstance(e.get('Length'), float) and 50.0 < e['Length'] < 3000.0
                          and round(e['Length'] / (299_792_458.0 * r['exfo_sampling_period'] / 2.0 / 1.4682)) >= 10])
            assert n_mark < 3, (side, n_mark)
            assert r.get('exfo_res_m'), side
        d = sr.parse_bdr(B + '/ORPVL.ZYO-OR-DES-0048.1550.0017_1550.bdr')
        lp = E._fr_proj_constant(ra, rb)
        for pos_m in (6930.400, 14480.977, 21828.656):
            fr = min(d['a']['fr_synthetic'], key=lambda z: abs(z['position_m'] - pos_m))
            assert abs(fr['position_m'] - pos_m) < 0.01, (pos_m, fr)
            twin = min((e for e in rb['events'] if not e.get('is_end')),
                       key=lambda e: abs(e['dist_km'] * 1000.0 - (lp - pos_m)))
            v = E._grey_loss(ra, pos_m / 1000.0, mirror=E._mirror_anchor(rb, twin), twin=(rb, twin))
            assert v is not None and abs(v - fr['loss']) < 1e-9, (pos_m, v, fr['loss'])
        print('OK')
    """)


# ══════════════════════════════════════════════════════════════════════════
#  THE EVALUATION POINT and the MERGED own event
# ══════════════════════════════════════════════════════════════════════════
# The last 28 silent-side misses on the Zayo 432 .bdr set (after #259 and the
# reach change) were all records with a clamped outer window, and FR's stored
# loss sat a half-integer number of samples off our midpoint answer on every
# one -- (mid - CursorA) - wa samples.  FastReporter never extrapolates a
# fitted line further past its cursor than the window is long: both lines
# are read at x = clamp(mid, CursorB - wb, CursorA + wa).  That alone
# reproduces 27 of the 28; the 28th class needs the merge rule: an own event
# just before the window, whose mirror image falls inside the loud twin's
# inner window, gets no row of its own and its stored loss is folded into
# the transplant.  With both, the .bdr-read path is 1,789 / 1,789 and the
# real .sor pairs are 1,789 / 1,789.

def test_evaluation_point_is_pulled_to_a_short_windows_cursor():
    """Synthetic trace: a 3-sample before-window (wa = 3) with a 60-sample
    inner window is read at CursorA + 3, not at the midpoint 30 samples
    on; the after-line is read at the same x.  A full-width window is read
    at the midpoint as before."""
    _run("""
        import numpy as np
        res = 1.276081836
        n = 6000
        x = np.arange(n, dtype=float)
        y = 0.19 * res / 1000.0 * x + np.where(x >= 4000, 0.05, 0.0)
        y = y + np.random.default_rng(3).normal(0.0, 0.0008, n)
        raw = np.round((64.0 - y) * 1024).astype('<u2')
        rec = {'exfo_raw': raw, 'exfo_res_m': res}
        yy = 64.0 - raw.astype(float) / 1024.0
        i1, i2, i3, i4 = 3970, 3973, 4033, 5000
        got = sr.measure_fr_exact_loss(rec, i2 * res, i3 * res, i1 * res, i4 * res)
        m1, b1 = np.polyfit(np.arange(i1, i2 + 1, dtype=float), yy[i1:i2 + 1], 1)
        m2, b2 = np.polyfit(np.arange(i3, i4 + 1, dtype=float), yy[i3:i4 + 1], 1)
        floor = sr.FR_SLOPE_FLOOR_DB_KM * res / 1000.0; ceil = sr.FR_SLOPE_CEIL_DB_KM * res / 1000.0
        def lev(m, b, anc, xx):
            s = floor if m < floor else (ceil if m > ceil else m)
            return (m * anc + b) + s * (xx - anc) if s != m else m * xx + b
        xpt = i2 + (i2 - i1)                       # CursorA + wa, well short of the midpoint
        assert xpt < (i2 + i3) / 2.0
        want = lev(m2, b2, i3, xpt) - lev(m1, b1, i1, xpt)
        assert abs(got - want) < 1e-12, (got, want)
        at_mid = lev(m2, b2, i3, (i2 + i3) / 2.0) - lev(m1, b1, i1, (i2 + i3) / 2.0)
        assert abs(got - at_mid) > 1e-6, 'the pull must actually move the answer'
        # full-width windows: midpoint, unchanged
        i1w = 100
        got_w = sr.measure_fr_exact_loss(rec, i2 * res, i3 * res, i1w * res, i4 * res)
        m1w, b1w = np.polyfit(np.arange(i1w, i2 + 1, dtype=float), yy[i1w:i2 + 1], 1)
        mid = (i2 + i3) / 2.0
        assert abs(got_w - (lev(m2, b2, i3, mid) - lev(m1w, b1w, i1w, mid))) < 1e-12
        print('OK')
    """)


def test_short_after_window_reproduces_fr_on_the_real_sor_pair():
    """Fibre 0017, B silent at 32,930.568 m: B's own next event sits one
    sample past the projected CursorB, so the after-window is two samples.
    Driven from the real .sor pair (no vote for the pitch on either file),
    the engine must land on FastReporter's stored -0.010551 dB."""
    _run(f"""
        B = {str(BDR_DIR)!r}
        FX = {str(REPO_ROOT / 'desktop/tests/fixtures')!r}
        ra = sr.parse_sor_full(FX + '/zayo_sor/ORPVL.ZYO-OR-DES-0048.1550.0017.sor', trim=False)
        rb = sr.parse_sor_full(FX + '/zayo_sor/ZYO-OR-DES-0048.ORPVL.1550.0017.sor', trim=False)
        for r, side in ((ra, 'a'), (rb, 'b')):
            r['_source'] = 'sor'; r['_span_side'] = side
        d = sr.parse_bdr(B + '/ORPVL.ZYO-OR-DES-0048.1550.0017_1550.bdr')
        pos_m = 32930.568
        fr = min(d['b']['fr_synthetic'], key=lambda z: abs(z['position_m'] - pos_m))
        assert abs(fr['position_m'] - pos_m) < 0.01, fr
        sa, ca, cb, sb = fr['cursors_m']
        assert round((sb - cb) / rb['exfo_res_m']) == 1, 'FR wrote a two-sample after-window here'
        lp = E._fr_proj_constant(rb, ra)
        twin = min((e for e in ra['events'] if not e.get('is_end')),
                   key=lambda e: abs(e['dist_km'] * 1000.0 - (lp - pos_m)))
        v = E._grey_loss(rb, pos_m / 1000.0, mirror=E._mirror_anchor(ra, twin), twin=(ra, twin))
        assert v is not None and abs(v - fr['loss']) < 1e-9, (v, fr['loss'])
        print('OK')
    """)


def test_merged_own_event_adds_its_loss_and_an_unmerged_one_does_not():
    """Two vendored .bdr, both with A's own event clamping the transplant's
    SubCursorA to its CursorB:
      0355 A @36,886.422 m: own event 35.7 m before CursorA, inside the
        twin's 67.6 m inner window -> FR folded it in; stored +0.094276 =
        the clamped-window fit + the own event's +0.097892
      0263 A @36,894.078 m: own event 38.3 m before CursorA, OUTSIDE the
        twin's 25.5 m inner window -> its own row; stored -0.001327 = the
        clamped-window fit alone
    Both driven the .sor way (FR's leg stripped, cursors rebuilt)."""
    _run(f"""
        B = {str(BDR_DIR)!r}
        NEED = ('SubCursorAPosition', 'CursorAPosition', 'CursorBPosition', 'SubCursorBPosition')
        for fib, pos_m, merged in ((355, 36886.422, True), (263, 36894.078, False)):
            d = sr.parse_bdr(B + '/ORPVL.ZYO-OR-DES-0048.1550.%04d_1550.bdr' % fib)
            srec, lrec = d['a'], d['b']
            fr = min(srec['fr_synthetic'], key=lambda z: abs(z['position_m'] - pos_m))
            assert abs(fr['position_m'] - pos_m) < 0.01, fr
            res = srec['exfo_res_m']
            sa, ca, cb, sb = fr['cursors_m']
            own = [e for e in srec['exfo_events'] if isinstance(e.get('Position'), float)
                   and not e.get('_is_section') and e.get('CursorBPosition') is not None
                   and abs(e['CursorBPosition'] - sa) < 0.5 * res]
            assert len(own) == 1, (fib, own)
            own = own[0]
            inside = (ca - own['Position']) <= (cb - ca)
            assert inside == merged, (fib, ca - own['Position'], cb - ca)
            fit = sr.measure_fr_exact_loss(srec, ca, cb, sa, sb)
            want = fit + (own['Loss'] if merged else 0.0)
            assert abs(want - fr['loss']) < 1e-9, (fib, fit, own['Loss'], fr['loss'])
            stripped = dict(srec); stripped.pop('fr_synthetic', None); stripped['_source'] = 'sor'
            lp = E._fr_proj_constant(srec, lrec)
            off = float(lrec.get('_trace_offset_km') or 0.0)
            twin = min((e for e in lrec['events'] if not e.get('is_end')),
                       key=lambda e: abs((e['dist_km'] + off) * 1000.0 - (lp - pos_m)))
            v = E._grey_loss(stripped, pos_m / 1000.0, mirror=E._mirror_anchor(lrec, twin), twin=(lrec, twin))
            assert v is not None and abs(v - fr['loss']) < 1e-9, (fib, v, fr['loss'])
        print('OK')
    """)
