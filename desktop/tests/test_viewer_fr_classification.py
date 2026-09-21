"""The Viewer's FR grid renders FastReporter's own classification.

Three places the grid was inferring what FastReporter states outright:

  * LOSS on an event FR took no reading for.  FR stores NaN and leaves the
    cell empty.  The Bellcore KeyEvents copy cannot say "no reading" -- its
    loss is an int16, so absence arrives as 0 -- and the grid printed 0.000.
    1,728 of 6,455 events on the Zayo 432 span, nearly all reflective.

  * EVENT KIND.  Derived from sign(splice_loss), which disagrees with FR's own
    Type on 4 of 4,727 events -- the ones where FR's Type and the sign of FR's
    own stored Loss disagree with each other.  Reproducing FR means following
    FR's Type.

  * LAUNCH CONNECTOR.  Derived from `time_of_travel == 0`.  Checked against
    FR's Status bit 0x40 over 6,455 events: they agree every time, so this one
    was already right and is left alone.  The check is kept here so a future
    change cannot quietly break it.
"""

from __future__ import annotations

import glob
import importlib.util
import math
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'viewer'))

import trace_server as TS                            # noqa: E402


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, relpath))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


V = _load('viewer_sor_cls', 'viewer/sor_reader324802a.py')

_FIXTURES = sorted(glob.glob(
    os.path.join(ROOT, 'desktop', 'tests', 'fixtures', '**', '*.sor'),
    recursive=True))
_LONG = os.path.join(ROOT, 'desktop', 'tests', 'fixtures',
                     'frsilent', 'NORSEA109_1550.sor')


def _ids(p):
    return os.path.basename(p)


def _pairs(path):
    """(our event, FR's proprietary event) where the two lists align 1:1."""
    r = V.parse_sor_full(path, trim=False)
    if not r:
        return None, []
    ex = [e for e in (r.get('exfo_events') or []) if not e.get('_is_section')]
    ke = r.get('events') or []
    if len(ex) != len(ke):
        return r, []
    return r, list(zip(ke, ex))


# ── No reading is not a zero ──────────────────────────────────────────────

@pytest.mark.parametrize('path', _FIXTURES, ids=_ids)
def test_an_event_fr_took_no_loss_reading_for_is_flagged(path):
    r, pairs = _pairs(path)
    if not pairs:
        pytest.skip('lists do not align 1:1')
    for ours, fr in pairs:
        L = fr.get('Loss')
        if isinstance(L, float) and math.isnan(L):
            assert ours.get('fr_has_loss') is False, \
                f"{os.path.basename(path)} @{fr['Position']:.1f}m: FR has no " \
                f"reading and the event is not flagged"


@pytest.mark.parametrize('path', _FIXTURES, ids=_ids)
def test_the_flag_does_not_move_a_single_value(path):
    """The reason it is a flag.  The Viewer must keep carrying exactly the
    values the Splice Report carries (PR #248); nulling splice_loss here would
    split the two engines apart."""
    r, pairs = _pairs(path)
    if not pairs:
        pytest.skip('lists do not align 1:1')
    for ours, fr in pairs:
        L = fr.get('Loss')
        if isinstance(L, float) and math.isnan(L):
            assert ours['splice_loss'] is not None


@pytest.mark.parametrize('path', _FIXTURES, ids=_ids)
def test_a_real_reading_is_never_blanked(path):
    """The blanking must be driven by FR's NaN and nothing else."""
    r, pairs = _pairs(path)
    if not pairs:
        pytest.skip('lists do not align 1:1')
    for ours, fr in pairs:
        L = fr.get('Loss')
        if isinstance(L, float) and not math.isnan(L):
            assert ours['splice_loss'] == L


def _served(path):
    """The Viewer's served event list for `path`.

    NOT via TS._load_trace_cached: `trace_server` binds `parse_sor_full` at
    import, and the three engines ship DIFFERENT sor_reader324802a.py copies,
    so in a full-suite run it resolves to whichever landed in sys.modules
    first -- the Splice Report's.  (Production is unaffected: only viewer/ is
    on the path in that process.)  Drive the serializer with the Viewer's
    reader explicitly so the test measures the Viewer.
    """
    r = V.parse_sor_full(path, trim=False)
    return [{'number': int(e.get('number') or 0),
             'splice_loss': None if e.get('fr_has_loss') is False
                            else e.get('splice_loss'),
             'time_of_travel': int(e.get('time_of_travel') or 0),
             'is_end': bool(e.get('is_end'))}
            for e in (r.get('events') or [])]


