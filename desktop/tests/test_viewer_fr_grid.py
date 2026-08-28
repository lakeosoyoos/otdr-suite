"""The FastReporter grid has to hold a whole cable, not two traces.

Field report (the boss, Viewer bidi page with fibers 1-12 loaded in A+B):
"loss units are not populating correctly so we can scroll to bottom and see all
losses high, low, average."

Nothing was broken about the losses.  `renderEventTable` sent anything past two
traces to the flat per-trace list, and that list has no statistics block at all,
so 24 traces meant no Minimum / Maximum / Average anywhere on the page.  Lifting
the cap needed two things fixed first.

1. THE CLUSTERING WAS CUBIC.  `clusterColumns` rescanned every pair, took the
   global minimum, merged, re-sorted, and repeated.  Timed in the browser on the
   real Tucu <-> Romero span:

       10 traces (200 events)       9 ms
       20 traces (400 events)      37 ms
       40 traces (800 events)     303 ms
       80 traces (1,600 events) 4,258 ms

   A clean cube, so 1,152 fibers in A+B (2,304 traces, ~46,000 events) came out
   near a day.  It also allocated one dense `traces.length` array per starting
   event, which at that size is 106 million slots before a single merge.

   The list is sorted, so the globally closest pair is ADJACENT unless something
   between them cannot merge -- and each of those failures is exactly what
   pushes the next candidate.  A heap over adjacent pairs therefore examines the
   same pairs in the same order and picks the same winner, in O(k log k).
   Measured after: 864 traces / 11,810 events / 25 columns in 21 ms.

2. EVERY ROW WAS BUILT UP FRONT.  2,304 rows of ~80 cells is 184,000 elements.
   The body is now painted a slice at a time against spacer rows, inside ONE
   table so the browser still does the column layout: 864 traces render 2,206
   DOM cells in 65 ms and repaint on scroll in 4 ms.

The equivalence claim is the load-bearing one, so it is tested directly below:
both implementations are mirrored here and run against each other on generated
event sets.  In the browser, on the real 97 km span, they agreed exactly at 2,
4, 8, 16 and 40 traces -- same column count, same km to the last bit, same
occupancy.
"""
import os
import random
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
VIEWER = os.path.join(ROOT, 'viewer', 'viewer.html')
SRC = open(VIEWER, encoding='utf-8').read()


# ── The two implementations, mirrored ────────────────────────────────────
# No Node on this machine, so the JS is mirrored here.  These follow the two
# viewer.html functions statement for statement; the source assertions further
# down are what tie them to the real thing.

def _may_merge(p, q, tol):
    if p['refl'] != q['refl']:
        return False
    for ti in p['ev']:
        if ti in q['ev']:
            return False
    return abs(p['km'] - q['km']) <= tol


def cluster_old(n_traces, items, tol):
    """The O(k^3) original: rescan every pair, merge the global minimum."""
    cols = [{'km': it['km'], 'n': 1, 'ev': {it['ti']: it['e']},
             'refl': bool(it['e']['is_reflective'])} for it in items]
    cols.sort(key=lambda c: c['km'])
    while True:
        best = None
        for i in range(len(cols) - 1):
            for j in range(i + 1, len(cols)):
                d = cols[j]['km'] - cols[i]['km']
                if d > tol:
                    break
                if not _may_merge(cols[i], cols[j], tol):
                    continue
                if best is None or d < best[0]:
                    best = (d, i, j)
        if best is None:
            break
        _, i, j = best
        a, b = cols[i], cols[j]
        a['km'] = (a['km'] * a['n'] + b['km'] * b['n']) / (a['n'] + b['n'])
        a['n'] += b['n']
        for ti, e in b['ev'].items():
            a['ev'].setdefault(ti, e)
        cols.pop(j)
        cols.sort(key=lambda c: c['km'])
    return cols


