"""
FQA Builder regression tests.

The fixture is a synthetic production sheet built in-process from the
same two worksheet layouts the real ones use, so the suite does not need
a 70 MB customer workbook on disk.  The numbers in it are Span 4 Flagler
to Bethune's, including its two wrong footage marks, because those are
what the reconciliation exists to catch.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date

import openpyxl
import pytest

from conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT))

from fqa.event_chain import (EventChain, build_chain, footage_segments,  # noqa: E402
                             parse_feet, span_heading)
from fqa.fat import build_fat, lateral_cable_sizes                                   # noqa: E402
from fqa.job_facts import JobFacts, derive                                           # noqa: E402
from fqa.production_sheet import SPLICE, TERMINATION, read_production_sheet          # noqa: E402
from fqa.run_fqa import DEFAULT_TEMPLATE, build                                      # noqa: E402
from fqa.writer import (EXPECTED_FORM_VERSION, Exception_, FqaBuild,
                        form_version, write_fqa)                               # noqa: E402
from fqa.xlsx_patch import Cell, Formula, WorkbookPatch                              # noqa: E402

# Span 4's real Event Log, as Lumen received it: metres from Site A for
# each of the twelve splice locations, then the span length.
SPAN4_CLOSURES = [60, 5010, 7650, 13540, 18700, 23500, 27160,
                  32970, 38950, 44910, 50790, 54330]
SPAN4_LENGTH_M = 54390

# (sheet name, east mark, west mark) down the span.  Splice 3 and Splice 2
# carry the field's own transcription errors.
SPAN4_MARKS = [
    ('Flagler Entry', 16406, None),
    ('Splice 10', 8678, 52),
    ('Splice 9', 19446, 26),
    ('Splice 8', 2188, 108),
    ('Splice 7', 4104, 19046),
    ('Splice 6', 4500, 19840),
    ('Splice 5', 204, 16460),
    ('Splice 4', 19264, 19672),
    ('Splice 3', 208, 114),
    ('Splice 2', 14748, 226),
    ('Splice 1', 8304, 19506),
    ('Bethune Entry', None, 19882),
]


def _write_splice_sheet(ws, name, address, east, west, *, enclosure='Commscope',
                        tech='JJ', when=date(2026, 7, 22)):
    ws['C3'] = 'Splice Location Worksheet'
    ws['AH3'] = 'Pictures Insert Below'
    ws['C5'] = 'Location Information'
    ws['C6'], ws['G6'] = 'Name', name
    ws['C7'], ws['G7'] = 'Address', address
    ws['C9'], ws['G9'] = 'Placement', 'MH'
    ws['C15'] = 'Enclosure Information'
    ws['C16'], ws['G16'] = 'Manufacturer', enclosure
    ws['C17'], ws['G17'] = 'Ports Available', '4'
    ws['C18'], ws['G18'] = 'Splice Trays', '4'
    ws['C21'] = 'Installer Information'
    ws['C22'], ws['G22'] = 'Company', 'ZDB'
    ws['C23'], ws['G23'] = 'Technician(s)', tech
    ws['C24'], ws['G24'] = 'Date', when
    ws['C25'], ws['G25'] = 'Work Order', 'Denver to Kansas City'
    ws['C27'] = 'Cable Information'
    ws['D28'], ws['M28'], ws['S28'], ws['Y28'] = (
        'Cable Part Number', 'Sequential at', 'Sequential at', 'From')
    ws['M29'], ws['S29'], ws['Y29'] = 'Enclosure', 'Duct', 'Direction'
    row = 30
    for mark, heading in ((west, 'West'), (east, 'East')):
        if mark is None:
            continue
        ws[f'C{row}'] = 'Corning 1152f 07/26'
        ws[f'L{row}'] = mark
        ws[f'R{row}'] = mark + 50
        ws[f'X{row}'] = heading
        row += 1
    ws['C37'] = 'Splice Information'


def _write_termination_sheet(ws, building, address, bay_shelf, laterals):
    ws['C3'] = 'Termination Location Worksheet'
    ws['AJ3'] = 'Pictures Insert Below'
    ws['C5'] = 'Location Information'
    ws['C6'], ws['G6'] = 'Customer', 'HP/Lumen'
    ws['C7'], ws['G7'] = 'Building', building
    ws['C8'], ws['G8'] = 'Address', address
    ws['C10'], ws['G10'] = 'Bay/Shelf', bay_shelf
    ws['C13'] = 'Fiber Distribution Frame Information'
    ws['C14'], ws['H14'] = 'Manufacturer', 'Commscope'
    ws['C15'], ws['H15'] = 'Model/RU', 'NG4'
    ws['C16'], ws['H16'] = 'Capacity Ports', '576'
    ws['C19'] = 'Installer Information'
    ws['C20'], ws['H20'] = 'Company', 'Zero DB Communications'
    ws['C23'], ws['H23'] = 'Work Order', 'Denver to Kansas City'
    ws['C25'] = 'Cable Information'
    ws['C26'], ws['O26'], ws['V26'], ws['AC26'] = (
        'Cable Part Number', 'Sequential at', 'Sequential at', 'From')
    ws['O27'], ws['V27'], ws['AC27'] = 'Frame', 'Duct', 'Location'
    ws['C28'] = ' / '.join(f'Commscope {n}f 04/25' for n in laterals[:2])
    ws['N28'], ws['U28'], ws['AB28'] = '06846ft / 04340ft', '06872ft', 'Cable 1 / Cable 2'
    if len(laterals) > 2:
        ws['C29'] = f'Commscope {laterals[2]}f 05/25'
        ws['N29'], ws['U29'], ws['AB29'] = '04740ft', '04764ft', 'Cable 3'
    ws['C31'] = 'Termination Information'
    ws['C32'], ws['I32'], ws['R32'], ws['X32'], ws['AC32'] = (
        'Mounting Postion', 'From Cable (Direction)',
        'From Fibers (colors or numbers)', 'To Jack Number(s) in Frame',
        'Connector Type (SC,ST,FC,ect.)')
    ws['C34'], ws['I34'], ws['R34'], ws['X34'], ws['AC34'] = (
        'RMU 8', 'Cable 1, Cable 2', '1-576', '1-576', 'LC/UPC')
    ws['C35'], ws['I35'], ws['R35'], ws['X35'], ws['AC35'] = (
        'RMU 19', 'Cable 2, Cable 3', '577-1152', '1-576', 'LC/UPC')
    ws['C38'] = 'Location A:'
    ws['C41'] = 'Actual Ports'


@pytest.fixture(scope='module')
def production_sheet(tmp_path_factory):
    """A Span-4-shaped production sheet, marks and errors included."""
    path = tmp_path_factory.mktemp('fqa') / 'span4_production.xlsx'
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    _write_termination_sheet(wb.create_sheet('Flagler ILA'), 'Flagler ILA',
                             '39.475609, -103.023762', 'RR 100.07',
                             [432, 432, 288])
    for name, east, west in SPAN4_MARKS:
        _write_splice_sheet(wb.create_sheet(name), name,
                            f'GPS for {name}', east, west)
    _write_termination_sheet(wb.create_sheet('Bethune  ILA'), 'Bethune ILA',
                             '39.366775, -102.428434', 'RR 100.08',
                             [432, 432, 288])
    wb.save(path)
    return str(path)


# ── production sheet ──────────────────────────────────────────────────────

def test_reads_every_location_in_route_order(production_sheet):
    prod = read_production_sheet(production_sheet)
    assert [l.sheet for l in prod.locations][:3] == [
        'Flagler ILA', 'Flagler Entry', 'Splice 10']
    assert prod.locations[0].kind == TERMINATION
    assert prod.locations[-1].kind == TERMINATION
    assert len(prod.splices) == 12
    assert prod.warnings == []


def test_vault_ids_come_off_the_tab_names(production_sheet):
    prod = read_production_sheet(production_sheet)
    vaults = [l.vault_id for l in prod.splices]
    assert vaults == ['ENTRY', 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 'ENTRY']


def test_cable_rows_keep_their_columns_apart(production_sheet):
    """The footage columns are found by header band, not by fixed column.

    Regression: an earlier read took the LAST non-empty cell before the
    next header, which slid every value one column left -- part numbers
    landed in the footage column and the whole chain was silently wrong.
    """
    prod = read_production_sheet(production_sheet)
    s10 = next(l for l in prod.locations if l.sheet == 'Splice 10')
    west = s10.cable_toward('west')
    east = s10.cable_toward('east')
    assert west.part_number == 'Corning 1152f 07/26'
    assert parse_feet(west.seq_at_node) == 52
    assert parse_feet(east.seq_at_node) == 8678


def test_a_tab_that_is_not_a_worksheet_is_reported_not_guessed(production_sheet,
                                                               tmp_path):
    wb = openpyxl.load_workbook(production_sheet)
    wb.create_sheet('Notes')['A1'] = 'call the locate before digging'
    out = tmp_path / 'extra_tab.xlsx'
    wb.save(out)
    prod = read_production_sheet(str(out))
    assert len(prod.splices) == 12
    assert any('Notes' in w for w in prod.warnings)


# ── distances ─────────────────────────────────────────────────────────────

def test_heading_is_read_off_the_first_entry_splice(production_sheet):
    prod = read_production_sheet(production_sheet)
    assert span_heading(prod) == ('east', 'west')


def test_traces_fill_the_column_and_footage_only_checks_it(production_sheet):
    prod = read_production_sheet(production_sheet)
    chain = build_chain(prod, trace_distances_m=SPAN4_CLOSURES,
                        span_length_m=SPAN4_LENGTH_M)
    assert chain.distance_source == 'trace'
    assert [e.dist_from_a_m for e in chain.splice_events] == SPAN4_CLOSURES
    assert chain.events[0].dist_from_a_m == 0
    assert chain.events[-1].dist_from_a_m == SPAN4_LENGTH_M


def test_from_z_and_to_next_are_consistent_with_from_a(production_sheet):
    prod = read_production_sheet(production_sheet)
    chain = build_chain(prod, trace_distances_m=SPAN4_CLOSURES,
                        span_length_m=SPAN4_LENGTH_M)
    for ev in chain.events:
        assert ev.dist_from_a_m + ev.dist_from_z_m == SPAN4_LENGTH_M
    for a, b in zip(chain.events, chain.events[1:]):
        assert a.dist_to_next_m == b.dist_from_a_m - a.dist_from_a_m
    assert chain.events[-1].dist_to_next_m == 0


def test_bad_footage_marks_are_named_by_segment(production_sheet):
    """Span 4's Splice 3 and Splice 2 both record ~200 ft on one reel.

    The reconciliation must name those two segments and leave the seven
    honest ones alone -- reporting per cumulative distance instead would
    blame every sheet downstream of the first error.
    """
    prod = read_production_sheet(production_sheet)
    chain = build_chain(prod, trace_distances_m=SPAN4_CLOSURES,
                        span_length_m=SPAN4_LENGTH_M)
    flagged = ' '.join(chain.warnings)
    assert 'Splice 3 → Splice 2' in flagged
    assert 'Splice 2 → Splice 1' in flagged
    assert 'Splice 10 → Splice 9' not in flagged
    assert 'Splice 7 → Splice 6' not in flagged


def test_honest_segments_agree_with_the_traces_within_tolerance(production_sheet):
    prod = read_production_sheet(production_sheet)
    segs = footage_segments(prod)
    # Flagler Entry -> Splice 10 -> Splice 9: marks and traces within 35 m.
    assert abs(segs[1] - 4950) <= 35
    assert abs(segs[2] - 2640) <= 35


def test_without_traces_the_chain_falls_back_and_says_so(production_sheet):
    prod = read_production_sheet(production_sheet)
    chain = build_chain(prod)
    assert chain.distance_source == 'footage'
    assert chain.events[1].dist_from_a_m == 60


def test_a_closure_count_mismatch_does_not_silently_shift_the_chain(production_sheet):
    """Ten measured closures against twelve worksheets must not be zipped
    together -- that would put vault 1's distance on vault 3's row."""
    prod = read_production_sheet(production_sheet)
    chain = build_chain(prod, trace_distances_m=SPAN4_CLOSURES[:10])
    assert chain.distance_source == 'footage'
    assert any('10 closures' in w and '12 splice' in w for w in chain.warnings)


# ── FAT ───────────────────────────────────────────────────────────────────

def test_fat_row_count_and_backbone_ranges(production_sheet):
    prod = read_production_sheet(production_sheet)
    rows = build_fat(1152, prod.site_a, prod.site_z)
    assert len(rows) == 48
    assert rows[0].backbone == '001-024'
    assert rows[-1].backbone == '1129-1152'
    assert rows[0].block_a == 'A' and rows[23].block_a == 'X'
    assert rows[24].block_a == 'A'          # blocks restart on the next RMU


def test_lateral_numbers_restart_on_each_lateral_cable(production_sheet):
    """Backbone 433-456 is lateral 001-024 of Cable 2, and 865-888 is
    lateral 001-024 of Cable 3.  This is the whole point of the table."""
    prod = read_production_sheet(production_sheet)
    assert lateral_cable_sizes(prod.site_a) == [432, 432, 288]
    rows = build_fat(1152, prod.site_a, prod.site_z)
    by_backbone = {r.backbone: r.lateral_a for r in rows}
    assert by_backbone['001-024'] == '001-024'
    assert by_backbone['409-432'] == '409-432'
    assert by_backbone['433-456'] == '001-024'
    assert by_backbone['577-600'] == '145-168'
    assert by_backbone['865-888'] == '001-024'
    assert by_backbone['1129-1152'] == '265-288'


def test_a_site_with_no_laterals_numbers_laterals_as_backbone():
    rows = build_fat(144, None, None)
    assert len(rows) == 6
    assert rows[-1].lateral_a == rows[-1].backbone == '121-144'


# ── job facts ─────────────────────────────────────────────────────────────

def test_derivation_reads_the_rack_and_the_cable(production_sheet):
    prod = read_production_sheet(production_sheet)
    job = derive(prod)
    assert job.fiber_count == 1152
    assert job.site_a.aisle == '100' and job.site_a.bay == '007'
    assert job.site_z.bay == '008'
    assert job.site_a.rmu == '8 & 19'
    assert job.site_a.panel_type == 'NG4'
    assert job.site_a.panel_port_count == 576
    assert job.site_a.connector_type == 'LC'
    assert job.site_a.alias == 'Flagler' and job.site_z.alias == 'Bethune'


def test_supplied_facts_are_never_overwritten_by_derivation(production_sheet):
    prod = read_production_sheet(production_sheet)
    job = derive(prod, JobFacts.from_dict(
        {'site_a': {'alias': 'Flagler East', 'bay': '999'},
         'fiber_count': 864}))
    assert job.site_a.alias == 'Flagler East'
    assert job.site_a.bay == '999'
    assert job.fiber_count == 864


def test_test_from_device_needs_every_part(production_sheet):
    prod = read_production_sheet(production_sheet)
    job = derive(prod)
    assert job.site_a.test_from_device is None      # no floor/room supplied
    job.site_a.floor, job.site_a.room = '001', '0001'
    assert job.site_a.test_from_device == '001.0001..100.007.8 & 19'


def test_missing_facts_names_what_a_reviewer_will_bounce(production_sheet):
    prod = read_production_sheet(production_sheet)
    missing = derive(prod).missing()
    assert 'A site address' in missing
    assert 'A site CLLI' in missing
    assert 'date of most recent test-equipment calibration' in missing
    assert 'number of fibers tested' not in missing      # derived


def test_site_text_joins_with_exactly_three_spaces():
    job = JobFacts()
    job.site_a.address, job.site_a.alias = '7250 County Rd HH', 'Flagler'
    job.site_a.clli = 'FLGLCOAC'
    assert job.site_a.site_text == '7250 County Rd HH   Flagler'
    assert job.site_a.header_text == '7250 County Rd HH   Flagler   FLGLCOAC'


# ── the workbook patcher ──────────────────────────────────────────────────

def test_patch_preserves_every_part_but_the_calc_chain(tmp_path):
    """A finished package carries customXml, a sensitivity label, printer
    settings and comments that openpyxl cannot round-trip.  Losing any of
    them would change what the customer receives."""
    out = tmp_path / 'patched.xlsm'
    patch = WorkbookPatch(DEFAULT_TEMPLATE)
    patch.set_cells([Cell('Event Log', 'W8', 1234)])
    patch.save(out)

    import zipfile
    before = set(zipfile.ZipFile(DEFAULT_TEMPLATE).namelist())
    after = set(zipfile.ZipFile(out).namelist())
    assert before - after <= {'xl/calcChain.xml'}
    assert not after - before
    assert 'xl/vbaProject.bin' in after
    assert openpyxl.load_workbook(out, data_only=True)['Event Log']['W8'].value == 1234


def test_the_package_is_lumens_form_part_for_part(production_sheet, tmp_path):
    """The deliverable must BE Lumen's form, not a copy of it.

    Everything except the sheets we write, the calc chain we have to drop
    and the workbook part that carries fullCalcOnLoad comes through with
    its original bytes -- named here one by one, because every one of them
    is something openpyxl would have quietly thrown away.
    """
    import hashlib
    import zipfile

    out = tmp_path / 'package.xlsm'
    build(production_sheet, str(out))

    tpl = zipfile.ZipFile(DEFAULT_TEMPLATE)
    got = zipfile.ZipFile(out)
    assert set(tpl.namelist()) - set(got.namelist()) <= {'xl/calcChain.xml'}
    assert not set(got.namelist()) - set(tpl.namelist())

    must_survive_byte_for_byte = [
        'xl/vbaProject.bin',                 # the form's macros
        'docMetadata/LabelInfo.xml',         # Microsoft sensitivity label
        'customXml/item1.xml',
        'customXml/itemProps1.xml',
        'xl/styles.xml',                     # every fill, font and border
        'xl/sharedStrings.xml',
        'xl/printerSettings/printerSettings1.bin',
        'xl/comments1.xml',
    ]
    for part in must_survive_byte_for_byte:
        assert part in got.namelist(), f'{part} was dropped'
        assert hashlib.sha256(tpl.read(part)).hexdigest() == \
               hashlib.sha256(got.read(part)).hexdigest(), \
               f'{part} was modified'


def test_the_writer_never_changes_a_cell_style(production_sheet, tmp_path):
    """The tan and blue on this form are Lumen's, and the package has to
    look like the form a reviewer knows. A written cell keeps the style
    index it had; only its value changes."""
    out = tmp_path / 'styled.xlsm'
    build(production_sheet, str(out),
          job_data={'site_a': {'address': '7250 County Rd HH',
                               'clli': 'FLGLCOAC'}})
    tpl = openpyxl.load_workbook(DEFAULT_TEMPLATE)
    got = openpyxl.load_workbook(out)
    for sheet, refs in (('Site Survey Data', ['E9', 'K9', 'E11', 'F88', 'F97']),
                        ('Event Log', ['W8', 'N18', 'Q18', 'AB18']),
                        ('FAT', ['B16', 'O16', 'S16'])):
        for ref in refs:
            assert got[sheet][ref]._style == tpl[sheet][ref]._style, \
                f'{sheet}!{ref} changed style'


def test_the_template_carries_no_site_photos(production_sheet, tmp_path):
    """The template is built from a finished package, which came with
    780 KB of one customer's ILA photos. The Pictures tab is where the
    tech puts their own."""
    import zipfile
    out = tmp_path / 'nopics.xlsm'
    build(production_sheet, str(out))
    assert not [n for n in zipfile.ZipFile(out).namelist()
                if n.startswith('xl/media/')]


def test_patch_keeps_a_cell_style_and_can_create_a_missing_row(tmp_path):
    out = tmp_path / 'styled.xlsm'
    before = openpyxl.load_workbook(DEFAULT_TEMPLATE)['Event Log']['W8']._style
    patch = WorkbookPatch(DEFAULT_TEMPLATE)
    patch.set_cells([Cell('Event Log', 'W8', 42),
                     Cell('Event Log', 'AB117', 99)])
    patch.save(out)
    after = openpyxl.load_workbook(out)['Event Log']
    assert after['W8']._style == before
    assert after['AB117'].value == 99


def test_patch_writes_formulas_not_cached_values(tmp_path):
    out = tmp_path / 'formula.xlsm'
    patch = WorkbookPatch(DEFAULT_TEMPLATE)
    patch.set_cells([Cell('Event Log', 'AB40', Formula('$W$8-1'))])
    patch.save(out)
    assert openpyxl.load_workbook(out)['Event Log']['AB40'].value == '=$W$8-1'


def test_patch_forces_recalculation_on_open(tmp_path):
    """Without this the Submittal Checklist shows the Y/N answers cached
    for whatever span the template was last saved with."""
    out = tmp_path / 'recalc.xlsm'
    patch = WorkbookPatch(DEFAULT_TEMPLATE)
    patch.set_cells([Cell('Event Log', 'W8', 1)])
    patch.save(out)
    import zipfile
    wb_xml = zipfile.ZipFile(out).read('xl/workbook.xml').decode('utf-8')
    assert 'fullCalcOnLoad="1"' in wb_xml


def test_template_ships_no_customer_data(tmp_path):
    """The template is built from a real submitted package, so the build
    has to strip it: no site photos, no span values."""
    import zipfile
    names = zipfile.ZipFile(DEFAULT_TEMPLATE).namelist()
    assert not [n for n in names if n.startswith('xl/media/')]
    wb = openpyxl.load_workbook(DEFAULT_TEMPLATE, data_only=True)
    survey, events, fat = wb['Site Survey Data'], wb['Event Log'], wb['FAT']
    assert survey['E9'].value is None and survey['K9'].value is None
    assert events['W8'].value is None
    assert all(events[f'AB{r}'].value is None for r in range(18, 119))
    assert all(fat[f'B{r}'].value is None for r in range(16, 64))


# ── the whole package ─────────────────────────────────────────────────────

@pytest.fixture(scope='module')
def built_package(production_sheet, tmp_path_factory):
    out = tmp_path_factory.mktemp('fqa_out') / 'span4_fqa.xlsm'
    manifest = build(
        production_sheet, str(out),
        job_data={
            'site_a': {'address': '7250 County Rd HH', 'clli': 'FLGLCOAC',
                       'alias': 'Flagler', 'floor': '001', 'room': '0001',
                       'vendor_part': 'OCP-LC-576-5U'},
            'site_z': {'address': '32353 State Hwy 40', 'clli': 'BTHNCOAA',
                       'alias': 'Bethune', 'floor': '001', 'room': '0001',
                       'vendor_part': 'OCP-LC-576-5U'},
            'market': 'Denver', 'contractor': 'ZERODB CHRIS LINDSAY',
            'tester_1': 'DS', 'tester_2': 'TD',
            'calibration_date': '2025-01-04',
            'project': 'P.259588 / Denver to Kansas City', 'customer': 'Lumen',
        },
        closures=SPAN4_CLOSURES, span_length_m=SPAN4_LENGTH_M,
        exceptions=[{'fiber': 195, 'event': 6,
                     'description': '.162 REBURNED 3 TIMES'}],
    )
    return manifest, str(out)


def test_manifest_reports_what_it_built(built_package):
    manifest, _ = built_package
    assert manifest['ok'] is True
    assert manifest['events'] == 12
    assert manifest['fat_rows'] == 48
    assert manifest['span_length_m'] == SPAN4_LENGTH_M
    assert manifest['distance_source'] == 'trace'
    assert manifest['missing_facts'] == []


def test_event_log_matches_the_submitted_package(built_package):
    manifest, out = built_package
    ws = openpyxl.load_workbook(out, data_only=True)['Event Log']
    assert ws['W8'].value == SPAN4_LENGTH_M
    assert [ws[f'AB{r}'].value for r in range(18, 31)] == SPAN4_CLOSURES + [SPAN4_LENGTH_M]
    assert [ws[f'N{r}'].value for r in (18, 19, 28, 29)] == ['ENTRY', 10, 1, 'ENTRY']
    assert ws['Q19'].value == 'New Field Splice'
    assert ws['Q30'].value == 'New FTP/FDP Termination'


def test_the_site_z_row_is_marked_and_left_to_the_form(built_package):
    """Column AL's 'x' is what makes the form call that row Site Z, and
    the address there must stay the form's formula so it follows the
    cover page."""
    _, out = built_package
    ws = openpyxl.load_workbook(out)['Event Log']
    assert ws['AL30'].value == 'X'
    assert str(ws['D30'].value).startswith('=IF(B30="Site Z"')
    assert ws['AL29'].value is None


def test_rows_past_the_span_are_blank_not_del_row(built_package):
    _, out = built_package
    ws = openpyxl.load_workbook(out)['Event Log']
    for r in (31, 60, 118):
        assert ws[f'B{r}'].value is None
        assert ws[f'AB{r}'].value is None


def test_cover_page_carries_the_job(built_package):
    _, out = built_package
    ws = openpyxl.load_workbook(out, data_only=True)['Site Survey Data']
    assert ws['E9'].value == '7250 County Rd HH'
    assert ws['K9'].value == 'FLGLCOAC'
    assert ws['E12'].value == 'Bethune'
    assert ws['J51'].value == '100' and ws['L51'].value == '007'
    assert ws['F52'].value == '8 & 19'
    assert ws['F97'].value == '1152 Fibers'
    assert ws['F91'].value == 'P.259588 / Denver to Kansas City'


def test_fat_is_written_and_stops_at_the_fiber_count(built_package):
    _, out = built_package
    ws = openpyxl.load_workbook(out, data_only=True)['FAT']
    assert ws['O16'].value == '001-024'
    assert ws['O63'].value == '1129-1152'
    assert ws['S34'].value == '001-024'      # lateral restarts on Cable 2
    assert ws['B64'].value is None


def test_fat_rack_columns_stay_wired_to_the_cover_page(built_package):
    _, out = built_package
    ws = openpyxl.load_workbook(out)['FAT']
    assert ws['F16'].value == "='Site Survey Data'!J51"
    assert ws['X63'].value == "='Site Survey Data'!F60"


def test_exceptions_land_and_the_rest_of_the_block_is_clear(built_package):
    _, out = built_package
    ws = openpyxl.load_workbook(out, data_only=True)['Exception Reporting']
    assert (ws['B12'].value, ws['E12'].value) == ('195', '6')
    assert ws['G12'].value == '.162 REBURNED 3 TIMES'
    assert ws['B13'].value is None


def test_a_span_with_fewer_fibers_shortens_the_fat(production_sheet, tmp_path):
    out = tmp_path / 'small.xlsm'
    manifest = build(production_sheet, str(out),
                     job_data={'fiber_count': 144},
                     closures=SPAN4_CLOSURES, span_length_m=SPAN4_LENGTH_M)
    assert manifest['fat_rows'] == 6
    ws = openpyxl.load_workbook(out, data_only=True)['FAT']
    assert ws['O21'].value == '121-144'
    assert ws['O22'].value is None


def test_a_template_without_the_tabs_is_refused(tmp_path):
    """Pointed at the wrong workbook the writer must say so, not write
    cells into whatever tabs happen to be there."""
    bad = tmp_path / 'not_an_fqa.xlsx'
    openpyxl.Workbook().save(bad)
    empty = FqaBuild(job=JobFacts(), chain=EventChain(events=[]))
    with pytest.raises(ValueError, match='FQA Site Survey form'):
        write_fqa(str(bad), str(tmp_path / 'x.xlsm'), empty)


def test_the_shipped_template_is_the_revision_the_cell_map_was_read_off():
    assert form_version(DEFAULT_TEMPLATE) == EXPECTED_FORM_VERSION


def test_any_revision_of_the_lumen_form_is_accepted(production_sheet, tmp_path):
    """Two revisions are in circulation and a tech's own blank may be
    either. Refusing one would block the work; the revision is reported
    in the completeness list instead."""
    older = tmp_path / 'older_form.xlsm'
    patch = WorkbookPatch(DEFAULT_TEMPLATE)
    # Blank the Version History tab, which is what the older form lacks.
    patch.set_cells([Cell('Version History', f'B{r}', None) for r in (4, 5)])
    patch.save(older)
    assert form_version(str(older)) is None

    m = build(production_sheet, str(tmp_path / 'built.xlsm'),
              template=str(older))
    assert m['ok'] is True
    assert m['form_revision'] is None
    form = [g for g in m['completeness'] if g['where'] == 'Form']
    assert len(form) == 1
    assert 'no Version History tab' in form[0]['what']
    assert form[0]['level'] == 'check'          # reported, never blocking


def test_the_shipped_revision_raises_no_form_gap(production_sheet, tmp_path):
    m = build(production_sheet, str(tmp_path / 'b.xlsm'))
    assert m['form_revision'] == EXPECTED_FORM_VERSION
    assert not [g for g in m['completeness'] if g['where'] == 'Form']


def test_engine_prints_exactly_one_manifest_line(production_sheet, tmp_path):
    """The subprocess contract the hub relies on: one JSON line on stdout
    and nothing else, whatever the engine printed while working."""
    out = tmp_path / 'cli.xlsm'
    proc = subprocess.run(
        [sys.executable, '-m', 'fqa.run_fqa',
         '--production', production_sheet, '--out', str(out)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180)
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    assert len(lines) == 1, proc.stdout
    manifest = json.loads(lines[0])
    assert manifest['ok'] is True
    assert os.path.exists(out)


def test_engine_reports_a_bad_input_as_a_manifest_not_a_traceback(tmp_path):
    proc = subprocess.run(
        [sys.executable, '-m', 'fqa.run_fqa',
         '--production', str(tmp_path / 'nope.xlsx'),
         '--out', str(tmp_path / 'x.xlsm')],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
    manifest = json.loads(proc.stdout.strip().splitlines()[-1])
    assert manifest['ok'] is False
    assert 'FileNotFoundError' in manifest['error']


# ── the app's own paste parsing ───────────────────────────────────────────

def _parse_closures(text):
    """Pulled out of the source: fqa.ui imports streamlit at module scope."""
    path = os.path.join(REPO_ROOT, 'fqa', 'ui.py')
    src = open(path, encoding='utf-8').read()
    start = src.index('def _parse_closures')
    end = src.index('def _parse_exceptions')
    ns: dict = {}
    exec(compile(src[start:end], path, 'exec'), ns)
    return ns['_parse_closures'](text)


@pytest.mark.parametrize('text', [
    '60, 5010, 7650', '60 5010 7650', '[60, 5010, 7650]', '60\n5010\n7650'])
def test_pasted_distances_take_any_separator(text):
    assert _parse_closures(text) == [60.0, 5010.0, 7650.0]


def test_the_unit_is_decided_for_the_list_not_the_value():
    """Regression: a per-value rule read the 60 m entry splice as 60 km
    and left the rest of the chain in metres."""
    assert _parse_closures('60, 5010, 54330') == [60.0, 5010.0, 54330.0]
    assert _parse_closures('0.06, 5.01, 54.33') == [60.0, 5010.0, 54330.0]


def test_a_non_number_is_refused_by_name():
    """A unit suffix has to be rejected loudly.  Silently dropping it
    would shift every later event onto the wrong vault -- and the comma
    is a separator here, so '5,010m' arrives as '5' and '010m'."""
    with pytest.raises(ValueError, match="'010m' is not a number"):
        _parse_closures('60 5,010m')


# ── the app ───────────────────────────────────────────────────────────────

def _fqa_app(tmp_path, **kwargs):
    """The standalone app, which is a thin wrapper over the same render()
    the hub calls -- so driving it here exercises the hub page too."""
    from streamlit.testing.v1 import AppTest
    return AppTest.from_file(os.path.join(REPO_ROOT, 'fqa', 'app.py'),
                             default_timeout=120, **kwargs)


def test_app_starts_and_asks_for_a_production_sheet(tmp_path):
    at = _fqa_app(tmp_path).run()
    assert not at.exception
    assert any('FQA Builder' in m.value for m in at.markdown)
    # Drag-and-drop / Browse is the front door; the path box is the
    # way round the browser for the 250 MB sheets.
    assert len(at.get('file_uploader')) == 1
    assert at.text_input[0].label == 'Production sheet path'


def test_app_reads_a_sheet_and_prefills_the_job(production_sheet, tmp_path):
    at = _fqa_app(tmp_path).run()
    at.text_input[0].set_value(production_sheet).run()
    assert not at.exception
    assert any('14 locations' in s.value for s in at.success)
    labels = {t.label: t.value for t in at.text_input}
    assert labels['A alias'] == 'Flagler'
    assert labels['Z alias'] == 'Bethune'
    assert labels['Aisle'] == '100'          # first Aisle box is Site A's


def test_a_sheet_with_no_termination_table_says_the_fat_is_empty(production_sheet,
                                                                 tmp_path):
    """Two real spans (Clinton to Rock Falls, Durkee to Ontario) have an
    entirely empty Termination Information table.  There is no fibre count
    to read, so the FAT cannot be built, and that has to be said rather
    than shipped as a blank tab."""
    wb = openpyxl.load_workbook(production_sheet)
    for name in wb.sheetnames:
        ws = wb[name]
        if str(ws['C31'].value or '').lower().startswith('termination info'):
            for r in (34, 35):
                for col in ('C', 'I', 'R', 'X', 'AC'):
                    ws[f'{col}{r}'] = None
    out = tmp_path / 'no_term_table.xlsx'
    wb.save(out)
    m = build(str(out), str(tmp_path / 'x.xlsm'))
    assert m['fiber_count'] is None
    assert m['fat_rows'] == 0
    assert any('no fibre count' in w for w in m['warnings'])
    assert 'number of fibers tested' in m['missing_facts']


def test_app_builds_a_package_from_pasted_distances(production_sheet, tmp_path):
    """The whole glue path: paste the measured distances, press the button,
    get a workbook whose Event Log carries them."""
    at = _fqa_app(tmp_path).run()
    at.text_input[0].set_value(production_sheet).run()

    out_dir = tmp_path / 'out'
    out_dir.mkdir()
    by_label = {t.label: t for t in at.text_input}
    by_label['Save to'].set_value(str(out_dir))
    by_label['File name'].set_value('built.xlsm')
    at.text_area[0].set_value(' '.join(str(d) for d in SPAN4_CLOSURES))
    at.text_area[1].set_value('195, 6, .162 REBURNED 3 TIMES')
    at.number_input[0].set_value(SPAN4_LENGTH_M)     # span length
    at.button[0].click().run()

    assert not at.exception
    built = out_dir / 'built.xlsm'
    assert built.exists()
    ws = openpyxl.load_workbook(built, data_only=True)['Event Log']
    assert ws['W8'].value == SPAN4_LENGTH_M
    assert [ws[f'AB{r}'].value for r in range(18, 30)] == SPAN4_CLOSURES
    assert ws['G12'] is not None
    # Distances came off the paste, so the footage-fallback warning must
    # NOT be shown -- and the two bad segments must be.
    warnings = ' '.join(w.value for w in at.warning)
    assert 'footage marks, not from a trace' not in warnings
    assert 'Splice 3 → Splice 2' in warnings


def test_app_refuses_a_distance_count_that_does_not_match(production_sheet,
                                                          tmp_path):
    """Ten distances for twelve vaults would put the wrong distance on the
    wrong row, so the build has to stop rather than zip them together."""
    at = _fqa_app(tmp_path).run()
    at.text_input[0].set_value(production_sheet).run()
    at.text_area[0].set_value(' '.join(str(d) for d in SPAN4_CLOSURES[:10]))
    at.button[0].click().run()
    assert not at.exception
    assert any('12 splice locations' in e.value for e in at.error)


# ── a second project's conventions ────────────────────────────────────────
#
# Stradford to El Paso writes its production sheets differently from Denver
# to Kansas City, and every difference below broke something the first
# time it was tried against a real package:
#
#   headings      SOUTH/WEST and NORTH/EAST, not East and West
#   laterals      both labelled SOUTH/WEST on the termination sheet
#   RMU           'H05.02 RMU 13', so the first number in the cell is not it
#   From Fibers   a bare count ('288'), not a range ('577-1152')
#   lateral size  576-count cables carrying 432 fibres each

TUCU_CLOSURES = [60, 8170, 15440, 23310, 31420, 38930, 46710,
                 54570, 61550, 69570, 76870, 84910, 92850, 98360]
TUCU_LENGTH_M = 98410


def _write_compass_splice(ws, name, address, toward_z, toward_a,
                          laterals=None):
    _write_splice_sheet(ws, name, address, None, None)
    ws['L30'] = ws['R30'] = ws['X30'] = None
    ws['L31'] = ws['R31'] = ws['X31'] = None
    row = 30
    for mark, heading in ((toward_a, 'NORTH/EAST'), (toward_z, 'SOUTH/WEST')):
        if mark is None:
            continue
        ws[f'C{row}'] = 'CORNING 864CT 08/25'
        ws[f'L{row}'] = f'{mark:05d}FT'
        ws[f'R{row}'] = f'{mark + 50:05d}FT'
        ws[f'X{row}'] = heading
        row += 1
    if laterals:
        for i, size in enumerate(laterals, 1):
            ws[f'C{row}'] = f'COMMSCOPE {size}CT 06/25'
            ws[f'L{row}'] = '12578FT'
            ws[f'X{row}'] = f'CABLE {i}'
            row += 1
        # Splice Information: how the backbone divides across the laterals.
        ws['C37'] = 'Splice Information'
        ws['C38'], ws['H38'] = 'DATE', 'CORNING 864CT 08/25'
        ws['H40'] = 'SOUTH/WEST'
        for i in range(len(laterals)):
            col = 'LP'[i]
            ws[f'{col}38'] = f'COMMSCOPE {laterals[i]}CT 06/25'
            ws[f'{col}40'] = f'CABLE {i + 1}'
            ws[f'{col}42'] = '1-432'
        ws['H42'] = '1-864'


@pytest.fixture(scope='module')
def compass_sheet(tmp_path_factory):
    path = tmp_path_factory.mktemp('fqa2') / 'tucumcari_production.xlsx'
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    for title, bay in (('Termination', 'H05.02'),):
        ws = wb.create_sheet(title)
        _write_termination_sheet(ws, 'TUCUMCARI ILA',
                                 '35.2248667, -103.6141389', bay, [576, 576])
        # Both laterals labelled with the compass heading they leave on,
        # and the fibre column a bare count.
        ws['AB28'] = ws['AB29'] = 'SOUTH/WEST'
        ws['C29'] = None
        ws['C34'], ws['R34'], ws['X34'] = 'H05.02 RMU 13', '576', '576'
        ws['C35'], ws['R35'], ws['X35'] = 'H05.02 RMU 19', '288', '288'

    _write_compass_splice(wb.create_sheet('ENTRY TUCUMCARI'), 'ENTRY TUCUMCARI',
                          '35 13 29.5 N 103 36 50.9 W',
                          toward_z=26752, toward_a=None, laterals=[576, 576])
    marks = [(26356, 344), (26670, 2640), (26648, 672), (26462, 126),
             (26446, 2104), (26428, 776), (26296, 566), (26608, 3682),
             (24958, 510), (26742, 994), (26580, 514), (26038, 626)]
    for i, (sw, ne) in enumerate(marks, 1):
        _write_compass_splice(wb.create_sheet(f'splice {i}'), f'splice {i}',
                              f'GPS {i}', toward_z=sw, toward_a=ne)
    _write_compass_splice(wb.create_sheet('ENTRY SANTA ROSA'), 'ENTRY SANTA ROSA',
                          '34 58 27.7 N 104 34 56.8 W',
                          toward_z=None, toward_a=7958, laterals=[432, 432])

    ws = wb.create_sheet('Termination 2')
    _write_termination_sheet(ws, 'SANTA ROSA ILA',
                             '34 58 27.23 N 104 34 57.17 W', 'H05.01', [432, 432])
    ws['AB28'] = ws['AB29'] = 'NORTH/EAST'
    ws['C29'] = None
    ws['C34'], ws['R34'], ws['X34'] = 'H05.01 RMU 13', '576', '576'
    ws['C35'], ws['R35'], ws['X35'] = 'H05.01 RMU 19', '288', '288'
    wb.save(path)
    return str(path)


def test_headings_are_learnt_from_the_span_not_assumed(compass_sheet):
    prod = read_production_sheet(compass_sheet)
    assert span_heading(prod) == ('south/west', 'north/east')


def test_a_span_with_no_headings_turns_the_crosscheck_off(production_sheet,
                                                          tmp_path):
    """Guessing east/west on a sheet that names neither would pair two
    cables that are not on the same reel and invent a distance."""
    wb = openpyxl.load_workbook(production_sheet)
    for name in wb.sheetnames:
        ws = wb[name]
        for r in range(30, 34):
            if ws[f'X{r}'].value:
                ws[f'X{r}'] = None
    out = tmp_path / 'no_headings.xlsx'
    wb.save(out)
    prod = read_production_sheet(str(out))
    assert span_heading(prod) == ('', '')
    chain = build_chain(prod, trace_distances_m=SPAN4_CLOSURES,
                        span_length_m=SPAN4_LENGTH_M)
    assert all(e.delta_m is None for e in chain.splice_events[1:])


def test_termination_cable_rows_are_laterals_whatever_they_are_labelled(compass_sheet):
    """Tucumcari labels both of its laterals SOUTH/WEST, which is where
    they leave the building. The backbone never reaches the frame, so a
    termination's cable rows are laterals by definition."""
    prod = read_production_sheet(compass_sheet)
    assert prod.site_a.backbone_cables == []
    assert len(prod.site_a.lateral_cables) >= 1


