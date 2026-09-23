"""
The span's event chain
======================

The FQA Event Log is one row per place the fibre is fused or connected,
from Site A to Site Z, each carrying its distance from both ends.  This
module turns a parsed production sheet into that chain.

Where the distances come from
-----------------------------
The production sheet records, at every location, the cable's sequential
footage mark as it enters the enclosure.  Two neighbouring locations sit
on the same reel, so the difference between their marks is the length of
cable between them, and the marks should chain all the way down the span.

On Span 4 Flagler to Bethune that chain reproduces the real Event Log to
within 4-35 m over seven of its eleven segments -- and then falls apart.
Splice 3 and Splice 2 both record a mark near 200 ft on the same reel,
which cannot be true of two ends of one cable, and the two segments
around them come out 1.5 km short.  The footage marks are hand-copied off
a cable in a hole at night; some of them are wrong.

So the measured trace is what fills the column, and the footage marks
become the check on it.  `reconcile()` walks both chains together and
reports every location where they disagree by more than `tolerance_m`.
That disagreement is the useful output: on Span 4 it points straight at
the two sheets a crew filled in wrong.

Distances are round metres.  The Event Log is a metre-resolution form and
Lumen's own examples are all whole metres.

Robert, 2026-09-23.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .production_sheet import Location, ProductionSheet, SPLICE, TERMINATION

FT_TO_M = 0.3048

# Distance from the ILA's frame out to the entry splice in the vault at
# the building.  A short run the production sheet does not measure, and
# it is NOT the same at both ends -- Tucumcari to Santa Rosa is 60 m at
# the A end and 50 m at the Z end -- so each end is set separately and
# this is only the default for both.
DEFAULT_ENTRY_OFFSET_M = 60

# How far the footage chain may sit from the trace before we call it a
# disagreement.  The two measure different things -- fibre in the tube
# versus cable sheath, plus the slack coiled in each enclosure -- so they
# never agree exactly.  Span 4's seven good segments land within 35 m,
# which is 0.7% of a 5 km segment, so 75 m passes an honest sheet and
# still catches the 1.5 km errors.
DEFAULT_TOLERANCE_M = 75

# ...and a share of the segment on top of it, because the slack coiled in
# each enclosure and the fibre's helix inside the tube both scale with the
# cable, so a fixed metre figure is too tight on a long segment.  The two
# spans we can check bracket the choice: Tucumcari's honest segments sit
# ~87 m out over 7.5 km (1.2%) while Span 4's two bad ones are 124 m over
# 5.8 km (2.1%) and 143 m over 6.0 km (2.4%).  1.5% separates them.
DEFAULT_TOLERANCE_PCT = 0.015

SITE_A = 'Site A'
SITE_Z = 'Site Z'

TERMINATION_EVENT = 'New FTP/FDP Termination'
FIELD_SPLICE_EVENT = 'New Field Splice'


@dataclass
class SpanEvent:
    """One row of the Event Log."""
    number: object                  # 'Site A' | 1..n | 'Site Z'
    location: Location
    location_text: str | None = None
    vault_id: str | None = None
    splice_type: str = FIELD_SPLICE_EVENT

    dist_from_a_m: int | None = None
    dist_from_z_m: int | None = None
    dist_to_next_m: int | None = None

    # The cross-check.  `footage_from_a_m` is where the production sheet's
    # own footage marks put this event; `delta_m` is how far that is from
    # the column we actually wrote.
    footage_from_a_m: int | None = None
    delta_m: int | None = None

    @property
    def is_terminal(self) -> bool:
        return self.number in (SITE_A, SITE_Z)


@dataclass
class EventChain:
    events: list[SpanEvent]
    span_length_m: int | None = None
    distance_source: str = 'footage'          # 'trace' | 'footage'
    warnings: list[str] = field(default_factory=list)

    @property
    def splice_events(self) -> list[SpanEvent]:
        return [e for e in self.events if not e.is_terminal]

    @property
    def event_count(self) -> int:
        """What the Event Log's own 'N Events' counter should read: the
        events between the two terminations, not the terminations."""
        return len(self.splice_events)


# ── footage helpers ───────────────────────────────────────────────────────

_FOOT_RE = re.compile(r'(-?\d[\d,]*(?:\.\d+)?)')


def parse_feet(value) -> float | None:
    """'06846ft' -> 6846.0, '16,406' -> 16406.0, 52 -> 52.0.

    Returns None for the multi-cable cells a termination sheet carries
    ('06846ft / 04340ft'): two laterals share one row there, and a lateral
    is not on the backbone chain, so there is nothing to take.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value)
    if '/' in text:
        return None
    m = _FOOT_RE.search(text.replace(',', ''))
    return float(m.group(1)) if m else None


