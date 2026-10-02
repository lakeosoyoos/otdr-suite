"""Uni grid: the number printed in a cell several fibers share.

The tech's group convention is "139,144 .277": every listed fiber is out and
the value is the worst one.  uni_format_cell_label took max() of the SIGNED
readings, so on a cell where every reading is a gainer it printed the one
closest to zero.  A 432-fiber span at a 0.160 gate, ribbon 27, Splice 5:
Flagged Events lists F316 -0.161 and F322 -0.278, and the cell read
"F316,F322 -.161".

The same max() also listed a fiber that broke at the closure under another
fiber's loss ("F302,F307 .317"), although the break-at-closure rule promises
"broke" in exactly those cells.

A cell holding a loss and a gainer printed the loss alone ("F354,F355 .175"
with F355 at -0.214); it now prints each group with its own number.

Reflective columns carry a reflectance, where the worst reading IS the one
closest to zero, so they keep max().
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'splicereport'))

import splicereportmatchexfo as E  # noqa: E402


def test_all_gainer_cell_prints_the_biggest_reading():
    # Ribbon 27, Splice 5 and Bend/Damage 1 at the 0.160 gate.
    assert E.uni_format_cell_label([(316, -0.161), (322, -0.278)]) == 'F316,F322 -.278'
    # Ribbon 6, Bend/Damage 3: F63 -0.187, F69 -0.172.
    assert E.uni_format_cell_label([(69, -0.172), (63, -0.187)]) == 'F63,F69 -.187'


def test_loss_cells_unchanged():
    assert E.uni_format_cell_label([(23, 0.18)]) == 'F23 .180'
    assert E.uni_format_cell_label([(23, 0.18), (47, 0.22)]) == 'F23,F47 .220'
    assert E.uni_format_cell_label([(23, -0.105)]) == 'F23 -.105'
    assert E.uni_format_cell_label([(12, None), (19, None)]) == 'F12,F19 broke'
    assert E.uni_format_cell_label([(5, 1.234), (2, 0.4)]) == 'F2,F5 1.234'


def test_mixed_loss_and_gainer_prints_both():
    # Ribbon 30, Splice 8: F354 .175, F355 -0.214.  The cell read
    # "F354,F355 .175", hiding the bigger reading.
    assert E.uni_format_cell_label([(355, -0.214), (354, 0.175)]) == 'F354 .175 F355 -.214'
    # Ribbon 5, Bend/Damage 5: three losses and one gainer.
    assert (E.uni_format_cell_label([(49, 0.17), (51, 0.213), (55, 0.186), (60, -0.176)])
            == 'F49,F51,F55 .213 F60 -.176')
    # ...and a broke fiber still comes last.
    assert (E.uni_format_cell_label([(1, -0.3), (2, None), (3, 0.2)])
            == 'F3 .200 F1 -.300 F2 broke')


def test_fiber_broke_at_closure_is_not_listed_under_a_loss():
    # test_uni_break_at_closure's mixed cell: F302 dies at the closure, F307
    # reads 0.317 there.
    assert E.uni_format_cell_label([(302, None), (307, 0.317)]) == 'F307 .317 F302 broke'
    assert (E.uni_format_cell_label([(303, None), (307, 0.317), (302, None), (301, 0.2)])
            == 'F301,F307 .317 F302,F303 broke')


def test_reflective_cell_keeps_the_strongest_reflectance():
    entries = [(1, -60.0), (2, -45.0)]
    assert E.uni_format_cell_label(entries, 'reflective') == 'F1,F2 -45.000'


def test_writer_passes_the_column_kind(tmp_path):
    """End to end through uni_write_xlsx: a gainer splice cell prints its
    biggest reading, and a reflective cell next to it still prints the
    strongest reflectance (the writer must tell the formatter which is
    which)."""
    import openpyxl
    cols = [{'kind': 'splice', 'position_km_refined': 5.0,
             'position_km_display': 5.0, 'fiber_count': 12,
             'is_entry_case': False},
            {'kind': 'reflective', 'position_km_refined': 7.0,
             'position_km_display': 7.0,
             'refl_members': {1: -60.0, 2: -45.0}}]
    grid = {(0, 0): [(4, -0.161), (10, -0.278)],
            (0, 1): [(1, -60.0), (2, -45.0)]}
    out = str(tmp_path / 'uni.xlsx')
    E.uni_write_xlsx(grid, cols, 12, 12, 10.0, out, site_a='SITEA', site_b='SITEB')
    ws = openpyxl.load_workbook(out)['Unidir Events']
    row = next(r for r in ws.iter_rows(values_only=True)
               if r[0] and str(r[0]).startswith('Fiber 1-12'))
    assert row[1] == 'F4,F10 -.278', row
    assert row[2] == 'F1,F2 -45.000', row