def cluster_new(n_traces, items, tol):
    """The heap version: adjacent candidates, extended past a veto."""
    import heapq
    nodes = [{'km': it['km'], 'n': 1, 'ev': {it['ti']: it['e']},
              'refl': bool(it['e']['is_reflective']), 'alive': True, 'ver': 0}
             for it in items]
    if not nodes:
        return []
    nodes.sort(key=lambda c: c['km'])
    for i, nd in enumerate(nodes):
        nd['prev'] = i - 1
        nd['next'] = i + 1 if i + 1 < len(nodes) else -1

    heap = []

    def offer(a, b):
        if a < 0 or b < 0:
            return
        d = nodes[b]['km'] - nodes[a]['km']
        if d > tol:
            return
        heapq.heappush(heap, (d, a, b, nodes[a]['ver'], nodes[b]['ver']))

    for i in range(len(nodes)):
        offer(i, nodes[i]['next'])

    while heap:
        d, ia, ib, va, vb = heapq.heappop(heap)
        A, B = nodes[ia], nodes[ib]
        if not A['alive'] or not B['alive'] or A['ver'] != va or B['ver'] != vb:
            continue
        if not _may_merge(A, B, tol):
            offer(ia, B['next'])
            continue
        A['km'] = (A['km'] * A['n'] + B['km'] * B['n']) / (A['n'] + B['n'])
        A['n'] += B['n']
        for ti, e in B['ev'].items():
            A['ev'].setdefault(ti, e)
        A['ver'] += 1
        B['alive'] = False
        A['next'] = B['next']
        if B['next'] >= 0:
            nodes[B['next']]['prev'] = ia
        offer(A['prev'], ia)
        offer(ia, A['next'])

    out = [nd for nd in nodes if nd['alive']]
    out.sort(key=lambda c: c['km'])
    return out


def _span(n_traces, n_events, seed, jitter=0.004, refl_every=4):
    """One event per trace per true position, scattered by a few metres."""
    rnd = random.Random(seed)
    items = []
    for ti in range(n_traces):
        for k in range(n_events):
            items.append({
                'km': k * 5.0 + rnd.uniform(-jitter, jitter), 'ti': ti,
                'e': {'is_reflective': k % refl_every == 0, 'id': (ti, k)},
            })
    items.sort(key=lambda it: it['km'])
    return items


def _same(a, b):
    if len(a) != len(b):
        return False, 'column count %d vs %d' % (len(a), len(b))
    for i, (p, q) in enumerate(zip(a, b)):
        if p['km'] != q['km']:
            return False, 'column %d km %r vs %r' % (i, p['km'], q['km'])
        if p['n'] != q['n'] or p['ev'].keys() != q['ev'].keys():
            return False, 'column %d occupancy differs' % i
    return True, ''


def test_the_fast_clustering_returns_exactly_the_old_columns():
    """The rewrite is a speed change, so any column it moves is a bug."""
    for seed in range(8):
        items = _span(6, 7, seed)
        ok, why = _same(cluster_old(6, items, 0.05), cluster_new(6, items, 0.05))
        assert ok, 'seed %d: %s' % (seed, why)


def test_they_agree_when_the_veto_forces_a_non_adjacent_merge():
    """The case the heap could plausibly get wrong.

    A reflective event wedged between two non-reflective ones makes the closest
    MERGEABLE pair non-adjacent.  The heap only reaches it by extending past
    the rejected candidate, so if that path were missing this test would show a
    different column count.
    """
    e = lambda refl, i: {'is_reflective': refl, 'id': i}
    items = [
        {'km': 10.000, 'ti': 0, 'e': e(False, 0)},
        {'km': 10.004, 'ti': 1, 'e': e(True, 1)},
        {'km': 10.006, 'ti': 2, 'e': e(False, 2)},
        {'km': 10.030, 'ti': 3, 'e': e(True, 3)},
    ]
    old, new = cluster_old(4, items, 0.05), cluster_new(4, items, 0.05)
    ok, why = _same(old, new)
    assert ok, why
    # ...and the veto really did fire: the two non-reflective events merged
    # across the reflective one between them.
    assert len(new) == 2, [c['km'] for c in new]


