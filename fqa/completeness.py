"""
What is not finished yet
========================

A package goes back if anything on it is blank, and the things that end up
blank are spread across three places: the job facts nobody typed, the
production sheet the crew left half-filled, and the measurements that were
never taken.  Chasing them means opening the workbook, the sheet and the
Event Log and holding all three in your head.

So the tool holds them instead.  `audit()` walks everything it has and
returns one flat list of what is not complete, each item saying where the
gap is, what is missing, and what to do about it.

It is deliberately not a pass/fail.  Plenty of these are legitimate --
a span with no exceptions has nothing to report, a mid-span splice with no
traffic notes is a splice in a field -- so the list is what a person reads
before deciding the package is done, not a gate the tool enforces.

Robert, 2026-09-23.
"""
from __future__ import annotations

from dataclasses import dataclass

from .production_sheet import SPLICE, TERMINATION

# How serious a gap is.  'blocking' means Lumen will send the package back;
# 'check' means it is probably fine but somebody should have looked.
BLOCKING = 'blocking'
CHECK = 'check'

# The form revision this tool's cell map was read off.  A different
# revision is accepted -- the layouts match everywhere we have compared
# them -- but it is reported, because an unchecked revision is exactly the
# kind of thing that is obvious in hindsight.
VERIFIED_FORM_VERSION = '1.1'


@dataclass(frozen=True)
class Gap:
    """One thing that is not complete."""
    where: str              # 'Job facts', 'Splice 4', 'Event Log', 'Form'
    what: str               # what is missing
    fix: str = ''           # what to do about it
    level: str = BLOCKING

    @property
    def line(self) -> str:
        return f'{self.where}: {self.what}' + (f'; {self.fix}' if self.fix else '')


def _job_gaps(job) -> list[Gap]:
    """The fields no production sheet carries.

    `JobFacts.missing()` already names them for the manifest; this turns
    each into a Gap so they sit in one list with everything else rather
    than in a second place the tech has to remember to read.
    """
    fixes = {
        'A site address': 'the street address of the A-end building',
        'Z site address': 'the street address of the Z-end building',
        'A site CLLI': 'the 8-character CLLI for the A end',
        'Z site CLLI': 'the 8-character CLLI for the Z end',
        'A site alias': 'the plain name of the A-end site',
        'Z site alias': 'the plain name of the Z-end site',
        'A test-from-device vendor part number': 'the panel part number, e.g. OCP-LC-576-5U',
        'Z test-from-device vendor part number': 'the panel part number, e.g. OCP-LC-576-5U',
        'splicing contractor': 'the company and contact on the package',
        'tester #1': 'who ran the test',
        'date of most recent test-equipment calibration': 'the OTDR’s last calibration date',
        'number of fibers tested': 'the cable’s working fibre count',
    }
    return [Gap('Job facts', name, fixes.get(name, ''), BLOCKING)
            for name in job.missing()]


def _location_gaps(prod) -> list[Gap]:
    """What the crew left blank on the production sheet.

    Only fields that reach the FQA are checked.  A blank 'Traffic Notes'
    never leaves the production sheet, so flagging it would be noise in a
    list whose whole value is that everything on it matters.
    """
    out: list[Gap] = []
    for loc in prod.locations:
        where = loc.sheet

        if not loc.address:
            out.append(Gap(where, 'no address or GPS fix',
                           'the Event Log prints this as the event location',
                           BLOCKING))
        if not loc.work_date:
            out.append(Gap(where, 'no date', 'when the work was done', CHECK))
        if not loc.technicians:
            out.append(Gap(where, 'no technician', 'who did the work', CHECK))

        if loc.kind == SPLICE:
            if not loc.enclosure_manufacturer:
                out.append(Gap(where, 'no enclosure manufacturer', '', CHECK))
            if not loc.cables:
                out.append(Gap(where, 'the Cable Information table is empty',
                               'no footage marks to check the distances against',
                               CHECK))
            elif not loc.backbone_cables:
                out.append(Gap(where, 'no backbone cable row',
                               'every cable row names a lateral, so the span '
                               'does not chain through here', CHECK))
        else:
            if not loc.terminations:
                out.append(Gap(where,
                               'the Termination Information table is empty',
                               'this is where the fibre count and the panel '
                               'layout come from', BLOCKING))
            if not loc.fdf_model and not loc.fdf_manufacturer:
                out.append(Gap(where, 'no fiber distribution frame details',
                               'panel type and port count', CHECK))
            if not loc.bay_shelf:
                out.append(Gap(where, 'no Bay/Shelf',
                               'the aisle and bay on the cover page come '
                               'from it', BLOCKING))
    return out


def _measurement_gaps(chain, fat_rows, fiber_count) -> list[Gap]:
    out: list[Gap] = []

    if chain.distance_source != 'trace':
        out.append(Gap(
            'Event Log', 'the distances are the production sheet’s footage marks',
            'they are copied off a cable by hand; paste the measured '
            'closure distances', BLOCKING))

    blanks = [e for e in chain.events if e.dist_from_a_m is None]
    if blanks:
        out.append(Gap('Event Log',
                       f'{len(blanks)} event(s) have no distance at all',
                       'the chain does not reach them', BLOCKING))

    if not chain.span_length_m:
        out.append(Gap('Event Log', 'no span length',
                       'the whole from-Z column is computed from it', BLOCKING))

    # Every reconciliation warning is a gap in its own right: the two
    # measurements of the same segment disagree and one of them is wrong.
    for w in chain.warnings:
        out.append(Gap('Event Log', w, '', CHECK))

    if not fat_rows:
        out.append(Gap('FAT', 'no rows',
                       'needs the fibre count' if not fiber_count
                       else 'nothing was generated', BLOCKING))
    return out


def audit(prod, job, chain, fat_rows, exceptions=None,
          form_revision=None) -> list[Gap]:
    """Everything about this package that is not finished.

    Blocking items first, then the ones worth a look, each group in the
    order a person would work through them: the form, the job, the span,
    then the sheets.
    """
    gaps: list[Gap] = []

    if form_revision != VERIFIED_FORM_VERSION:
        what = (f'this form is Lumen revision {form_revision}, not '
                f'{VERIFIED_FORM_VERSION}') if form_revision else \
               ('this form has no Version History tab, so its revision is '
                'unknown: it is the older Lumen form')
        gaps.append(Gap(
            'Form', what,
            f'the cell map was read off revision {VERIFIED_FORM_VERSION} and '
            'matches this one everywhere the two have been compared, but it '
            'has not been checked cell by cell; give the output a look',
            CHECK))

    gaps += _job_gaps(job)
    gaps += _measurement_gaps(chain, fat_rows, job.fiber_count)
    gaps += _location_gaps(prod)

    if not exceptions:
        gaps.append(Gap('Exception Reporting', 'no fibres listed',
                        'correct if every fibre passed; otherwise the OOS '
                        'and reburned fibres go here', CHECK))

    order = {BLOCKING: 0, CHECK: 1}
    return sorted(gaps, key=lambda g: order.get(g.level, 2))


def summary(gaps: list[Gap]) -> dict:
    return {
        'total': len(gaps),
        'blocking': sum(1 for g in gaps if g.level == BLOCKING),
        'check': sum(1 for g in gaps if g.level == CHECK),
    }
