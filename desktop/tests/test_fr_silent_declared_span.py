"""FR mode's silent-side loss on a .sor pair with a DECLARED span start.

When a tech (or our Viewer) sets the span start on the launch panel, EXFO
writes every event position relative to that panel while the samples still
start at the OTDR port, a reel length earlier.  measure_fr_section_loss has
always added that origin (_fr_origin_idx); the silent-side transplant did not,
so it fitted its windows a reel upstream of where they belong.

Lumen Span 7 fiber 229 is the case that found it.  A's Monument panel reads
4.787 dB.  At Splice 1 (2.03 km) only B sees an event, so A's loss is
measured from the trace, and the unshifted window straddled the panel:
5.408 dB, averaged with B's 0.048 to 2.728, a flagged cell on a clean
splice.  FastReporter, loading this same pair (2026-09-25), prints A -0.013
and an average of 0.017.

Engine tests run in a clean subprocess (three engines, three sor_reader
copies, never one process).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
FX = REPO_ROOT / "desktop" / "tests" / "fixtures" / "frspan_s7"


def _run(body):
    header = ("import sys, os\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import sor_reader324802a as sr\n"
              "import splicereportmatchexfo as E\n"
              f"FX = {str(FX)!r}\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_silent_legs_match_fastreporter_on_a_declared_span():
    _run("""
        A = sr.parse_sor_full(os.path.join(FX, 'MONGRA0229_1550.sor'), trim=False)
        B = sr.parse_sor_full(os.path.join(FX, 'GRAMON0229_1550.sor'), trim=False)
        for r, s in ((A, 'a'), (B, 'b')):
            r['_source'] = 'sor'; r['_span_side'] = s
        assert abs(A['user_offset_km'] - 1.0095) < 1e-3, A['user_offset_km']
        assert E._fr_origin_idx(A) > 0
        rows = {round(r['mean_pos_m'] / 1000.0, 1): r for r in E.fr_bidi_table(A, B)}
        # (km, silent leg, FR's silent leg, FR's average), read off FR's table
        for km, leg, fr_leg, fr_avg in ((2.0, 'a', -0.013, 0.017),
                                        (7.9, 'b', -0.035, 0.014),
                                        (25.3, 'a', 0.033, 0.117)):
            r = rows[km]
            assert r[leg]['synthetic'], (km, leg)
            assert round(r[leg]['loss'], 3) == fr_leg, (km, r[leg]['loss'], fr_leg)
            assert round(r['loss'], 3) == fr_avg, (km, r['loss'], fr_avg)
        print('OK')
    """)
