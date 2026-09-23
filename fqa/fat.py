"""
The Fiber Assignment Table
==========================

The FAT is one row per block of 24 fibres, from fibre 1 to the top of the
cable, saying where each block lands at each end: which aisle, bay, RMU
and block (tray), and what the fibres are called on both sides of the
entry splice.

It is entirely mechanical, which is why it is worth generating.  A
1152-fibre span is 48 rows, each carrying eight numbers that have to
agree with the row above it, and it is transcribed by hand today.

The two numbering systems
-------------------------
A backbone fibre number runs 1..N over the whole cable and is the same at
both ends.  A lateral fibre number is the fibre's position within the
lateral cable that carries it from the entry splice into the frame, and
those cables restart at 1.  Flagler lands 1152 backbone fibres on three
laterals of 432, 432 and 288, so backbone 433-456 arrives on lateral
001-024 of Cable 2, and backbone 865-888 on lateral 001-024 of Cable 3.

Blocks are lettered A..X, 24 of them to a 576-port NG4 panel, and restart
at A on each RMU.

Robert, 2026-09-23.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .production_sheet import Location, TERMINATION

FIBERS_PER_BLOCK = 24
BLOCK_LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWX'      # 24 blocks to a panel

_CABLE_COUNT_RE = re.compile(r'(\d{2,5})\s*(?:ct|f)\b', re.I)


@dataclass
class FatRow:
    """One row of the FAT."""
    block_index: int                 # 0-based, over the whole span
    port_a: str
    block_a: str
    lateral_a: str
    backbone: str
    lateral_z: str
    block_z: str
    port_z: str


def lateral_cable_sizes(loc: Location | None,
                        entry: Location | None = None) -> list[int]:
    """How many fibres each lateral cable carries, in order.

    Two sources, and the smaller wins:

    * the cable part numbers on the termination sheet -- a cable cannot
      carry more fibres than it has;
    * the fibre ranges in the entry splice's Splice Information table --
      a lateral cannot carry more than were spliced onto it.

    Neither alone is right.  Tucumcari lands 864 fibres on two 576-count
    laterals, 432 on each, so the part numbers say 576 and only the
    splice table says 432.  Span 4's Bethune Entry sheet says Cable 3
    takes 1-432 when the cable is a 288, so there only the part number is
    right.  min() reads both correctly.
    """
    parts: list[int] = []
    if loc is not None and loc.kind == TERMINATION:
        for cable in loc.lateral_cables:
            for piece in str(cable.part_number or '').split('/'):
                m = _CABLE_COUNT_RE.search(piece)
                if m:
                    parts.append(int(m.group(1)))

    spliced: list[int] = []
    if entry is not None:
        # Only the 'Cable N' columns; the backbone column in the same
        # table is the whole span and would swamp the list.
        numbered = [(n, top) for label, top in entry.assignments.items()
                    if (n := _cable_number(label)) is not None]
        spliced = [int(top) for _, top in sorted(numbered)]

    if parts and spliced:
        n = max(len(parts), len(spliced))
        out = []
        for i in range(n):
            vals = [v for v in (parts[i:i + 1] + spliced[i:i + 1])]
            out.append(min(vals) if vals else 0)
        return [v for v in out if v]
    return parts or spliced


def _cable_number(label: str) -> int | None:
    m = re.match(r'^\s*cable\s*(\d+)\s*$', str(label), re.I)
    return int(m.group(1)) if m else None


def _lateral_ranges(total: int, sizes: list[int]) -> list[tuple[int, int]]:
    """Lateral fibre range for each block of 24, walking the laterals.

    With no lateral sizes to walk (a site that terminates the backbone
    straight onto the frame) the lateral number is the backbone number,
    which is what the single-cable sites in Lumen's own examples show.
    """
    blocks = -(-total // FIBERS_PER_BLOCK)
    if not sizes:
        return [(i * FIBERS_PER_BLOCK + 1, min((i + 1) * FIBERS_PER_BLOCK, total))
                for i in range(blocks)]

    out: list[tuple[int, int]] = []
    cable = 0
    within = 0                      # fibres already used on this lateral
    for _ in range(blocks):
        while cable < len(sizes) and within >= sizes[cable]:
            cable += 1
            within = 0
        if cable >= len(sizes):
            # More backbone than the laterals account for.  Keep counting
            # on the last cable rather than stopping: a short row is
            # easier for a reviewer to spot than a missing one.
            start = within + 1
            out.append((start, start + FIBERS_PER_BLOCK - 1))
            within += FIBERS_PER_BLOCK
            continue
        start = within + 1
        end = min(within + FIBERS_PER_BLOCK, sizes[cable])
        out.append((start, end))
        within += FIBERS_PER_BLOCK
    return out


def _fmt(lo: int, hi: int, width: int = 3) -> str:
    return f'{lo:0{width}d}-{hi:0{width}d}'


def build_fat(fiber_count: int,
              site_a: Location | None = None,
              site_z: Location | None = None,
              entry_a: Location | None = None,
              entry_z: Location | None = None,
              port_label: str = '1-24') -> list[FatRow]:
    """Every row of the FAT for a span of `fiber_count` fibres."""
    if not fiber_count or fiber_count < 1:
        return []
    blocks = -(-fiber_count // FIBERS_PER_BLOCK)
    lat_a = _lateral_ranges(fiber_count, lateral_cable_sizes(site_a, entry_a))
    lat_z = _lateral_ranges(fiber_count, lateral_cable_sizes(site_z, entry_z))

    rows: list[FatRow] = []
    for i in range(blocks):
        lo = i * FIBERS_PER_BLOCK + 1
        hi = min((i + 1) * FIBERS_PER_BLOCK, fiber_count)
        letter = BLOCK_LETTERS[i % len(BLOCK_LETTERS)]
        rows.append(FatRow(
            block_index=i,
            port_a=port_label,
            block_a=letter,
            lateral_a=_fmt(*lat_a[i]),
            backbone=_fmt(lo, hi),
            lateral_z=_fmt(*lat_z[i]),
            block_z=letter,
            port_z=port_label,
        ))
    return rows
