"""
.trc ingestion: an EXFO FTB unit's multi-wavelength trace file.

A .trc is ONE direction shot at several wavelengths, in the same EXFO
container as the .sor proprietary block and the .bdr.  parse_trc cuts each
wavelength's Trace subtree out into a one-trace stream and reads it with the
same code that reads a .sor's block, then rebuilds the Bellcore blocks a .sor
would carry (KeyEvents, DataPts, GenParams, FxdParams) from the tree.  These
tests pin:

  1. EXFO's own numbers.  TRCSPAN0001 is a 68 km, 3-wavelength shot whose
     original came with EXFO's exported event table; every event's position,
     loss, reflectance, reflectivity and the attenuation of the section into
     it must print the same at the precision EXFO printed.
  2. The .sor twin.  A .sor's proprietary block is the same tree with one
     trace, so the .trc path run on a real .sor's block must give what
     parse_sor_full gives for that .sor: identity, events, trace, trim,
     declared-span offset.  This is where every rebuilt-Bellcore rule was
     checked (3,493 .sor, 30,757 events on the spans it was built against).
  3. The declared span start, wavelength choice, and refusing a bad file.

The fixtures are real acquisitions with the customer, company, job, tech and
site strings replaced in place by same-length placeholders; the traces and
event records are the originals, byte for byte.
"""
import json
import os
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, 'fixtures')
TRC = os.path.join(FIX, 'trc')
sys.path.insert(0, os.path.join(os.path.dirname(HERE), '..', 'splicereport'))

import sor_reader324802a as sr                            # noqa: E402
# The .trc reader lives INSIDE sor_reader324802a, as the .bdr reader does: a
# new engine module would freeze fleet hot-updates until every tech
# reinstalls.

SPAN = os.path.join(TRC, 'TRCSPAN0001_131015501625.trc')     # no declared span
DECL = os.path.join(TRC, 'TRCDECL0001_155016251310.trc')     # span declared 1 km in
EXFO_TABLE = os.path.join(TRC, 'TRCSPAN0001_exfo_table.json')


# ── 1. Every fixture decodes, one record per wavelength ─────────────

@pytest.mark.parametrize('name', sorted(
    f for f in os.listdir(TRC) if f.lower().endswith('.trc')))
def test_every_fixture_trc_decodes(name):
    sides = sr.parse_trc(os.path.join(TRC, name))
    assert len(sides) == 3
    assert len({s['_trc_nominal_nm'] for s in sides}) == 3
    for s in sides:
        assert s['num_points'] > 1000
        assert s['exfo_raw'] is not None and s['exfo_res_m'] > 0
        assert sum(1 for e in s['events'] if e['is_end']) == 1
        assert s['events'][0]['dist_km'] == pytest.approx(0.0, abs=0.001)
        # Every key a .sor record has, so no caller needs a .trc branch.
        assert {'gen_loc_a', 'gen_loc_b', 'gen_fiber_id', 'events', 'trace',
                'acq_range', 'user_offset_km', 'exfo_events',
                'test_settings', 'exfo_calibration'} <= set(s)


def test_wavelengths_keep_file_order_and_exact_nm():
    """Traces come back in the file's own order; `wavelength` is the laser's
    measured wavelength to 0.1 nm, as a .sor's FxdParams stores it, and the
    nominal band is kept beside it for choosing."""
    sides = sr.parse_trc(SPAN)
    assert [s['_trc_nominal_nm'] for s in sides] == [1310, 1550, 1625]
    assert [s['wavelength'] for s in sides] == [1300.4, 1554.8, 1626.2]
    assert sr.parse_trc(DECL)[0]['_trc_nominal_nm'] == 1550


def test_wavelength_choice():
    assert sr.parse_trc_wavelength(SPAN)['_trc_nominal_nm'] == 1550
    assert sr.parse_trc_wavelength(SPAN, 1310)['_trc_nominal_nm'] == 1310
    assert sr.parse_trc_wavelength(SPAN, 1625)['_trc_nominal_nm'] == 1625