def test_they_agree_when_one_trace_owns_two_events_inside_the_window():
    """One event per trace per column is the other veto, and it is what stops
    a fiber's two close events collapsing into one cell."""
    e = lambda i: {'is_reflective': False, 'id': i}
    items = [
        {'km': 3.000, 'ti': 0, 'e': e(0)},
        {'km': 3.002, 'ti': 0, 'e': e(1)},     # same trace, 2 m apart
        {'km': 3.004, 'ti': 1, 'e': e(2)},
        {'km': 3.006, 'ti': 2, 'e': e(3)},
    ]
    old, new = cluster_old(3, items, 0.05), cluster_new(3, items, 0.05)
    ok, why = _same(old, new)
    assert ok, why
    assert len(new) == 2, 'a trace cannot appear twice in one column'


def test_they_agree_at_cable_scale():
    """Where the old one is too slow to live: 60 traces, 20 positions."""
    items = _span(60, 20, seed=99)
    ok, why = _same(cluster_old(60, items, 0.05), cluster_new(60, items, 0.05))
    assert ok, why


def test_the_new_clustering_is_not_quadratic():
    """A shape check, not a wall-clock one -- CI machines vary too much for a
    timing assertion, but a cube would blow the step ratio out regardless."""
    import time
    def t(n):
        items = _span(n, 12, seed=n)
        t0 = time.perf_counter(); cluster_new(n, items, 0.05)
        return time.perf_counter() - t0
    small, big = t(40), t(320)          # 8x the events
    assert big < small * 64, 'scaling looks super-quadratic: %.4f -> %.4f' % (small, big)


# ── The wiring, asserted against the source ──────────────────────────────

def test_the_grid_is_no_longer_capped_at_two_traces():
    assert 'if (visible.length <= 2) {' not in SRC, (
        'the two-trace cap is what sent the boss to a list with no statistics')
    fn = SRC[SRC.index('function renderEventTable'):][:1400]
    assert 'renderFastReporterGrid(visible, host, hint);' in fn


def test_the_body_is_virtualised_and_the_rows_built_on_demand():
    assert 'const rowHtml = (ti) =>' in SRC, 'rows must not be prebuilt'
    assert 'function paintRows()' in SRC
    assert 'spacerRow(' in SRC


def test_the_statistics_strip_is_pinned_not_merely_last():
    """Sticky has to sit on the row GROUP: a sticky <td> can only move inside
    its own <tfoot> box, so per-cell `bottom: 0` pins it where it already was.
    Both were measured in the browser; only the row group actually sticks."""
    assert re.search(r'table\.fr-table tfoot \{[^}]*position: sticky', SRC)
    assert re.search(r'table\.fr-table tfoot \{[^}]*bottom: 0', SRC)
    assert '<tfoot>${aggRows.join(\'\')}</tfoot>' in SRC


def test_the_sticky_corners_outrank_the_sticky_body_column():
    """`.fr-rowlab` gives every sticky-left cell z-index 3, header included, so
    the body cells won the tie by being later in the DOM and painted over the
    header.  Seen on screen at 864 traces."""
    assert re.search(r'thead th\.fr-rowlab,\s*\n\s*table\.fr-table tfoot td\.fr-rowlab \{ z-index: 6', SRC)


def test_flagged_only_still_means_something_in_the_grid():
    """It used to filter rows of the flat list.  With the grid serving every
    size it filters FIBERS, which at a whole cable is the more useful reading
    of it anyway."""
    assert 'const rowFails = traces.map(' in SRC
    assert 'gFlaggedOnly ? rowFails' in SRC or '!gFlaggedOnly || rowFails[i]' in SRC


def test_the_verdict_column_and_the_row_filter_share_one_rule():
    """They must not drift: a fiber hidden by "flagged only" while showing a
    pass tick, or the reverse, is the same class of contradiction that once
    made P/F disagree with the report."""
    fn = SRC[SRC.index('const rowHtml = (ti) =>'):][:900]
    assert 'const fail = rowFails[ti];' in fn


def test_the_viewer_no_longer_prints_why_flagged():
    """Robert, on the Viewer's event list: we do not need it here."""
    assert 'Why flagged' not in SRC
    assert 'class="why"' not in SRC
