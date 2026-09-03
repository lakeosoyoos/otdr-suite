"""Writing a declared span start/end into a .sor, the way FastReporter does.

The ground truth is a real pair in fixtures/frspan: a Defuniak fiber as the
instrument saved it, and the same file after FastReporter 3 had its span set
to events 3 (the launch reel's far end, 1036.03 m) and 4 (end of fiber).
`set_span` must reproduce FR's file, and the test says exactly where it may
differ: the two ORL numbers FR re-integrates from the trace, which we leave
alone rather than fake.
"""
import os
import struct
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'viewer'))

import trace_server as TS                       # noqa: E402
from test_sor_writer import make_sor            # noqa: E402

FX = os.path.join(HERE, 'fixtures', 'frspan')


def _orig():
    return open(os.path.join(FX, 'DNN1DNN20001.sor'), 'rb').read()


def _fr():
    return open(os.path.join(FX, 'DNN1DNN20001_fr_span_e3_e4.sor'), 'rb').read()


def _prop_values(data):
    _, bl = TS.split(data)
    pb = next(b for b in bl if b.name.startswith(b'ExfoNewProprietaryBlock'))
    _, chunks, _ = TS._prop_chunks(pb.body)
    st = b''.join(d for _, d in chunks)
    return [(r['name'], r['tc'], v) for r, v in TS._prop_typed(st)]


def _same(x, y):
    if isinstance(x, float) and isinstance(y, float):
        # FR recomputes a section Length and lands 7e-15 off; that is noise,
        # not a difference in what the file says
        return (x != x and y != y) or x == y or abs(x - y) <= 1e-9 * max(1.0, abs(x))
    return x == y


# ── the replay ───────────────────────────────────────────────────────────

def test_the_fixtures_are_the_same_shot():
    a, b = TS.read_identifiers(_orig()), TS.read_identifiers(_fr())
    assert a['cable_id'] == b['cable_id'] == 'DNN1DNN2' and a['fiber_id'] == b['fiber_id']
    assert TS.roundtrip_ok(_orig()) and TS.roundtrip_ok(_fr())


def test_set_span_reproduces_fastreporters_file_except_orl():
    out = TS.set_span(_orig(), start_km=1.0361, end_km=2.0415)
    assert TS.roundtrip_ok(out)
    _, ours = TS.split(out)
    _, frs = TS.split(_fr())
    assert [b.name for b in ours] == [b.name for b in frs]
    for a, b in zip(ours, frs):
        if a.name in (b'GenParams', b'SupParams', b'FxdParams', b'DataPts', b'ExfoAdditionalInfo'):
            assert a.body == b.body, a.name
    # KeyEvents: every event identical; the summary identical except ORL
    ea, sa = TS._kev_parse(TS._find(ours, b'KeyEvents').body)
    eb, sb = TS._kev_parse(TS._find(frs, b'KeyEvents').body)
    assert ea == eb
    assert sa[:3] == sb[:3] and sa[4:] == sb[4:]
    assert sa[3] != sb[3], 'FR re-integrated ORL; we deliberately did not'
    # proprietary: every one of the 573 records identical except TotalOrl
    diffs = [(x, y) for x, y in zip(_prop_values(out), _prop_values(_fr())) if not _same(x[2], y[2])]
    assert [x[0] for x, y in diffs] == ['TotalOrl']


def test_what_a_span_edit_touches_in_the_bellcore_blocks():
    out = TS.set_span(_orig(), start_km=1.0361, end_km=2.0415)
    _, bl = TS.split(out)
    gp = TS._find(bl, b'GenParams').body
    go = TS.genparams_offsets(gp)
    assert struct.unpack_from('<i', gp, go['user_offset'])[0] == 50801
    assert struct.unpack_from('<i', gp, go['user_offset_dist'])[0] == 10360   # 1036.0 m
    evs, summary = TS._kev_parse(TS._find(bl, b'KeyEvents').body)
    assert [e['tot'] for e in evs] == [-1528, 0, 49297]      # port dropped, re-based
    assert [e['num'] for e in evs] == [1, 2, 3]
    assert [e['code'][1:2] for e in evs] == [b'F', b'F', b'E']
    assert evs[0]['slope'] == 0
    assert summary[0] == 802                                  # 0.802 dB over the span


