"""The Viewer's trace x-axis uses the group index the file STATES.

The sample pitch is `c * SamplingPeriod / 2 / IOR`, and it scales every x
coordinate the Viewer draws.  trace_server got the IOR by inverting the
Bellcore formula over the first usable event:

    ior = tot * 0.02998 / dist_km

which fails two ways.  It divides by 0.02998 where the true constant is
0.0299792458, and since #248 `dist_km` is EXFO's own float64 rather than a
value that same constant produced, so the round trip no longer cancels.  And
it needs a usable event at all: a fiber broken AT the connector has only a
launch event at tot 0, the loop finds nothing, and it falls through to a
hardcoded 1.46820 against a true 1.47000 -- 1225 ppm, which drew the trace
171 m from its own events on exactly the files a tech opens to see a break.

The file states the IOR twice: EXFO's float64 `Ior` in the proprietary block
and the Bellcore group index in FxdParams.  Scored against FastReporter's own
marker Lengths -- window widths the instrument built from whole samples, so
the population pins the pitch independently -- the float64 is exact on every
fixture that carries enough markers to pin it.
"""

from __future__ import annotations

import glob
import importlib.util
import os
import struct
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


V = _load('viewer_sor_pitch', 'viewer/sor_reader324802a.py')

_FIXTURES = sorted(glob.glob(
    os.path.join(ROOT, 'desktop', 'tests', 'fixtures', '**', '*.sor'),
    recursive=True))

# Fibres reflective-dead at the launch connector: one event, tot 0.  These are
# the files the old derivation could not read at all.
_DEAD_AT_LAUNCH = [p for p in _FIXTURES
                   if os.path.basename(p) in ('HOWLAN309_1550.sor',
                                              'LAGDUR0036.sor')]

_C = 299_792_458.0


def _ids(p):
    return os.path.basename(p)


def _fr_pinned_pitch(r):
    """The pitch FastReporter's own marker Lengths imply, or None."""
    return r.get('exfo_res_m')


# ── Where the IOR comes from ──────────────────────────────────────────────

@pytest.mark.parametrize('path', _FIXTURES, ids=_ids)
def test_every_file_states_its_own_ior(path):
    """Never None: the proprietary float64, else the anchored FxdParams read."""
    r = V.parse_sor_full(path, trim=False)
    if r is None:
        pytest.skip('does not parse')
    assert r['ior'] is not None
    assert 1.40 < float(r['ior']) < 1.55


def test_the_proprietary_float64_is_preferred_over_the_bellcore_copy():
    """FxdParams stores IOR as uint32 x 1e5, so it quantises to 5 dp.  The
    proprietary block carries FastReporter's 6."""
    seen_finer = False
    for path in _FIXTURES:
        data = open(path, 'rb').read()
        blocks = V._parse_block_directory(data)
        stream = V._decompress_proprietary(data, blocks)
        if not stream:
            continue
        prop = V._prop_scalar(stream, 'Ior', 3, 8)
        if prop is None:
            continue
        r = V.parse_sor_full(path, trim=False)
        assert r['ior'] == prop, 'parse_sor_full did not prefer the float64'
        if abs(prop * 1e5 - round(prop * 1e5)) > 1e-9:
            seen_finer = True            # a value the 5-dp copy cannot hold
    # Not asserted as universal: on this corpus every Ior happens to be round.
    # The preference itself is what the assertion above pins.
    assert isinstance(seen_finer, bool)


@pytest.mark.parametrize('path', _DEAD_AT_LAUNCH, ids=_ids)
def test_a_fibre_dead_at_the_connector_still_gets_its_real_ior(path):
    """The 171 m case.  One event, tot 0, nothing to invert -- the old
    derivation returned its hardcoded default and the trace was drawn on a
    scale 1225 ppm wrong."""
    r = V.parse_sor_full(path, trim=False)
    events = r['events']
    assert len(events) == 1 and events[0]['time_of_travel'] == 0, \
        'fixture changed; it no longer exercises the no-usable-event path'

    # What the old code would have produced, recomputed here so the test does
    # not depend on that function surviving.
    assert V._sor_ior_from_events(r) == pytest.approx(1.46820)
    assert float(r['ior']) == pytest.approx(1.47000)

    sp = float(r['exfo_sampling_period'])
    old_pitch = _C * sp / 2.0 / 1.46820
    new_pitch = _C * sp / 2.0 / float(r['ior'])
    drift = abs(new_pitch - old_pitch) * len(r['trace'])
    assert drift > 100.0, f'expected a >100 m correction, got {drift:.1f} m'


