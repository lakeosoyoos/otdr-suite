"""Robert, 2026-10-02: "need to see the range and the time stamp in our
reporting out of viewer".  The Summary Report's Traces table and each fiber
page print the distance range the tech set (FR's Test Parameters Range, from
the EXFO block) and when the trace was shot (the same clock the Files list's
day reads).  A file that carries neither prints '---', never a made-up value.
"""
import calendar
import datetime
import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'viewer'))

import trace_server as T  # noqa: E402

# The Viewer's OWN reader, the one trace_server imported: conftest puts the
# Splice Report engine's copy of the same module name first in sys.modules.
R = types.SimpleNamespace(**T.read_test_panel.__globals__)

FIX = os.path.join(HERE, 'fixtures')
SRC = open(os.path.join(ROOT, 'viewer', 'viewer.html'), encoding='utf-8').read()


def _first(folder, ext='.sor'):
    """The folder's first file of a kind (fixture names are site codes)."""
    return sorted(f for f in os.listdir(os.path.join(FIX, folder)) if f.lower().endswith(ext))[0]


def _trace(folder, ext='.sor'):
    T._load_trace_cached.cache_clear()
    return T._load_trace_cached(os.path.join(FIX, folder), _first(folder, ext), 0)


def _bytes(folder):
    return open(os.path.join(FIX, folder, _first(folder)), 'rb').read()


@pytest.mark.parametrize('folder,km', [
    ('splice_A', 100.0),      # 500 ns shot
    ('frspan', 5.0),
    ('negtot', 2.5),          # 5 ns panel shot
    ('endlaunch', 140.0),
])
def test_a_sor_serves_the_range_fr_prints(folder, km):
    assert _trace(folder)['range_km'] == km
    # the same number FR's Test Parameters panel reads
    assert R.read_test_panel(_bytes(folder))['range_km'] == km


def test_range_is_not_read_out_of_display_range():
    """`DisplayRange` comes first in the stream and ends in `Range\\0`; a
    bare find would read the chart's zoom, not the acquisition range."""
    data = _bytes('splice_A')
    stream = R._decompress_proprietary(data, R._parse_block_directory(data))
    assert stream.find(b'DisplayRange\x00') < stream.find(b'\x00Range\x00')
    assert R._parse_proprietary_stream(stream)['range_m'] == 100000.0


def test_a_trc_serves_its_range_and_when_it_was_shot():
    """A .trc had no time at all: its tree's Date is the clock reading a
    .sor's FxdParams stores, so it is read the same way."""
    t = _trace('trc', '.trc')
    assert t['range_km'] == 2.5
    assert t['acq_time'] == calendar.timegm(datetime.datetime(2026, 4, 17, 9, 59, 11).timetuple())


def test_a_sor_block_date_and_its_fxdparams_time_are_the_same_clock():
    """What the .trc read relies on: on a .sor both are stored, and agree."""
    for folder in ('splice_A', 'negtot', 'endlaunch'):
        path = os.path.join(FIX, folder, _first(folder))
        data = open(path, 'rb').read()
        stream = R._decompress_proprietary(data, R._parse_block_directory(data))
        assert R._trc_date_time(stream) == R.parse_sor_full(path, trim=False)['date_time'] > 0


def test_no_range_reads_as_none_not_zero():
    assert T._range_km({'exfo_range_m': None}) is None
    assert T._range_km({}) is None
    assert T._range_km({'exfo_range_m': 0.0}) is None
    assert T._range_km({'exfo_range_m': 80000.0}) == 80.0


# ─── the page half ────────────────────────────────────────────────────────

def _fn(name):
    body = SRC.split(f'function {name}(', 1)[1]
    return body.split('\nfunction ', 1)[0]


def test_the_traces_table_prints_date_time_and_range():
    pay = _fn('reportPayload')
    assert ("head: [['Trace', 'File', 'Date / Time', 'λ (nm)', 'Pulse (ns)', 'Range', 'IOR', 'End', 'Events']"
            in pay)
    assert "{ t: fileShot(d.acq_time) || '---', s: C }," in pay
    assert "{ t: rangeLabel(d.range_km) || '---', s: C }," in pay


def test_each_fiber_page_prints_when_each_direction_was_shot_and_the_range():
    pay = _fn('reportPayload')
    assert "meta.push([`Date / time ${arrow(t)}`, fileShot(t.data && t.data.acq_time) || '']);" in pay
    assert "meta.push(['Range', one(ts, t => rangeLabel(t.data.range_km))]);" in pay


def test_the_time_is_the_files_lists_day_plus_the_clock():
    shot = _fn('fileShot')
    assert 'if (!acq) return null;' in shot
    assert '`${fileDay(acq)} ${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`' in shot
    assert 'return km ? `${+km.toFixed(4)} km` : null;' in _fn('rangeLabel')
