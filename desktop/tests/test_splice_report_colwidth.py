"""Regression: the Splice Report grid is one Excel column per splice with no
merged cells, and its columns are MINIMUM-fit: wide enough for their content
and no wider.

Each splice used to spread over a merged pair of Excel columns, so every
header sat on two cells and inserting, deleting or copying a column broke the
merges (2026-09-28).  Also guards against reverting to fixed-wide columns.
"""
import subprocess
import sys
from pathlib import Path

import openpyxl
import pytest
from openpyxl.utils import get_column_letter

REPO_ROOT = Path(__file__).resolve().parents[2]
FX = REPO_ROOT / "desktop" / "tests" / "fixtures"


@pytest.fixture(scope="module")
def report_ws(tmp_path_factory):
    out = tmp_path_factory.mktemp("colwidth") / "report.xlsx"
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "splicereport" / "run_splicereport.py"),
         "--dir-a", str(FX / "splice_A"), "--dir-b", str(FX / "splice_B"),
         "--out", str(out), "--site-a", "HOWESPAN", "--site-b", "LANCASTER"],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-600:]
    return openpyxl.load_workbook(out)["Splice Report"]


def _longest_line(v):
    return max(len(x) for x in str(v).splitlines())


def test_splice_report_is_one_column_per_splice(report_ws):
    ws = report_ws
    assert not ws.merged_cells.ranges
    heads = [ws.cell(3, c).value for c in range(2, ws.max_column + 1)]
    assert len(heads) >= 3, heads                     # two ILA ends + a splice
    assert heads[0].startswith("A-End ILA") and heads[-1].startswith("B-End ILA")
    # every column carries its own header and both distances: no blank half
    for c in range(3, ws.max_column + 1):
        assert ws.cell(3, c).value, f"col {c} has no header"
        assert ws.cell(1, c).value and ws.cell(2, c).value, f"col {c} has no distance"


def test_splice_report_columns_minimum_fit(report_ws):
    ws = report_ws
    widest = {c: 0 for c in range(1, ws.max_column + 1)}
    for r in range(1, ws.max_row + 1):
        for c in range(1, ws.max_column + 1):
            v = ws.cell(row=r, column=c).value
            if v:
                widest[c] = max(widest[c], _longest_line(v))

    for c in range(1, ws.max_column + 1):
        w = ws.column_dimensions[get_column_letter(c)].width
        assert w is not None, f"col {c} has no explicit width"
        cap = 120.5 if 3 <= c < ws.max_column else 60.5   # splice cols get 2x
        assert 3.0 <= w <= cap, f"col {c} width {w} out of bounds"
        # MINIMUM-fit: never gratuitously wider than its widest cell.
        assert w <= widest[c] * 1.5 + 4.0, (
            f"col {c} width {w} is wider than its content ({widest[c]} chars)")

    # CONTENT NOT CLIPPED: the distances and headers fit their own column.
    for r in (1, 2, 3):
        for c in range(1, ws.max_column + 1):
            v = ws.cell(row=r, column=c).value
            if not v:
                continue
            w = ws.column_dimensions[get_column_letter(c)].width
            assert w + 1.0 >= _longest_line(v) * 1.05, (
                f"'{str(v)[:24]}' doesn't fit column {c} ({w:.1f})")


def test_splice_report_every_cell_centred(report_ws):
    """Every cell on every sheet has its text centred, not just the splice
    cells: the ribbon column, both ILA end columns, the distance rows and
    the other sheets used to sit left-aligned (2026-09-29)."""
    for ws in report_ws.parent.worksheets:
        for row in ws.iter_rows():
            for c in row:
                a = c.alignment
                assert (a.horizontal, a.vertical) == ("center", "center"), (
                    f"{ws.title}!{c.coordinate} is {a.horizontal}/{a.vertical}")