def test_lateral_size_is_the_smaller_of_the_cable_and_what_was_spliced(compass_sheet):
    """576-count laterals carrying 432 fibres each: the part number alone
    says 576 and only the entry splice's table says 432."""
    prod = read_production_sheet(compass_sheet)
    entry = prod.locations[1]
    assert entry.assignments.get('cable 1') == 432
    assert lateral_cable_sizes(prod.site_a, entry) == [432, 432]


def test_rmu_is_the_number_after_rmu_not_the_first_in_the_cell(compass_sheet):
    prod = read_production_sheet(compass_sheet)
    job = derive(prod)
    assert job.site_a.rmu == '13 & 19'
    assert job.site_a.aisle == '005' and job.site_a.bay == '002'
    # Floor and room are not in a production sheet, so the device string
    # stays None until a person supplies them.
    assert job.site_a.test_from_device is None
    job.site_a.floor, job.site_a.room = '001', 'H101'
    assert job.site_a.test_from_device == '001.H101..005.002.13 & 19'


def test_fiber_count_reads_bare_counts_as_well_as_ranges(compass_sheet):
    prod = read_production_sheet(compass_sheet)
    assert derive(prod).fiber_count == 864          # 576 + 288


def test_block_label_follows_how_full_each_panel_is(compass_sheet,
                                                    production_sheet):
    """576 fibres fill trays A-X and 288 fill A-L. Writing A-X for a
    half-loaded panel claims twelve trays that are not in use."""
    assert derive(read_production_sheet(compass_sheet)).site_a.block == 'A-X & A-L'
    assert derive(read_production_sheet(production_sheet)).site_a.block == 'A-X & A-X'


