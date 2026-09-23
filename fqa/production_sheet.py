"""
Reader for the ZeroDB production sheet
======================================

A production sheet is one workbook per span.  Every tab is one physical
location on that span, in order from the A end to the Z end, and the tab
is one of two forms:

  'Termination Location Worksheet'  the ILA / hut at each end of the span
  'Splice Location Worksheet'       an entry splice, a handhole, a vault

So a span reads as:  <A termination> <entry splice> <vault> ... <vault>
<entry splice> <Z termination>.  Span 4 Flagler to Bethune is 14 tabs:
Flagler ILA, Flagler Entry, Splice 10 ... Splice 1, Bethune Entry,
Bethune ILA.  That order IS the cable route, which is what makes the
Event Log derivable at all.

Why the parse is label-driven and not by cell address
-----------------------------------------------------
Every field here was located by reading real sheets, and the addresses do
not hold still.  'Building' is G7 on Flagler ILA and the blank Span 1
template has it empty at H7.  The two worksheet types put the cable
table's footage columns in different places (N/U/AB on a termination,
L/R/X on a splice).  Crews insert rows.  So nothing below is addressed by
cell: we find the row whose label column says 'Building', then take the
first non-empty cell to its right.  A sheet that has been edited in ways
we have not seen degrades to a missing field, which the QC report names,
rather than to a wrong value silently written into a customer's form.

Robert, 2026-09-23.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime

import openpyxl

TERMINATION = 'termination'
SPLICE = 'splice'

# The worksheet title in C3 is the only reliable discriminator between the
# two forms.  Crews rename tabs freely ('Flagler ILA', 'Bethune  ILA' with
# two spaces, 'Termination 2'), so the tab name never decides the type.
_TITLE_ROW_MAX = 8
_TYPE_MARKERS = (
    ('termination location worksheet', TERMINATION),
    ('splice location worksheet', SPLICE),
)

# Section headings, used to stop a table scan before it runs into the next
# block of the form.
_SECTION_HEADS = (
    'location information', 'fiber distribution frame information',
    'enclosure information', 'installer information', 'cable information',
    'termination information', 'splice information',
    'materials & equipment used', 'materials & comments',
)

# Rows below the Termination Information table on a termination sheet.
# They are labels in the same column as the table's own rows, so the table
# scan has to be told where the table stops.
_TERM_TABLE_STOPS = (
    'location a:', 'location z:', 'actual ports', 'header name',
    'pulse width', 'resolution', 'range', 'time', 'total span',
    'total connectors/patches', 'passing fec',
)

# The right-hand third of both worksheets is a photo well captioned
# 'Pictures Insert Below'.  A field whose value cell is empty must not
# scan on into a photo caption, so every rightward search stops here.
_PICTURE_WELL = 'pictures insert below'


def _norm(v) -> str:
    """Lower-case, collapse whitespace.  Sheets are full of trailing
    spaces and double spaces ('Bethune  ILA', ' 39 28 7.75 N')."""
    if v is None:
        return ''
    return re.sub(r'\s+', ' ', str(v)).strip().lower()


def _clean(v):
    """Trim a value for output but keep its type (dates stay dates)."""
    if isinstance(v, str):
        s = re.sub(r'\s+', ' ', v).strip()
        return s or None
    return v


@dataclass
class CableRow:
    """One row of a sheet's Cable Information table.

    `seq_at_node` is the footage mark where the cable meets this location
    (the header reads 'Sequential at Frame' on a termination and
    'Sequential at Enclosure' on a splice); `seq_at_duct` is the mark
    where it enters the duct, a few tens of feet away.  `direction` is the
    'From' column: 'East' / 'West' on a backbone cable, or 'Cable 1' /
    'Cable 2' for the lateral cables at an ILA.
    """
    part_number: str | None = None
    seq_at_node: str | None = None
    seq_at_duct: str | None = None
    direction: str | None = None

    @property
    def is_backbone(self) -> bool:
        """East/West rows are the through cable; 'Cable N' rows are the
        laterals that run from the entry splice into the ILA frame."""
        return _norm(self.direction) in ('east', 'west')


@dataclass
class TerminationRow:
    """One row of a termination sheet's Termination Information table."""
    mounting_position: str | None = None
    from_cable: str | None = None
    from_fibers: str | None = None
    to_jacks: str | None = None
    connector_type: str | None = None


