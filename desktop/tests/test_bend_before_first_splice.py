"""A bend upstream of Splice 1 must print, in its own column.

A 576-fiber span shot with no launch reel (the first real event 2.1 km from
the port): four edge fibers of the first ribbons lose 0.15-0.39 dB at 2.14 km,
in BOTH directions (A 0.387 / B 0.402 on the worst), 3.9 km before the first
closure.  FastReporter prints the event; Suite printed nothing, not even with a
lower gate, because two gates stood between the event and the grid:

  1. flag_consensus_bends' launch-closure guard dropped EVERY cluster at or
     before the first closure.  It was written for a cluster sitting 112 m
     before Splice 1, which is that closure's own splice; it is now bounded to
     the bend fold distance of the first closure, or the launch zone.
  2. split_offsplice_events_into_own_columns never builds a column inside
     LAUNCH_FIBER_MAX (3 km) of the launch, sized for a launch reel.  When the
     A population provably has no launch reel the zone is NO_LAUNCH_DEAD_KM,
     the same doctrine as the unidirectional front dead zone.

Synthetic records only: the engine functions are the real ones.  Engine tests
run in a clean subprocess (3-engine sor_reader isolation).
"""
import subprocess
import sys
import textwrap

from conftest import REPO_ROOT

SPLICEREPORT_DIR = REPO_ROOT / "splicereport"

# Shared synthetic span: 24 fibers, closures every ~6 km starting at
# `first_km`, both ends at 66.55 km.  `extra` maps fiber -> (km, A loss,
# B loss) for one additional event on top of the fiber's splices.
_SPAN = """
SPAN = 66.55


def span(extra, first_km=6.03, reel_absent=True, n=24):
    closures = [first_km, 12.10, 17.75, 22.32, 29.21, 34.76, 39.69]
    fa, fb = {}, {}
    for f in range(1, n + 1):
        ea = [{'dist_km': c, 'splice_loss': 0.03, 'is_end': False,
               'is_reflective': False, 'type': '0F9999LS'} for c in closures]
        eb = [{'dist_km': round(SPAN - c, 4), 'splice_loss': 0.03,
               'is_end': False, 'is_reflective': False, 'type': '0F9999LS'}
              for c in closures]
        if f in extra:
            km, a_loss, b_loss = extra[f]
            ea.append({'dist_km': km, 'splice_loss': a_loss, 'is_end': False,
                       'is_reflective': False, 'type': '0F9999LS'})
            eb.append({'dist_km': round(SPAN - km + 0.01, 4),
                       'splice_loss': b_loss, 'is_end': False,
                       'is_reflective': False, 'type': '0F9999LS'})
        for ev in (ea, eb):
            ev.append({'dist_km': SPAN, 'splice_loss': 0.0, 'is_end': True,
                       'is_reflective': True, 'type': '1E9999LS'})
            ev.sort(key=lambda e: e['dist_km'])
        stamp = {} if reel_absent is None else {'_launch_reel_absent': reel_absent}
        fa[f] = {'events': ea, **stamp}
        fb[f] = {'events': eb, **stamp}
    splices = [{'position_km': c, 'position_km_refined': c,
                'column_kind': 'splice'} for c in closures]
    return fa, fb, splices


# The four edge fibers of the field case, A / B losses as measured.
EDGE = {10: (2.149, 0.151, 0.154), 11: (2.144, 0.181, 0.191),
        12: (2.1465, 0.387, 0.402), 24: (2.139, 0.161, 0.171)}


def bend_cells(km_by_fiber, si=0):
    return {(f, 90000 + f): {
                'fiber': f, 'splice_idx': si, 'bidir_dist': km,
                'bidir_loss': 0.2, 'is_bend': True, 'is_flagged': True,
                'is_break': False, 'is_broke': False, 'is_ref': False,
                'event_source': 'bend'}
            for f, km in km_by_fiber.items()}
"""


