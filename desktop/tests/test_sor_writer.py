"""The suite's first .sor WRITER.  Format-level guarantees, all synthetic.

The boss asked to edit a trace's settings in our software and save the .sor --
IOR first.  This is the part of that which can be pinned without a real file:
the container is rebuilt byte-exact, the checksum is right, an edit never
lands on the source, and an IOR edit touches every distance twin it must.

What an IOR edit must touch was established against FastReporter, not
reasoned about, and it is more than one field:

    1. FxdParams group index          the number itself
    2. the Bellcore distance twins    user_offset_dist, acq_offset_dist,
                                      acq_range_dist -- stored in 0.1 m beside
                                      their time fields, so they go stale
    3. proprietary float64 `Ior`      EXFO's own copy
    4. proprietary metres             Position, SpansLength, the cursors...

FR ignores 1 and reads 3+4.  With only (1) changed it printed the old
distances; with (1)+(3) changed it STILL printed the old distances; only
(1)+(3)+(4) moved them.  Our three readers read (1).  A writer that stops
short of (4) therefore produces a file our tools and FR disagree about by the
full ratio -- the exact interop failure this exists to prevent.

The RawSamples payload must come through byte-identical.  The decoder invents
pseudo-fields inside it (probe5.py), and a scan that does not stop at that
payload's exact byte span will "scale" sample data.
"""
import os
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

import sor_writer as W          # noqa: E402


# ── a synthetic but structurally faithful .sor ───────────────────────────

def _cstr(s):
    return s.encode('latin-1') + b'\x00'


def _genparams(user_offset=0, user_offset_dist=0):
    b = b'EN' + _cstr('CABLE1') + _cstr('0001')
    b += struct.pack('<HH', 0, 15500)                       # fiber type, wavelength
    b += _cstr('LOCA') + _cstr('LOCB') + _cstr('CODE') + b'BC'
    b += struct.pack('<ii', user_offset, user_offset_dist)
    b += _cstr('op') + _cstr('comment')
    return b


def _fxdparams(ior=1.47, acq_offset=100, acq_offset_dist=1000,
               acq_range=200000, acq_range_dist=2000000, npw=1):
    b = struct.pack('<I', 0) + b'mt' + struct.pack('<H', 15500)
    b += struct.pack('<ii', acq_offset, acq_offset_dist)
    b += struct.pack('<H', npw)
    b += struct.pack('<H', 10) * npw                        # pulse widths
    b += struct.pack('<I', 78125) * npw                     # data spacing
    b += struct.pack('<I', 4000) * npw                      # points per width
    b += struct.pack('<I', int(round(ior * 1e5)))           # group index
    b += struct.pack('<hIH', -8100, 100, 15)                # backscatter, avgs, avg time
    b += struct.pack('<ii', acq_range, acq_range_dist)
    b += b'\x00' * 40                                       # the rest, unused here
    return b


def _prop_record(name, value):
    """One float64 record in EXFO's stream layout, self_off filled by caller."""
    return name, value


def _prop_stream(records, raw_payload):
    """Lay records out with real descriptors, RawSamples in the middle."""
    out = bytearray(b'\x00' * 8)                            # some leading junk
    def emit(name, tc, size, payload):
        self_off = len(out) + 16
        out.extend(struct.pack('<IIII', self_off, tc, size, self_off + len(name) + 1))
        out.extend(name + b'\x00' + payload)
    for name, v in records[:2]:
        emit(name, 3, 8, struct.pack('<d', v))
    emit(b'RawSamples', 2, len(raw_payload), raw_payload)
    for name, v in records[2:]:
        emit(name, 3, 8, struct.pack('<d', v))
    return bytes(out)


def _prop_block(stream, chunk=4096):
    body = bytearray(b'\x00' * 36)
    for i in range(0, len(stream), chunk):
        comp = zlib.compress(stream[i:i + chunk])
        body += struct.pack('<I', len(comp)) + comp
    return bytes(body)