@dataclass
class Location:
    """One tab of the production sheet: one location on the span."""
    sheet: str
    kind: str                       # TERMINATION | SPLICE
    index: int                      # position in the workbook, 0-based

    name: str | None = None         # 'Name' (splice) / 'Building' (term)
    address: str | None = None
    address_notes: str | None = None
    placement: str | None = None
    traffic_notes: str | None = None
    customer: str | None = None
    room: str | None = None
    bay_shelf: str | None = None
    access_info: str | None = None

    enclosure_manufacturer: str | None = None
    ports_available: str | None = None
    splice_trays: str | None = None

    fdf_manufacturer: str | None = None
    fdf_model: str | None = None
    fdf_capacity_ports: str | None = None

    company: str | None = None
    technicians: str | None = None
    work_date: date | None = None
    work_order: str | None = None

    cables: list[CableRow] = field(default_factory=list)
    terminations: list[TerminationRow] = field(default_factory=list)

    @property
    def backbone_cables(self) -> list[CableRow]:
        return [c for c in self.cables if c.is_backbone]

    def cable_toward(self, heading: str) -> CableRow | None:
        """The backbone cable leaving this location east- or west-bound.

        Returns None when the sheet has no row for that heading, which is
        the normal case at the two ends of the span and a defect anywhere
        in between.
        """
        want = _norm(heading)
        for c in self.backbone_cables:
            if _norm(c.direction) == want:
                return c
        return None

    @property
    def vault_id(self):
        """The Event Log's 'Vault ID #'.

        Read off the tab name: 'Splice 10' -> 10, anything with 'entry'
        in it -> 'ENTRY' (which is the literal Lumen wants there), and a
        termination has no vault at all.

        A numbered vault comes back as an int because that is how Lumen's
        own packages hold it -- the column is right-aligned numbers with
        'ENTRY' and 'NA' as the only text in it.
        """
        if self.kind == TERMINATION:
            return None
        n = _norm(self.sheet)
        if 'entry' in n:
            return 'ENTRY'
        m = re.search(r'(\d+)', self.sheet)
        return int(m.group(1)) if m else None


@dataclass
class ProductionSheet:
    path: str
    locations: list[Location]
    warnings: list[str] = field(default_factory=list)

    @property
    def terminations(self) -> list[Location]:
        return [l for l in self.locations if l.kind == TERMINATION]

    @property
    def splices(self) -> list[Location]:
        return [l for l in self.locations if l.kind == SPLICE]

    @property
    def site_a(self) -> Location | None:
        t = self.terminations
        return t[0] if t else None

    @property
    def site_z(self) -> Location | None:
        t = self.terminations
        return t[-1] if len(t) > 1 else None


# ── sheet scanning helpers ────────────────────────────────────────────────

class _Grid:
    """A worksheet flattened to a list of rows, 1-based lookup.

    Read once into memory: these workbooks reach 70 MB because of the
    embedded site photos, and re-walking a sheet per field would be slow
    for no reason.  Only values are needed, never styles.
    """

    def __init__(self, ws):
        self.rows = [list(r) for r in ws.iter_rows(values_only=True)]
        self.width = max((len(r) for r in self.rows), default=0)
        self.right_limit = self._find_picture_well()

    def _find_picture_well(self) -> int:
        for row in self.rows[:10]:
            for ci, v in enumerate(row, 1):
                if _norm(v) == _PICTURE_WELL:
                    return ci - 1
        return self.width

    def cell(self, row: int, col: int):
        if 1 <= row <= len(self.rows):
            r = self.rows[row - 1]
            if 1 <= col <= len(r):
                return r[col - 1]
        return None

    def find_label(self, label: str, max_col: int = 8) -> tuple[int, int] | None:
        """Row/column of the first cell whose text is exactly `label`.

        Restricted to the left-hand columns: the form repeats words like
        'Date' and 'Company' inside the wide instruction blocks on the
        right, and those must never be mistaken for the field label.
        """
        want = _norm(label)
        for ri, row in enumerate(self.rows, 1):
            for ci, v in enumerate(row[:max_col], 1):
                if _norm(v) == want:
                    return ri, ci
        return None

    def value_right_of(self, row: int, col: int, stop_col: int | None = None):
        """First non-empty cell to the right of a label on the same row.

        The form merges the value area, so the value can sit anywhere from
        one to several columns over, and where it sits differs between the
        two worksheet types.
        """
        last = stop_col or self.right_limit
        for c in range(col + 1, last + 1):
            v = self.cell(row, c)
            if v is not None and str(v).strip() != '':
                return v
        return None

    def labelled(self, label: str, max_col: int = 8, stop_col: int | None = None):
        hit = self.find_label(label, max_col=max_col)
        if not hit:
            return None
        return _clean(self.value_right_of(hit[0], hit[1], stop_col=stop_col))


