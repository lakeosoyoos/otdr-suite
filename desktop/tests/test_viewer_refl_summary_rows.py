"""The Minimum / Maximum / Average rows print reflectance too.

Asked for 2026-09-29: "in viewer reflective needs to have the min max and
average at the bottom show as well".  The three summary rows under every
event table filled the Loss cell of each column and left its Refl. cell
blank.  Now each Refl. cell carries the same statistic over the column's
reflectance readings, in all three tables:

  * one-direction table: every reading the column prints (a 0 prints as "-",
    so it is no reading here either);
  * both A+B tables: the A->B and B->A rows, because the Average row prints
    no reflectance; measured legs only, never a transplanted (grey) one.

Plain JS, no runtime here: pins the source.
"""
from __future__ import annotations

from conftest import VIEWER_DIR

SRC = (VIEWER_DIR / "viewer.html").read_text(encoding="utf-8")


def _agg(fn_name):
    # the first summary-row builder after the table's entry point (the A+B
    # tables hand their fetched data to a second function that builds them)
    body = SRC.split(f"\nfunction {fn_name}(", 1)[1]
    return body.split("const aggRow = (label, fn, gated) => {", 1)[1].split("\n  };", 1)[0]


def test_one_direction_summary_rows_carry_reflectance():
    agg = _agg("renderFastReporterGrid")
    assert ("const rs = c.ev.map(e => e ? e.reflection : null)"
            ".filter(r => r != null && !isNaN(r) && r !== 0);") in agg
    assert "`<td>${cellText(fmtR(rs.length ? fn(rs) : null))}</td>`" in agg
    assert "`<td>${cellText('-')}</td>`" not in agg


def test_fr_bidi_summary_rows_take_reflectance_from_both_directions():
    agg = _agg("renderFrBidiGrid")
    assert "flatMap(x => ['a', 'b'].map(w => x.row[w]))" in agg
    assert ".filter(leg => leg && !leg.synthetic).map(leg => leg.refl).filter(num);" in agg
    assert "`<td>${cellText(rs.length ? fmtR(fn(rs)) : '---')}</td>`" in agg
    assert "+ '<td></td>');" not in agg


def test_suite_bidi_summary_rows_take_reflectance_from_both_directions():
    agg = _agg("renderSuiteBidiGrid")
    assert "const rs = xs.flatMap(x => [x.a, x.b]).filter(leg => leg && !leg.grey)" in agg
    assert ".map(leg => leg.refl).filter(num);" in agg
    assert "`<td>${cellText(rs.length ? fmtR(fn(rs)) : '---')}</td>`" in agg
    assert "+ '<td></td>');" not in agg


def test_the_same_statistic_as_the_loss_cell():
    # Minimum / Maximum / Average of reflectance uses the row's own `fn`, so the
    # Refl. cell can never say a different statistic than the Loss beside it.
    for name in ("renderFastReporterGrid", "renderFrBidiGrid", "renderSuiteBidiGrid"):
        assert "fn(rs)" in _agg(name), name