def test_864_fat_splits_its_laterals_at_433(compass_sheet):
    prod = read_production_sheet(compass_sheet)
    rows = build_fat(864, prod.site_a, prod.site_z,
                     entry_a=prod.locations[1], entry_z=prod.locations[-2])
    assert len(rows) == 36
    by_backbone = {r.backbone: (r.lateral_a, r.block_a) for r in rows}
    assert by_backbone['409-432'] == ('409-432', 'R')
    assert by_backbone['433-456'] == ('001-024', 'S')
    assert by_backbone['481-504'] == ('049-072', 'U')
    assert by_backbone['841-864'] == ('409-432', 'L')   # blocks restart on RMU 19


def test_the_site_a_row_gets_its_event_type(compass_sheet, tmp_path):
    """Regression: the A-end row was never written, so it kept whatever
    the template had -- 'New' on a hut that has been there for years."""
    out = tmp_path / 'preexisting.xlsm'
    build(compass_sheet, str(out), closures=TUCU_CLOSURES,
          span_length_m=TUCU_LENGTH_M, entry_offset_m=60, entry_offset_z_m=50,
          termination_type='Pre-Existing FTP/FDP Termination')
    ws = openpyxl.load_workbook(out, data_only=True)['Event Log']
    assert ws['Q17'].value == 'Pre-Existing FTP/FDP Termination'
    assert ws['Q32'].value == 'Pre-Existing FTP/FDP Termination'
    assert ws['Q18'].value == 'New Field Splice'


