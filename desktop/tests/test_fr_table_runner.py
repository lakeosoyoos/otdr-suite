"""The Viewer's way to FR's table: `run_splicereport.py --fr-table`.

FastReporter mode's Viewer table is fr_bidi_table's output, and the Viewer
runs in the hub process on its own sor_reader copy, so it asks the Splice
Report engine over a subprocess boundary -- the runner's --fr-table mode,
which prints one JSON line per call.  Pinned here: the payload shape, the
0017 pair row for row against fr_bidi_table in-process, sections in the
payload, NaN never reaching the JSON, a bad pair reported without sinking
the good ones, and the normal report mode still requiring its folders.
"""
import json
import os
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
RUNNER = SPLICEREPORT_DIR / "run_splicereport.py"
FIX = REPO_ROOT / "desktop" / "tests" / "fixtures" / "zayo_sor"
PA = str(FIX / "ORPVL.ZYO-OR-DES-0048.1550.0017.sor")
PB = str(FIX / "ZYO-OR-DES-0048.ORPVL.1550.0017.sor")


def _runner(spec):
    p = subprocess.run([sys.executable, str(RUNNER), "--fr-table", json.dumps(spec),
                        "--analysis", "fr"], capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, p.stdout                   # exactly one JSON line
    assert "NaN" not in lines[0] and "Infinity" not in lines[0]
    return json.loads(lines[0])


def test_the_runner_prints_fr_bidi_table_for_a_sor_pair():
    out = _runner([[17, PA, PB]])
    assert out["ok"] is True and out["errors"] == {} and out["analysis_mode"] == "fr"
    rows = out["tables"]["17"]
    assert len(rows) == 11
    for i, r in enumerate(rows):
        assert set(r) >= {"mean_pos_m", "loss", "length_m", "type", "status", "refl", "a", "b", "section"}
        for leg in ("a", "b"):
            assert set(r[leg]) >= {"pos_m", "loss", "type", "status", "length_m", "refl",
                                   "synthetic", "absorbed", "cur_a_m", "cur_b_m"}
        if i < len(rows) - 1:
            s = r["section"]
            assert s and s["loss"] is not None and s["length_m"] > 0
            assert s["a"]["loss"] is not None and s["b"]["loss"] is not None
            assert abs(s["att_db_km"] - s["loss"] / s["length_m"] * 1000.0) < 1e-9
        else:
            assert r["section"] is None
    # the launch and end rows carry no loss (NaN in the file -> null here)
    assert rows[0]["loss"] is None and rows[-1]["loss"] is None
    assert rows[0]["status"] == 64 and rows[-1]["status"] == 128


def test_the_subprocess_rows_equal_the_in_process_table():
    """Same numbers whichever way the table is asked for."""
    out = _runner([[17, PA, PB]])
    body = textwrap.dedent(f"""
        import sys, json, math
        sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})
        import sor_reader324802a as sr, splicereportmatchexfo as E
        ra = sr.parse_sor_full({PA!r}, trim=False); rb = sr.parse_sor_full({PB!r}, trim=False)
        for r, side in ((ra, 'a'), (rb, 'b')):
            r['_source'] = 'sor'; r['_span_side'] = side
        rows = E.fr_bidi_table(ra, rb)
        def clean(x):
            if isinstance(x, float): return x if math.isfinite(x) else None
            if isinstance(x, dict): return {{str(k): clean(v) for k, v in x.items()}}
            if isinstance(x, (list, tuple)): return [clean(v) for v in x]
            return x
        print(json.dumps(clean(rows)))
    """)
    p = subprocess.run([sys.executable, "-c", body], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout.strip().splitlines()[-1]) == out["tables"]["17"]


def test_a_bad_pair_is_reported_and_the_good_one_still_comes_back():
    out = _runner([[17, PA, PB], [99, "/nonexistent/a.sor", "/nonexistent/b.sor"]])
    assert out["ok"] is True
    assert len(out["tables"]["17"]) == 11
    assert "99" in out["errors"] and "99" not in out["tables"]


def test_bad_json_is_a_clean_refusal():
    p = subprocess.run([sys.executable, str(RUNNER), "--fr-table", "{not json"],
                       capture_output=True, text=True)
    assert p.returncode == 0
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out["ok"] is False and "JSON" in out["error"]


def test_the_report_mode_still_requires_its_folders():
    p = subprocess.run([sys.executable, str(RUNNER), "--dir-a", str(FIX)],
                       capture_output=True, text=True)
    assert p.returncode != 0 and "--out" in p.stderr
