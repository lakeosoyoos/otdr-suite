"""Both-events escape from the bend fold — regression tests.

Lumen Border (2026-07-23): real bends @7.77 km sit ~170 m from the real
7.94 km splice — inside the 200 m fold — so the bidirectional report
folded them into the splice column while the uni tool (tighter radii)
correctly gave them their own Bend/Damage column.  The escape: a cluster
whose member fibers ALSO carry their own splice event at the column is
genuinely additional damage (a fiber's splice can't be in two places) and
keeps its own column at any fold distance.  PLACHE-style lay tails
(single events, no separate splice entry) keep folding exactly as before.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'splicereport'))

import splicereportmatchexfo as E  # noqa: E402


def _cell(fnum, km, loss=0.1):
    return {'fiber': fnum, 'splice_idx': 0, 'bidir_loss': loss,
            'a_loss': loss, 'b_loss': loss, 'bidir_dist': km,
            'is_break': False, 'is_broke': False, 'is_bend': True,
            'is_flagged': True, 'event_source': 'bend',
            'label': f'{fnum} BEND .100 bidi'}


def _fiber(events_km):
    return {'events': [{'dist_km': k, 'splice_loss': 0.1, 'is_end': False,
                        'is_reflective': False, 'time_of_travel': 0}
                       for k in events_km]}


SPLICES = [{'position_km': 7.936, 'position_km_refined': 7.936}]


def test_both_events_cluster_escapes_fold():
    """Three bend cells ~170 m from the splice, each fiber ALSO carrying
    its own splice event at the column -> own bend column."""
    results = {(f, 0): _cell(f, 7.766) for f in (1, 2, 3)}
    fibers_a = {f: _fiber([7.766, 7.936]) for f in (1, 2, 3)}
    out, spl = E.split_offsplice_events_into_own_columns(
        dict(results), list(SPLICES), total_span_km=11.4, fibers_a=fibers_a)
    kinds = [sp.get('column_kind', 'splice') for sp in spl]
    assert 'bend' in kinds, kinds                 # own column created
    bend_idx = kinds.index('bend')
    assert abs(spl[bend_idx]['position_km_refined'] - 7.766) < 0.05


def test_lay_tail_cluster_still_folds():
    """PLACHE class: same geometry but NO second event on any member
    fiber -> cluster stays folded into the splice column (no new column)."""
    results = {(f, 0): _cell(f, 7.766) for f in (1, 2, 3)}
    fibers_a = {f: _fiber([7.766]) for f in (1, 2, 3)}   # only the one event
    out, spl = E.split_offsplice_events_into_own_columns(
        dict(results), list(SPLICES), total_span_km=11.4, fibers_a=fibers_a)
    assert len(spl) == 1 and spl[0].get('column_kind', 'splice') == 'splice'


def test_single_both_events_member_is_not_enough():
    """One member with both events could be a table quirk — below
    BEND_CLUSTER_BOTH_EVENTS_MIN the fold behavior is unchanged."""
    results = {(f, 0): _cell(f, 7.766) for f in (1, 2, 3)}
    fibers_a = {1: _fiber([7.766, 7.936]),
                2: _fiber([7.766]), 3: _fiber([7.766])}
    out, spl = E.split_offsplice_events_into_own_columns(
        dict(results), list(SPLICES), total_span_km=11.4, fibers_a=fibers_a)
    assert len(spl) == 1


def test_far_cluster_unaffected():
    """Clusters beyond the fold distance never consulted the escape —
    own column exactly as before."""
    results = {(f, 0): _cell(f, 7.400) for f in (1, 2, 3)}
    fibers_a = {f: _fiber([7.400]) for f in (1, 2, 3)}
    out, spl = E.split_offsplice_events_into_own_columns(
        dict(results), list(SPLICES), total_span_km=11.4, fibers_a=fibers_a)
    assert any(sp.get('column_kind') == 'bend' for sp in spl)


def test_no_fibers_a_fails_open_to_fold():
    """Without fiber data the escape can't run — classic fold behavior."""
    results = {(f, 0): _cell(f, 7.766) for f in (1, 2, 3)}
    out, spl = E.split_offsplice_events_into_own_columns(
        dict(results), list(SPLICES), total_span_km=11.4, fibers_a=None)
    assert len(spl) == 1


def test_source_lock_both_events_escape():
    src = open(os.path.join(ROOT, 'splicereport', 'splicereportmatchexfo.py'),
               encoding='utf-8').read()
    assert 'BEND_CLUSTER_BOTH_EVENTS_MIN = 2' in src
    assert 'BEND_OWN_SPLICE_TOL_KM' in src
    assert src.count('_n_both < BEND_CLUSTER_BOTH_EVENTS_MIN') == 1