def test_the_two_ends_can_have_different_entry_offsets(compass_sheet, tmp_path):
    """Tucumcari is 60 m at the A end and 50 m at the Z end."""
    out = tmp_path / 'offsets.xlsm'
    m = build(compass_sheet, str(out), entry_offset_m=60, entry_offset_z_m=50)
    log = m['event_log']
    assert log[1]['from_a_m'] - log[0]['from_a_m'] == 60
    assert log[-1]['from_a_m'] - log[-2]['from_a_m'] == 50


def test_the_tolerance_scales_with_the_segment(compass_sheet):
    """A fixed 75 m is too tight on a 7.5 km segment: Tucumcari's honest
    marks sit ~87 m out there, which is 1.2%."""
    prod = read_production_sheet(compass_sheet)
    chain = build_chain(prod, trace_distances_m=TUCU_CLOSURES,
                        span_length_m=TUCU_LENGTH_M,
                        entry_offset_m=60, entry_offset_z_m=50)
    assert chain.distance_source == 'trace'
    assert chain.warnings == []
    tight = build_chain(prod, trace_distances_m=TUCU_CLOSURES,
                        span_length_m=TUCU_LENGTH_M, entry_offset_m=60,
                        entry_offset_z_m=50, tolerance_pct=0.0)
    assert tight.warnings, 'a percentage of zero must reinstate the flags'


