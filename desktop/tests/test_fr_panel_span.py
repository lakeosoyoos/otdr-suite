"""FastReporter's table on a 5 ns tie-panel span.

Las Cruces LSC1<->LSC6 (48 pairs, FR's own .bdr keys made 2026-09-23): a
1 km launch reel, the launch connector, the end connector 31.5 m on, and a
1 km receive reel beyond.  FR's table is the launch row and the end row and
nothing else; this table now reproduces all 48 from the .bdr keys and from
the original .sor files, rows and every section leg.  Three things had to be
right, none of which a WSC, Zayo or SEANOR key ever exercised:

  * the table STARTS at the launch.  B events past B's own end marker (the
    receive reel's far end) mirror to about -1,019 m, and on fiber 2 a small
    B event mirrors to -4.7 m; FR keeps neither.
  * the frame is B's end marker.  The validated projection constant is one
    sample short on five of the 48 (fiber 8 among them), which put their
    launch row at -0.04 m where FR prints 0.
  * the samples start at the OTDR port, 1,028.3 m before position 0.  The
    tech set the span start on the launch connector, so every position is
    short of its sample by the reel; FR's section fit reads the samples
    after the reel, and without the origin the fit read 12,908 samples early
    (0.0255 dB where FR stores 0.0052).

Engine tests run in a clean subprocess (three engines, three sor_reader
copies, never one process).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
BDR_DIR = REPO_ROOT / "desktop" / "tests" / "fixtures" / "bdr"
PANEL_DIR = REPO_ROOT / "desktop" / "tests" / "fixtures" / "lsc_panel"


def _run(body):
    header = ("import sys, os, math\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import sor_reader324802a as sr\n"
              "import splicereportmatchexfo as E\n"
              f"BDR = {str(BDR_DIR)!r}\n"
              f"PANEL = {str(PANEL_DIR)!r}\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_the_sor_pair_gives_fr_s_two_rows_and_its_section():
    # FR's key for this pair is vendored as LSC1LSC60002_1550.bdr; the .sor
    # files are the tech's originals (their own fiber ids, the declared span
    # start in GenParams)
    _run("""
        A = sr.parse_sor_full(os.path.join(PANEL, 'LSC1LSC60002_1550.sor'), trim=False)
        B = sr.parse_sor_full(os.path.join(PANEL, 'LSC6LSC10002_1550.sor'), trim=False)
        for r, s in ((A, 'a'), (B, 'b')):
            r['_source'] = 'sor'; r['_span_side'] = s
        assert abs(A['user_offset_km'] - 1.0283) < 1e-3, A['user_offset_km']
        key = sr.parse_bdr_side(os.path.join(BDR, 'LSC1LSC60002_1550.bdr'), 'a')
        # the .sor's declared span start and the key's integer cursors agree
        # on where position 0 sits: after ~12,900 samples of launch reel
        assert E._fr_origin_idx(A) == E._fr_origin_idx(key) and E._fr_origin_idx(A) > 12000, \
            (E._fr_origin_idx(A), E._fr_origin_idx(key))
        fr = [m for m in key['_bdr_merged'] if 'Type' in m]
        sec = [m for m in key['_bdr_merged'] if 'Type' not in m]
        rows = E.fr_bidi_table(A, B)
        # launch and end, nothing ahead of the launch (no -1,019 m reel end,
        # no -4.7 m phantom), nothing past the end
        assert [r['status'] for r in rows] == [E.FR_ROW_LAUNCH, E.FR_ROW_END], [(r['mean_pos_m'], r['status']) for r in rows]
        for m, r in zip(fr, rows):
            assert abs(m['Position'] - r['mean_pos_m']) < 1e-6, (m['Position'], r['mean_pos_m'])
            assert abs(m['Loss'] - r['loss']) < 1e-6, (m['Loss'], r['loss'])
        s = rows[0]['section']
        assert len(sec) == 1 and s is not None
        assert abs(sec[0]['_ab']['Loss'] - s['a']['loss']) < 1e-9, (sec[0]['_ab']['Loss'], s['a']['loss'])
        assert abs(sec[0]['_ba']['Loss'] - s['b']['loss']) < 1e-9, (sec[0]['_ba']['Loss'], s['b']['loss'])
        print('OK')
    """)


def test_no_origin_on_the_long_spans():
    # every other vendored key has its position 0 on sample 0
    _run("""
        import glob
        for fp in sorted(glob.glob(os.path.join(BDR, '*.bdr'))):
            for side in ('a', 'b'):
                o = E._fr_origin_idx(sr.parse_bdr_side(fp, side))
                if os.path.basename(fp).startswith('LSC'):
                    assert o > 12000, (fp, side, o)
                else:
                    assert o == 0, (fp, side, o)
        print('OK')
    """)
