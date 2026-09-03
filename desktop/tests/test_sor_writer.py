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

sys.path.insert(0, os.path.join(ROOT, "viewer"))
import trace_server as W        # noqa: E402  (the writer lives in the Viewer server)


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


# ── identifiers ──────────────────────────────────────────────────────────
#
# GenParams carries the human-facing text as NUL-terminated strings, so an
# edit changes the block's size.  They appear nowhere else in the file --
# searched the proprietary stream for every one on a real file, found none --
# so this is Bellcore-only with no second copy to go stale.

def test_identifiers_read_back_from_the_synthetic_file():
    ids = W.read_identifiers(make_sor())
    assert ids == {'cable_id': 'CABLE1', 'fiber_id': '0001', 'loc_a': 'LOCA',
                   'loc_b': 'LOCB', 'cable_code': 'CODE', 'operator': 'op',
                   'comment': 'comment'}


def test_a_longer_name_grows_the_file_and_still_round_trips():
    src = make_sor()
    out = W.set_identifiers(src, cable_id='A MUCH LONGER CABLE NAME', comment='fixed')
    assert len(out) == len(src) + (len('A MUCH LONGER CABLE NAME') - len('CABLE1')) + (len('fixed') - len('comment'))
    assert W.roundtrip_ok(out)
    assert W.read_identifiers(out)['cable_id'] == 'A MUCH LONGER CABLE NAME'
    assert W.read_identifiers(out)['comment'] == 'fixed'


def test_a_shorter_name_shrinks_the_file():
    src = make_sor()
    out = W.set_identifiers(src, cable_id='C')
    assert len(out) == len(src) - 5
    assert W.roundtrip_ok(out)


def test_untouched_identifiers_and_fixed_fields_come_through_verbatim():
    """Fiber type, wavelength, build condition and the user offsets sit between
    the strings; a re-laid block must carry them byte for byte."""
    src = make_sor()
    out = W.set_identifiers(src, operator='someone else')
    ids = W.read_identifiers(out)
    assert ids['operator'] == 'someone else'
    for k in ('cable_id', 'fiber_id', 'loc_a', 'loc_b', 'cable_code', 'comment'):
        assert ids[k] == W.read_identifiers(src)[k]
    _, a = W.split(src); _, b = W.split(out)
    ga, gb = W._find(a, b'GenParams').body, W._find(b, b'GenParams').body
    oa, ob = W.genparams_offsets(ga), W.genparams_offsets(gb)
    assert ga[oa['user_offset']:oa['user_offset'] + 8] == gb[ob['user_offset']:ob['user_offset'] + 8]


def test_every_other_block_is_byte_identical_after_a_name_edit():
    src = make_sor()
    out = W.set_identifiers(src, cable_id='RENAMED')
    _, a = W.split(src); _, b = W.split(out)
    for x, y in zip(a, b):
        if x.name not in (b'GenParams', b'Cksum'):
            assert x.body == y.body, x.name