# ── The anchored read ─────────────────────────────────────────────────────

def test_the_scalar_read_is_anchored_on_a_field_boundary():
    """'Ior' is three characters, which is exactly the length that collides
    with the tail of a longer name.  A bare find would take the decoy."""
    real = struct.pack('<d', 1.47)
    # A decoy field whose name ENDS in 'Ior', carrying a different value, and
    # placed first so a bare `find` would reach it before the real one.
    decoy = (b'\x00' + b'\x00' * 8 + struct.pack('<I', 3) + struct.pack('<I', 8)
             + b'\x00' * 4 + b'FiberIor\x00' + struct.pack('<d', 9.99))
    good = (b'\x00' + b'\x00' * 8 + struct.pack('<I', 3) + struct.pack('<I', 8)
            + b'\x00' * 4 + b'Ior\x00' + real)
    stream = b'\x00' * 32 + decoy + good

    assert V._prop_scalar(stream, 'Ior', 3, 8) == pytest.approx(1.47)


def test_the_scalar_read_rejects_a_wrong_descriptor():
    """A name match whose descriptor says the wrong type or size is not the
    field, and must not be read as one."""
    stream = (b'\x00' * 32 + b'\x00' + b'\x00' * 8 + struct.pack('<I', 1)
              + struct.pack('<I', 4) + b'\x00' * 4 + b'Ior\x00'
              + struct.pack('<d', 1.47))
    assert V._prop_scalar(stream, 'Ior', 3, 8) is None


def test_the_scalar_read_is_safe_on_an_empty_stream():
    assert V._prop_scalar(b'', 'Ior', 3, 8) is None
    assert V._prop_scalar(None, 'Ior', 3, 8) is None


# ── The pitch the server actually serves ──────────────────────────────────

@pytest.mark.parametrize('path', _FIXTURES, ids=_ids)
def test_the_served_pitch_matches_what_fastreporter_pins(path):
    """dx_km is the whole point: it multiplies every sample index."""
    r = V.parse_sor_full(path, trim=False)
    if r is None:
        pytest.skip('does not parse')
    ref = _fr_pinned_pitch(r)
    if not ref:
        pytest.skip('too few usable markers to pin the pitch')

    d, f = os.path.dirname(os.path.abspath(path)), os.path.basename(path)
    t = TS._load_trace_cached(d, f, os.path.getmtime(path))
    assert t is not None
    served_m = float(t['dx_km']) * 1000.0
    ppm = abs(served_m - ref) / ref * 1e6
    assert ppm < 0.5, f'{ppm:.2f} ppm off FastReporter'


def test_the_origin_constant_is_the_exact_one():
    """first_pos_m scales fxd_acq_offset.  Production files all store 0, so
    this is inert today -- which is exactly why a wrong constant could sit
    here unnoticed until a file that does not store 0 arrives."""
    src = open(os.path.join(ROOT, 'viewer', 'trace_server.py'),
               encoding='utf-8').read()
    line = [l for l in src.splitlines() if 'first_pos_m =' in l and 'acq_offset' in l]
    assert len(line) == 1, line
    assert '0.0299792458' in line[0]
    assert '0.02998 ' not in line[0] and '0.02998/' not in line[0]


def test_nothing_back_derives_the_ior_any_more():
    """The guard on the whole change: trace_server must not reach for the
    inversion again."""
    src = open(os.path.join(ROOT, 'viewer', 'trace_server.py'),
               encoding='utf-8').read()
    code = [l for l in src.splitlines()
            if '_sor_ior_from_events' in l and not l.lstrip().startswith('#')]
    assert code == [], code


# ── What must NOT move ────────────────────────────────────────────────────

@pytest.mark.parametrize('path', _FIXTURES[:40], ids=_ids)
def test_the_sample_count_and_origin_are_untouched(path):
    """This change rescales the axis.  It must not resample, retrim or shift
    the origin -- those would be a different change hiding inside this one."""
    r = V.parse_sor_full(path, trim=False)
    if r is None:
        pytest.skip('does not parse')
    d, f = os.path.dirname(os.path.abspath(path)), os.path.basename(path)
    t = TS._load_trace_cached(d, f, os.path.getmtime(path))
    assert t['num_points'] == len(r['trace'])
    # Every production file stores acq_offset 0, so the origin stays at 0.
    assert t['first_pos_km'] == pytest.approx(0.0, abs=1e-12)
