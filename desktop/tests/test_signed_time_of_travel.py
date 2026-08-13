"""KeyEvents time-of-travel is SIGNED.

An OTDR can place an event just BEFORE the trace origin — a negative
time-of-travel — which is what the tie-panel / panel-to-panel sets on disk
write for their instrument-port event.  `_parse_key_events` read the field
as '<I' (unsigned) while the three fields immediately after it were already
signed, so a tot of -750 (about -15 m) came back as 4294966546 and turned
into a distance of 87,593.94 km.

Consequences, all visible on the fixture: the phantom event sorts FIRST, so
the event table is out of order; every span/EOF estimate derived from it is
nonsense; and any span-relative rule downstream is working from a fiber it
believes is 87,594 km long.

Survey behind the fix (918 files, 155 folders on disk): no legitimate
acquisition has a tot within four orders of magnitude of 2**31, and the ONLY
folders containing one are tie-panel sets — FTH01_FTH06, FTH06_FTH01,
Reubensville ILA, Tie Panel Sacrificial Jumpers — at 286 of 288 files each.
The change is therefore a no-op on every other file by construction.

Fixture: one real, unmodified acquisition from
~/Desktop/FTH01_FTH06 West Panel B/.
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'splicereport'))

import sor_reader324802a as SR  # noqa: E402

FIX = os.path.join(HERE, 'fixtures', 'negtot',
                   'FTHNTXAD01_FTHNTXAD06_001.sor')


def _events():
    return SR.parse_sor_full(FIX)['events']


def test_field_is_read_signed():
    """The guard against a silent revert: the three neighbouring fields were
    always signed, and this one has to be too."""
    src = open(os.path.join(ROOT, 'splicereport', 'sor_reader324802a.py'),
               encoding='utf-8').read()
    body = src.split('def _parse_key_events', 1)[1].split('\ndef ', 1)[0]
    line = next(l for l in body.splitlines() if l.strip().startswith('tot '))
    assert "'<i'" in line, f"time_of_travel must be signed, got: {line.strip()}"


def test_negative_tot_decodes_to_a_small_negative_distance():
    evs = _events()
    first = evs[0]
    assert first['time_of_travel'] < 0, "fixture no longer carries a negative tot"
    # -750 units -> about -15 m, an instrument-port event just before origin
    assert -0.05 < first['dist_km'] < 0.0
    # ... and emphatically NOT the unsigned misread
    assert first['dist_km'] < 1000.0


def test_unsigned_misread_would_have_produced_87594_km():
    """Pin the failure so its magnitude is on the record.  The exact km
    depends on the file's own group index, so derive it from the fixture
    rather than assuming a default IOR."""
    raw = struct.unpack('<I', struct.pack('<i', -750))[0]
    assert raw == 4294966546
    ior = SR._read_ior(open(FIX, 'rb').read())
    misread_km = (raw * 0.02998 / ior) / 1000.0
    assert 87_000 < misread_km < 88_000, misread_km
    # the signed reading of the same bytes, for contrast
    assert abs((-750 * 0.02998 / ior) / 1000.0) < 0.02


def test_event_table_is_in_distance_order():
    """The phantom sorted first and put the whole table out of order, which
    every 'first event' / 'first is_end' scan in the engine depends on."""
    kms = [e['dist_km'] for e in _events()]
    assert kms == sorted(kms), kms


def test_span_is_plausible():
    kms = [e['dist_km'] for e in _events()]
    span = max(kms)
    assert 0.5 < span < 5.0, f"span {span} km is not a physical tie-panel length"


def test_no_event_is_absurdly_far():
    for e in _events():
        assert -1.0 < e['dist_km'] < 1000.0, e
