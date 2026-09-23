"""
Filling the Lumen FQA Site Survey workbook
==========================================

Every cell address in this file was read off real submitted packages --
Span 4 Flagler to Bethune (Titanium, 1152ct) and the two Cle Elum 144f
packages -- and they agree, because the form is a controlled Lumen
document (Version History tab: v1.1, published 2025-11-14).  The
addresses are therefore constants of the form, not of a span, and they
are written down here once.

What this module deliberately does NOT write
--------------------------------------------
The form computes a great deal of itself.  The Event Log's 'from Z'
column is $W$8 minus 'from A'; its 'to next event' column is the next
row minus this one; its event counter sums a helper column; the FAT's
rack columns point back at the Site Survey Data tab; the whole Submittal
Checklist is formulas over the other tabs.  All of that is left alone.
We write the facts and let the form do its arithmetic, because a
reviewer who edits one cell by hand should see the rest follow -- which
is exactly what stops happening when a generator flattens a form to
literals.

The one place we put a formula back is the Site Z row of the Event Log.
Its pristine formula is keyed on the 'x' marker in column AL, and that
marker moves with the span's event count, so the row that becomes Site Z
gets the form's own formula written into it rather than a literal
address.  (The submitted Span 4 package has a literal there, which is
why its Site Z row no longer follows the cover page.)

Robert, 2026-09-23.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import openpyxl

from .event_chain import EventChain, SITE_A, SITE_Z
from .fat import FatRow
from .job_facts import JobFacts
from .xlsx_patch import Cell, Formula, WorkbookPatch

# The revision of the Lumen form this cell map was read off.  Two are in
# circulation: this one, which carries a Version History tab, and an older
# 7-tab one without it.  Their layouts are identical for every cell we
# write -- checked row by row -- but "identical everywhere I looked" is
# not a guarantee, and writing a customer's submittal into a form we have
# not verified is not a risk worth taking silently.  So the revision is
# checked, and adopting a new one is a deliberate act (make_template).
EXPECTED_FORM_VERSION = '1.1'
_VERSION_RE = re.compile(r'^\d+(\.\d+)*$')
SHEET_VERSION = 'Version History'

SHEET_SURVEY = 'Site Survey Data'
SHEET_FAT = 'FAT'
SHEET_EVENTS = 'Event Log'
SHEET_EXCEPTIONS = 'Exception Reporting'

# ── Event Log geometry ────────────────────────────────────────────────────
EVENT_FIRST_ROW = 18            # row 17 is Site A and is never a splice
EVENT_LAST_ROW = 118            # the form's own limit (AO17 sums AO18:AO118)
EVENT_SITE_A_ROW = 17

EV_NUMBER = 'B'
EV_LOCATION = 'D'
EV_VAULT = 'N'
EV_TYPE = 'Q'
EV_FIBER_FROM = 'X'
EV_FIBER_TO = 'Z'
EV_DIST_A = 'AB'
EV_DIST_Z = 'AE'
EV_TO_NEXT = 'AH'
EV_LAST_MARK = 'AL'
EV_HELPER = 'AM'

# Columns to clear on the rows past the end of the span, so the form does
# not print 'DEL ROW' down the page.  A and AO are left: they are the
# helper column and the event counter, and both read as 0 when the row
# beside them is empty.
EVENT_CLEAR_COLS = (EV_NUMBER, EV_LOCATION, EV_VAULT, EV_TYPE, EV_FIBER_FROM,
                    EV_FIBER_TO, EV_DIST_A, EV_DIST_Z, EV_TO_NEXT,
                    EV_LAST_MARK, EV_HELPER)

# ── FAT geometry ──────────────────────────────────────────────────────────
FAT_FIRST_ROW = 16
FAT_LAST_ROW = 879              # the form's merges run this far

FAT_PORT_A = 'B'
FAT_AISLE_A = 'F'
FAT_BAY_A = 'G'
FAT_RMU_A = 'H'
FAT_BLOCK_A = 'J'
FAT_LATERAL_A = 'L'
FAT_BACKBONE_A = 'O'
FAT_BACKBONE_Z = 'Q'
FAT_LATERAL_Z = 'S'
FAT_AISLE_Z = 'V'
FAT_BAY_Z = 'W'
FAT_RMU_Z = 'X'
FAT_BLOCK_Z = 'Z'
FAT_PORT_Z = 'AB'

FAT_CLEAR_COLS = (FAT_PORT_A, FAT_AISLE_A, FAT_BAY_A, FAT_RMU_A, FAT_BLOCK_A,
                  FAT_LATERAL_A, FAT_BACKBONE_A, FAT_BACKBONE_Z,
                  FAT_LATERAL_Z, FAT_AISLE_Z, FAT_BAY_Z, FAT_RMU_Z,
                  FAT_BLOCK_Z, FAT_PORT_Z)

# The FAT's rack columns are the form's own references into the cover
# page.  Writing them as formulas keeps one source of truth for the rack
# position: correct it on the cover and all 48 FAT rows follow.
FAT_RACK_FORMULAS = {
    FAT_AISLE_A: "'Site Survey Data'!J51",
    FAT_BAY_A: "'Site Survey Data'!L51",
    FAT_RMU_A: "'Site Survey Data'!F52",
    FAT_AISLE_Z: "'Site Survey Data'!J59",
    FAT_BAY_Z: "'Site Survey Data'!L59",
    FAT_RMU_Z: "'Site Survey Data'!F60",
}

# ── Exception Reporting geometry ──────────────────────────────────────────
EXC_FIRST_ROW = 12
EXC_LAST_ROW = 32               # 'Do Not Attach Data To This Package.' is 33
EXC_FIBER = 'B'
EXC_EVENT = 'E'
EXC_TEXT = 'G'


@dataclass
class Exception_:
    """One row of the Exception Reporting tab: a fibre Lumen must be told
    about.  `event` is the Event Log event number the issue sits at, or
    None when it is not at a splice."""
    fiber: object
    event: object = None
    description: str = ''


@dataclass
class FqaBuild:
    """Everything that goes into one FQA workbook."""
    job: JobFacts
    chain: EventChain
    fat_rows: list[FatRow] = field(default_factory=list)
    exceptions: list[Exception_] = field(default_factory=list)


def _header_block(job: JobFacts) -> list[Cell]:
    """The identical A/Z/contractor/tester header that three tabs repeat."""
    a, z = job.site_a, job.site_z
    out: list[Cell] = []

    # FAT
    out += [
        Cell(SHEET_FAT, 'E3', a.header_text),
        Cell(SHEET_FAT, 'E4', z.header_text),
        Cell(SHEET_FAT, 'E5', job.market),
        Cell(SHEET_FAT, 'G6', a.test_from_device),
        Cell(SHEET_FAT, 'G7', z.test_from_device),
        Cell(SHEET_FAT, 'V3', job.contractor),
        Cell(SHEET_FAT, 'V4', job.tester_1),
        Cell(SHEET_FAT, 'V5', job.tester_2),
        Cell(SHEET_FAT, 'V6', job.revision),
        Cell(SHEET_FAT, 'V7', job.direction),
        Cell(SHEET_FAT, 'B11', a.header_text),
        Cell(SHEET_FAT, 'S11', z.header_text),
        Cell(SHEET_FAT, 'B12', a.test_from_device),
        Cell(SHEET_FAT, 'AB12', z.test_from_device),
    ]

    # Event Log
    out += [
        Cell(SHEET_EVENTS, 'F3', a.header_text),
        Cell(SHEET_EVENTS, 'F4', z.header_text),
        Cell(SHEET_EVENTS, 'I5', a.test_from_device),
        Cell(SHEET_EVENTS, 'L6', a.vendor_part),
        Cell(SHEET_EVENTS, 'I7', z.test_from_device),
        Cell(SHEET_EVENTS, 'L8', z.vendor_part),
        Cell(SHEET_EVENTS, 'W3', job.contractor),
        Cell(SHEET_EVENTS, 'W4', job.tester_1),
        Cell(SHEET_EVENTS, 'W5', job.tester_2),
        Cell(SHEET_EVENTS, 'W6', job.revision),
        Cell(SHEET_EVENTS, 'W7', job.direction),
        Cell(SHEET_EVENTS, 'W9', job.cable_comp),
        Cell(SHEET_EVENTS, 'M12', job.same_fiber_type),
        Cell(SHEET_EVENTS, 'AF12', job.fiber_type),
    ]

    # Exception Reporting
    out += [
        Cell(SHEET_EXCEPTIONS, 'E3', a.header_text),
        Cell(SHEET_EXCEPTIONS, 'E4', z.header_text),
        Cell(SHEET_EXCEPTIONS, 'H5', a.test_from_device),
        Cell(SHEET_EXCEPTIONS, 'H6', z.test_from_device),
        Cell(SHEET_EXCEPTIONS, 'T3', job.contractor),
        Cell(SHEET_EXCEPTIONS, 'T4', job.tester_1),
        Cell(SHEET_EXCEPTIONS, 'T5', job.tester_2),
        Cell(SHEET_EXCEPTIONS, 'T6', job.revision),
    ]
    return out


def _survey_cells(job: JobFacts) -> list[Cell]:
    a, z = job.site_a, job.site_z
    S = SHEET_SURVEY
    cells = [
        Cell(S, 'E9', a.address), Cell(S, 'K9', a.clli),
        Cell(S, 'E10', a.alias), Cell(S, 'K10', job.market),
        Cell(S, 'E11', z.address), Cell(S, 'K11', z.clli),
        Cell(S, 'E12', z.alias),
    ]
    # 1.2 Fiber Panel Information -- A block on rows 51-56, Z on 59-63.
    for site, rows in ((a, (51, 52, 53, 56)), (z, (59, 60, 61, 63))):
        r_rack, r_panel, r_attr, r_term = rows
        cells += [
            Cell(S, f'F{r_rack}', site.floor),
            Cell(S, f'H{r_rack}', site.room),
            Cell(S, f'J{r_rack}', site.aisle),
            Cell(S, f'L{r_rack}', site.bay),
            Cell(S, f'F{r_panel}', site.rmu),
            Cell(S, f'I{r_panel}', site.block),
            Cell(S, f'F{r_attr}', site.panel_existing),
            Cell(S, f'H{r_attr}', site.rack_size),
            Cell(S, f'J{r_attr}', site.panel_port_count),
            Cell(S, f'M{r_attr}', site.panel_rmus),
            Cell(S, f'F{r_term}', site.connector_type),
            Cell(S, f'I{r_term}', site.osp_facing),
            Cell(S, f'K{r_term}', site.riser_panel),
            Cell(S, f'M{r_term}', site.panel_type),
        ]
    # 1.5 FQA Information
    cells += [
        Cell(S, 'F84', job.network_type),
        Cell(S, 'F85', job.package_type),
        Cell(S, 'F86', job.revision),
        Cell(S, 'F87', job.prepared_by or job.contractor),
        Cell(S, 'F88', job.calibration_date),
        Cell(S, 'F89', job.tester_1),
        Cell(S, 'F90', job.tester_2),
        Cell(S, 'F91', job.project),
        Cell(S, 'F92', job.cable_manufacturer),
        Cell(S, 'F93', job.cable_type),
        Cell(S, 'F94', job.hybrid_breakdown),
        Cell(S, 'F95', job.cable_size),
        Cell(S, 'F96', job.ring_loop_id),
        Cell(S, 'F97', f'{job.fiber_count} Fibers' if job.fiber_count else None),
        Cell(S, 'F98', job.customer),
    ]
    return cells


def _row_formulas(row: int) -> list[Cell]:
    """The form's own formulas for one event row.

    Building the template blanks these columns on every row -- it has to,
    or a short span prints 'DEL ROW' down the page -- so each row we
    actually use has to have them put back.  They are copied verbatim
    from Lumen's own rows:

      B   the event number, counting on from the row above until the 'x'
          in AL makes the row Site Z
      X   the fibre type arriving from the previous event
      Z   the fibre type leaving, or NA at Site Z
      AE  distance from Z, which is the span length minus AB
      AH  distance to the next event
      AM  the helper the event counter sums

    Writing these as formulas rather than values is the whole point: a
    reviewer who corrects one distance sees the rest of the row follow,
    which is how the form is meant to behave.

    Missing this was not visible in a spot check of the cells we write --
    only in a sweep of every cell Lumen's own package fills.
    """
    prev = row - 1
    b = ('IF(AL18="x", "Site Z",1)' if row == EVENT_FIRST_ROW else
         f'IF(AL{row}="x", "Site Z",IF(ISNUMBER(B{prev}),B{prev}+1,"DEL ROW"))')
    return [
        Cell(SHEET_EVENTS, f'{EV_NUMBER}{row}', Formula(b)),
        Cell(SHEET_EVENTS, f'{EV_FIBER_FROM}{row}',
             Formula(f'IF(Z{prev}="Choose", "", Z{prev})')),
        Cell(SHEET_EVENTS, f'{EV_FIBER_TO}{row}',
             Formula(f'IF(B{row}="Site Z","NA",IF(M$12="Yes",AF$12,"Choose"))')),
        Cell(SHEET_EVENTS, f'{EV_DIST_Z}{row}', Formula(f'$W$8-AB{row}')),
        Cell(SHEET_EVENTS, f'{EV_TO_NEXT}{row}',
             Formula(f'IF(B{row}="Site Z",0,AB{row + 1}-AB{row})')),
        Cell(SHEET_EVENTS, f'{EV_HELPER}{row}', Formula(f'B{row}')),
    ]


def _event_cells(chain: EventChain) -> list[Cell]:
    S = SHEET_EVENTS
    cells: list[Cell] = [Cell(S, 'W8', chain.span_length_m)]

    events = chain.events

    # Site A sits on its own row and the form already builds its address
    # from the cover page, so its distance and its event type are the only
    # things ours to write.  The type matters: a new hut is a 'New
    # FTP/FDP Termination' and an existing one is 'Pre-Existing', and
    # leaving the row alone meant the A end silently kept whatever the
    # template was built from.
    site_a = next((e for e in events if e.number == SITE_A), None)
    cells += [
        Cell(S, f'{EV_DIST_A}{EVENT_SITE_A_ROW}', 0 if events else None),
        Cell(S, f'{EV_TYPE}{EVENT_SITE_A_ROW}',
             site_a.splice_type if site_a else None),
    ]

    row = EVENT_FIRST_ROW
    last_row = EVENT_FIRST_ROW - 1     # nothing written yet
    for ev in events:
        if ev.number == SITE_A:
            continue
        if row > EVENT_LAST_ROW:
            break
        cells += [
            Cell(S, f'{EV_VAULT}{row}', ev.vault_id),
            Cell(S, f'{EV_TYPE}{row}', ev.splice_type),
            Cell(S, f'{EV_DIST_A}{row}', ev.dist_from_a_m),
        ]
        cells += _row_formulas(row)
        if ev.number == SITE_Z:
            # Hand the row back to the form: the 'x' in AL makes B read
            # 'Site Z', and the address formula keys off that.
            cells += [
                Cell(S, f'{EV_LAST_MARK}{row}', 'X'),
                Cell(S, f'{EV_LOCATION}{row}', Formula(
                    'IF(B%d="Site Z",\'Site Survey Data\'!$E$11&"   "&'
                    '\'Site Survey Data\'!$E$12,"")' % row)),
            ]
        else:
            cells += [
                Cell(S, f'{EV_LOCATION}{row}', ev.location_text),
                Cell(S, f'{EV_LAST_MARK}{row}', None),
            ]
        last_row = row
        row += 1

    # Everything below the span is blanked so the form stops printing
    # 'DEL ROW' where a reviewer expects white space.
    for r in range(last_row + 1, EVENT_LAST_ROW + 1):
        for col in EVENT_CLEAR_COLS:
            cells.append(Cell(S, f'{col}{r}', None))
    return cells


def _fat_cells(rows: list[FatRow]) -> list[Cell]:
    S = SHEET_FAT
    cells: list[Cell] = []
    for i, r in enumerate(rows):
        xl = FAT_FIRST_ROW + i
        if xl > FAT_LAST_ROW:
            break
        cells += [
            Cell(S, f'{FAT_PORT_A}{xl}', r.port_a),
            Cell(S, f'{FAT_BLOCK_A}{xl}', r.block_a),
            Cell(S, f'{FAT_LATERAL_A}{xl}', r.lateral_a),
            Cell(S, f'{FAT_BACKBONE_A}{xl}', r.backbone),
            Cell(S, f'{FAT_BACKBONE_Z}{xl}', r.backbone),
            Cell(S, f'{FAT_LATERAL_Z}{xl}', r.lateral_z),
            Cell(S, f'{FAT_BLOCK_Z}{xl}', r.block_z),
            Cell(S, f'{FAT_PORT_Z}{xl}', r.port_z),
        ]
        for col, ref in FAT_RACK_FORMULAS.items():
            cells.append(Cell(S, f'{col}{xl}', Formula(ref)))

    first_unused = FAT_FIRST_ROW + len(rows)
    for xl in range(first_unused, FAT_LAST_ROW + 1):
        for col in FAT_CLEAR_COLS:
            cells.append(Cell(S, f'{col}{xl}', None))
    return cells


def _text(value):
    return None if value in (None, '') else str(value)


def _exception_cells(rows: list[Exception_]) -> list[Cell]:
    S = SHEET_EXCEPTIONS
    cells: list[Cell] = []
    for i in range(EXC_FIRST_ROW, EXC_LAST_ROW + 1):
        idx = i - EXC_FIRST_ROW
        item = rows[idx] if idx < len(rows) else None
        # The fibre and event columns are text in Lumen's own packages --
        # a fibre number can read '195' or '195/196' or 'F195', so the
        # column has to hold both and it holds them as text.
        cells += [
            Cell(S, f'{EXC_FIBER}{i}', _text(item.fiber) if item else None),
            Cell(S, f'{EXC_EVENT}{i}', _text(item.event) if item else None),
            Cell(S, f'{EXC_TEXT}{i}', item.description if item else None),
        ]
    return cells


def form_version(path: str) -> str | None:
    """The revision recorded on the form's Version History tab.

    The tab lists every published revision; the last row with a version in
    it is the one the file is built on.  Returns None for the older form,
    which has no such tab.
    """
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        if SHEET_VERSION not in wb.sheetnames:
            return None
        ws = wb[SHEET_VERSION]
        latest = None
        for row in ws.iter_rows(min_row=2, max_row=40, values_only=True):
            if len(row) < 2 or row[1] in (None, ''):
                continue
            text = str(row[1]).strip()
            # Skip the column heading, which is the literal 'Version' and
            # sits in the same column as the numbers under it.
            if not _VERSION_RE.match(text):
                continue
            latest = text
        return latest
    finally:
        wb.close()


def write_fqa(template_path: str, out_path: str, build: FqaBuild,
              require_version: str | None = None) -> list[str]:
    """Fill `template_path` with `build` and save it to `out_path`.

    Returns the list of sheets that were written, so a caller can say what
    it changed rather than claiming the whole workbook.

    `require_version` is normally None: every revision of the Lumen form
    is accepted, and which one it was comes back through the completeness
    audit rather than stopping the build.  Set it only where a caller has
    a reason to demand one exact revision.
    """
    cells: list[Cell] = []
    cells += _survey_cells(build.job)
    cells += _header_block(build.job)
    cells += _event_cells(build.chain)
    cells += _fat_cells(build.fat_rows)
    cells += _exception_cells(build.exceptions)

    patch = WorkbookPatch(template_path)
    missing = [s for s in (SHEET_SURVEY, SHEET_FAT, SHEET_EVENTS,
                           SHEET_EXCEPTIONS) if s not in patch.sheet_names]
    if missing:
        raise ValueError(
            f'{template_path} is missing the tab(s) {", ".join(missing)} — '
            f'it does not look like a Lumen FQA Site Survey form')

    if require_version is not None:
        found = form_version(template_path)
        if found != require_version:
            raise ValueError(
                f'{os.path.basename(template_path)} is revision '
                f'{found or "an older one with no Version History tab"}; '
                f'this tool writes the Lumen form revision {require_version}. '
                f'To adopt a different revision, build a template from a '
                f'package made on it: python -m fqa.make_template')

    patch.set_cells(cells)
    patch.save(out_path)
    return sorted({c.sheet for c in cells})
