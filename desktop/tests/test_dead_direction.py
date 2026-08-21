"""A dead direction is reported as a re-shoot, not as silence.

#85 stopped an end-of-fiber event at 0 km being read as a launch connector.
That was right — the Fresnel on that marker is an open OTDR port, not a
connector in the plant — but it left the fiber printing NOTHING.  Before #85
the tech got a wrong reflectance; after #85 the tech got nothing.  Neither
says what actually happened, which is that THIS DIRECTION'S ACQUISITION IS
UNUSABLE and has to be shot again.

The condition, chosen from a census of every span on disk (43 direction
folders, 29,520 traces): the trace's FIRST event is an end-of-fiber marker at
or inside DEAD_TRACE_EOF_MAX_KM.  Exactly three traces on disk qualify —

  HOWLAN A F631   `2E9999LS` @ 0.0000 km, refl -28.836, 2 events
  HOWLAN A F309   `1E9999LS` @ 0.0000 km, refl -28.695, 1 event
  LAGDUR A F36    `1E9999LS` @ 0.0000 km, refl -49.828, 1 event

— and the next-smallest end-of-fiber distance anywhere on disk is 0.9904 km,
so the rule has a 990 m gap under it and is not threshold-sensitive.

The rejected alternatives are pinned below: an event-count rule ("no splices
found") misses F631, which carries a stray 1F at 117.1 km after its 0 km end
marker, and a "<= 2 events" rule sweeps in 103 ordinary short/broken fibers.

LANHOW631_1550.sor is F631's OTHER direction — 12 events, a healthy -56.962 dB
launch, EOF at 117.2789 km.  It is here to hold the two halves of the point
together: the fiber is fine, the shot is not.
"""

import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'endlaunch')

sys.path.insert(0, os.path.join(_REPO, 'splicereport'))

import sor_reader324802a as R              # noqa: E402
import splicereportmatchexfo as E          # noqa: E402

DEAD = ['HOWLAN631_1550.sor', 'HOWLAN309_1550.sor', 'LAGDUR0036.sor']
ALIVE = 'LANHOW631_1550.sor'


def _rec(name):
    return R.parse_sor_full(os.path.join(_FIX, name), trim=False)


# ── the raw bytes, so the fixtures cannot silently drift ────────────────────

@pytest.mark.parametrize('name,refl,n', [('HOWLAN631_1550.sor', -28.836, 2),
                                         ('HOWLAN309_1550.sor', -28.695, 1),
                                         ('LAGDUR0036.sor',     -49.828, 1)])
def test_fixture_is_a_shot_that_ended_at_zero(name, refl, n):
    ev = _rec(name)['events']
    assert len(ev) == n
    assert ev[0]['is_end'] is True
    assert ev[0]['dist_km'] == 0.0
    assert ev[0]['reflection'] == pytest.approx(refl, abs=1e-3)


def test_the_other_direction_of_f631_is_a_normal_trace():
    """The fiber is fine — only the A shot is dead."""
    ev = _rec(ALIVE)['events']
    assert len(ev) == 12
    assert ev[0]['is_end'] is False
    assert ev[0]['reflection'] == pytest.approx(-56.962, abs=1e-3)
    assert ev[-1]['is_end'] is True
    assert ev[-1]['dist_km'] == pytest.approx(117.2789, abs=1e-3)


# ── the behaviour under test (fails on pristine main) ───────────────────────

@pytest.mark.parametrize('name', DEAD)
def test_a_dead_shot_is_detected(name):
    assert E._is_dead_acquisition(_rec(name)) is True


@pytest.mark.parametrize('name', DEAD)
def test_a_dead_shot_is_reported_as_a_reshoot(name):
    """The finding reaches the report as a per-direction re-shoot tag."""
    rec = _rec(name)
    issues = E.detect_launch_issues({1: rec}, {1: _rec(ALIVE)})
    assert 1 in issues, f'{name}: a dead A-direction produced no finding at all'
    assert issues[1]['a_tags'] == ['RESHOOT_DEAD_TRACE']
    assert issues[1]['b_tags'] == []          # the live direction stays clean
    assert issues[1]['severity'] == 'HIGH'


@pytest.mark.parametrize('name', DEAD)
def test_the_finding_is_not_a_reflectance_finding(name):
    """#85 must stay closed: no reflectance tag may come back on these.

    The whole point of the new tag is that it replaces silence with an
    ACQUISITION verdict.  If a threshold move ever lets either reflectance
    rule (launch or tailbox, both of which now emit a bare ``REFL`` tag) fire
    here again, the report is back to blaming a connector that does not exist.
    """
    issues = E.detect_launch_issues({1: _rec(name)}, {1: _rec(ALIVE)})
    tags = issues.get(1, {}).get('a_tags', [])
    assert not [t for t in tags if 'REFL' in t], tags
    assert E._fiber_launch_info(_rec(name))[0] is None


