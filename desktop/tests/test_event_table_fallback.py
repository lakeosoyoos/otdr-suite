"""Event-table ranking: the fallback for folders whose trace cannot answer.

Guards the three things that make it safe to ship:
  1. it stays silent when the fingerprint was competent (OK folders unchanged),
  2. it ranks rather than filters, so a pair outside the gate keeps its place,
  3. it never touches p_dup or any routing key.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..',
                                'secretsauce'))

from report_sor import (  # noqa: E402
    _event_table_fallback, _evt_fb_compare, _evt_fb_events,
    _EVT_FB_LOSS_DB, _EVT_FB_REFL_DB,
)


def _f(name, events, ts=0):
    return {'name': name, 'timestamp': ts, 'events': events}


def _ev(dist_km, loss, refl):
    return {'dist_km': dist_km, 'splice_loss': loss, 'reflection': refl}


# A pair that agrees on both axes, a pair that agrees only on reflectance,
# and a pair that agrees on neither.  Positions are identical throughout so
# the matcher is not what is under test.
ALIKE   = [_ev(0.0, 0.100, -53.000), _ev(0.069, 0.110, -52.000),
           _ev(1.074, 0.000, -57.000)]
ALIKE2  = [_ev(0.0, 0.103, -53.050), _ev(0.069, 0.112, -52.040),
           _ev(1.074, 0.000, -57.030)]
LOSS_OFF = [_ev(0.0, 0.180, -53.010), _ev(0.069, 0.190, -52.020),
            _ev(1.074, 0.000, -57.010)]
FAR     = [_ev(0.0, 0.400, -48.000), _ev(0.069, 0.500, -45.000),
           _ev(1.074, 0.000, -51.000)]

FILES = [_f('a', ALIKE, 100), _f('b', ALIKE2, 200),
         _f('c', LOSS_OFF, 300), _f('d', FAR, 400)]
PAIRS = [{'a': x, 'b': y, 'p_dup': 0.0}
         for i, x in enumerate('abcd') for y in 'abcd'[i + 1:]]


def _pairs():
    return [dict(p) for p in PAIRS]


def test_silent_when_the_fingerprint_was_competent():
    """An OK folder must not grow a section, so competent reports stay
    byte-stable."""
    assert _event_table_fallback(FILES, _pairs(), {'status': 'OK'}) is None


@pytest.mark.parametrize('status', ['NOT MEASURED', 'MARGINAL', None])
def test_speaks_when_the_fingerprint_could_not(status):
    fb = _event_table_fallback(FILES, _pairs(), {'status': status})
    assert fb is not None and fb['n_compared'] == len(PAIRS)


def test_ranks_rather_than_filters():
    """Every comparable pair keeps a rank, including the ones outside the
    gate: the boundary is soft and a pair one notch out must stay visible."""
    fb = _event_table_fallback(FILES, _pairs(), {'status': 'NOT MEASURED'})
    assert len(fb['rows']) == len(PAIRS)
    assert [r['rank'] for r in fb['rows']] == list(range(1, len(PAIRS) + 1))
    assert fb['n_within'] < len(PAIRS), 'this fixture must contain rejects'
    # a->b agrees on both axes and must lead
    assert {fb['rows'][0]['a'], fb['rows'][0]['b']} == {'a', 'b'}
    assert fb['rows'][0]['within_gate'] is True
    # pairs inside the gate sort ahead of every pair outside it
    flags = [r['within_gate'] for r in fb['rows']]
    assert flags == sorted(flags, reverse=True)


def test_loss_gate_demotes_but_does_not_delete():
    """a<->c agrees on reflectance and fails on loss.  It must appear, be
    marked outside the gate, and rank below the pairs inside it."""
    fb = _event_table_fallback(FILES, _pairs(), {'status': 'NOT MEASURED'})
    row = next(r for r in fb['rows'] if {r['a'], r['b']} == {'a', 'c'})
    assert row['max_drefl_db'] <= _EVT_FB_REFL_DB
    assert row['max_dloss_db'] > _EVT_FB_LOSS_DB
    assert row['within_gate'] is False
    assert row['rank'] > fb['n_within']


def test_never_touches_p_dup_or_routing():
    pairs = _pairs()
    _event_table_fallback(FILES, pairs, {'status': 'NOT MEASURED'})
    assert all(p['p_dup'] == 0.0 for p in pairs)
    assert all(not k.startswith('evt_fb_') or k in (
        'evt_fb_n_events', 'evt_fb_max_dpos_m', 'evt_fb_max_dloss_db',
        'evt_fb_max_drefl_db', 'evt_fb_within_gate', 'evt_fb_gap_s')
        for p in pairs for k in p)


def test_launch_and_far_end_events_are_kept():
    """`_event_match_quality`'s interior rule drops the end event and
    everything inside 10 m, which on a three-event panel span leaves one
    event and no opinion.  This selector must keep all three."""
    assert len(_evt_fb_events(_f('x', ALIKE))) == 3


def test_no_opinion_when_the_tables_do_not_line_up():
    a = _evt_fb_events(_f('x', ALIKE))
    b = _evt_fb_events(_f('y', [_ev(0.5, 0.1, -53.0)]))
    assert _evt_fb_compare(a, b) is None


def test_no_comparable_pairs_reports_why():
    thin = [_f('a', [_ev(0.0, 0.1, -53.0)]), _f('b', [_ev(0.9, 0.1, -53.0)])]
    fb = _event_table_fallback(thin, [{'a': 'a', 'b': 'b', 'p_dup': 0.0}],
                               {'status': 'NOT MEASURED'})
    assert fb['n_compared'] == 0 and fb['rows'] == [] and fb['note']