# ── the hub page ──────────────────────────────────────────────────────────

def test_the_hub_offers_the_fqa_builder():
    from conftest import run_streamlit
    at = run_streamlit(default_timeout=180).run()
    assert not at.exception
    tool = next(r for r in at.sidebar.radio if r.label == 'Tool')
    assert tool.options == ['Viewer', 'Splice Report', 'Unidirectional',
                            'Secret Sauce', 'FQA Builder']


def test_the_hub_page_renders_the_same_ui_as_the_standalone_app():
    """One copy of the interface, called two ways. If these drift, a fix
    lands in the app the tech is not using."""
    from conftest import run_streamlit
    at = run_streamlit(default_timeout=180).run()
    tool = next(r for r in at.sidebar.radio if r.label == 'Tool')
    at = tool.set_value('FQA Builder').run()
    assert not at.exception
    assert any('FQA Builder' in m.value for m in at.markdown)
    # The hub's own trace drop-zone lives in the sidebar, so count only
    # the uploader the page itself drew.
    assert len(at.main.get('file_uploader')) == 1


def test_the_builder_ships_in_the_exe_and_in_the_auto_update():
    """A page nobody can install is not shipped. Every module the page
    imports, and the blank form it patches, has to be in both lists."""
    launcher = (REPO_ROOT / 'desktop' / 'launcher.py').read_text(encoding='utf-8')
    for rel in ('fqa/ui.py', 'fqa/run_fqa.py', 'fqa/writer.py',
                'fqa/production_sheet.py', 'fqa/event_chain.py', 'fqa/fat.py',
                'fqa/job_facts.py', 'fqa/xlsx_patch.py',
                'fqa/templates/FQA_Site_Survey_v1_1.xlsm'):
        assert f'"{rel}"' in launcher, f'{rel} missing from ENGINE_FILES'
    for spec in ('OTDRSuite.spec', 'OTDRSuite-mac.spec'):
        text = (REPO_ROOT / 'desktop' / spec).read_text(encoding='utf-8')
        assert '_add_tree("fqa", (".py", ".xlsm"))' in text, spec


