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

import itertools
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
                location_texts: dict | None = None,
                splice_type: str = FIELD_SPLICE_EVENT,
                termination_type: str = TERMINATION_EVENT,
                tolerance_m: int = DEFAULT_TOLERANCE_M,
                tolerance_pct: float = DEFAULT_TOLERANCE_PCT) -> EventChain:
    """Build the Event Log chain for a span.

    `location_texts` ({production-sheet tab: text}) replaces a splice's
    Event Location, e.g. with a GPS fix taken in the field.

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
            f'the span has only {n} location worksheets; an Event Log needs '
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
                f'production sheet has {len(splice_idx)} splice worksheets; '
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
            location_text=((location_texts or {}).get(loc.sheet)
                           if loc.kind != TERMINATION and (location_texts or {}).get(loc.sheet)
                           else _location_text(loc, site_a_text, site_z_text,
                                               at_a_end=(i <= mid))),
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
            return ['the footage marks do not chain the whole span: some '
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
                f'({measured - marked:+d} m). Check the Cable Information '
                f'rows on both sheets')
    return out


# ── distances from a Splice Report ──────────────────────────────────────
# The Event Log distances can come from the span's traces.
#
# The Event Log wants one distance from Site A per splice location.  Until
# now a tech read those off the traces and typed them into the FQA Builder.
# This section takes them from the Splice Report engine instead: the closure
# columns its grid prints are population-validated splice positions across
# every fibre of the cable, which is a better answer than any one fibre a
# tech happens to open.
#
# What it does NOT do is read a trace.  The FQA Builder imports no
# sor_reader (the three engines ship divergent copies that cannot share one
# namespace), so the hub runs -- or reuses -- a Splice Report and hands the
# engine's JSON manifest in here.  Everything below is plain numbers.
#
# The manifest fields used
# ------------------------
#   columns   [{'km', 'kind', 'is_repair', 'num'}, ...]  -- the grid's columns.
#             kind 'splice' is a validated closure (the entry case at the A
#             end included); 'bend' / 'damage' / 'ref' columns are off-splice
#             findings and 'connector' / 'section' belong to panel spans.
#   span_km   the cable end, median of the far end of the longest fibres.
#
# Both are launch-normalised: 0 km is the A panel, the launch reel removed.
# That is the frame the Event Log uses too -- on a real span the engine's
# closures land within the spread of the individual fibres of what the tech
# typed, and its span end within 10 m of the form's span length.
#
# Mapping closures to splice worksheets
# -------------------------------------
# The two ENTRY splices sit a few tens of metres from the frame, inside the
# launch dead zone, so the traces rarely see them and the finished packages
# we have carry them at the entry offset (60 m from Site A, span length
# minus 50-60 m at the Z end).  An entry splice therefore takes a measured
# closure only when one sits within ENTRY_WINDOW_M of its end of the span
# (the engine's own entry-case window); otherwise it takes the offset.
#
# Every other splice worksheet is a vault on the backbone and takes one
# closure, in span order.  When the counts agree that is the whole job, and
# build_chain's reconcile() reports any segment where the footage marks
# disagree.  When the traces found MORE closures than the sheet has vaults
# (a repair splice, a closure nobody wrote a sheet for), the footage marks
# decide which ones to drop -- but only when exactly one choice puts every
# vault within tolerance of its marks.  Anything else, fewer closures than
# vaults included, returns no distances and says why: a wrong vault with a
# wrong distance is worse than a blank the tech can see.

# A closure this close to either end of the span is that end's entry
# splice.  The Splice Report's ENTRY_CASE_MAX_KM (1.0 km) draws the same
# line at the A end; the Z end mirrors it.
ENTRY_WINDOW_M = 1000.0

# How many ways of dropping extra closures we are willing to try.  A span
# with a handful of extras is a few hundred; past this the traces and the
# sheet are not describing the same thing and guessing is the wrong answer.
MAX_ALIGNMENTS = 50000

METHOD_ORDER = 'order'
METHOD_FOOTAGE = 'footage-aligned'
METHOD_NONE = 'none'


def _result(distances=None, span=None, closures=None, method=METHOD_NONE,
            warnings=None, matches=None, dropped=None):
    return {
        'distances_m': distances,
        'span_length_m': span,
        'closures_found_m': closures or [],
        'dropped_m': dropped or [],
        'matches': matches or [],
        'method': method,
        'warnings': warnings or [],
    }


def _entry_positions(prod: ProductionSheet) -> tuple[int | None, int | None]:
    """Index into prod.splices of the A and Z entry splices, or None.

    An entry splice is the splice worksheet right beside a termination
    whose tab says it is the entry.  A splice beside a termination that is
    NOT named an entry is a vault like any other and has to be measured.
    """
    locs = prod.locations
    splices = prod.splices
    a = z = None
    if (len(locs) >= 2 and locs[0].kind == TERMINATION
            and locs[1].kind == SPLICE and locs[1].vault_id == 'ENTRY'):
        a = splices.index(locs[1])
    if (len(locs) >= 3 and locs[-1].kind == TERMINATION
            and locs[-2].kind == SPLICE and locs[-2].vault_id == 'ENTRY'):
        zi = splices.index(locs[-2])
        if zi != a:
            z = zi
    return a, z


def _footage_anchors(prod: ProductionSheet, span_m: float | None,
                     entry_offset_m: int, entry_offset_z_m: int | None):
    """Where the footage marks put each location, counted from each end.

    Two chains, not one: a single bad mark breaks the chain from A for
    every location after it, but the chain from Z still places those.  A
    closure that agrees with EITHER chain agrees with the sheet.
    Returns two lists (metres from Site A), one entry per location, None
    where that chain has broken.
    """
    segs = footage_segments(prod, entry_offset_m=entry_offset_m,
                            entry_offset_z_m=entry_offset_z_m)
    n = len(prod.locations)
    from_a: list[float | None] = [0.0]
    for s in segs:
        prev = from_a[-1]
        from_a.append(None if (s is None or prev is None) else prev + s)
    from_z_rev: list[float | None] = [0.0]
    for s in reversed(segs):
        prev = from_z_rev[-1]
        from_z_rev.append(None if (s is None or prev is None) else prev + s)
    from_z = list(reversed(from_z_rev))
    if span_m is None:
        from_z = [None] * n
    else:
        from_z = [None if v is None else span_m - v for v in from_z]
    return from_a[:n], from_z[:n]


def _agrees(d: float, fa: float | None, fz: float | None, span_m: float | None,
            tol_m: float, tol_pct: float) -> bool | None:
    """Does a closure at `d` agree with a location's footage marks?

    None when the marks cannot say (both chains broken there).  The share
    of distance allowed grows with the distance from the anchoring end,
    because what the marks miss -- slack in each enclosure, the helix of
    fibre inside the tube -- accumulates along the cable.
    """
    votes = []
    if fa is not None:
        votes.append(abs(d - fa) <= max(tol_m, tol_pct * d))
    if fz is not None and span_m is not None:
        votes.append(abs(d - fz) <= max(tol_m, tol_pct * (span_m - d)))
    if not votes:
        return None
    return any(votes)


def splice_distances_from_manifest(
        manifest: dict,
        prod: ProductionSheet,
        *,
        entry_offset_m: int = DEFAULT_ENTRY_OFFSET_M,
        entry_offset_z_m: int | None = None,
        tolerance_m: int = DEFAULT_TOLERANCE_M,
        tolerance_pct: float = DEFAULT_TOLERANCE_PCT) -> dict:
    """One distance from Site A per splice worksheet, from a Splice Report.

    `manifest` is the JSON manifest run_splicereport.py prints (or the one
    the hub cached for the Splice Report grid).  Returns

        distances_m      list, len(prod.splices), metres from Site A in
                         span order -- or None, with the reason in warnings
        span_length_m    the engine's span end in metres, or None
        closures_found_m every closure column the traces found, metres
        dropped_m        closures left off because no worksheet is theirs
        matches          per splice worksheet: sheet, vault, distance_m,
                         source ('trace' | 'entry offset'), footage_from_a_m
        method           'order' | 'footage-aligned' | 'none'
        warnings         list of plain-English strings
    """
    warnings: list[str] = []
    if not manifest or not manifest.get('ok'):
        err = (manifest or {}).get('error') or 'no report'
        return _result(warnings=[f'the Splice Report did not run: {err}'])
    if manifest.get('analysis_mode') == 'fr':
        return _result(warnings=[
            'that Splice Report was run in FastReporter mode, whose columns '
            'are FastReporter’s event rows rather than validated closures. '
            'Run it in OTDR mode for the Event Log distances'])

    span_km = manifest.get('span_km')
    span_m = float(span_km) * 1000.0 if span_km else None
    if span_m is None:
        warnings.append('the Splice Report gave no span length')

    cols = [c for c in (manifest.get('columns') or [])
            if c.get('kind', 'splice') == 'splice' and c.get('km') is not None]
    closures = sorted(float(c['km']) * 1000.0 for c in cols)
    found = [round(d, 1) for d in closures]

    splices = prod.splices
    n_s = len(splices)
    if n_s == 0:
        return _result(span=span_m, closures=found, warnings=warnings + [
            'the production sheet has no splice worksheets'])

    ent_a, ent_z = _entry_positions(prod)
    off_z = entry_offset_z_m if entry_offset_z_m is not None else entry_offset_m
    loc_index = {id(l): i for i, l in enumerate(prod.locations)}
    fa_all, fz_all = _footage_anchors(prod, span_m, entry_offset_m,
                                      entry_offset_z_m)

    def attempt(take_entries: bool):
        pool = list(closures)
        entry_d: dict[int, tuple[float, str]] = {}
        if ent_a is not None:
            near = [d for d in pool if d < ENTRY_WINDOW_M] if take_entries else []
            if near:
                entry_d[ent_a] = (near[0], 'trace')
                pool.remove(near[0])
            else:
                entry_d[ent_a] = (float(entry_offset_m), 'entry offset')
        if ent_z is not None:
            near = ([d for d in pool if span_m is not None
                     and span_m - d < ENTRY_WINDOW_M] if take_entries else [])
            if near:
                entry_d[ent_z] = (near[-1], 'trace')
                pool.remove(near[-1])
            elif span_m is not None:
                entry_d[ent_z] = (span_m - off_z, 'entry offset')
            else:
                entry_d[ent_z] = (None, 'entry offset')
        vault_idx = [i for i in range(n_s) if i not in entry_d]
        return pool, entry_d, vault_idx

    def align(pool, vault_idx):
        """-> (assigned closures per vault, dropped, method) or (None, reason)."""
        n_v, n_t = len(vault_idx), len(pool)
        if n_t == n_v:
            return pool, [], METHOD_ORDER, None
        if n_t < n_v:
            return None, None, None, (
                f'the traces found {n_t} closure{"s" * (n_t != 1)} between the '
                f'entry splices but the production sheet has {n_v} splice '
                f'worksheets there; a closure the traces did not see cannot '
                f'be placed, so the distances are left to the tech')
        fa = [fa_all[loc_index[id(splices[i])]] for i in vault_idx]
        fz = [fz_all[loc_index[id(splices[i])]] for i in vault_idx]
        total = 1
        for k in range(n_v):
            total = total * (n_t - k) // (k + 1)
        if total > MAX_ALIGNMENTS:
            return None, None, None, (
                f'the traces found {n_t} closures for {n_v} splice worksheets, '
                f'too many extras to line up against the footage marks')
        fits = []
        for keep in itertools.combinations(range(n_t), n_v):
            ok = True
            for k, j in enumerate(keep):
                verdict = _agrees(pool[j], fa[k], fz[k], span_m,
                                  tolerance_m, tolerance_pct)
                if verdict is False:
                    ok = False
                    break
            if ok:
                fits.append(keep)
                if len(fits) > 1:
                    break
        extra = n_t - n_v
        if not fits:
            return None, None, None, (
                f'the traces found {n_t} closures for {n_v} splice worksheets '
                f'and no choice of {extra} to leave out agrees with the '
                f'production sheet’s footage marks')
        if len(fits) > 1:
            return None, None, None, (
                f'the traces found {n_t} closures for {n_v} splice worksheets '
                f'and the footage marks do not say which {extra} to leave out '
                f'(more than one choice fits)')
        keep = fits[0]
        dropped = [pool[j] for j in range(n_t) if j not in keep]
        return [pool[j] for j in keep], dropped, METHOD_FOOTAGE, None

    pool, entry_d, vault_idx = attempt(take_entries=True)
    assigned, dropped, method, reason = align(pool, vault_idx)
    if assigned is None and any(src == 'trace' for _, src in entry_d.values()):
        # A vault within a kilometre of the frame, with the real entry in
        # the dead zone: give the near closure back to the vaults and retry.
        pool2, entry_d2, vault_idx2 = attempt(take_entries=False)
        a2, d2, m2, _ = align(pool2, vault_idx2)
        if a2 is not None:
            pool, entry_d, vault_idx = pool2, entry_d2, vault_idx2
            assigned, dropped, method, reason = a2, d2, m2, None
    if assigned is None:
        return _result(span=span_m, closures=found, warnings=warnings + [reason])

    dist: list[float | None] = [None] * n_s
    src: list[str] = [''] * n_s
    for i, (d, s) in entry_d.items():
        dist[i], src[i] = d, s
    for i, d in zip(vault_idx, assigned):
        dist[i], src[i] = d, 'trace'

    if any(d is None for d in dist):
        return _result(span=span_m, closures=found, warnings=warnings + [
            'the Z entry splice needs the span length and the Splice Report '
            'gave none'])

    if dropped:
        warnings.append(
            'left out closure'
            + ('s' if len(dropped) > 1 else '') + ' the traces found at '
            + ', '.join(f'{d / 1000:.2f} km' for d in dropped)
            + ': no splice worksheet is theirs by the footage marks '
              '(a repair, or a closure with no sheet). Check they are not '
              'a missing worksheet')

    matches = []
    for i, loc in enumerate(splices):
        li = loc_index[id(loc)]
        matches.append({
            'sheet': loc.sheet, 'vault': loc.vault_id,
            'distance_m': round(dist[i], 1), 'source': src[i],
            'footage_from_a_m': (None if fa_all[li] is None
                                 else round(fa_all[li], 1)),
        })

    return _result(distances=[round(d, 1) for d in dist],
                   span=round(span_m, 1) if span_m is not None else None,
                   closures=found, method=method, warnings=warnings,
                   matches=matches, dropped=[round(d, 1) for d in dropped])


def splice_distances(dir_a: str, dir_b: str, prod, *,
                     manifest: dict | None = None,
                     get_manifest=None, **kwargs) -> dict:
    """Event Log distances for a span from its A and B trace folders.

    The traces are read by the Splice Report engine, never here: pass the
    engine's `manifest` if the caller already has it, or `get_manifest`,
    a callable (dir_a, dir_b) -> manifest dict that runs or reuses one.
    The hub supplies that callable (app.py fqa_trace_distances) because it
    alone knows how to launch the engine in its own interpreter.

    `prod` is a ProductionSheet or a path to one.  Keyword arguments go to
    splice_distances_from_manifest.  Never raises for a bad run: the
    reason comes back in 'warnings' with distances_m None.
    """
    if isinstance(prod, str):
        from .production_sheet import read_production_sheet
        prod = read_production_sheet(prod)
    if manifest is None:
        if get_manifest is None:
            return _result(warnings=['no Splice Report to read the closures '
                                     'from'])
        try:
            manifest = get_manifest(dir_a, dir_b)
        except Exception as exc:                          # noqa: BLE001
            return _result(warnings=[
                f'the Splice Report could not run: {type(exc).__name__}: {exc}'])
    return splice_distances_from_manifest(manifest, prod, **kwargs)