# ── 2. EXFO's own event table ────────────────────────────────────────

def _printed(s):
    """(value, half a unit in the last printed place)."""
    dp = len(s.split('.')[1]) if '.' in s else 0
    return float(s), 0.5 * 10 ** -dp + 1e-9


def test_matches_exfo_event_table():
    tables = json.load(open(EXFO_TABLE, encoding='utf-8'))['tables']
    checked = 0
    for side in sr.parse_trc(SPAN):
        rows = tables[str(side['_trc_nominal_nm'])]
        events = iter(side['events'])
        into = None
        assert len([r for r in rows if r['kind'] != 'Section']) == len(side['events'])
        for row in rows:
            if row['kind'] == 'Section':
                into = row['attenuation_db_km']
                continue
            e = next(events)
            v, tol = _printed(row['location_km'])
            assert abs(e['dist_km'] - v) <= tol, (side['_trc_nominal_nm'], row, e['dist_km'])
            if row['loss_db']:
                v, tol = _printed(row['loss_db'])
                assert abs(e['splice_loss'] - v) <= tol, (row, e['splice_loss'])
            if row['reflectance_db']:
                v, tol = _printed(row['reflectance_db'])
                assert e['is_reflective'] and abs(e['reflection'] - v) <= tol, (row, e)
            assert e['is_reflective'] == (row['kind'] in ('Reflective', 'Launch Level')), (row, e['type'])
            if into:
                v, tol = _printed(into)
                assert abs(e['slope'] - v) <= tol, (row, into, e['slope'])
            into = None
            checked += 1
    assert checked == 29          # 10 + 12 + 7 events at 1310 / 1550 / 1625


# ── 3. The declared span start ───────────────────────────────────────

def test_declared_span_offset():
    """The tech set the span start on the launch reel's far connector, so
    EXFO rebased every event to it and kept the OTDR port as an extra record
    1 km upstream.  That record is the offset, not an event -- exactly how a
    .sor carries it (GenParams user offset, no KeyEvent)."""
    for s in sr.parse_trc(DECL):
        assert s['user_offset_km'] == pytest.approx(1.0059, abs=0.0005)
        assert all(e['dist_km'] >= 0.0 for e in s['events'])
        assert s['events'][0]['type'][:2] == '1F'
    assert sr.parse_trc(SPAN)[0]['user_offset_km'] == 0.0


def test_identity_as_stored():
    """GenParams-style identity straight from the tree.  Locations are used
    in stored order: on every .sor carrying both, GenParams' pair IS the
    block's LocationA/LocationB whichever way LocationsDirection points."""
    s = sr.parse_trc_wavelength(DECL)
    assert (s['gen_fiber_id'], s['gen_loc_a'], s['gen_loc_b']) == ('TRCDECL0', 'SITA', 'SITB')
    assert s['gen_cable_id'] == ''          # EXFO's ' ' for blank, stripped


# ── 4. The .sor twin ─────────────────────────────────────────────────

# Spread over what the rebuilt blocks have to get right: long spans, declared
# spans, saturated ('2') reflections, tie-panel events before the span start,
# and short shots whose end lies past the acquisition range ('1O').
TWIN_DIRS = ('span_A', 'refl', 'satrefl', 'launchreel', 'lsc_panel', 'negtot',
             'frspan', 'paneljumper/A', 'strayreshoot/B', 'continuous',
             'panelreels/B')
TWINS = sorted(os.path.join(FIX, d, f) for d in TWIN_DIRS
               for f in os.listdir(os.path.join(FIX, d)) if f.lower().endswith('.sor'))

EVENT_KEYS = ('number', 'type', 'is_reflective', 'is_end', 'dist_km',
              'splice_loss', 'reflection', 'slope')
MARKERS = ('time_of_travel', 'tot_end_prev', 'tot_start_curr',
           'tot_end_curr', 'tot_start_next')