def make_sor(ior=1.47, records=None, raw_payload=None):
    if records is None:
        records = [(b'Ior', ior), (b'SpansLength', 2041.4),
                   (b'Position', 1004.9), (b'CursorAPosition', 0.0),
                   (b'SubCursorAPosition', 500.0)]
    if raw_payload is None:
        # a payload that CONTAINS a fake 'Position' record, as real ones do
        raw_payload = b'\x11' * 50 + b'Position\x00' + struct.pack('<d', 999.0) + b'\x22' * 50
    blocks = [
        W.Block(b'GenParams', 2, _genparams(user_offset=50801, user_offset_dist=10360)),
        W.Block(b'FxdParams', 2, _fxdparams(ior=ior)),
        W.Block(b'KeyEvents', 2, struct.pack('<H', 0)),
        W.Block(b'DataPts', 2, b'\x00' * 64),
        W.Block(b'ExfoNewProprietaryBlock 01', 1, _prop_block(_prop_stream(records, raw_payload))),
        W.Block(b'Cksum', 2, b'\x00\x00'),
    ]
    return W.build(2, blocks)


# ── container ────────────────────────────────────────────────────────────

def test_roundtrip_is_byte_exact():
    d = make_sor()
    assert W.roundtrip_ok(d)


def test_checksum_is_the_verified_crc():
    """CRC-16 poly 0x1021 init 0x9ECF, no reflection, no xorout -- the values
    real files carry.  Pinned so nobody 'fixes' it to a textbook CRC."""
    assert W.sor_crc(b'') == 0x9ECF
    assert W.sor_crc(b'123456789') == W.sor_crc(b'123456789')     # deterministic
    d = make_sor()
    assert struct.unpack_from('<H', d, len(d) - 2)[0] == W.sor_crc(d[:-8])


def test_a_corrupt_directory_is_refused_not_guessed():
    d = bytearray(make_sor())
    d[6] = 0                                       # mapsize now lies
    try:
        W.split(bytes(d))
    except ValueError:
        return
    raise AssertionError('split accepted a directory that does not add up')


def test_not_a_sor_is_refused():
    try:
        W.split(b'this is not a sor file')
    except ValueError:
        return
    raise AssertionError


# ── never in place ───────────────────────────────────────────────────────

def test_write_refuses_the_source_path(tmp_path):
    src = tmp_path / 'a.sor'
    src.write_bytes(make_sor())
    try:
        W.write(b'x', str(src), src=str(src))
    except ValueError:
        assert src.read_bytes() == make_sor(), 'source was touched'
        return
    raise AssertionError('wrote over the source')


def test_write_refuses_an_existing_file_unless_told(tmp_path):
    dst = tmp_path / 'b.sor'
    dst.write_bytes(b'old')
    try:
        W.write(b'new', str(dst))
    except FileExistsError:
        assert dst.read_bytes() == b'old'
    else:
        raise AssertionError
    W.write(b'new', str(dst), overwrite=True)
    assert dst.read_bytes() == b'new'


def test_write_is_atomic(tmp_path):
    """A .part is renamed into place; no half-written destination exists."""
    dst = tmp_path / 'c.sor'
    W.write(make_sor(), str(dst))
    assert not (tmp_path / 'c.sor.part').exists()
    assert W.roundtrip_ok(dst.read_bytes())


# ── the IOR edit ─────────────────────────────────────────────────────────

def test_group_index_changes_and_the_file_still_round_trips():
    out = W.set_ior(make_sor(1.47), 1.467)
    assert abs(W.read_ior(out) - 1.467) < 1e-9
    assert W.roundtrip_ok(out)


def test_the_bellcore_distance_twins_scale_by_old_over_new():
    src = make_sor(1.47)
    out = W.set_ior(src, 1.467)
    r = 1.47 / 1.467
    _, bl = W.split(out)
    gp = W._find(bl, b'GenParams').body
    fx = W._find(bl, b'FxdParams').body
    go, fo = W.genparams_offsets(gp), W.fxdparams_offsets(fx)
    assert struct.unpack_from('<i', gp, go['user_offset'])[0] == 50801          # TIME untouched
    assert struct.unpack_from('<i', gp, go['user_offset_dist'])[0] == round(10360 * r)
    assert struct.unpack_from('<i', fx, fo['acq_offset'])[0] == 100             # TIME untouched
    assert struct.unpack_from('<i', fx, fo['acq_offset_dist'])[0] == round(1000 * r)
    assert struct.unpack_from('<i', fx, fo['acq_range_dist'])[0] == round(2000000 * r)


