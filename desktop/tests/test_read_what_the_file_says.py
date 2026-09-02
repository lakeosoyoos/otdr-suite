"""Two numbers we were guessing at that the file states outright.

Both came out of scoping the boss's ask -- edit a trace's IOR in software and
save the .sor.  Neither is the writer; both are things the readers get wrong
today, and the first is a prerequisite for the writer being safe at all.

1. IOR WAS AN UNANCHORED SCAN.

   `_read_ior` walked the first 1000 bytes for any uint32 between 145000 and
   149000 and took the first hit, else returned 1.46820.  Wrong two ways, and
   correcting an IOR is exactly what exposes both:

     - outside 1.45-1.49 it lies quietly.  Two probe files written at 1.440 and
       1.496 BOTH read back as 1.46820 and produced identical event distances
       (1.0061 / 1.0373 / 2.0440 km).  Two different files cannot agree to four
       decimals by accident; that is the fallback firing.
     - nothing anchored it to the IOR field, so any other value landing in the
       band wins if it comes first.

   helixcal/sor_fields.py already had this right and says so in its own
   docstring -- it calls `_read_ior` "an unanchored scan of the first 1000
   bytes" and uses it only as a last resort.  The three readers now agree with
   helixcal instead of contradicting it.

   Benign on today's data, and verified so: 72 production files across 12
   folders, anchored and scan agree 72/72, every one 1.47000.  Which is the
   point -- 1.47 against a true 1.467 is ~148 m over 74 km.

2. THE DECLARED SPAN'S OFFSET IS IN THE FILE.

   PR #144 shipped MEASURING it off the samples, on the belief that nothing
   recorded it.  GenParams records it exactly, as a time of travel, and it is
   the number FastReporter's Spans by Distance dialog shows as "Launch fiber
   length":

                        stored     measured
       launch_set       +0.01 m    -13.88 m
       FTH01            -0.03 m     +0.13 m
       PTL1PTL6         +0.03 m     -0.67 m

   Present on 40/40 span-declared fibers, zero on 52/52 fibers from folders
   with no span.  The measurement stays as the fallback.

   It is NOT read through `_parse_block_directory`.  That resolves a block by
   searching for its name from inside the map, and GenParams is the FIRST
   block, so the search lands on the directory ENTRY: measured wrong on 25/25
   files.  FxdParams, further down, happens to come out right on all 25 -- which
   is why the IOR fix can use it and this one cannot.

Synthetic only; CI has no .sor files.
"""
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, 'viewer'))

import trace_server as TS      # noqa: E402

READERS = ('viewer', 'splicereport', 'secretsauce')


def _reader(which):
    return open(os.path.join(ROOT, which, 'sor_reader324802a.py'), encoding='utf-8').read()


# ── the three readers must not drift again ───────────────────────────────

def test_every_reader_anchors_the_ior_to_fxdparams():
    for r in READERS:
        src = _reader(r)
        assert '_FXD_IOR_OFFSET' in src, f'{r} still scans for the IOR'
        assert re.search(r"struct\.unpack_from\('<I', data, body \+ _FXD_IOR_OFFSET\)", src), r


def test_every_reader_uses_the_same_constants_helixcal_uses():
    """helixcal/sor_fields.py is where this was right first.  Four numbers, one
    definition of what a sane group index is."""
    hx = open(os.path.join(ROOT, 'helixcal', 'sor_fields.py'), encoding='utf-8').read()
    want = {}
    for name in ('_FXD_IOR_OFFSET', '_IOR_U32_SCALE', '_IOR_SANE_MIN', '_IOR_SANE_MAX'):
        m = re.search(rf'^{name} = ([\d.]+)', hx, re.M)
        assert m, f'helixcal no longer defines {name}'
        want[name] = float(m.group(1))
    for r in READERS:
        src = _reader(r)
        for name, val in want.items():
            m = re.search(rf'^{name} = ([\d.]+)', src, re.M)
            assert m, f'{r} is missing {name}'
            assert float(m.group(1)) == val, f'{r}.{name} disagrees with helixcal'


def test_the_scan_survives_only_as_a_fallback():
    """A file this cannot anchor must be no WORSE off than before -- but the
    scan must not be reachable before the anchored read."""
    for r in READERS:
        src = _reader(r)
        fn = src[src.index('def _read_ior('):]
        fn = fn[:fn.index('\ndef ')]
        assert '145000 <= val <= 149000' in fn, f'{r} dropped the fallback entirely'
        assert fn.index('_FXD_IOR_OFFSET') < fn.index('145000 <= val'), \
            f'{r} still reaches the scan first'