def test_what_a_span_edit_touches_in_the_proprietary_block():
    vals = dict((n, v) for n, t, v in _prop_values(TS.set_span(_orig(), start_km=1.0361, end_km=2.0415))
                if n in ('SpansLength', 'SpansLoss', 'IncludeSpanStart', 'IncludeSpanEnd', 'ReflectiveEndOfFiber'))
    assert abs(vals['SpansLength'] - 1005.3626753720237) < 1e-9
    assert abs(vals['SpansLoss'] - 0.8020973644519824) < 1e-9
    assert vals['IncludeSpanStart'] == 1 and vals['IncludeSpanEnd'] == 1
    assert vals['ReflectiveEndOfFiber'] == 0
    statuses = [v for n, t, v in _prop_values(TS.set_span(_orig(), start_km=1.0361, end_km=2.0415)) if n == 'Status']
    assert statuses == [8, 0, 64, 132]                        # bit 64 = start, 128 = end


def test_the_samples_never_move():
    out = TS.set_span(_orig(), start_km=1.0361, end_km=2.0415)
    _, a = TS.split(_orig()); _, b = TS.split(out)
    assert TS._find(a, b'DataPts').body == TS._find(b, b'DataPts').body
    assert TS._find(a, b'FxdParams').body == TS._find(b, b'FxdParams').body


# ── the other shapes a declaration takes ─────────────────────────────────

def test_end_only_moves_nothing_but_the_end():
    """The B direction's case: an end declared, no start.  No re-basing."""
    out = TS.set_span(_orig(), end_km=1.0361)
    _, bl = TS.split(out)
    gp = TS._find(bl, b'GenParams').body
    assert struct.unpack_from('<i', gp, TS.genparams_offsets(gp)['user_offset'])[0] == 0
    evs, summary = TS._kev_parse(TS._find(bl, b'KeyEvents').body)
    assert [e['tot'] for e in evs] == [0, 49273, 50801, 100098]     # untouched
    assert [e['code'][1:2] for e in evs] == [b'F', b'F', b'E', b'F']  # end moved to event 3
    assert summary[2] == 50801
    vals = dict((n, v) for n, t, v in _prop_values(out) if n in ('IncludeSpanStart', 'IncludeSpanEnd', 'SpansLength', 'SpansLoss'))
    assert vals['IncludeSpanStart'] == 0 and vals['IncludeSpanEnd'] == 1
    assert abs(vals['SpansLength'] - 1036.0334067522322) < 1e-9
    # FR 3's Summary on this very file: Span length 1.0360 km, Span loss
    # 0.471 dB (0.187 section + -0.333 event 2 + 0.616 event 3, the END event
    # included).  Read off its screen, not assumed.
    assert abs(vals['SpansLoss'] - 0.4706) < 0.0005
    assert summary[0] == 471
    assert [v for n, t, v in _prop_values(out) if n == 'Status'] == [72, 0, 128, 4]


def test_the_written_span_reads_back():
    out = TS.set_span(_orig(), start_km=1.0361, end_km=2.0415)
    s = TS.read_span(out)
    assert s['start_km'] == 0.0
    assert abs(s['offset_km'] - 1.0361) < 0.0005
    assert abs(s['end_km'] - 1.0054) < 0.0005
    assert TS.read_span(_orig())['start_km'] is None


def test_a_km_that_is_not_on_an_event_is_refused():
    with pytest.raises(ValueError, match='from the nearest event'):
        TS.set_span(_orig(), start_km=0.5)


def test_an_end_before_the_start_is_refused():
    with pytest.raises(ValueError, match='after span start'):
        TS.set_span(_orig(), start_km=1.0361, end_km=1.0049)


def test_nothing_to_change_is_refused():
    with pytest.raises(ValueError, match='nothing'):
        TS.set_span(_orig())


def test_a_second_declaration_speaks_the_raw_frame():
    """Declaring again on a file that already carries a span: the km is still
    the raw-frame km, and the stored offset accumulates, so the tech's store
    (raw frame, always) can be replayed onto either file."""
    once = TS.set_span(_orig(), start_km=1.0049)              # start at event 2
    twice = TS.set_span(once, start_km=1.0361)                # then move it to event 3
    direct = TS.set_span(_orig(), start_km=1.0361)
    _, a = TS.split(twice); _, b = TS.split(direct)
    ga, gb = TS._find(a, b'GenParams').body, TS._find(b, b'GenParams').body
    assert ga == gb
    ea, _ = TS._kev_parse(TS._find(a, b'KeyEvents').body)
    eb, _ = TS._kev_parse(TS._find(b, b'KeyEvents').body)
    assert [e['tot'] for e in ea] == [e['tot'] for e in eb]


def test_a_file_without_events_is_refused():
    with pytest.raises(ValueError, match='no events'):
        TS.set_span(make_sor(), start_km=0.0)