def test_every_fqa_module_is_listed_for_auto_update():
    """Adding a module without listing it ships a broken engine cache:
    the file is in the exe but never updated, so a fix never reaches the
    fleet."""
    launcher = (REPO_ROOT / 'desktop' / 'launcher.py').read_text(encoding='utf-8')
    for path in sorted((REPO_ROOT / 'fqa').glob('*.py')):
        rel = f'fqa/{path.name}'
        assert f'"{rel}"' in launcher, f'{rel} missing from ENGINE_FILES'


# ── the completeness audit ────────────────────────────────────────────────

def test_the_audit_names_the_job_facts_nobody_typed(production_sheet, tmp_path):
    m = build(production_sheet, str(tmp_path / 'bare.xlsm'))
    job = [g for g in m['completeness'] if g['where'] == 'Job facts']
    assert {g['what'] for g in job} >= {
        'A site address', 'A site CLLI', 'splicing contractor', 'tester #1'}
    assert all(g['level'] == 'blocking' for g in job)
    assert all(g['fix'] for g in job), 'every gap says what the field is'


def test_the_audit_shrinks_as_the_facts_are_supplied(production_sheet, tmp_path):
    bare = build(production_sheet, str(tmp_path / 'a.xlsm'))
    filled = build(production_sheet, str(tmp_path / 'b.xlsm'), job_data={
        'site_a': {'address': '7250 County Rd HH', 'clli': 'FLGLCOAC',
                   'vendor_part': 'OCP-LC-576-5U'},
        'site_z': {'address': '32353 State Hwy 40', 'clli': 'BTHNCOAA',
                   'vendor_part': 'OCP-LC-576-5U'},
        'contractor': 'ZERODB', 'tester_1': 'DS',
        'calibration_date': '2025-01-04'})
    assert (filled['completeness_summary']['blocking']
            < bare['completeness_summary']['blocking'])
    assert not [g for g in filled['completeness'] if g['where'] == 'Job facts']