def span_heading(prod: ProductionSheet) -> tuple[str, str]:
    """Which label on this span's sheets means 'towards Z', and which 'towards A'.

    The labels are not a fixed vocabulary.  Denver to Kansas City writes
    East and West; Stradford to El Paso writes SOUTH/WEST and NORTH/EAST.
    So they are learnt from the span instead of assumed: the first splice
    after Site A has only one backbone cable, the one heading down the
    span, and whatever else appears anywhere on the span is the way back.

    Returns ('', '') when the sheets carry no usable headings, which
    leaves the footage cross-check switched off rather than pairing two
    cables that are not on the same reel.
    """
    splices = [l for l in prod.locations if l.kind == SPLICE]
    seen: set = set()
    for loc in splices:
        seen |= loc.headings

    toward_z = ''
    for loc in splices:
        if len(loc.headings) == 1:
            toward_z = next(iter(loc.headings))
            break
    if not toward_z:
        return '', ''

    others = sorted(seen - {toward_z})
    return toward_z, (others[0] if len(others) == 1 else '')


def footage_segments(prod: ProductionSheet,
                     entry_offset_m: int = DEFAULT_ENTRY_OFFSET_M,
                     entry_offset_z_m: int | None = None
                     ) -> list[int | None]:
    """Cable length in metres between each pair of consecutive locations.

    One entry per gap, so len(locations) - 1 entries.  A gap whose two
    sheets do not both carry a usable backbone mark comes back None rather
    than a guess.
    """
    toward_z, toward_a = span_heading(prod)
    locs = prod.locations
    out: list[int | None] = []
    for i in range(len(locs) - 1):
        here, nxt = locs[i], locs[i + 1]
        # The two terminal gaps are frame-to-entry-splice runs, which the
        # footage marks do not describe (the lateral cables in those rows
        # go to the frame, not down the span).
        if here.kind == TERMINATION or nxt.kind == TERMINATION:
            at_z = nxt.kind == TERMINATION and i > 0
            out.append(entry_offset_z_m if (at_z and entry_offset_z_m is not None)
                       else entry_offset_m)
            continue
        if not (toward_z and toward_a):
            out.append(None)
            continue
        a = here.cable_toward(toward_z)
        b = nxt.cable_toward(toward_a)
        fa = parse_feet(a.seq_at_node) if a else None
        fb = parse_feet(b.seq_at_node) if b else None
        if fa is None or fb is None:
            out.append(None)
            continue
        out.append(int(round(abs(fa - fb) * FT_TO_M)))
    return out


# ── the chain ─────────────────────────────────────────────────────────────

def _location_text(loc: Location, site_a_text: str | None,
                   site_z_text: str | None, at_a_end: bool) -> str | None:
    """What goes in the Event Log's 'Event Location' cell.

    A termination and the entry splice beside it are both AT the building,
    so both print the street address and alias of that end.  Every
    location in between prints what the production sheet holds for it,
    which in practice is a GPS fix.
    """
    if loc.kind == TERMINATION or (loc.vault_id == 'ENTRY'):
        return site_a_text if at_a_end else site_z_text
    return loc.address or loc.name


