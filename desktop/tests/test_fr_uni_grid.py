"""Uni in FastReporter mode: FR's event list is the column set, the tech's
gates are the verdict.

`fr_uni_columns` clusters every fiber's proprietary events into columns
(splice at UNI_MIN_POP_SPLICE fibers, bend/damage below it, connector for
reflective rows, FR's launch and end rows left out) and hands each column
FR's own float64 loss per fiber; `uni_build_ribbon_grid` gates those at
UNI_BEND_THRESHOLD like the classic events.  Breaks and the Cable End stay
the classic columns.  OTDR Suite mode is untouched, pinned on the same
fixture.  Engine tests run in a clean subprocess.
"""
import json
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
RUNNER = SPLICEREPORT_DIR / "run_splicereport.py"
FIX_A = REPO_ROOT / "desktop" / "tests" / "fixtures" / "splice_A"


def _run(body):
    header = ("import sys, math\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n")
    p = subprocess.run([sys.executable, "-c", header + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_columns_members_and_gates_on_a_fabricated_cable():
    _run("""
        E._pulse_length_m = lambda r: 10.0            # tolerance floors at 20 m
        E.UNI_MIN_POP_SPLICE = 3
        def ev(pos_m, loss, typ=2, status=0, refl=float('nan')):
            return {'Position': float(pos_m), 'Loss': loss, 'Type': typ, 'Status': status, 'Reflectance': refl}
        fibers = {}
        for f in (1, 2, 3, 4):
            evs = [ev(0.0, float('nan'), 3, 72), ev(5000 + 3 * f, 0.30 if f == 2 else 0.05),
                   ev(30000.0, float('nan'), 3, 132)]
            if f < 3:
                evs.append(ev(12000 + f, 0.12))                       # two fibers: bend/damage
            if f == 4:
                evs.append(ev(20000.0, 0.9, 3, 0, -45.0))             # one reflective, over the gate
            fibers[f] = {'exfo_events': evs, '_trace_offset_km': 1.0, 'events': []}
        cols = E.fr_uni_columns(fibers)
        # columns stand at the founding fiber's own event position (FR's layout)
        assert [(round(c['position_km_refined'], 4), c['kind']) for c in cols] == [
            (4.003, 'splice'), (11.001, 'bend_damage'), (19.0, 'connector')], cols
        assert cols[0]['fiber_count'] == 4 and set(cols[0]['fr_members']) == {1, 2, 3, 4}
        assert cols[0]['fr_members'][2] == 0.30 and cols[1]['fr_members'] == {1: 0.12, 2: 0.12}
        assert cols[2]['conn_all'] == {4: 0.9} and cols[2]['conn_members'] == {4: 0.9}
        assert cols[2]['conn_refl'] == {4: -45.0} and cols[2]['is_launch'] is False
        # the grid judges FR's loss at the tech's gate: 0.30 flags at 0.250, 0.12 and 0.05 do not
        grid = E.uni_build_ribbon_grid(fibers, cols, 12)
        assert dict(grid) == {(0, 0): [(2, 0.30)], (0, 2): [(4, 0.9)]}, dict(grid)
        E.UNI_BEND_THRESHOLD = 0.100
        grid = E.uni_build_ribbon_grid(fibers, cols, 12)
        assert grid[(0, 1)] == [(1, 0.12), (2, 0.12)] and grid[(0, 0)] == [(2, 0.30)]
        assert E.fr_uni_columns({}) == []
        print('OK')
    """)


def _uni(mode, out):
    p = subprocess.run([sys.executable, str(RUNNER), "--dir-a", str(FIX_A), "--uni",
                        "--out", out, "--analysis", mode], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-1500:]
    return json.loads(p.stdout.strip().splitlines()[-1]), p.stderr


def test_the_runner_prints_fr_s_columns_in_fr_mode_and_ours_otherwise(tmp_path):
    fr, err = _uni("fr", str(tmp_path / "fr.xlsx"))
    assert fr["ok"] and fr["analysis_mode"] == "fr"
    assert "FastReporter mode: FR's event list is the column set" in err
    cols = [(c["km"], c["kind"]) for c in fr["uni"]["grid_columns"]]
    # ELMMIL: the reel connector at each end, FR's fourteen events between,
    # three of them a full-population splice
    assert len(cols) == 16 and cols[0] == (0.0, "connector") and cols[-1][1] == "connector"
    assert [k for _, k in cols].count("splice") == 4
    assert [(c["fiber"], c["kind"], c["loss"]) for c in fr["uni"]["cells"]] == [
        (12, "splice", 0.265), (20, "splice", 0.273)]
    suite, err = _uni("suite", str(tmp_path / "suite.xlsx"))
    assert suite["ok"] and suite["analysis_mode"] == "suite" and "FastReporter mode" not in err
    assert [(c["km"], c["kind"]) for c in suite["uni"]["grid_columns"]] == [
        (0.0, "connector"), (14.56, "splice"), (21.28, "splice"), (47.06, "splice"),
        (61.48, "bend_damage"), (67.54, "connector")]
    assert [(c["fiber"], c["loss"]) for c in suite["uni"]["cells"]] == [(12, 0.265), (20, 0.273)]
