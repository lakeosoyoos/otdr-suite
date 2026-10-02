"""Show failing cells only collapses the bidirectional table (boss 2026-09-26).

"F1-6 have nothing failing on events 1-10, so those would collapse to hidden
and only show the failing fibers or events", and "only see the average line
to consolidate all the cells".  Checked in the browser on a 1152-fibre job:
fibres 1-12, 18 event columns -> the 5 with a failure, Sections gone, each
fibre left with its Average row plus the direction rows that fail on their
own (reflectance is judged per direction, never on the Average).

Boss 2026-09-29: "When this is on, we only need to see if it's failing Bidi
Average."  Robert: that is for loss at events; reflectance stays per
direction; both tables; "Show warning cells only" the same.  So under either
filter a direction's own loss keeps nothing and prints blank: a fibre whose
Average passes leaves even when one direction fails the single-direction
gate (F2 B→A / F19 A→B in his screenshot).

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
    assert "(!gFlaggedOnly || rowFails[i])" in FN
    assert "(!collapse || ['a', 'b', 'avg'].some(w => legKept(i, w)))" in FN
    assert "!collapse || w === 'avg' || legKept(fi, w)" in FN


def test_only_the_average_keeps_a_loss():
    """Boss 2026-09-29: under the cell filters a splice's loss is judged on
    the Average row alone; a direction row stays only for its failing
    reflectance, or for its failing loss at a connector (next test)."""
    assert ("const cellKept = (x, which) => which === 'avg'\n"
            "    ? (gFailCellsOnly && cellFails(x, which)) || (gWarnCellsOnly && cellWarns(x, which))\n"
            "    : gFailCellsOnly && (isRefl(x) ? cellFails(x, which) : legReflFails(x, which));") in FN
    # a splice direction's loss prints blank under the filters: no gate, no warning
    assert "const judged = !cellFilterOn();" in FN
    assert "const gated = judged || isRefl(x);" in FN
    assert "gated ? legGateFor(isRefl(x)) : null," in FN
    assert "judged ? legWarnFor(isRefl(x)) : null)" in FN
    # the rows' own verdicts are untouched
    assert "const fail = legFails(fi, which);" in FN
    assert "const rowFails = have.map((_p, fi) => ['a', 'b', 'avg'].some(w => legFails(fi, w)));" in FN


def test_a_connector_keeps_each_failing_direction():
    """Boss 2026-09-29: "We can't average connector losses A and B.  We have
    to see them separately.  Flag them if over their threshold."  Robert:
    every connector, the two ends and mid-span alike.  So under "Show
    failing cells only" a connector's (reflective row's) direction row
    stays when its own loss fails at the one-direction connector gate
    (gateFor(true, true); off on a panel span), and its cell prints red.
    No warning band on a direction under the filters: "Show warning cells
    only" keeps a connector's Average warning, as a splice's.  A launch
    level (the OTDR port) is never judged (legOk)."""
    body = FN[FN.index("const cellKept = "):FN.index("const legKept = ")]
    assert "isRefl(x) ? cellFails(x, which)" in body
    assert "gWarnCellsOnly && cellWarns(x, which)" in body and body.count("gWarnCellsOnly") == 1
    # a direction's own loss judged at the one-direction connector gate (a
    # splice direction has none, Robert 2026-10-02: see legGateFor)
    assert ("const cellFails = (x, which) => {\n"
            "    if (which === 'avg') return clearsAt(x.row.loss, gateFor(isRefl(x), false));\n"
            "    if (legReflFails(x, which)) return true;\n"
            "    const leg = x.row[which];\n"
            "    if (!legOk(leg) || gainerHidden(leg.loss)) return false;\n"   # Show gainers
            "    return clearsAt(leg.loss, legGateFor(isRefl(x)));\n"
            "  };") in FN
    assert "const legOk = leg => !!leg && !leg.synthetic && !(Number(leg.status || 0) & 0x08);" in FN


def test_judging_is_defined_before_the_header_uses_it():
    assert FN.index("const cellFails = ") < FN.index("const keepCol = ")
    assert FN.index("const keepCol = ") < FN.index("// ── Header:")


def test_jump_to_a_row_counts_rows_not_three_per_fibre():
    assert "3 * k + off" not in FN
    assert "descs.findIndex(d => d[0] === fi && d[1] === w)" in FN


# ── The one-direction table collapses the same way (boss, same day) ──
UNI = SRC.split("function renderFastReporterGrid(", 1)[1].split("\nfunction renderFrBidiGrid(", 1)[0]


def test_uni_columns_nobody_fails_leave():
    assert "traces.some((_t, ti) => keeps(evLoss(c.ev[ti])))" in UNI
    assert "const keeps = v => (gFailCellsOnly && overGate(v))" in UNI
    # overGate is the report's gate (clearsGate) unless the table holds the
    # one-way fibres of a mixed load, judged at the single-direction gate
    assert "const overGate = opts.oneDir ? (v => clearsAt(v, gateFor(false, true))) : clearsGate;" in UNI
    assert UNI.count("if (!keepCol[i]) return;") == 3     # header, rows, footer


def test_uni_sections_and_statistics_leave_when_collapsed():
    assert "const showSec = gShowSections && !collapse;" in UNI
    assert "const STAT_EV  = collapse ? [] :" in UNI
    assert "if (!collapse) statArrs.forEach(" in UNI


def test_uni_passing_fibres_leave():
    assert "(!gFlaggedOnly || rowFails[i]) && (!collapse || rowKept[i])" in UNI
