"""A fibre loaded in both directions gets an A+B Average row under its two
legs, and for that pair ONLY the Average row is judged against the gate."""
from pathlib import Path

SRC = (Path(__file__).resolve().parents[2] / 'viewer' / 'viewer.html').read_text(encoding='utf-8')
GRID = SRC.split("function renderFastReporterGrid(", 1)[1].split("\n// ─── FastReporter mode", 1)[0]


def test_pairs_are_one_a_and_one_b_at_the_same_fibre_and_wavelength():
    assert "`${t.fiber}|${t.data.wavelength_nm || ''}`" in GRID
    assert "if (as.length !== 1 || bs.length !== 1) return;" in GRID


def test_the_pair_is_judged_on_its_average_not_its_legs():
    assert "pairs.forEach(p => { p.fail = cols.some(c => clearsGate(pairLoss(p, c))); });" in GRID
    assert "rowFails = traces.map((_t, ti) => pairOf.has(ti)" in GRID
    # a leg's loss cell is drawn ungated
    assert "e ? ` data-col=\"${i}\" data-km=\"${e.dist_km}\"` : '', inPair)" in GRID
    assert "else if (!ungated && clearsGate(v)) cls = ' class=\"fr-hi\"';" in GRID
    # the Average row's cell is gated
    assert "cells.push(lossCell(v, false) + `<td>${cellText('—')}</td>`);" in GRID


def test_the_average_row_sits_under_its_two_legs():
    assert "order.push(p.a, p.b, p);" in GRID
    assert "if (typeof ti !== 'number') return avgRowHtml(ti);" in GRID


def test_an_end_of_fiber_in_either_leg_is_not_averaged_as_a_splice():
    assert "if ((ea && ea.is_end) || (eb && eb.is_end)) return null;" in GRID