def test_footage_only_distances_are_a_blocking_gap(production_sheet, tmp_path):
    """A package whose Event Log was never measured is not finished, even
    though it opens and prints."""
    m = build(production_sheet, str(tmp_path / 'f.xlsm'))
    assert m['distance_source'] == 'footage'
    assert any('footage marks' in g['what'] and g['level'] == 'blocking'
               for g in m['completeness'])

    measured = build(production_sheet, str(tmp_path / 'm.xlsm'),
                     closures=SPAN4_CLOSURES, span_length_m=SPAN4_LENGTH_M)
    assert not [g for g in measured['completeness']
                if 'footage marks' in g['what'] and g['level'] == 'blocking']


def test_the_audit_names_the_sheet_and_the_field(production_sheet, tmp_path):
    """A gap has to say WHERE. 'no address' across sixteen tabs is not
    something anyone can act on."""
    wb = openpyxl.load_workbook(production_sheet)
    wb['Splice 7']['G7'] = None                     # blank its address
    wb['Splice 7']['G16'] = None                    # and its enclosure
    out = tmp_path / 'holed.xlsx'
    wb.save(out)
    m = build(str(out), str(tmp_path / 'x.xlsm'))
    theirs = [g for g in m['completeness'] if g['where'] == 'Splice 7']
    assert any(g['what'] == 'no address or GPS fix'
               and g['level'] == 'blocking' for g in theirs)
    assert any('enclosure manufacturer' in g['what'] for g in theirs)