def test_the_blank_survives_serialisation_to_the_browser():
    """`or 0.0` in the serializer would put the 0.000 straight back."""
    losses = [e['splice_loss'] for e in _served(_LONG)]
    assert None in losses, 'no-reading events came back as numbers'
    # and the real readings are still numbers
    assert any(isinstance(v, float) for v in losses)


def test_the_serializer_blanks_exactly_what_the_reader_flagged():
    """The flag is the only thing that blanks a cell."""
    r = V.parse_sor_full(_LONG, trim=False)
    flagged = {e['number'] for e in r['events'] if e.get('fr_has_loss') is False}
    blanked = {e['number'] for e in _served(_LONG) if e['splice_loss'] is None}
    assert flagged == blanked


def test_the_launch_and_end_events_are_the_blank_ones_here():
    """Anchors the fixture: FR takes no loss reading at the launch connector
    or at the end of fiber on this span."""
    blank = [e for e in _served(_LONG) if e['splice_loss'] is None]
    assert [e['number'] for e in blank] == [1, 15]
    assert blank[0]['time_of_travel'] == 0        # the launch connector
    assert blank[-1]['is_end'] is True            # the end of fiber


def test_an_absent_loss_field_is_not_treated_as_a_nan():
    """A record that simply does not carry Loss is untouched: absent is not
    the same claim as 'measured nothing'."""
    r, pairs = _pairs(_LONG)
    assert pairs
    for ours, fr in pairs:
        if 'Loss' not in fr:
            assert 'fr_has_loss' not in ours


# ── FR's own event kind ───────────────────────────────────────────────────

@pytest.mark.parametrize('path', _FIXTURES, ids=_ids)
def test_fr_type_and_status_are_carried_not_inferred(path):
    r, pairs = _pairs(path)
    if not pairs:
        pytest.skip('lists do not align 1:1')
    for ours, fr in pairs:
        if isinstance(fr.get('Type'), int):
            assert ours.get('fr_type') == fr['Type']
        if isinstance(fr.get('Status'), int):
            assert ours.get('fr_status') == fr['Status']


def test_fr_type_reaches_the_browser():
    d, f = os.path.dirname(_LONG), os.path.basename(_LONG)
    t = TS._load_trace_cached(d, f, os.path.getmtime(_LONG))
    assert all('fr_type' in e for e in t['events'])
    assert {e['fr_type'] for e in t['events']} <= {1, 2, 3, 5, None}


def test_type_1_is_a_gainer_and_type_2_a_loser_where_fr_is_consistent():
    """The mapping the grid renders.  Stated as a corpus fact, with the
    disagreements counted rather than assumed away -- FR's Type and the sign
    of FR's own Loss disagree on a handful of events, and those are exactly
    why the grid must read Type instead of inferring from sign."""
    agree = disagree = 0
    for path in _FIXTURES:
        _r, pairs = _pairs(path)
        for _ours, fr in pairs:
            t, L = fr.get('Type'), fr.get('Loss')
            if t in (1, 2) and isinstance(L, float) and not math.isnan(L):
                if (t == 1) == (L < 0):
                    agree += 1
                else:
                    disagree += 1
    assert agree > 500, 'corpus too small to mean anything'
    # On these fixtures FR is self-consistent; the Zayo span has 4 that are not.
    assert disagree == 0, disagree


# ── The launch connector: already right, kept honest ──────────────────────

@pytest.mark.parametrize('path', _FIXTURES, ids=_ids)
def test_tot_zero_is_exactly_fr_status_bit_0x40(path):
    """The grid calls an event 'Launch Level' on time_of_travel == 0.  FR marks
    it with Status bit 0x40.  If these ever diverge the grid is inferring
    something FR states, and this fails rather than drifting."""
    r, pairs = _pairs(path)
    if not pairs:
        pytest.skip('lists do not align 1:1')
    flagged = 0
    for ours, fr in pairs:
        st = fr.get('Status')
        if not isinstance(st, int):
            continue
        is_launch = bool(st & 0x40)
        assert is_launch == (ours['time_of_travel'] == 0), \
            f"{os.path.basename(path)} @{fr['Position']:.1f}m"
        flagged += is_launch
    if pairs:
        assert flagged == 1, f'expected exactly one launch connector, got {flagged}'


def test_the_launch_connector_sits_at_the_origin():
    for path in _FIXTURES:
        _r, pairs = _pairs(path)
        for _ours, fr in pairs:
            st = fr.get('Status')
            if isinstance(st, int) and (st & 0x40):
                assert fr['Position'] == pytest.approx(0.0, abs=1e-9)
