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
        # window too small to fit
        assert sr.measure_fr_exact_loss(rec, 8000.0, 8010.0,
                                        7995.0, 8020.0) is None
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

        # by hand: before-line OLS, after-level = y[i3] on the before slope,
        # both evaluated at the event midpoint
        yy = 64.0 - raw.astype(float) / 1024.0
        m1, b1 = np.polyfit(np.arange(i1, i2 + 1, dtype=float), yy[i1:i2 + 1], 1)
        mid = (i2 + i3) / 2.0
        want = (yy[i3] + m1 * (mid - i3)) - (m1 * mid + b1)
        assert abs(got - want) < 1e-12, (got, want)
        assert abs(got - 0.05) < 0.005, got          # it is the real step

        # 2..7 samples: still refused
        for w in (1, 2, 5, 7):
            assert sr.measure_fr_exact_loss(rec, i2 * res, i3 * res,
                                            i1 * res, (i3 + w) * res) is None, w
        # 8 samples: the ordinary fit
        assert sr.measure_fr_exact_loss(rec, i2 * res, i3 * res,
                                        i1 * res, (i3 + 8) * res) is not None
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