def _as_trc(path, trim):
    with open(path, 'rb') as fh:
        data = fh.read()
    stream = sr._decompress_proprietary(data, sr._parse_block_directory(data))
    root, traces = sr._trc_traces(stream)
    assert len(traces) == 1
    return sr._trc_record(sr._trc_substream(stream, root, traces, 0), path, trim)


@pytest.mark.parametrize('path', TWINS, ids=lambda p: os.path.relpath(p, FIX))
def test_sor_block_read_as_trc_matches_sor(path):
    got, want = _as_trc(path, False), sr.parse_sor_full(path, trim=False)
    for k in ('gen_cable_id', 'gen_fiber_id', 'gen_loc_a', 'gen_loc_b',
              'wavelength', 'date_time', 'duration_sec', 'otdr_model',
              'otdr_serial', 'backscatter_db', 'fxd_pulse_ns',
              'full_points', 'test_settings', 'exfo_calibration',
              'otdr_calibration_date', 'exfo_res_m'):
        assert got[k] == want[k], k
    # Data spacing is SamplingPeriod x 5e13; at 0.78125 ns that is 39,062.5,
    # which some units store 39,063 and others 39,062 -- 26 ppm on km per
    # sample, a hundredth of a sample at the far end of a tie panel.
    assert abs(got['acq_range'] - want['acq_range']) <= 1
    assert got['ior'] == pytest.approx(want['ior'], abs=1e-9)
    assert got['user_offset_km'] == pytest.approx(want['user_offset_km'], abs=5e-4)
    assert np.array_equal(got['exfo_raw'], want['exfo_raw'])
    # DataPts is rebuilt bit-exact from the raw samples -- except on shots
    # whose end lies past the acquisition range ('1O'), where EXFO writes
    # DataPts lifted by a constant (15.4-15.6 dB) no stored field accounts
    # for.  A constant cannot move a loss or a slope.
    diff = got['trace'] - want['trace']
    if any(e['type'][1:2] == 'O' for e in want['events']):
        assert np.ptp(diff) < 0.0015
    else:
        assert not np.any(diff)
    assert len(got['events']) == len(want['events'])
    for g, w in zip(got['events'], want['events']):
        for k in EVENT_KEYS:
            if isinstance(w[k], float):
                assert g[k] == pytest.approx(w[k], abs=5e-4), (k, w['dist_km'])
            else:
                assert g[k] == w[k], (k, w['dist_km'])
        # EXFO's own time of travel comes from a value the tree does not
        # store; rebuilt from Position it is exact on ~92% of events and one
        # unit (2 cm) off on the rest.
        for k in MARKERS:
            assert abs(g[k] - w[k]) <= 1 or abs(g[k] - w[k]) >= 2**32 - 1, (k, w['dist_km'])


@pytest.mark.parametrize('path', TWINS, ids=lambda p: os.path.relpath(p, FIX))
def test_sor_block_read_as_trc_trims_like_sor(path):
    """Same trim window as parse_sor_full(trim=True).  The end can land a
    sample or two apart on a tie panel, where the last event's time of travel
    is one unit off and crosses KeyEvents' 4-decimal rounding."""
    got, want = _as_trc(path, True), sr.parse_sor_full(path, trim=True)
    assert got['start_index'] == want['start_index']
    assert abs(got['end_index'] - want['end_index']) <= 2


# ── 5. Refusals ──────────────────────────────────────────────────────

def test_not_a_trc_is_refused(tmp_path):
    bad = tmp_path / 'x.trc'
    bad.write_bytes(b'not an otdr file' * 100)
    with pytest.raises(ValueError):
        sr.parse_trc(str(bad))


def test_truncated_trc_is_refused(tmp_path):
    """A copy cut off mid-transfer must not load as a short, dead fiber."""
    data = open(SPAN, 'rb').read()
    cut = tmp_path / 'cut.trc'
    cut.write_bytes(data[:len(data) // 3])
    with pytest.raises(ValueError):
        sr.parse_trc(str(cut))


def test_is_trc():
    assert sr.is_trc('A/B/F0001_155016251310.TRC')
    assert not sr.is_trc('F0001_1550.sor')
