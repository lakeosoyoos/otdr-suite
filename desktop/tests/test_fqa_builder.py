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
from fqa.writer import Exception_, FqaBuild, write_fqa                               # noqa: E402
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
    """Imported lazily: fqa.app runs Streamlit at module scope."""
    import importlib.util
    path = os.path.join(REPO_ROOT, 'fqa', 'app.py')
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
    from streamlit.testing.v1 import AppTest
    return AppTest.from_file(os.path.join(REPO_ROOT, 'fqa', 'app.py'),
                             default_timeout=120, **kwargs)


def test_app_starts_and_asks_for_a_production_sheet(tmp_path):
    at = _fqa_app(tmp_path).run()
    assert not at.exception
    assert any('FQA Builder' in m.value for m in at.markdown)
    assert at.text_input[0].label == 'Production sheet'


def test_app_reads_a_sheet_and_prefills_the_job(production_sheet, tmp_path):
    at = _fqa_app(tmp_path).run()
    at.text_input[0].set_value(production_sheet).run()
    assert not at.exception
    assert any('14 locations' in s.value for s in at.success)
    labels = {t.label: t.value for t in at.text_input}
    assert labels['A alias'] == 'Flagler'
    assert labels['Z alias'] == 'Bethune'
    assert labels['Aisle'] == '100'          # first Aisle box is Site A's


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