def _run(body):
    header = ("import sys\n"
              f"sys.path.insert(0, {str(SPLICEREPORT_DIR)!r})\n"
              "import splicereportmatchexfo as E\n")
    p = subprocess.run([sys.executable, "-c",
                        header + _SPAN + textwrap.dedent(body)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    assert p.stdout.strip().splitlines()[-1] == "OK", p.stdout


def test_consensus_bend_upstream_of_splice_1_is_flagged():
    """The field case: a 4-fiber cluster 3.9 km BEFORE the first closure on a
    span with no launch reel is a bend, not Splice 1.  The old guard dropped
    it (returned nothing)."""
    _run("""
        fa, fb, splices = span(EDGE)
        new = E.flag_consensus_bends({}, fa, fb, splices, SPAN)
        got = {r['fiber']: r for r in new.values()}
        assert sorted(got) == [10, 11, 12, 24], f"expected the 4 edge fibers: {got}"
        assert all(r['is_bend'] and r['is_flagged'] for r in got.values()), got
        assert abs(got[12]['bidir_loss'] - 0.3945) < 1e-3, got[12]
        assert all(abs(r['bidir_dist'] - EDGE[f][0]) < 1e-9
                   for f, r in got.items()), got
        print('OK')
    """)


def test_upstream_bend_gets_its_own_column_without_launch_reel():
    """End to end through the two passes: the cluster becomes a 'bend' column
    at ~2.14 km and all four cells land in it, not in Splice 1."""
    _run("""
        fa, fb, splices = span(EDGE)
        allr = dict(E.flag_consensus_bends({}, fa, fb, splices, SPAN))
        out, sp2 = E.split_offsplice_events_into_own_columns(
            allr, [dict(s) for s in splices], total_span_km=SPAN, fibers_a=fa)
        bend_cols = [i for i, s in enumerate(sp2) if s.get('column_kind') == 'bend']
        assert len(bend_cols) == 1, f"expected one bend column: {sp2}"
        col = bend_cols[0]
        assert abs(sp2[col]['position_km_refined'] - 2.145) < 0.01, sp2[col]
        assert sorted(f for (f, si) in out if si == col) == [10, 11, 12, 24], out
        assert [s['position_km'] for s in sp2 if s.get('column_kind') == 'splice'] \\
            == [s['position_km'] for s in splices], sp2
        print('OK')
    """)


def test_launch_zone_is_short_only_without_launch_reel():
    """split_offsplice alone: bend cells at 2.14 km get a column when the A
    population is stamped reel-absent, and stay in Splice 1 (the old 3 km
    launch zone) when a reel is present or the records carry no stamp."""
    _run("""
        km = {f: EDGE[f][0] for f in EDGE}
        for stamp, want_col in ((True, True), (False, False), (None, False)):
            fa, fb, splices = span(EDGE, reel_absent=stamp)
            out, sp2 = E.split_offsplice_events_into_own_columns(
                bend_cells(km), [dict(s) for s in splices],
                total_span_km=SPAN, fibers_a=fa)
            has = any(s.get('column_kind') == 'bend' for s in sp2)
            assert has == want_col, (stamp, sp2)
        assert E._launch_zone_km(None) == E.LAUNCH_FIBER_MAX
        assert E._launch_zone_km({}) == E.LAUNCH_FIBER_MAX
        print('OK')
    """)


def test_short_launch_zone_is_for_bends_only():
    """A bidirectional reburn cell whose km falls near the launch keeps the
    3 km zone even with no launch reel: its km can be the midpoint of two
    legs read far apart, where neither trace has an event."""
    _run("""
        fa, fb, splices = span({}, reel_absent=True)
        cells = bend_cells({7: 1.127})
        cells[(7, 90007)].update({'is_bend': False, 'event_source': 'bidir',
                                  'bidir_loss': 2.064})
        out, sp2 = E.split_offsplice_events_into_own_columns(
            cells, [dict(s) for s in splices], total_span_km=SPAN, fibers_a=fa)
        assert [s.get('column_kind') for s in sp2] == ['splice'] * len(splices), sp2
        print('OK')
    """)


def test_cluster_just_before_splice_1_is_still_that_splice():
    """The guard's own case, kept: a 2-fiber cluster 112 m before Splice 1
    (inside the bend fold distance) is Splice 1 reading short, not a bend,
    even on a span with no launch reel."""
    _run("""
        near = {8: (1.718, 0.10, 0.11), 21: (1.718, 0.09, 0.10)}
        fa, fb, splices = span(near, first_km=1.830)
        assert E.flag_consensus_bends({}, fa, fb, splices, SPAN) == {}
        print('OK')
    """)


def test_launch_zone_cluster_with_launch_reel_unchanged():
    """Reel present: the 2.14 km cluster sits inside the 3 km launch zone, so
    the guard still drops it, exactly as before this change."""
    _run("""
        fa, fb, splices = span(EDGE, reel_absent=False)
        assert E.flag_consensus_bends({}, fa, fb, splices, SPAN) == {}
        fa, fb, splices = span(EDGE, reel_absent=None)
        assert E.flag_consensus_bends({}, fa, fb, splices, SPAN) == {}
        print('OK')
    """)
