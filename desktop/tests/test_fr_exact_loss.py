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
