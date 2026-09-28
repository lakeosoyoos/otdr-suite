"""FastReporter's table on a BROKEN fiber, and its frame.

FR projects the B->A file into A's frame through one constant L (a B event
at raw p sits at L - p).  Back-solved from FR's own merged rows
(MeanPosition = (A + L - B) / 2) on every vendored .bdr key, L is the B->A
file's end-of-fibre event position, every time (92 keys checked, 47 vendored).
On a whole fiber _fr_proj_constant usually returns the same number, but on
five Las Cruces 5 ns panel fibers it is one sample short, so B's end marker
is the frame and the validated constant only its fallback.  On a broken fiber it
abstains -- there is no cable end to check against -- while FR carries on in
B's frame: WSC<->SUI fiber 230 dies 15.67 km from the A end, B's trace ends
48.38 km from its own, and FR's export prints the one B event at
48,380 - 37,622 = 10,758 m with A's leg synthesised there (A -0.007,
B -0.052, mean -0.029) and nothing past A's break: B's events beyond it
(16.06, 21.33, 32.66, 43.79 and 48.30 km mirrored, the last 18 m from
Splice 10's column) are absent from the export.

Engine tests run in a clean subprocess (three engines, three sor_reader
copies, never one process).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
BDR_DIR = REPO_ROOT / "desktop" / "tests" / "fixtures" / "bdr"
BROKEN_DIR = REPO_ROOT / "desktop" / "tests" / "fixtures" / "wsc_broken"


def _run(body):
    header = ("import sys, glob, os, math\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import sor_reader324802a as sr\n"
              "import splicereportmatchexfo as E\n"
              f"BDR = {str(BDR_DIR)!r}\n"
              f"BROKEN = {str(BROKEN_DIR)!r}\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_fr_projects_through_the_b_files_end_of_fibre_on_every_key():
    _run("""
        n = 0; validated_off = []
        for fp in sorted(glob.glob(os.path.join(BDR, '*.bdr'))):
            a = sr.parse_bdr_side(fp, 'a'); b = sr.parse_bdr_side(fp, 'b')
            Ls = [2 * m['Position'] - m['_ab']['Position'] + m['_ba']['Position']
                  for m in a['_bdr_merged'] if 'Type' in m and m.get('_ab') and m.get('_ba')]
            assert Ls, fp
            L_fr = sum(Ls) / len(Ls)
            assert max(abs(x - L_fr) for x in Ls) < 1e-6, (fp, min(Ls), max(Ls))
            b_end = E._fr_b_end_m(b)
            ours = E._fr_proj_constant(dict(a, _span_side='a'), dict(b, _span_side='b'))
            assert b_end is not None and abs(b_end - L_fr) < 1e-6, (fp, b_end, L_fr)
            if ours is None or abs(ours - L_fr) >= 1e-6:
                validated_off.append((os.path.basename(fp), None if ours is None else round(ours - L_fr, 4)))
            n += 1
        assert n == 51, n
        # the validated constant is one sample short on Las Cruces fiber 8 --
        # which is why B's end marker, not it, is the frame -- and abstains on
        # Tooele<->Knolls F241, whose two lists end a reel apart (a receive
        # reel on one end only); B's end marker is FR's frame there too
        assert validated_off == [('LSC1LSC60008_1550.bdr', -0.0797),
                                 ('TOOKNO_KNOTOO_0241_1550.bdr', None)], validated_off
        print('OK')
    """)


def test_a_broken_fiber_gets_fr_s_row_in_b_s_frame_and_stops_at_the_break():
    _run("""
        A = sr.parse_sor_full(os.path.join(BROKEN, 'WSC_SUI_0230.sor'), trim=False)
        B = sr.parse_sor_full(os.path.join(BROKEN, 'SUI_WSC_0230.sor'), trim=False)
        for r, s in ((A, 'a'), (B, 'b')):
            r['_source'] = 'sor'; r['_span_side'] = s
        # the validated constant abstains here; the B end marker is the frame
        assert E._fr_proj_constant(A, B) is None
        assert abs(E._fr_b_end_m(B) - 48379.7727) < 1e-3, E._fr_b_end_m(B)
        rows = E.fr_bidi_table(A, B)
        assert rows is not None and len(rows) == 3, [(r['mean_pos_m'], r['status']) for r in (rows or [])]
        launch, ev, end = rows
        assert launch['status'] == E.FR_ROW_LAUNCH and end['status'] == E.FR_ROW_END
        assert abs(end['mean_pos_m'] - 15667.725) < 1e-3, end['mean_pos_m']      # A's break
        assert abs(ev['mean_pos_m'] - 10757.859) < 1e-3, ev['mean_pos_m']
        assert ev['a']['synthetic'] and not ev['b']['synthetic']
        # FR's export cell for fiber 230: A -0.007, B -0.052, mean -0.029
        assert round(ev['a']['loss'], 3) == -0.007, ev['a']['loss']
        assert round(ev['b']['loss'], 3) == -0.052, ev['b']['loss']
        assert round(ev['loss'], 3) == -0.029, ev['loss']
        # and the report grid prints that one column for the fiber
        splices, results = E.fr_report_grid({230: A}, {230: B}, -9.0)
        assert [round(s['position_km'], 4) for s in splices] == [10.7579], splices
        assert round(results[(230, 0)]['bidir_loss'], 3) == -0.029
        print('OK')
    """)