def test_reconciliation_disagreements_are_in_the_list(production_sheet, tmp_path):
    m = build(production_sheet, str(tmp_path / 'r.xlsm'),
              closures=SPAN4_CLOSURES, span_length_m=SPAN4_LENGTH_M)
    log = [g for g in m['completeness'] if g['where'] == 'Event Log']
    assert any('Splice 3 → Splice 2' in g['what'] for g in log)
    assert all(g['level'] == 'check' for g in log if 'Splice' in g['what'])


def test_blocking_items_sort_above_the_rest(production_sheet, tmp_path):
    m = build(production_sheet, str(tmp_path / 's.xlsm'))
    levels = [g['level'] for g in m['completeness']]
    assert levels == sorted(levels, key=lambda l: 0 if l == 'blocking' else 1)


def test_the_app_shows_the_not_complete_section(production_sheet, tmp_path):
    at = _fqa_app(tmp_path).run()
    at.text_input[0].set_value(production_sheet).run()
    assert not at.exception
    assert any('Not complete' in m.value for m in at.markdown)
    assert any('will send the package back' in m.value for m in at.markdown)


def test_the_calibration_date_starts_empty_not_today(production_sheet, tmp_path):
    """Defaulting it to today answers a question nobody asked: the field
    stops reading as empty, the completeness list stops naming it, and
    the package ships claiming the OTDR was calibrated that morning."""
    at = _fqa_app(tmp_path).run()
    at.text_input[0].set_value(production_sheet).run()
    cal = next(d for d in at.date_input
               if d.label == 'Test-equipment calibration')
    assert cal.value is None
    # ...and the completeness table says so.  The page draws several
    # tables; the blocking one is the table with a 'Missing' column.
    blocking = next(d.value for d in at.dataframe
                    if 'Missing' in getattr(d.value, 'columns', []))
    assert any('calibration' in str(x) for x in blocking['Missing'])


# ── the form's own event-row formulas ─────────────────────────────────────

def test_every_event_row_carries_the_forms_formulas(built_package):
    """Four of the Event Log's columns are the form's own arithmetic, and
    building the template blanks them on every row (it has to, or a short
    span prints DEL ROW down the page). Each row we use must have them
    put back, or the package goes to Lumen with no event numbers and no
    from-Z or to-next distances.

    This was invisible to a spot check of the cells we write. It only
    showed up in a sweep of every cell Lumen's own package fills.
    """
    _, out = built_package
    ws = openpyxl.load_workbook(out)['Event Log']
    for row in range(18, 31):                      # 12 events plus Site Z
        assert ws[f'B{row}'].value, f'B{row} has no event number'
        assert str(ws[f'X{row}'].value).startswith('=IF(Z')
        assert str(ws[f'Z{row}'].value).startswith('=IF(B')
        assert ws[f'AE{row}'].value == f'=$W$8-AB{row}'
        assert ws[f'AH{row}'].value == f'=IF(B{row}="Site Z",0,AB{row + 1}-AB{row})'
        assert ws[f'AM{row}'].value == f'=B{row}'


def test_the_first_event_row_starts_the_count_at_one(built_package):
    """Row 18 cannot count on from the row above it -- that is Site A --
    so it carries the form's own special case."""
    _, out = built_package
    ws = openpyxl.load_workbook(out)['Event Log']
    assert ws['B18'].value == '=IF(AL18="x", "Site Z",1)'
    assert 'ISNUMBER(B18)' in ws['B19'].value


def test_rows_past_the_span_keep_no_formulas(built_package):
    """The clearing still has to win below Site Z, or the form prints
    DEL ROW for ninety rows."""
    _, out = built_package
    ws = openpyxl.load_workbook(out)['Event Log']
    for row in (31, 60, 118):
        for col in ('B', 'X', 'Z', 'AE', 'AH', 'AM'):
            assert ws[f'{col}{row}'].value is None, f'{col}{row} survived'


def test_panel_rack_units_come_off_the_vendor_part_number(production_sheet,
                                                          tmp_path):
    """An OCP-LC-576-5U is a 5U panel. Nothing on either production sheet
    carries that, and the FQA asks for it on the panel-attributes row."""
    m = build(production_sheet, str(tmp_path / 'ru.xlsm'), job_data={
        'site_a': {'vendor_part': 'OCP-LC-576-5U'},
        'site_z': {'vendor_part': 'OCP-LC-288-4U'}})
    assert m['job']['site_a']['panel_rmus'] == 5
    assert m['job']['site_z']['panel_rmus'] == 4
    ws = openpyxl.load_workbook(str(tmp_path / 'ru.xlsm'), data_only=True)
    assert ws['Site Survey Data']['M53'].value == 5
    assert ws['Site Survey Data']['M61'].value == 4


def test_a_part_number_with_no_rack_units_stays_blank(production_sheet,
                                                      tmp_path):
    m = build(production_sheet, str(tmp_path / 'noru.xlsm'),
              job_data={'site_a': {'vendor_part': 'SOME-PANEL'}})
    assert m['job']['site_a']['panel_rmus'] is None