def build_chain(prod: ProductionSheet,
                *,
                trace_distances_m: list[float] | None = None,
                span_length_m: float | None = None,
                entry_offset_m: int = DEFAULT_ENTRY_OFFSET_M,
                entry_offset_z_m: int | None = None,
                site_a_text: str | None = None,
                site_z_text: str | None = None,
                splice_type: str = FIELD_SPLICE_EVENT,
                termination_type: str = TERMINATION_EVENT,
                tolerance_m: int = DEFAULT_TOLERANCE_M,
                tolerance_pct: float = DEFAULT_TOLERANCE_PCT) -> EventChain:
    """Build the Event Log chain for a span.

    `trace_distances_m` is one distance from Site A per SPLICE location,
    in span order, as measured on the traces.  When it is given it fills
    the distance columns and the footage marks only check it.  When it is
    absent the footage marks fill the columns, and the chain says so.
    """
    locs = prod.locations
    warnings: list[str] = []
    n = len(locs)
    if n < 3:
        warnings.append(
            f'the span has only {n} location worksheets — an Event Log needs '
            f'a termination at each end and at least one splice between them')

    # Which end of the span each location belongs to, for the address text.
    mid = (n - 1) / 2

    segments = footage_segments(prod, entry_offset_m=entry_offset_m,
                                entry_offset_z_m=entry_offset_z_m)
    footage_cum: list[int | None] = [0]
    running = 0
    broken = False
    for seg in segments:
        if seg is None or broken:
            broken = True
            footage_cum.append(None)
            continue
        running += seg
        footage_cum.append(running)

    source = 'footage'
    dist: list[int | None] = list(footage_cum)

    if trace_distances_m is not None:
        splice_idx = [i for i, l in enumerate(locs) if l.kind == SPLICE]
        if len(trace_distances_m) != len(splice_idx):
            warnings.append(
                f'the traces found {len(trace_distances_m)} closures but the '
                f'production sheet has {len(splice_idx)} splice worksheets — '
                f'distances left on the production sheet’s footage marks')
        else:
            source = 'trace'
            total = span_length_m
            if total is None:
                total = max(trace_distances_m) + entry_offset_m
            dist = [None] * n
            dist[0] = 0
            dist[n - 1] = int(round(total))
            for i, d in zip(splice_idx, trace_distances_m):
                dist[i] = int(round(d))

    if span_length_m is not None:
        total_m = int(round(span_length_m))
    elif dist[n - 1] is not None:
        total_m = dist[n - 1]
    else:
        total_m = None

    events: list[SpanEvent] = []
    splice_no = 0
    for i, loc in enumerate(locs):
        if loc.kind == TERMINATION and i == 0:
            number, ev_type = SITE_A, termination_type
        elif loc.kind == TERMINATION and i == n - 1:
            number, ev_type = SITE_Z, termination_type
        else:
            splice_no += 1
            number, ev_type = splice_no, splice_type

        d_a = dist[i]
        ev = SpanEvent(
            number=number,
            location=loc,
            location_text=_location_text(loc, site_a_text, site_z_text,
                                         at_a_end=(i <= mid)),
            vault_id=loc.vault_id or 'NA',
            splice_type=ev_type,
            dist_from_a_m=d_a,
            dist_from_z_m=(None if d_a is None or total_m is None
                           else max(0, total_m - d_a)),
            footage_from_a_m=footage_cum[i],
        )
        if ev.footage_from_a_m is not None and d_a is not None:
            ev.delta_m = d_a - ev.footage_from_a_m
        events.append(ev)

    for i, ev in enumerate(events):
        nxt = events[i + 1] if i + 1 < len(events) else None
        if nxt and ev.dist_from_a_m is not None and nxt.dist_from_a_m is not None:
            ev.dist_to_next_m = nxt.dist_from_a_m - ev.dist_from_a_m
        elif nxt is None:
            ev.dist_to_next_m = 0

    chain = EventChain(events=events, span_length_m=total_m,
                       distance_source=source, warnings=warnings)
    chain.warnings.extend(reconcile(chain, tolerance_m=tolerance_m,
                                    tolerance_pct=tolerance_pct))
    return chain


def reconcile(chain: EventChain, tolerance_m: int = DEFAULT_TOLERANCE_M,
              tolerance_pct: float = DEFAULT_TOLERANCE_PCT) -> list[str]:
    """Name every location where the traces and the footage marks part ways.

    Reported per SEGMENT rather than per cumulative distance: once one
    segment is wrong every event after it is offset by the same amount,
    and a per-event report would blame ten innocent sheets for one bad
    one.
    """
    if chain.distance_source != 'trace':
        if any(e.dist_from_a_m is None for e in chain.events):
            return ['the footage marks do not chain the whole span — some '
                    'Event Log distances are blank']
        return []

    out: list[str] = []
    for i in range(len(chain.events) - 1):
        a, b = chain.events[i], chain.events[i + 1]
        if None in (a.dist_from_a_m, b.dist_from_a_m,
                    a.footage_from_a_m, b.footage_from_a_m):
            continue
        measured = b.dist_from_a_m - a.dist_from_a_m
        marked = b.footage_from_a_m - a.footage_from_a_m
        allowed = max(tolerance_m, abs(measured) * tolerance_pct)
        if abs(measured - marked) > allowed:
            out.append(
                f'{a.location.sheet} → {b.location.sheet}: the traces measure '
                f'{measured} m, the footage marks say {marked} m '
                f'({measured - marked:+d} m) — check the Cable Information '
                f'rows on both sheets')
    return out
