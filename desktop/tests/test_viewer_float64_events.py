"""The Viewer's event values come from EXFO's proprietary block, not KeyEvents.

The Viewer's event table sits beside the Splice Report's, and a tech reads
both.  They disagreed: the Splice Report upgraded its events to EXFO's float64
(PR #247) while the Viewer kept reading the Bellcore KeyEvents block, whose
loss and reflectance are int16 (1 mdB quantum) and whose position is an integer
time-of-travel scaled by 0.02998 m/unit -- 25.6 ppm long against the true
0.0299792458, which is 2.8 m at 110 km.

The upgrade could not even fire in the Viewer, because its copy of
`_parse_proprietary_block` still carried two extraction bugs the Splice
Report's copy had been fixed for: an 80 KB scan window that stopped short of
the records, and a plausibility filter reading Position as kilometres when the
block stores metres.  Together they left `exfo_events` holding a single record
on a real file.

These tests pin all of it: the extraction, the units, the upgrade, and the
agreement with the Splice Report engine that is the whole point.
"""

from __future__ import annotations

import glob
import importlib.util
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


def _load(name, relpath):
    """The three engines ship DIFFERENT sor_reader324802a.py copies, so a plain
    import hands back whichever landed in sys.modules first.  Load each by path
    under its own name."""
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, relpath))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


VIEWER = _load('viewer_sor_float64', 'viewer/sor_reader324802a.py')
ENGINE = _load('engine_sor_float64', 'splicereport/sor_reader324802a.py')

_FIXTURES = sorted(glob.glob(
    os.path.join(ROOT, 'desktop', 'tests', 'fixtures', '**', '*.sor'),
    recursive=True))

# A long span, so the 25 ppm position error is metres rather than centimetres.
_LONG = os.path.join(ROOT, 'desktop', 'tests', 'fixtures',
                     'frsilent', 'NORSEA109_1550.sor')


def _ids(p):
    return os.path.basename(p)


# ── Extraction ────────────────────────────────────────────────────────────

def test_the_block_yields_every_event_not_just_the_one_at_zero():
    """The 80 KB window + kilometre filter left exactly one record: the launch
    event at Position 0.0, the only one inside 500 m."""
    data = open(_LONG, 'rb').read()
    prop = VIEWER._parse_proprietary_block(
        data, VIEWER._parse_block_directory(data))
    events = [e for e in prop['exfo_events'] if not e.get('_is_section')]
    assert len(events) > 1, 'back to one record: the scan or the filter regressed'
    assert len(events) == 15
    # The far events are the ones both bugs discarded.
    assert max(e['Position'] for e in events) > 100_000.0


def test_position_is_metres():
    """The unit that caused the drop.  A 110 km fixture must carry positions in
    the 100_000s, not the 100s."""
    data = open(_LONG, 'rb').read()
    prop = VIEWER._parse_proprietary_block(
        data, VIEWER._parse_block_directory(data))
    far = max(e['Position'] for e in prop['exfo_events'])
    assert 100_000.0 < far < 120_000.0, far


def test_sections_are_tagged_by_curvelevel_not_field_order():
    """A record is a section iff the truck wrote no CurveLevel for it.  The old
    test was 'Loss arrived before Type', which mis-tags any event whose Type
    the scan has not reached yet."""
    data = open(_LONG, 'rb').read()
    prop = VIEWER._parse_proprietary_block(
        data, VIEWER._parse_block_directory(data))
    for e in prop['exfo_events']:
        assert e['_is_section'] == ('CurveLevel' not in e)
    assert any(e['_is_section'] for e in prop['exfo_events'])
    assert any(not e['_is_section'] for e in prop['exfo_events'])


def test_skipping_the_rawsamples_payload_changes_nothing():
    """The scan steps over RawSamples' sized binary payload instead of running
    the regex across it.  That is a speed measure only -- it must not change a
    single extracted record."""
    data = open(_LONG, 'rb').read()
    blocks = VIEWER._parse_block_directory(data)
    stream = VIEWER._decompress_proprietary(data, blocks)
    ri = stream.find(b'RawSamples\x00')
    assert ri >= 16, 'fixture has no RawSamples; this test would prove nothing'

    import struct
    dsz = struct.unpack_from('<I', stream, ri - 8)[0]
    vo = ri + len(b'RawSamples\x00')
    # The payload really is the bulk of the stream, and holds no field names.
    assert dsz / len(stream) > 0.5
    assert not any(VIEWER._PROP_NAME_RE.finditer(stream, vo, vo + dsz))


