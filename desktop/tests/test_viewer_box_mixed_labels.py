"""A Viewer box's one-direction table reads every file the box shows.

El Paso B (2026-10-02): 36 of the 40 B files carried the A side's site codes
(ELP->LSC), the 4 re-shots the right ones (LSC->ELP).  The Viewer drew all 40
as B, A+B paired them by fiber number, but B alone asked the one-direction
report for its table, which kept the 36 and set the 4 aside: fiber 241 read
"OTDR Suite table could not be built.  The report has no table for these
fibers."  The box names the folder's direction, so the Viewer's run folds in
every signature whose fiber numbers are disjoint from the rest; a signature
that re-uses fiber numbers is a real second direction and stays out, and the
Viewer then says why.  The Unidirectional report itself is unchanged.
"""
import json
import os
import subprocess
import sys

from conftest import FIXTURE_DIR, REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"
RUNNER = SPLICEREPORT_DIR / "run_splicereport.py"
if str(SPLICEREPORT_DIR) not in sys.path:
    sys.path.insert(0, str(SPLICEREPORT_DIR))
import splicereportmatchexfo as E  # noqa: E402

SRC = FIXTURE_DIR / "splice_B"            # 24 fibers, ELMDALE->MILLER
FLIP = (b"ELMDALE\x00MILLER\x00", b"MILLER\x00ELMDALE\x00")


def _folder(path, flipped=(), extra=()):
    """splice_B with the site codes of `flipped` fibers swapped (same bytes,
    so every offset holds); `extra` fibers also get a flipped copy under a
    second name, re-using their fiber number."""
    path.mkdir(parents=True)
    for name in sorted(os.listdir(SRC)):
        raw = (SRC / name).read_bytes()
        n = int(name[6:10])
        if n in flipped:
            assert raw.count(FLIP[0]) == 1
            raw = raw.replace(*FLIP)
        (path / name).write_bytes(raw)
        if n in extra:
            (path / f"ELMMIL{n:04d}_1550.sor").write_bytes(raw.replace(*FLIP))
    return path


def _uni(tmp_path, folder, tag, viewer=True):
    out = [sys.executable, str(RUNNER), "--uni", "--dir-a", str(folder),
           "--out", str(tmp_path / f"{tag}.xlsx")]
    tbl = tmp_path / f"{tag}.json"
    if viewer:
        out += ["--viewer-leg", "b", "--viewer-table", str(tbl)]
    p = subprocess.run(out, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-2000:]
    m = json.loads(p.stdout.strip().splitlines()[-1])
    assert m["ok"], m
    return m, (json.loads(tbl.read_text(encoding="utf-8")) if viewer else None)


def test_the_box_folds_in_files_labeled_the_other_way(tmp_path):
    d = _folder(tmp_path / "mix", flipped={5, 17})
    fibers, chosen, counts, merged, cov = E.uni_load_dir(str(d))
    assert len(fibers) == 22 and 5 not in fibers          # the report: unchanged
    fibers, chosen, counts, merged, cov = E.uni_load_dir(str(d), one_box=True)
    assert sorted(fibers) == list(range(1, 25))
    assert chosen == "ELMDALE->MILLER" and cov["complete"]
    assert merged == [{"signature": "MILLER->ELMDALE", "n_fibers": 2, "fibers": [5, 17]}]


def test_a_second_direction_re_using_fiber_numbers_stays_out(tmp_path):
    d = _folder(tmp_path / "two", extra={3, 4})
    fibers, chosen, counts, merged, cov = E.uni_load_dir(str(d), one_box=True)
    assert len(fibers) == 24 and merged == []
    assert cov["dropped_signatures"] == [
        {"signature": "MILLER->ELMDALE", "n_files": 2, "fiber_ranges": "3-4"}]


def test_the_viewer_table_is_the_clean_folder_s(tmp_path):
    """Only the labels differ, so the B-alone table is the one the same files
    give with every label right: same columns, same cells, every fiber."""
    _, clean = _uni(tmp_path, _folder(tmp_path / "clean"), "clean")
    m, mixed = _uni(tmp_path, _folder(tmp_path / "mix", flipped={5, 17}), "mix")
    assert m["uni"]["coverage_complete"]
    assert mixed["columns"] == clean["columns"]
    assert mixed["fibers"] == clean["fibers"] and len(mixed["fibers"]) == 24


def test_the_unidirectional_report_still_sets_them_aside(tmp_path):
    m, _ = _uni(tmp_path, _folder(tmp_path / "mix", flipped={5, 17}), "rep",
                viewer=False)
    assert m["uni"]["n_fibers"] == 22 and not m["uni"]["coverage_complete"]


def test_the_viewer_says_why_a_fiber_was_left_out(monkeypatch):
    sys.path.insert(0, str(REPO_ROOT / "viewer"))
    import trace_server as TS
    table = {"columns": [], "fibers": {"1": []}, "direction": "b", "gate_db": 0.25,
             "left_out": [{"signature": "MILLER->ELMDALE", "n_files": 2,
                           "fiber_ranges": "3-4"}]}
    monkeypatch.setattr(TS, "_one_direction_table", lambda d: (table, False, None))
    out = TS.suite_tables([3], "b")
    assert out["missing"] == [3]
    assert "MILLER->ELMDALE (fibers 3-4)" in out["error"], out
    # a fiber the report did not set aside keeps the plain "no table"
    assert TS.suite_tables([9], "b")["error"] is None
    assert TS.suite_tables([1, 3], "b")["error"] is None