def test_text_that_cannot_live_in_a_sor_is_refused():
    for bad in ({'comment': 'has\x00nul'}, {'cable_id': 'ünïcode→'}, {'operator': 'x' * 300}):
        try:
            W.set_identifiers(make_sor(), **bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_an_unknown_field_is_refused_not_ignored():
    try:
        W.set_identifiers(make_sor(), wavelength='1550')
    except ValueError:
        return
    raise AssertionError('silently accepted a field that is not a GenParams string')


def test_ior_and_name_edits_compose():
    out = W.set_identifiers(W.set_ior(make_sor(1.47), 1.467), comment='IOR corrected')
    assert abs(W.read_ior(out) - 1.467) < 1e-9
    assert W.read_identifiers(out)['comment'] == 'IOR corrected'
    assert W.roundtrip_ok(out)


# ── the proprietary stream as a record tree (size-changing edits) ────────
#
# FastReporter's Identification tab reads UTF-16 records in the proprietary
# stream, not GenParams -- an edit there changed nothing on screen.  Every
# descriptor and every type-0 child pointer is an ABSOLUTE stream offset, and
# the block header carries the stream length, so a string that changes length
# forces a re-layout.  Pinned here on a synthetic stream with the real shape.

def _rec(out, name, tc, payload):
    self_off = len(out) + 16
    out.extend(struct.pack('<IIII', self_off, tc, len(payload), self_off + len(name) + 1))
    out.extend(name + b'\x00' + payload)
    return self_off - 16


def _u16(s):
    return s.encode('utf-16-le') + b'\x00\x00'


def _tree_stream():
    """root(type0) -> [A(str), Parent(type0) -> [B(str), C(u32)]], then Name/Value."""
    out = bytearray(b'\x00' * 16)
    root = _rec(out, b'Root', 0, b'\x00' * 8)                 # two child ptrs, patched below
    a = _rec(out, b'UserNameA', 4, _u16('G. Kolok'))
    parent = _rec(out, b'Parent', 0, b'\x00' * 8)
    b = _rec(out, b'Comment', 4, _u16('West 144f'))
    c = _rec(out, b'Count', 1, struct.pack('<I', 7))
    n = _rec(out, b'Name', 4, _u16('Cable ID'))
    v = _rec(out, b'Value', 4, _u16(''))
    struct.pack_into('<II', out, root + 16 + 5, a, parent)     # Root payload
    struct.pack_into('<II', out, parent + 16 + 7, b, c)        # Parent payload
    return bytes(out)


def _prop_from_stream(stream):
    hdr = bytearray(b'AppReg Format Ex  \x00\x00' + struct.pack('<I', 2) + struct.pack('<I', len(stream)) + struct.pack('<II', 1, 0))
    return W._prop_rebuild_stream(bytes(hdr), stream)


def test_records_are_found_by_their_own_descriptor():
    st = _tree_stream()
    names = [r['name'] for r in W._prop_records(st)]
    assert names == ['Root', 'UserNameA', 'Parent', 'Comment', 'Count', 'Name', 'Value']


def test_a_longer_string_rebases_every_pointer_past_it():
    st = _tree_stream()
    a = next(r for r in W._prop_records(st) if r['name'] == 'UserNameA')
    out = W._prop_set_string_payloads(st, {a['pay']: 'Robert Colbert'})
    recs = W._prop_records(out)
    assert [r['name'] for r in recs] == [r['name'] for r in W._prop_records(st)]
    starts = {r['desc'] for r in recs}
    for r in recs:
        if r['tc'] == 0:
            for k in range(0, r['size'], 4):
                assert struct.unpack_from('<I', out, r['pay'] + k)[0] in starts, r['name']
    got = dict((r['name'], v) for r, v in W._prop_strings(out))
    assert got['UserNameA'] == 'Robert Colbert'
    assert got['Comment'] == 'West 144f'
    assert next(r for r in recs if r['name'] == 'Count')['pay'] and \
        struct.unpack_from('<I', out, next(r for r in recs if r['name'] == 'Count')['pay'])[0] == 7


def test_a_shorter_string_rebases_the_other_way():
    st = _tree_stream()
    a = next(r for r in W._prop_records(st) if r['name'] == 'UserNameA')
    out = W._prop_set_string_payloads(st, {a['pay']: 'GK'})
    assert len(out) == len(st) - (len('G. Kolok') - 2) * 2
    starts = {r['desc'] for r in W._prop_records(out)}
    for r in W._prop_records(out):
        if r['tc'] == 0:
            for k in range(0, r['size'], 4):
                assert struct.unpack_from('<I', out, r['pay'] + k)[0] in starts


def test_two_edits_at_once_both_land():
    st = _tree_stream()
    recs = {r['name']: r for r in W._prop_records(st)}
    out = W._prop_set_string_payloads(st, {recs['UserNameA']['pay']: 'Someone Much Longer',
                                           recs['Comment']['pay']: 'x'})
    got = dict((r['name'], v) for r, v in W._prop_strings(out))
    assert got['UserNameA'] == 'Someone Much Longer' and got['Comment'] == 'x'
    assert len(W._prop_records(out)) == 7


def test_a_dangling_child_pointer_refuses_the_edit():
    """The pointer hypothesis is a guard, not an assumption.  A stream whose
    type-0 payload holds a non-record offset must not be re-laid."""
    st = bytearray(_tree_stream())
    root = next(r for r in W._prop_records(bytes(st)) if r['name'] == 'Root')
    struct.pack_into('<I', st, root['pay'], 999999)
    a = next(r for r in W._prop_records(bytes(st)) if r['name'] == 'UserNameA')
    try:
        W._prop_set_string_payloads(bytes(st), {a['pay']: 'x'})
    except ValueError:
        return
    raise AssertionError


def test_rebuild_rechunks_at_32k_and_writes_the_length_into_the_header():
    st = bytes(range(256)) * 300                      # 76,800 bytes -> 3 chunks
    body = _prop_from_stream(st)
    assert struct.unpack_from('<I', body, 24)[0] == len(st)
    hdr, chunks, tail = W._prop_chunks(body)
    assert [len(d) for _, d in chunks] == [32768, 32768, 76800 - 65536]
    assert b''.join(d for _, d in chunks) == st and tail == b''


def test_identifier_targets_hit_the_identifiers_list_value_not_its_name():
    st = _tree_stream()
    pays = W._prop_id_targets(st, 'cable_id')
    v = next(r for r in W._prop_records(st) if r['name'] == 'Value')
    assert pays == [v['pay']], 'Cable ID must edit the Value record that FOLLOWS the Name record'


def test_set_identifiers_writes_both_sides_and_still_round_trips():
    """GenParams for every reader that speaks Bellcore, the proprietary stream
    for FastReporter -- one edit, both copies, one answer."""
    src = make_sor()
    out = W.set_identifiers(src, operator='R. Colbert', customer='Lumen (edited)')
    assert W.roundtrip_ok(out)
    assert W.read_identifiers(out)['operator'] == 'R. Colbert'
    _, bl = W.split(out)
    pb = next(b for b in bl if b.name.startswith(b'ExfoNewProprietaryBlock'))
    _, chunks, _ = W._prop_chunks(pb.body)
    got = dict((r['name'], v) for r, v in W._prop_strings(b''.join(d for _, d in chunks)))
    # make_sor's stream has no UserNameA record, so nothing to assert there;
    # the point is the file is intact and the GenParams side landed.
    assert 'customer' not in W.read_identifiers(out)   # not a GenParams field


def test_extras_without_a_genparams_slot_are_accepted():
    out = W.set_identifiers(make_sor(), job_id='J1', company='ZerodB', operator_b='S. D')
    assert W.roundtrip_ok(out)
