"""
Turn a finished FQA package into a blank template
=================================================

Lumen publishes the blank Site Survey form on an internal share that we
cannot reach from here, so the template that ships with this tool is
built from a package we HAVE: every span-specific value cleared, every
site photo stripped, and the form's own formulas and macros untouched.

Run it again whenever Lumen publishes a new revision of the form (the
Version History tab records them) against a package built on that
revision:

    python -m fqa.make_template "Span 4 ... final.xlsm" \\
        fqa/templates/FQA_Site_Survey_v1_1.xlsm

Robert, 2026-09-23.
"""
from __future__ import annotations

import argparse
import os
import sys

from .event_chain import EventChain
from .job_facts import JobFacts
from .writer import FqaBuild, write_fqa
from .xlsx_patch import Cell, WorkbookPatch

# Captions a crew typed over the photo wells on the Pictures tab.  They
# name the sites, so they are span-specific and have to go.
PICTURE_CAPTIONS = ('Pictures', 'D23', 'K23')


def make_template(src: str, dst: str) -> dict:
    """Write a blank copy of the FQA form at `dst`.

    Blanking is done by running the real writer with an empty build: the
    writer already knows every cell the tool manages, so the template can
    never keep a value in a cell the tool would later fill, and the two
    can never drift apart.
    """
    # JobFacts carries sensible DEFAULTS for a long-haul span ('Long
    # Haul', 'RIBBON', revision 1).  Those are the right answer for a
    # run and the wrong answer for a template, which must not pre-answer
    # anything, so they are blanked here.
    blank = JobFacts(network_type='', package_type='', revision='',
                     direction='', cable_comp='', fiber_type='',
                     same_fiber_type='', ring_loop_id='')
    empty = FqaBuild(job=blank, chain=EventChain(events=[]),
                     fat_rows=[], exceptions=[])
    write_fqa(src, dst, empty)

    # Second pass for the things that are not cells the writer manages.
    patch = WorkbookPatch(dst)
    images = patch.strip_media()
    sheet, *refs = PICTURE_CAPTIONS
    if sheet in patch.sheet_names:
        patch.set_cells([Cell(sheet, ref, None) for ref in refs])
    patch.save(dst)

    return {'images_removed': images,
            'bytes': os.path.getsize(dst),
            'sheets': patch.sheet_names}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('source', help='a completed FQA Site Survey .xlsm')
    ap.add_argument('dest', help='where to write the blank template')
    args = ap.parse_args(argv)

    if not os.path.exists(args.source):
        print(f'no such file: {args.source}', file=sys.stderr)
        return 2
    info = make_template(args.source, args.dest)
    print(f'wrote {args.dest} ({info["bytes"]:,} bytes, '
          f'{info["images_removed"]} images removed)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
