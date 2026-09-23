"""
The facts the production sheet does not carry
=============================================

Most of the FQA comes out of the production sheet, but not all of it.  A
production sheet records what the crew found in the field; the FQA also
carries what the job is -- the CLLIs, the street addresses, the project
number, who tested it and when the OTDR was last calibrated.  None of
that is in a splice worksheet, and none of it can be guessed.

So this module is two halves:

  JobFacts   the fields a person has to supply once per span
  derive()   everything the production sheet CAN answer, worked out and
             offered as the default so the person is confirming rather
             than typing

The split matters for a reason worth stating.  Span 4's Event Log has the
A-end entry splice at '7520 County Rd HH' while the cover page has the A
site at '7250 County Rd HH'.  One of those is a typo, and the gate code
on the production sheet is 7520 -- the number got copied from the wrong
line.  Anything derived is derived once here and used everywhere, so a
value cannot disagree with itself across the tabs of one workbook.

Robert, 2026-09-23.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date

from .production_sheet import ProductionSheet, TERMINATION

# 'Long Haul' / 'Metro' -- the FQA's Network Type.
NETWORK_LONG_HAUL = 'Long Haul'

# The Event Log asks for the cable's construction over the majority of the
# span.  A long-haul backbone at these counts is always ribbon.
CABLE_COMP_RIBBON = 'RIBBON'
CABLE_COMP_LOOSE_TUBE = 'Loose Tube'


@dataclass
class SiteFacts:
    """One end of the span."""
    address: str | None = None          # street address, no city/state
    alias: str | None = None            # 'Flagler'
    clli: str | None = None             # 'FLGLCOAC'
    floor: str | None = None            # '001'
    room: str | None = None             # '0001'
    aisle: str | None = None            # '100'
    bay: str | None = None              # '007'
    rmu: str | None = None              # '8 & 19'
    block: str | None = None            # 'A-X & A-X'
    panel_existing: str | None = 'Yes'
    rack_size: str | None = '23"'
    panel_port_count: int | None = None
    panel_rmus: int | None = None
    connector_type: str | None = None   # 'LC'
    osp_facing: str | None = 'Yes'
    riser_panel: str | None = 'No'
    panel_type: str | None = None       # 'NG4'
    vendor_part: str | None = None      # 'OCP-LC-576-5U'

    @property
    def site_text(self) -> str:
        """'7250 County Rd HH   Flagler' -- the form joins the address and
        the alias with three spaces, and several tabs rebuild that string
        with their own formulas, so it has to be exactly three."""
        return '   '.join(x for x in (self.address, self.alias) if x)

    @property
    def header_text(self) -> str:
        """The FAT / Event Log header form: address, alias, CLLI."""
        return '   '.join(x for x in (self.address, self.alias, self.clli) if x)

    @property
    def test_from_device(self) -> str | None:
        """'001.0001..100.007.8 & 19' -- floor.room..aisle.bay.rmu.

        The empty field between room and aisle is the form's own; every
        Lumen example has the double dot.
        """
        parts = (self.floor, self.room, self.aisle, self.bay, self.rmu)
        if not all(parts):
            return None
        return f'{self.floor}.{self.room}..{self.aisle}.{self.bay}.{self.rmu}'


@dataclass
class JobFacts:
    """Everything the FQA needs that a production sheet cannot answer."""
    site_a: SiteFacts = field(default_factory=SiteFacts)
    site_z: SiteFacts = field(default_factory=SiteFacts)

    market: str | None = None               # 'Denver'
    project: str | None = None              # 'P.259588 / Denver to Kansas City'
    contractor: str | None = None           # 'ZERODB CHRIS LINDSAY'
    prepared_by: str | None = None
    tester_1: str | None = None
    tester_2: str | None = None
    revision: str = '1'
    direction: str = 'NA - Single Span'
    calibration_date: date | None = None

    network_type: str = NETWORK_LONG_HAUL
    package_type: str = 'Package Type: New Backbone'
    customer: str | None = None             # 'Lumen'
    ring_loop_id: str = 'N/A'

    cable_manufacturer: str | None = None   # 'Corning'
    cable_type: str | None = None           # '1152ct Contour, SMF-28'
    hybrid_breakdown: str | None = None     # '1-1152 SMF-28'
    cable_size: str | None = None           # '1152ct'
    cable_comp: str = CABLE_COMP_RIBBON
    fiber_type: str = 'SMF 28'
    same_fiber_type: str = 'Yes'
    fiber_count: int | None = None

    def to_json(self) -> str:
        def enc(o):
            if isinstance(o, date):
                return o.isoformat()
            raise TypeError(type(o))
        return json.dumps(asdict(self), indent=2, default=enc)

    @classmethod
    def from_dict(cls, data: dict) -> 'JobFacts':
        data = dict(data or {})
        sa = SiteFacts(**(data.pop('site_a', None) or {}))
        sz = SiteFacts(**(data.pop('site_z', None) or {}))
        cal = data.pop('calibration_date', None)
        job = cls(site_a=sa, site_z=sz, **data)
        if isinstance(cal, str) and cal:
            job.calibration_date = date.fromisoformat(cal)
        elif isinstance(cal, date):
            job.calibration_date = cal
        return job

    def missing(self) -> list[str]:
        """Fields the FQA will show as blank if nobody fills them.

        Only the ones a Lumen reviewer bounces the package for: the
        Submittal Checklist has a line for each.
        """
        out = []
        for tag, site in (('A', self.site_a), ('Z', self.site_z)):
            if not site.address:
                out.append(f'{tag} site address')
            if not site.clli:
                out.append(f'{tag} site CLLI')
            if not site.alias:
                out.append(f'{tag} site alias')
            if not site.vendor_part:
                out.append(f'{tag} test-from-device vendor part number')
        if not self.contractor:
            out.append('splicing contractor')
        if not self.tester_1:
            out.append('tester #1')
        if not self.calibration_date:
            out.append('date of most recent test-equipment calibration')
        if not self.fiber_count:
            out.append('number of fibers tested')
        return out


# ── derivation from the production sheet ──────────────────────────────────

_RANGE_RE = re.compile(r'(\d+)\s*-\s*(\d+)')
_COUNT_RE = re.compile(r'(\d{2,5})\s*(?:ct|f\b|F\b)')
_MANUFACTURERS = ('Corning', 'Commscope', 'OFS', 'Prysmian', 'AFL', 'Sumitomo')


def _fiber_count(prod: ProductionSheet) -> int | None:
    """Highest fibre number terminated at either end.

    The Termination Information table lists what landed on each RMU
    ('1-576', '577-1152'), so the top of the last range is the cable's
    working count -- which is what 'Number Of Fibers Tested' means.
    """
    top = 0
    for loc in prod.locations:
        if loc.kind != TERMINATION:
            continue
        for row in loc.terminations:
            m = _RANGE_RE.search(str(row.from_fibers or ''))
            if m:
                top = max(top, int(m.group(2)))
    return top or None


def _backbone_part_numbers(prod: ProductionSheet) -> list[str]:
    out = []
    for loc in prod.locations:
        for c in loc.backbone_cables:
            if c.part_number:
                out.append(c.part_number)
    return out


def _cable_manufacturer(parts: list[str]) -> str | None:
    """Most common manufacturer across the backbone rows.

    Taken by vote rather than from the first row because the part numbers
    are typed by hand and carry real spelling variation -- Span 4 has
    'Cornning 05/26 1152ct' on two sheets.
    """
    votes: dict[str, int] = {}
    for p in parts:
        for name in _MANUFACTURERS:
            if name.lower()[:5] in p.lower().replace('nn', 'n'):
                votes[name] = votes.get(name, 0) + 1
    if not votes:
        return None
    return max(votes, key=votes.get)


def _cable_size(parts: list[str], fiber_count: int | None) -> str | None:
    for p in parts:
        m = _COUNT_RE.search(p)
        if m:
            return f'{int(m.group(1))}ct'
    return f'{fiber_count}ct' if fiber_count else None


def _rack_from_bay_shelf(text: str | None) -> tuple[str | None, str | None]:
    """'RR 100.07' -> aisle '100', bay '007'.

    The production sheet writes the relay-rack position as aisle.bay with
    the bay unpadded; the FQA wants it three digits.
    """
    if not text:
        return None, None
    m = re.search(r'(\d+)\s*[.\-]\s*(\d+)', str(text))
    if not m:
        return None, None
    return m.group(1), m.group(2).zfill(3)


def _rmu_label(loc) -> str | None:
    """'RMU 8' + 'RMU 19' -> '8 & 19'."""
    nums = []
    for row in getattr(loc, 'terminations', []) or []:
        m = re.search(r'(\d+)', str(row.mounting_position or ''))
        if m and m.group(1) not in nums:
            nums.append(m.group(1))
    return ' & '.join(nums) if nums else None


def _site_from_termination(loc) -> SiteFacts:
    site = SiteFacts()
    if loc is None:
        return site
    aisle, bay = _rack_from_bay_shelf(loc.bay_shelf)
    site.aisle, site.bay = aisle, bay
    site.rmu = _rmu_label(loc)
    site.panel_type = loc.fdf_model
    site.alias = None
    if loc.fdf_capacity_ports:
        m = re.search(r'(\d+)', str(loc.fdf_capacity_ports))
        if m:
            site.panel_port_count = int(m.group(1))
    conn = next((r.connector_type for r in loc.terminations if r.connector_type),
                None)
    if conn:
        site.connector_type = str(conn).split('/')[0].strip()
    # The panel occupies as many RMUs as the terminations list.
    if loc.terminations:
        site.block = ' & '.join(['A-X'] * len(_rmu_label(loc).split(' & '))) \
            if _rmu_label(loc) else None
    return site


def derive(prod: ProductionSheet, base: JobFacts | None = None) -> JobFacts:
    """Fill in everything the production sheet can answer.

    Anything already set on `base` wins: a person's correction is never
    overwritten by a derivation.
    """
    job = base or JobFacts()

    a_term = prod.site_a
    z_term = prod.site_z

    for site, term in ((job.site_a, a_term), (job.site_z, z_term)):
        got = _site_from_termination(term)
        for key, value in vars(got).items():
            if value is not None and getattr(site, key) in (None, ''):
                setattr(site, key, value)

    if not job.fiber_count:
        job.fiber_count = _fiber_count(prod)

    parts = _backbone_part_numbers(prod)
    if not job.cable_manufacturer:
        job.cable_manufacturer = _cable_manufacturer(parts)
    if not job.cable_size:
        job.cable_size = _cable_size(parts, job.fiber_count)
    if not job.cable_type and job.cable_size:
        job.cable_type = f'{job.cable_size} {job.fiber_type}'
    if not job.hybrid_breakdown and job.fiber_count:
        job.hybrid_breakdown = f'1-{job.fiber_count} {job.fiber_type}'

    if not job.project:
        wo = next((l.work_order for l in prod.locations if l.work_order), None)
        job.project = wo
    if not job.customer:
        cust = next((l.customer for l in prod.locations if l.customer), None)
        job.customer = cust

    # The aliases are the plain-English names the crew gave the two ILAs
    # ('Flagler ILA' -> 'Flagler'), which is what the FQA's Site Alias is.
    for site, term in ((job.site_a, a_term), (job.site_z, z_term)):
        if not site.alias and term is not None and term.name:
            site.alias = re.sub(r'\s*\b(ILA|HUT|POP)\b\s*$', '',
                                str(term.name).strip(), flags=re.I).strip()

    return job
