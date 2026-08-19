"""An end-of-fiber event is never a launch connector.

`_fiber_launch_info` takes `events[0]` as the fiber's launch connector when it
is reflective and sits under 0.5 km.  It never excluded END events — and `1E`
has always been reflective — so a trace whose first event is an end marker at
0 km was reported as having a launch connector with that marker's Fresnel.

These are DEAD acquisitions: the OTDR declared end-of-fiber before it saw any
fiber.  Their reflectance is an unmated open connector (~-28 dB), which then
gets ranked against the span's mated buried connectors at -50..-64 dB and
clears the -49.9 dB gate by 20 dB.  The result is a BAD_LAUNCH_REFL cell
pointing at a connector that does not exist, on a fiber whose real problem is
that the direction needs re-shooting.

Both fixtures are unmodified field acquisitions from HOW<->LAN:

  HOWLAN631_1550.sor  2 events, first `2E9999LS` @ 0.0000 km, refl -28.836
  HOWLAN309_1550.sor  1 event,  first `1E9999LS` @ 0.0000 km, refl -28.695

F631 became wrong only when saturated `2` codes started reading as reflective;
F309 has been wrong for as long as the function has existed.  Both are fixed by
the same condition, which is why they are pinned together.
"""

import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'endlaunch')

sys.path.insert(0, os.path.join(_REPO, 'splicereport'))

import sor_reader324802a as R              # noqa: E402
import splicereportmatchexfo as E          # noqa: E402


def _events(name):
    raw = open(os.path.join(_FIX, name), 'rb').read()
    return raw, R._parse_key_events(raw, R._parse_block_directory(raw))


# ── the raw bytes, so the fixtures cannot silently drift ────────────────────

def test_f631_first_event_is_a_saturated_end_marker_at_zero():
    raw, ev = _events('HOWLAN631_1550.sor')
    assert raw.count(b'2E9999LS') == 1
    assert len(ev) == 2
    e0 = ev[0]
    assert e0['type'].startswith('2E')
    assert e0['dist_km'] == 0.0
    assert e0['is_end'] is True
    # #82 made saturated codes reflective, which is correct and stays correct.
    assert e0['is_reflective'] is True
    assert e0['reflection'] == pytest.approx(-28.836, abs=1e-3)


def test_f309_is_a_one_event_trace_ending_at_zero():
    raw, ev = _events('HOWLAN309_1550.sor')
    assert raw.count(b'1E9999LS') == 1
    assert len(ev) == 1                      # nothing but the end marker
    e0 = ev[0]
    assert e0['type'].startswith('1E')
    assert e0['dist_km'] == 0.0
    assert e0['is_end'] is True
    assert e0['is_reflective'] is True       # `1E` always was
    assert e0['reflection'] == pytest.approx(-28.695, abs=1e-3)


# ── the behaviour under test ────────────────────────────────────────────────

@pytest.mark.parametrize('name', ['HOWLAN631_1550.sor', 'HOWLAN309_1550.sor'])
def test_end_event_is_not_taken_as_the_launch_connector(name):
    _, ev = _events(name)
    launch_evt, end_km, n_events = E._fiber_launch_info({'events': ev})
    assert launch_evt is None, (
        f'{name}: an end-of-fiber event was reported as the launch connector'
    )
    assert end_km == 0.0                     # the end IS still found
    assert n_events == len(ev)


@pytest.mark.parametrize('name,refl', [('HOWLAN631_1550.sor', -28.836),
                                       ('HOWLAN309_1550.sor', -28.695)])
def test_the_old_predicate_would_have_taken_it(name, refl):
    """Guard against a test that passes with or without the fix.

    This restates the pre-fix condition literally.  If it ever stops selecting
    these events, the fixtures no longer exercise the bug and the test above is
    vacuous.
    """
    _, ev = _events(name)
    old_predicate = (ev and ev[0].get('is_reflective')
                     and ev[0]['dist_km'] < 0.5)
    assert old_predicate, f'{name} no longer reproduces the pre-fix selection'
    assert ev[0]['reflection'] == pytest.approx(refl, abs=1e-3)
    # ...and that reflectance is what would have been printed as a bad launch.
    assert ev[0]['reflection'] > E.LAUNCH_BAD_REFL_DB


# ── a real launch connector must still be found ─────────────────────────────

def test_a_genuine_launch_connector_is_unaffected():
    """The guard must be narrow: only `is_end` is excluded, nothing else."""
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


def test_a_saturated_launch_connector_is_still_found():
    """#82's own case must survive: `2F` at the launch is a real connector.

    This is the WSC<->SUI F34 / SAN<->DUR F76 shape -- saturated, not an end
    event -- and it must keep flagging.

    Note the frame: `_fiber_launch_info` runs on the launch-NORMALISED event
    list, where the launch connector sits at ~0 and the OTDR port has been
    trimmed away.  In the raw file F34's connector is at 1.0044 km.
    """
    ev = [
        {'type': '2F9999LS', 'dist_km': 0.0, 'is_reflective': True,
         'is_end': False, 'reflection': -25.072, 'splice_loss': 0.37},
        {'type': '0F9999LS', 'dist_km': 26.4, 'is_reflective': False,
         'is_end': False, 'reflection': 0.0, 'splice_loss': 0.16},
        {'type': '1E9999LS', 'dist_km': 64.0, 'is_reflective': True,
         'is_end': True, 'reflection': -62.0, 'splice_loss': 0.0},
    ]
    launch_evt, _, _ = E._fiber_launch_info({'events': ev})
    assert launch_evt is not None
    assert launch_evt['reflection'] == pytest.approx(-25.072)
    assert launch_evt['reflection'] > E.LAUNCH_BAD_REFL_DB