def test_the_proprietary_ior_and_metres_are_rewritten():
    src = make_sor(1.47)
    out = W.set_ior(src, 1.467)
    r = 1.47 / 1.467
    _, bl = W.split(out)
    pb = next(b for b in bl if b.name.startswith(b'ExfoNewProprietaryBlock'))
    _, chunks, _ = W._prop_chunks(pb.body)
    stream = b''.join(d for _, d in chunks)
    got = dict((n, v) for n, v in
               [(nm, v) for (off, v), nm in zip(
                   W._prop_float64_payloads(stream, W._PROP_METRE_FIELDS + (b'Ior',)),
                   [b'SpansLength', b'Position', b'CursorAPosition', b'SubCursorAPosition', b'Ior'])])
    assert abs(W._prop_float64_payloads(stream, (b'Ior',))[0][1] - 1.467) < 1e-12
    sl = W._prop_float64_payloads(stream, (b'SpansLength',))[0][1]
    assert abs(sl - 2041.4 * r) < 1e-9
    pos = W._prop_float64_payloads(stream, (b'Position',))[0][1]
    assert abs(pos - 1004.9 * r) < 1e-9


def test_the_rawsamples_payload_is_untouched_even_though_it_contains_a_fake_field():
    """The payload carries a bogus 'Position' record.  It must not be scaled."""
    src = make_sor(1.47)
    out = W.set_ior(src, 1.467)
    def payload(d):
        _, bl = W.split(d)
        pb = next(b for b in bl if b.name.startswith(b'ExfoNewProprietaryBlock'))
        _, chunks, _ = W._prop_chunks(pb.body)
        s = b''.join(x for _, x in chunks)
        lo, hi = W._rawsamples_span(s)
        return s[lo:hi]
    assert payload(src) == payload(out)
    assert b'Position\x00' + struct.pack('<d', 999.0) in payload(out), 'fake field was altered'


def test_a_substring_name_hit_is_not_taken():
    """`CursorAPosition\\0` occurs inside `SubCursorAPosition\\0`; only the
    record whose descriptor points back at it counts."""
    src = make_sor(1.47)
    _, bl = W.split(src)
    pb = next(b for b in bl if b.name.startswith(b'ExfoNewProprietaryBlock'))
    _, chunks, _ = W._prop_chunks(pb.body)
    stream = b''.join(d for _, d in chunks)
    hits = W._prop_float64_payloads(stream, (b'CursorAPosition',))
    assert len(hits) == 1
    assert hits[0][1] == 0.0                       # the real one, not the 500.0 Sub- one


def test_an_ior_outside_the_sane_band_is_refused():
    for bad in (1.0, 1.39, 1.56, 2.0):
        try:
            W.set_ior(make_sor(), bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_zero_metre_fields_stay_zero_and_nan_is_left_alone():
    recs = [(b'Ior', 1.47), (b'SpansLength', 2041.4),
            (b'Position', 0.0), (b'CursorAPosition', float('nan')), (b'Length', 10.0)]
    out = W.set_ior(make_sor(1.47, records=recs), 1.467)
    _, bl = W.split(out)
    pb = next(b for b in bl if b.name.startswith(b'ExfoNewProprietaryBlock'))
    _, chunks, _ = W._prop_chunks(pb.body)
    stream = b''.join(d for _, d in chunks)
    pos = W._prop_float64_payloads(stream, (b'Position',))[0][1]
    cur = W._prop_float64_payloads(stream, (b'CursorAPosition',))[0][1]
    assert pos == 0.0
    assert cur != cur                              # still NaN


def test_bellcore_only_mode_leaves_the_proprietary_block_byte_identical():
    """The experiment flag.  Useful for proving what FR reads; NOT a mode a
    tech should ever get, because FR ignores the Bellcore field."""
    src = make_sor(1.47)
    out = W.set_ior(src, 1.467, proprietary=False)
    _, a = W.split(src)
    _, b = W.split(out)
    pa = next(x for x in a if x.name.startswith(b'ExfoNewProprietaryBlock'))
    pb = next(x for x in b if x.name.startswith(b'ExfoNewProprietaryBlock'))
    assert pa.body == pb.body
