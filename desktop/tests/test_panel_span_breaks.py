"""A fiber broken AT a tie panel prints as broke, at that panel, from both sides.

Red Rock 4-6 West, tech's V1 sheet (2026-09-09), fixture = RDR4<->RDR6
fibers 41-46 and 70-75:

    RDR4RDR6 0046  Broke  1.0082km  A SIDE     RDR6RDR4 0046  Broke  1.0376km  B SIDE
    RDR4RDR6 0070  Broke  1.0082km  A SIDE     RDR6RDR4 0070  Broke  1.0376km  B SIDE
    RDR4RDR6 0073  Broke  1.0394km  A SIDE     RDR6RDR4 0073  Broke  1.0065km  B SIDE
    RDR4RDR6 0074  Broke  1.0394km  A SIDE     RDR6RDR4 0074  Broke  1.0065km  B SIDE

Two independent shots stopping at the same panel: 46 and 70 are open at the
RDR4 panel, 73 and 74 at the RDR6 panel.  The report printed
RESHOOT_DEAD_TRACE instead -- normalization re-zeroed each far-panel end to
"end-of-fiber at 0 km", which is the signature of a shot that never entered
the cable -- and filed it under the wrong end, while each near-panel end
printed nothing.  A re-shoot cannot fix a broken fiber.
"""
from __future__ import annotations

import openpyxl

from conftest import run_splicereport, FIXTURE_DIR

PANEL_DIR = FIXTURE_DIR / "panelbreak"


def _run(tmp_path):
    out = tmp_path / "panelbreak.xlsx"
    rc, m, stderr = run_splicereport(PANEL_DIR / "A", PANEL_DIR / "B", out,
                                     "RDR4", "RDR6")
    assert rc == 0 and m and m.get("ok"), f"runner failed: {stderr[-1200:]}"
    return m, out


def _grid(out):
    ws = openpyxl.load_workbook(out)["Splice Report"]
    heads = {c: str(ws.cell(3, c).value or "") for c in range(1, ws.max_column + 1)}
    return [(heads.get(c, ""), str(ws.cell(r, c).value))
            for r in range(4, ws.max_row + 1) for c in range(2, ws.max_column + 1)
            if ws.cell(r, c).value is not None]


def test_each_break_prints_at_its_own_panel_from_both_sides(tmp_path):
    m, out = _run(tmp_path)
    conn = [c for c in m["columns"] if c["kind"] == "connector"]
    assert len(conn) == 2, m["columns"]
    grid = _grid(out)
    near = [t for h, t in grid if h == "Connector @ %.2fkm" % conn[0]["km"]]
    far = [t for h, t in grid if h == "Connector @ %.2fkm" % conn[-1]["km"]]
    assert near == ["46 broke A 1.0081km, B 1.0375km",
                    "70 broke A 1.0081km, B 1.0368km"], near
    assert far == ["73,74 broke A 1.0389km, B 1.0063km"], far


def test_a_broken_fiber_is_never_sent_for_a_reshoot(tmp_path):
    _, out = _run(tmp_path)
    assert not any("RESHOOT_DEAD_TRACE" in t for _, t in _grid(out)), _grid(out)


def test_the_breaks_are_flags(tmp_path):
    m, _ = _run(tmp_path)
    broke = sorted(c["fiber"] for c in m["cells"]
                   if c["is_flagged"] and c["category"] == "broke")
    assert broke == [46, 70, 73, 74], broke