def _sheet_kind(grid: _Grid) -> str | None:
    for row in range(1, _TITLE_ROW_MAX + 1):
        for col in range(1, 10):
            n = _norm(grid.cell(row, col))
            for marker, kind in _TYPE_MARKERS:
                if n == marker:
                    return kind
    return None


def _read_cable_table(grid: _Grid) -> list[CableRow]:
    """Rows of the Cable Information table.

    The header is split across two rows -- 'Sequential at' sits above
    'Enclosure' -- so the anchor columns come from the row that holds
    'Cable Part Number' and the rows beneath it are read at those same
    columns until the table runs out.
    """
    hit = grid.find_label('Cable Part Number', max_col=10)
    if not hit:
        return []
    hrow, _ = hit
    anchors: list[tuple[str, int]] = []
    for ci in range(1, grid.width + 1):
        n = _norm(grid.cell(hrow, ci))
        if n == 'cable part number':
            anchors.append(('part', ci))
        elif n == 'sequential at':
            anchors.append(('seq', ci))
        elif n == 'from':
            anchors.append(('from', ci))
    if not anchors:
        return []

    # A header sits at the LEFT edge of its merged caption but the value
    # beneath it starts a column or two earlier ('Cable Part Number' is at
    # D on a splice sheet, its values at C).  So each header owns the band
    # of columns from halfway back to the previous header to halfway on to
    # the next, and the first non-empty cell in that band is its value.
    cols = [c for _, c in anchors]
    bands: list[tuple[int, int]] = []
    for i, col in enumerate(cols):
        lo = 1 if i == 0 else (cols[i - 1] + col) // 2 + 1
        hi = grid.width if i == len(cols) - 1 else (col + cols[i + 1]) // 2
        bands.append((lo, hi))

    def at(row: int, band: tuple[int, int]):
        for c in range(band[0], band[1] + 1):
            v = grid.cell(row, c)
            if v is not None and str(v).strip() != '':
                return _clean(v)
        return None

    # The row under the header carries the second half of the split
    # captions -- 'Enclosure'/'Frame', 'Duct', 'Direction'/'Location' --
    # and never any data.
    _SUBHEAD = {'enclosure', 'frame', 'duct', 'direction', 'location'}

    out: list[CableRow] = []
    blanks = 0
    for r in range(hrow + 1, min(hrow + 14, len(grid.rows) + 1)):
        if _norm(grid.cell(r, 3)) in _SECTION_HEADS:
            break
        vals = [at(r, b) for b in bands]
        if vals and all(v is None or _norm(v) in _SUBHEAD for v in vals):
            continue
        seqs = [v for (key, _), v in zip(anchors, vals) if key == 'seq']
        part = next((v for (key, _), v in zip(anchors, vals) if key == 'part'), None)
        direction = next((v for (key, _), v in zip(anchors, vals) if key == 'from'), None)
        if not part and not any(seqs):
            blanks += 1
            if blanks >= 3:
                break
            continue
        blanks = 0
        out.append(CableRow(
            part_number=part,
            seq_at_node=seqs[0] if len(seqs) > 0 else None,
            seq_at_duct=seqs[1] if len(seqs) > 1 else None,
            direction=direction,
        ))
    return out