@pytest.mark.parametrize('path', _FIXTURES, ids=_ids)
def test_viewer_extracts_what_the_engine_extracts(path):
    """Same bytes, same records.  The Viewer keeps its own faster scanner, so
    this is the guard that the two implementations stay equivalent."""
    data = open(path, 'rb').read()
    pv = VIEWER._parse_proprietary_block(data, VIEWER._parse_block_directory(data))
    pe = ENGINE._parse_proprietary_block(data, ENGINE._parse_block_directory(data))
    assert (pv is None) == (pe is None)
    if pv is None:
        return
    ev, ee = pv['exfo_events'], pe['exfo_events']
    assert len(ev) == len(ee)
    for a, b in zip(ev, ee):
        assert set(a) == set(b)
        for k in a:
            if isinstance(a[k], float) and a[k] != a[k]:
                assert b[k] != b[k]          # NaN both sides
            else:
                assert a[k] == b[k], (path, k, a[k], b[k])


# ── The upgrade ───────────────────────────────────────────────────────────

def test_loss_carries_full_precision_and_says_so():
    r = VIEWER.parse_sor_full(_LONG, trim=False)
    upgraded = [e for e in r['events'] if e.get('loss_full_precision')]
    assert upgraded, 'no event was upgraded'
    # int16 millibels can only land on a 1 mdB grid; a float64 read will not.
    assert any(abs(e['splice_loss'] * 1000.0
                   - round(e['splice_loss'] * 1000.0)) > 1e-6
               for e in upgraded)


def test_position_sheds_the_25_ppm_error():
    """KeyEvents positions are tot * 0.02998 / ior.  The true constant is
    0.0299792458, so every stored distance reads 25.6 ppm long -- a fixed
    fractional error, which is what this asserts rather than any one gap."""
    data = open(_LONG, 'rb').read()
    raw = VIEWER._parse_key_events(data, VIEWER._parse_block_directory(data))
    up = VIEWER.parse_sor_full(_LONG, trim=False)['events']
    assert len(raw) == len(up)

    ppm = [((a['dist_km'] - b['dist_km']) / b['dist_km']) * 1e6
           for a, b in zip(raw, up) if b['dist_km'] > 5.0]
    assert len(ppm) >= 8
    # Every event carries the same fractional error, within the 0.1 m rounding.
    assert all(20.0 < p < 32.0 for p in ppm), ppm
    # And it is metres by the far end, not a rounding curiosity.
    assert abs(raw[-1]['dist_km'] - up[-1]['dist_km']) * 1000.0 > 2.0


def test_reflectance_is_upgraded_only_where_fr_recorded_one():
    """A NaN Reflectance means 'not a reflective event'.  Writing 0.0 over the
    KeyEvents value would be indistinguishable from a real reading."""
    for path in _FIXTURES:
        r = VIEWER.parse_sor_full(path, trim=False)
        if not r:
            continue
        for e in r['events'] or []:
            if not e.get('is_reflective'):
                continue
            assert e['reflection'] == e['reflection']     # never NaN


def test_a_misaligned_block_leaves_the_quantized_values_alone(monkeypatch):
    """The guard: 1:1 alignment AND each pair within 10 m.  A block that does
    not line up must be ignored, not half-applied."""
    before = VIEWER.parse_sor_full(_LONG, trim=False)['events']

    real = VIEWER._parse_proprietary_block

    def shifted(data, blocks):
        prop = real(data, blocks)
        if prop:
            for e in prop['exfo_events']:
                if isinstance(e.get('Position'), float):
                    e['Position'] = e['Position'] + 50.0      # > the 10 m guard
        return prop

    monkeypatch.setattr(VIEWER, '_parse_proprietary_block', shifted)
    after = VIEWER.parse_sor_full(_LONG, trim=False)['events']

    assert len(before) == len(after)
    for a, b in zip(before, after):
        assert not b.get('loss_full_precision')
    # And the positions fell back to the tot-derived ones, not the shifted block.
    assert any(abs(a['dist_km'] - b['dist_km']) > 1e-9
               for a, b in zip(before, after))


# ── The point of the exercise ─────────────────────────────────────────────

@pytest.mark.parametrize('path', _FIXTURES, ids=_ids)
def test_the_viewer_prints_what_the_splice_report_prints(path):
    """Two pages of one app, one file, one set of numbers."""
    rv = VIEWER.parse_sor_full(path, trim=False)
    re_ = ENGINE.parse_sor_full(path, trim=False)
    if not rv or not re_:
        pytest.skip('fixture does not parse in both engines')
    a, b = rv.get('events') or [], re_.get('events') or []
    assert len(a) == len(b)
    for x, y in zip(a, b):
        for f in ('dist_km', 'splice_loss', 'slope', 'reflection',
                  'type', 'is_reflective', 'is_end'):
            u, w = x.get(f), y.get(f)
            if isinstance(u, float) and u != u:
                assert isinstance(w, float) and w != w
            else:
                assert u == w, (os.path.basename(path), f, u, w)
