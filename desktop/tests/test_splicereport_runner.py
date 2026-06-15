"""Splice Report runner tests (subprocess-isolated, like Secret Sauce).

The splice engine ships its own sor_reader324802a.py, so it runs as a
subprocess (never imported in-process). These exercise it through the
conftest run_splicereport() helper against the 24-fiber/dir splice fixture
(>= MIN_POP_SPLICE), and assert the grid JSON the Splice Report page needs
to drive the Viewer.
"""
from __future__ import annotations

from pathlib import Path

from conftest import run_splicereport, FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR


def test_splicereport_happy_path(tmp_path):
    out = tmp_path / "rep" / "report.xlsx"
    rc, m, stderr = run_splicereport(FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, out, "Elm", "Mil")
    assert rc == 0, f"runner exited {rc}; stderr:\n{stderr[-1500:]}"
    assert m is not None and m.get("ok") is True, f"manifest not ok: {m}"
    # Pipeline discovered splice columns and wrote the workbook.
    assert m["n_columns"] >= 1, "no splice/bend columns discovered"
    assert m["n_fibers"] == 24
    assert Path(m["xlsx"]).exists() and m["xlsx"].endswith(".xlsx")


def test_splicereport_grid_cells_are_navigable(tmp_path):
    """Every flagged cell must carry the fields the click-to-jump needs:
    fiber (int), km (number), category (str)."""
    out = tmp_path / "report.xlsx"
    rc, m, stderr = run_splicereport(FIXTURE_SPLICE_A_DIR, FIXTURE_SPLICE_B_DIR, out)
    assert rc == 0 and m and m["ok"], f"runner failed: {stderr[-1000:]}"
    cells = m["cells"]
    assert cells, "expected at least one flagged cell on this fixture"
    for c in cells:
        assert isinstance(c["fiber"], int) and c["fiber"] >= 1
        assert isinstance(c["km"], (int, float)) and c["km"] >= 0
        assert c["category"]
    # Columns carry km positions for the header + column-jump.
    for col in m["columns"]:
        assert isinstance(col["km"], (int, float))


def test_splicereport_requires_both_folders(tmp_path):
    # B missing → clean error, not a crash.
    rc, m, stderr = run_splicereport(FIXTURE_SPLICE_A_DIR, tmp_path / "does_not_exist",
                                     tmp_path / "r.xlsx")
    assert m is not None and m.get("ok") is False
    assert "folder" in m.get("error", "").lower()