def _read_termination_table(grid: _Grid) -> list[TerminationRow]:
    hit = grid.find_label('Mounting Postion', max_col=10)
    if not hit:                       # the form's own typo; accept both
        hit = grid.find_label('Mounting Position', max_col=10)
    if not hit:
        return []
    hrow, _ = hit
    anchors = {}
    for ci in range(1, grid.width + 1):
        n = _norm(grid.cell(hrow, ci))
        if n.startswith('mounting pos'):
            anchors['mount'] = ci
        elif n.startswith('from cable'):
            anchors['cable'] = ci
        elif n.startswith('from fibers'):
            anchors['fibers'] = ci
        elif n.startswith('to jack'):
            anchors['jacks'] = ci
        elif n.startswith('connector type'):
            anchors['conn'] = ci
    out: list[TerminationRow] = []
    blanks = 0
    for r in range(hrow + 1, min(hrow + 12, len(grid.rows) + 1)):
        head = _norm(grid.cell(r, 3))
        if head in _SECTION_HEADS or head in _TERM_TABLE_STOPS:
            break
        row = TerminationRow(
            mounting_position=_clean(grid.cell(r, anchors.get('mount', 3))),
            from_cable=_clean(grid.cell(r, anchors.get('cable', 9))),
            from_fibers=_clean(grid.cell(r, anchors.get('fibers', 18))),
            to_jacks=_clean(grid.cell(r, anchors.get('jacks', 24))),
            connector_type=_clean(grid.cell(r, anchors.get('conn', 29))),
        )
        if not any(vars(row).values()):
            blanks += 1
            if blanks >= 3:
                break
            continue
        blanks = 0
        out.append(row)
    return out


def _read_location(ws, index: int) -> Location | None:
    grid = _Grid(ws)
    kind = _sheet_kind(grid)
    if kind is None:
        return None

    loc = Location(sheet=ws.title, kind=kind, index=index)
    loc.address = grid.labelled('Address')
    loc.company = grid.labelled('Company')
    loc.technicians = grid.labelled('Technician(s)')
    loc.work_order = grid.labelled('Work Order')

    d = grid.labelled('Date')
    if isinstance(d, datetime):
        d = d.date()
    loc.work_date = d if isinstance(d, date) else None

    if kind == SPLICE:
        loc.name = grid.labelled('Name')
        loc.address_notes = grid.labelled('Address Notes')
        loc.placement = grid.labelled('Placement')
        loc.traffic_notes = grid.labelled('Traffic Notes')
        loc.enclosure_manufacturer = grid.labelled('Manufacturer')
        loc.ports_available = grid.labelled('Ports Available')
        loc.splice_trays = grid.labelled('Splice Trays')
    else:
        loc.customer = grid.labelled('Customer')
        loc.name = grid.labelled('Building')
        loc.room = grid.labelled('Room')
        loc.bay_shelf = grid.labelled('Bay/Shelf')
        loc.access_info = grid.labelled('Access Info.')
        loc.fdf_manufacturer = grid.labelled('Manufacturer')
        loc.fdf_model = grid.labelled('Model/RU')
        loc.fdf_capacity_ports = grid.labelled('Capacity Ports')
        loc.terminations = _read_termination_table(grid)

    loc.cables = _read_cable_table(grid)
    return loc


def read_production_sheet(path: str) -> ProductionSheet:
    """Parse a ZeroDB production sheet into an ordered span model."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        locations: list[Location] = []
        warnings: list[str] = []
        for i, ws in enumerate(wb.worksheets):
            loc = _read_location(ws, len(locations))
            if loc is None:
                warnings.append(
                    f'tab {ws.title!r} is neither a Termination nor a Splice '
                    f'Location Worksheet — skipped')
                continue
            locations.append(loc)
    finally:
        wb.close()

    if not locations:
        raise ValueError(
            f'{os.path.basename(path)} has no location worksheets — '
            f'is it a ZeroDB production sheet?')

    terms = [l for l in locations if l.kind == TERMINATION]
    if len(terms) < 2:
        warnings.append(
            f'expected a termination worksheet at each end of the span, '
            f'found {len(terms)}')
    elif terms[0].index != 0 or terms[-1].index != len(locations) - 1:
        warnings.append(
            'the termination worksheets are not the first and last tabs — '
            'tab order is taken to be the cable route, so check the order')

    return ProductionSheet(path=path, locations=locations, warnings=warnings)