# ── the rejected definitions, pinned ────────────────────────────────────────

def test_an_event_count_rule_would_miss_f631():
    """Why the rule is keyed on the EOF distance, not on "no splices found".

    F631 carries a stray 1F at 117.1 km AFTER its 0 km end marker, so it has a
    non-END event and a "no splices in this direction" rule does not select it.
    """
    ev = _rec('HOWLAN631_1550.sor')['events']
    non_end = [e for e in ev if not e.get('is_end')]
    assert len(non_end) == 1                     # it is NOT event-less
    assert non_end[0]['dist_km'] == pytest.approx(117.1056, abs=1e-3)
    assert E._is_dead_acquisition(_rec('HOWLAN631_1550.sor')) is True


def test_a_short_fiber_is_not_a_dead_shot():
    """A trace that ends early is a BREAK, not a failed acquisition.

    DURLAG F3's shape: a real launch connector at 0, a real end-of-fiber at
    13.9 km.  Two events — which is why the "<= 2 events" candidate was
    rejected; on disk it sweeps in 103 of these.
    """
    rec = {'events': [
        {'type': '1F9999LS', 'dist_km': 0.0, 'is_reflective': True,
         'is_end': False, 'reflection': -53.82, 'splice_loss': 0.405},
        {'type': '1E9999LS', 'dist_km': 13.9091, 'is_reflective': True,
         'is_end': True, 'reflection': -30.0, 'splice_loss': 0.0},
    ]}
    assert E._is_dead_acquisition(rec) is False


def test_the_threshold_is_not_on_a_cliff_edge():
    """0.9904 km is the next-smallest EOF anywhere on disk.

    Any value from 1 mm to 990 m selects the same three traces, so the
    constant is nominal rather than tuned.  This pins that it stays well under
    the nearest real EOF.
    """
    assert 0.0 < E.DEAD_TRACE_EOF_MAX_KM < 0.9904
    near_miss = {'events': [{'type': '1E9999LS', 'dist_km': 0.9904,
                             'is_reflective': True, 'is_end': True,
                             'reflection': -30.0, 'splice_loss': 0.0}]}
    assert E._is_dead_acquisition(near_miss) is False


# ── invariance: holds on pristine main AND on the branch ────────────────────

def test_a_healthy_trace_never_carries_a_reshoot_tag():
    issues = E.detect_launch_issues({1: _rec(ALIVE)}, {1: _rec(ALIVE)})
    tags = issues.get(1, {}).get('a_tags', []) + issues.get(1, {}).get('b_tags', [])
    assert not [t for t in tags if 'RESHOOT' in t], tags


def test_a_genuine_launch_connector_is_still_found():
    ev = [
        {'type': '1F9999LS', 'dist_km': 0.0, 'is_reflective': True,
         'is_end': False, 'reflection': -55.6, 'splice_loss': 0.101},
        {'type': '0F9999LS', 'dist_km': 12.5, 'is_reflective': False,
         'is_end': False, 'reflection': 0.0, 'splice_loss': 0.08},
        {'type': '1E9999LS', 'dist_km': 60.0, 'is_reflective': True,
         'is_end': True, 'reflection': -63.0, 'splice_loss': 0.0},
    ]
    launch_evt, end_km, n_events = E._fiber_launch_info({'events': ev})
    assert launch_evt is not None
    assert launch_evt['reflection'] == pytest.approx(-55.6)
    assert end_km == 60.0
    assert n_events == 3


def test_the_legend_documents_the_tag_without_adding_a_row():
    """The tag is explained, but no Legend row is inserted.

    A clean span must gain ZERO rows anywhere in the workbook.  Adding a
    thirteenth Legend entry would have shifted FIELD GAINER down by one on
    every span on disk, clean or not, so the explanation rides on the
    existing Orange / ILA-column row instead.
    """
    import inspect
    src = inspect.getsource(E.write_xlsx)
    body = src[src.index('legend_items = ['):]
    body = body[:body.index(']\n')]
    assert body.count('("Orange"') == 1, 'a second Orange legend row was added'
    assert body.count('", "') == 12 * 2, 'the legend row count changed'
    assert 'RESHOOT_DEAD_TRACE' in body
    assert 'must be shot again' in body
    assert 'NOT a reflectance finding' in body


def test_an_end_event_with_no_distance_is_not_a_dead_shot():
    """A missing distance is a parse failure, not a measured zero."""
    assert E._is_dead_acquisition(
        {'events': [{'type': '1E9999LS', 'dist_km': None, 'is_end': True,
                     'is_reflective': True, 'reflection': -30.0}]}) is False
    assert E._is_dead_acquisition({'events': []}) is False
    assert E._is_dead_acquisition(None) is False