def test_the_readers_stay_byte_identical_on_this_function():
    """They are deliberately isolated copies, which is exactly how the signed
    time-of-travel drifted for months.  Same bug, same fix, same text."""
    bodies = []
    for r in READERS:
        src = _reader(r)
        i = src.index('def _read_ior(')
        bodies.append(src[i:src.index('\ndef ', i)])
    assert bodies[0] == bodies[1] == bodies[2]


# ── the stored offset ────────────────────────────────────────────────────

def test_genparams_is_not_read_through_the_block_directory():
    """It resolves GenParams to the directory entry, wrong on 25/25 files."""
    for r in READERS:
        src = _reader(r)
        fn = src[src.index('def _read_user_offset_km('):]
        fn = fn[:fn.index('\ndef ')]
        assert '_block_body_offsets' in fn
        assert '_parse_block_directory' not in fn


def test_the_cumulative_walk_lands_on_the_first_block_too():
    """Blocks run contiguously from the end of the map, in directory order.
    A synthetic two-block file the search-by-name approach would get wrong."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'vr', os.path.join(ROOT, 'viewer', 'sor_reader324802a.py'))
    R = importlib.util.module_from_spec(spec); spec.loader.exec_module(R)

    names = [b'GenParams', b'FxdParams']
    bodies = [b'A' * 10, b'B' * 20]
    mapsize = 12 + sum(len(n) + 1 + 6 for n in names)
    data = b'Map\x00' + struct.pack('<HIH', 2, mapsize, len(names) + 1)
    for n, bd in zip(names, bodies):
        data += n + b'\x00' + struct.pack('<HI', 2, len(n) + 1 + len(bd))
    for n, bd in zip(names, bodies):
        data += n + b'\x00' + bd

    got = R._block_body_offsets(data)
    assert got['GenParams'] == mapsize + len(b'GenParams') + 1
    assert data[got['GenParams']:got['GenParams'] + 10] == b'A' * 10
    assert data[got['FxdParams']:got['FxdParams'] + 20] == b'B' * 20


def test_a_file_with_no_declared_span_reports_zero_not_none():
    """0.0 is the answer for an ordinary file, and it must not raise on bytes
    that are not a SOR at all."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'vr2', os.path.join(ROOT, 'viewer', 'sor_reader324802a.py'))
    R = importlib.util.module_from_spec(spec); spec.loader.exec_module(R)
    assert R._read_user_offset_km(b'not a sor file at all') == 0.0
    assert R._read_user_offset_km(b'') == 0.0


def test_the_stored_offset_is_preferred_over_measuring_it():
    """And the preference has to be reachable.  It was not, at first: the field
    lives on the READER's result and trace_server builds its own dict, so the
    key was absent and read as "no stored offset" -- the fallback fired every
    time and nothing said so."""
    src = open(os.path.join(ROOT, 'viewer', 'trace_server.py'), encoding='utf-8').read()
    fn = src[src.index('def _trace_span_launch_km('):]
    fn = fn[:fn.index('\ndef ')]
    assert "t.get('user_offset_km')" in fn
    assert fn.index("user_offset_km") < fn.index('_trace_eof_km'), \
        'the measurement must be the fallback, not the first answer'
    assert "'user_offset_km'" in src.split('def decimate_minmax')[0], \
        'the field is not carried onto the trace dict, so the preference is dead'


def test_a_stored_offset_of_zero_falls_through_to_measuring():
    """0.0 means "no span declared", so on a file whose events ARE re-based it
    must not be taken as the answer."""
    t = {'events': [{'dist_km': -0.03}, {'dist_km': 0.0}, {'dist_km': 1.0}],
         'user_offset_km': 0.0,
         'dist_km': [i * 0.001 for i in range(4000)],
         'trace_db': [10.0 if i < 2100 else -18.0 for i in range(4000)]}
    got = TS._trace_span_launch_km(t)
    assert got is not None and abs(got - (2.1 - 1.0)) < 0.05


def test_a_stored_offset_wins_outright_when_present():
    t = {'events': [{'dist_km': -0.03}, {'dist_km': 0.0}, {'dist_km': 1.0}],
         'user_offset_km': 1.2345,
         'dist_km': [i * 0.001 for i in range(4000)],
         'trace_db': [10.0 if i < 2100 else -18.0 for i in range(4000)]}
    assert TS._trace_span_launch_km(t) == 1.2345
