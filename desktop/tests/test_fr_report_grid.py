"""FastReporter mode's Splice Report: FR's columns, FR's numbers, our gates.

`fr_report_grid` turns fr_bidi_table's rows for every fiber into the classic
(splices, results) shapes: rows clustered across the cable into columns, a
cell wherever FR's merged loss clears the tech's gate.  The runner takes that
branch instead of the classic discovery / analysis chain when --analysis fr.
OTDR Suite mode is untouched, and pinned here on the same fixture.

Engine tests run in a clean subprocess (three engines, three sor_reader
copies, never one process).
"""
import json
import os
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
RUNNER = SPLICEREPORT_DIR / "run_splicereport.py"
FIX_A = REPO_ROOT / "desktop" / "tests" / "fixtures" / "splice_A"
FIX_B = REPO_ROOT / "desktop" / "tests" / "fixtures" / "splice_B"


def _run(body):
    header = ("import sys, math\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


FAKE_TABLE = """
    # fr_bidi_table stands in: a fixed table per fiber, in metres
    def leg(loss, synthetic=False, refl=None):
        return {'loss': loss, 'synthetic': synthetic, 'refl': refl, 'pos_m': 0.0}
    def row(pos_m, loss, typ=2, status=0, a=None, b=None, refl=None):
        a = a if a is not None else leg(loss); b = b if b is not None else leg(loss)
        return {'mean_pos_m': pos_m, 'loss': loss, 'type': typ, 'status': status,
                'refl': refl, 'a': a, 'b': b, 'section': None, 'length_m': 0.0}
    TABLES = {}
    E.fr_bidi_table = lambda ra, rb: TABLES.get(ra['fnum'])
    E._pulse_length_m = lambda r: 10.0          # pulse 10 m -> tolerance floors at 20 m
    def recs(fibers, off=0.0):
        return ({f: {'fnum': f, '_trace_offset_km': off} for f in fibers},
                {f: {'fnum': f} for f in fibers})
"""


def test_columns_cells_and_gates_on_a_fabricated_cable():
    _run(textwrap.dedent(FAKE_TABLE) + textwrap.dedent("""
        TABLES[1] = [row(0.0, None, 3, 64), row(5000.0, 0.170), row(12000.0, 0.050),
                     row(20000.0, 0.7, 3, 0, refl=-45.2), row(30000.0, None, 3, 128)]
        TABLES[2] = [row(0.0, None, 3, 64), row(5015.0, 0.1595), row(12030.0, -0.161),
                     row(20010.0, 0.3, 3, 0, refl=-52.0), row(30000.0, None, 3, 128)]
        TABLES[3] = [row(0.0, None, 3, 64), row(5100.0, 0.2, a=leg(0.3), b=leg(0.1, True)),
                     row(30000.0, None, 3, 128)]
        fa, fb = recs([1, 2, 3, 4])           # 4 has no table -> contributes nothing
        splices, results = E.fr_report_grid(fa, fb, 0.160)
        # launch and end rows are not columns; the 5 km rows of 1 and 2 share one
        # column (15 m apart), fiber 3's at 5.1 km is its own (100 m off, > 20 m)
        assert [(sp['position_km'], sp['column_kind']) for sp in splices] == [
            (5.0075, 'splice'), (5.1, 'splice'), (12.015, 'splice'), (20.005, 'connector')], splices
        assert [sp['fr_rows'] for sp in splices] == [2, 1, 2, 2]
        # the gate is on the PRINTED number: 0.1595 prints .160 and flags
        assert set(results) == {(1, 0), (2, 0), (3, 1), (2, 2), (1, 3)}, sorted(results)
        assert results[(1, 0)]['label'] == '1 .170' and results[(2, 0)]['label'] == '2 .160'
        assert results[(2, 2)]['is_gainer'] and results[(2, 2)]['label'] == '2 -.161'
        # a synthesised leg is named, never hidden
        assert results[(3, 1)]['fr_synthetic'] == 'b' and results[(3, 1)]['a_loss'] == 0.3
        # the connector column is judged at BIDIR_CONNECTOR_LOSS, and prints its reflectance
        assert results[(1, 3)]['event_source'] == 'connector' and results[(1, 3)]['is_ref']
        assert results[(1, 3)]['label'] == '1 .700 REFL-45.2dB'
        assert (2, 3) not in results          # 0.3 dB is under the connector gate
        for r in results.values():
            assert r['is_flagged'] and not r['is_break'] and not r['is_broke'] and not r['is_bend']
        print('OK')
    """))


def test_positions_are_in_the_report_frame_and_empty_input_is_empty():
    _run(textwrap.dedent(FAKE_TABLE) + textwrap.dedent("""
        TABLES[1] = [row(0.0, None, 3, 64), row(6000.0, 0.2), row(30000.0, None, 3, 128)]
        fa, fb = recs([1], off=1.0)             # a 1 km launch reel
        splices, results = E.fr_report_grid(fa, fb, 0.160)
        assert [sp['position_km'] for sp in splices] == [5.0]
        assert abs(results[(1, 0)]['bidir_dist'] - 5.0) < 1e-9
        assert E.fr_report_grid({}, {}, 0.160) == ([], {})
        assert E.fr_report_grid({1: {'fnum': 1}}, {}, 0.160) == ([], {})   # no B
        print('OK')
    """))


def _runner(analysis, out):
    p = subprocess.run([sys.executable, str(RUNNER), "--dir-a", str(FIX_A), "--dir-b", str(FIX_B),
                        "--out", out, "--analysis", analysis], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout.strip().splitlines()[-1]), p.stderr


def test_the_runner_prints_fr_s_grid_in_fr_mode_and_ours_otherwise(tmp_path):
    fr, err = _runner("fr", str(tmp_path / "fr.xlsx"))
    assert fr["ok"] and fr["analysis_mode"] == "fr"
    assert "FastReporter mode: building FR's bidirectional table" in err
    assert "distributed-loss pass skipped" in err
    kinds = [c["kind"] for c in fr["columns"]]
    # ELMMIL: a reel connector at each end, FR's splice rows between
    assert len(fr["columns"]) == 16 and kinds[0] == "connector" and kinds[-1] == "connector"
    assert kinds.count("splice") == 14 and "bend" not in kinds and "damage" not in kinds
    assert [c["num"] for c in fr["columns"] if c["kind"] == "splice"] == list(range(1, 15))
    # one cell clears the 0.160 gate on FR's numbers: fiber 20 at splice 14 (61.5 km)
    assert [(c["fiber"], c["km"], c["loss"], c["category"]) for c in fr["cells"]] == \
        [(20, 61.5109, 0.2, "reburn")]
    assert fr["n_flagged"] == 1 and fr["n_distributed_loss"] == 0
    assert os.path.getsize(tmp_path / "fr.xlsx") > 5000
    # OTDR Suite mode: the report it always produced
    suite, err = _runner("suite", str(tmp_path / "suite.xlsx"))
    assert suite["ok"] and suite["analysis_mode"] == "suite"
    assert "FastReporter mode" not in err
    assert [(c["km"], c["kind"]) for c in suite["columns"]] == [
        (14.5588, 'splice'), (19.6598, 'bend'), (21.2862, 'splice'), (32.5399, 'bend'),
        (47.0528, 'splice'), (52.6791, 'bend'), (55.5814, 'bend'), (61.5084, 'splice')]
    assert len(suite["cells"]) == 9
