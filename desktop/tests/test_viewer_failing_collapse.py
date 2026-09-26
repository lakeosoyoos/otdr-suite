"""Show failing cells only collapses the bidirectional table (boss 2026-09-26).

"F1-6 have nothing failing on events 1-10, so those would collapse to hidden
and only show the failing fibers or events", and "only see the average line
to consolidate all the cells".  Checked in the browser on a 1152-fibre job:
fibres 1-12, 18 event columns -> the 5 with a failure, Sections gone, each
fibre left with its Average row plus the direction rows that fail on their
own (reflectance is judged per direction, never on the Average).

Plain JS, no runtime here: pins the source.
"""
from __future__ import annotations

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")
FN = SRC.split("function paintFrBidiGrid(", 1)[1].split("\n// ─── Declaring the span", 1)[0]


def test_columns_nobody_fails_leave():
    assert "const keepCol = cols.map(c => !collapse" in FN
    assert FN.count("if (!keepCol[i]) return;") == 3     # header, rows, footer


def test_sections_leave_when_collapsed():
    assert "const showSec = gShowSections && !collapse;" in FN
    assert "gShowSections &&" not in FN.replace("const showSec = gShowSections &&", "")


def test_passing_fibres_leave_and_the_average_stays():
    assert "!(gFlaggedOnly || collapse) || rowFails[i]" in FN
    assert "!collapse || w === 'avg' || legFails(fi, w)" in FN


def test_judging_is_defined_before_the_header_uses_it():
    assert FN.index("const cellFails = ") < FN.index("const keepCol = ")
    assert FN.index("const keepCol = ") < FN.index("// ── Header:")


def test_jump_to_a_row_counts_rows_not_three_per_fibre():
    assert "3 * k + off" not in FN
    assert "descs.findIndex(d => d[0] === fi && d[1] === w)" in FN
