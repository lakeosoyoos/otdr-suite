"""Write a Bellcore/Telcordia SR-4731 .sor back out, edited.

The suite has read these files for years and never written one.  This module
exists because the boss asked to edit a trace's settings in our software and
save the .sor -- IOR first ("IOR is wrong and correct with software"), trace
name second, a declared span start/stop third.

RULES THIS MODULE ENFORCES, NOT JUST DOCUMENTS
----------------------------------------------
* Never in place.  `write()` refuses a destination that is the source, or that
  already exists unless told to overwrite.  These are customer measurement
  records and sometimes the only copy.
* Byte-exact round trip.  A file split and rebuilt with no edit must come back
  identical, and `roundtrip_ok()` checks it before any edited write is offered.
* The checksum is recomputed, always.  CRC-16, polynomial 0x1021, init 0x9ECF,
  non-reflected, no final xor -- worked out from real files and verified on
  every one since.  A file with a stale Cksum is a file FastReporter may refuse.

WHAT AN IOR EDIT ACTUALLY TOUCHES
---------------------------------
Event positions and the trace's x-axis are DERIVED: KeyEvents store time of
travel, DataPts store samples at a fixed sampling period, and every distance is
`time * c / IOR`.  So changing the group index in FxdParams re-scales all of
them for free -- verified: a 1.47 -> 1.467 edit moves a 2.0415 km end event to
2.0456, exactly the ratio.

But the format ALSO stores distances outright, as twins beside their times, and
those go stale unless rewritten:

    GenParams  user_offset_dist    (0.1 m)    twin of user_offset   (time)
    FxdParams  acq_offset_dist     (0.1 m)    twin of acq_offset    (time)
    FxdParams  acq_range_dist      (0.1 m)    twin of acq_range     (time)

`set_ior()` rewrites those by the same ratio.  And EXFO's proprietary block
carries its own `Ior` as a float64 plus a family of positions in METRES
(Position, SpansLength, the cursors).  Which of those FastReporter trusts was
run as an experiment, not assumed.  Three files cut from the same source, each
opened cold in FR 3 and its IOR-by-distance dialog and event table read:

    Bellcore group index only                      FR: 2.0414 km  (ignored)
    + proprietary float64 Ior                      FR: 2.0414 km  (STILL ignored)
    + proprietary metre fields scaled by old/new   FR: 2.0456 km  (correct)

So FR never recomputes a distance from an IOR; it reads the stored metres.
Our three readers read the Bellcore field.  A writer that stops short of
scaling the metres therefore produces a file our tools and FR disagree about
by the full ratio -- the exact interop failure this exists to prevent.  The
`proprietary=False` flag survives only to reproduce that experiment.

The RawSamples payload is never touched, and that is checked, not hoped: the
decoder invents pseudo-fields inside it, and the scan stops at the payload's
exact byte span.

WHAT A NAME EDIT ACTUALLY TOUCHES
---------------------------------
GenParams holds cable id, fiber id, locations, operator and comment as Latin-1
strings, and every reader of ours speaks it.  FastReporter does not: its
Identification tab is driven by UTF-16 records in the proprietary stream --
UserNameA, CustomerName, CompanyName, Comment, a Job-ID `Identifier`, and a
Name/Value "Identifiers" list for Cable ID / Fiber ID / Location A / B.  An
edit to GenParams alone changed nothing on FR's screen; that was tried.

Those records change LENGTH when edited, and the stream is a tree of absolute
offsets -- every 16-byte descriptor, every type-0 child pointer, and the block
header's stream length -- so `set_identifiers()` re-lays the stream, rebasing
all of them, and re-chunks at 32 KiB.  Verified in FR on a file whose stream
grew by 126 bytes: every edited field displayed, every untouched one intact.
Both copies are written, so one file gives one answer everywhere.
"""
from __future__ import annotations

import os
import struct
import zlib

C_M_PER_S = 299_792_458.0

CRC_POLY, CRC_INIT = 0x1021, 0x9ECF


# ─── checksum ─────────────────────────────────────────────────────────────

def sor_crc(data: bytes) -> int:
    """CRC-16 over everything before the Cksum block's 2-byte body."""
    c = CRC_INIT
    for by in data:
        c ^= by << 8
        for _ in range(8):
            c = ((c << 1) ^ CRC_POLY) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    return c


# ─── block directory ──────────────────────────────────────────────────────

class Block:
    __slots__ = ('name', 'ver', 'body', 'scaled_fields')

    def __init__(self, name: bytes, ver: int, body: bytes):
        self.name, self.ver, self.body = name, ver, body

    def __repr__(self):
        return f'Block({self.name!r}, ver={self.ver}, {len(self.body)} bytes)'


def split(data: bytes):
    """-> (map_version, [Block, ...]) in physical order, Cksum last.

    Walks the directory's SIZES, which is what the format specifies.  Not a
    search-by-name: GenParams is the first block and its name also appears in
    the directory, so searching lands on the directory entry (wrong on 25/25
    real files -- see the readers' `_block_body_offsets`).
    """
    if data[:4] != b'Map\x00':
        raise ValueError('not a Bellcore SOR (no Map block)')
    ne = data.index(b'\x00') + 1
    mapver = struct.unpack_from('<H', data, ne)[0]
    mapsize = struct.unpack_from('<I', data, ne + 2)[0]
    nb = struct.unpack_from('<H', data, ne + 6)[0]
    off, cum, out = ne + 8, mapsize, []
    for _ in range(nb - 1):
        e = data.index(b'\x00', off) + 1
        nm = data[off:e - 1]
        bv = struct.unpack_from('<H', data, e)[0]
        bs = struct.unpack_from('<I', data, e + 2)[0]
        hdr = nm + b'\x00'
        if data[cum:cum + len(hdr)] != hdr:
            raise ValueError(f'block {nm!r} not where the directory says (offset {cum})')
        out.append(Block(nm, bv, data[cum + len(hdr): cum + bs]))
        cum += bs
        off = e + 6
    if off != mapsize:
        raise ValueError(f'directory walk ended at {off}, map says {mapsize}')
    return mapver, out


def build(mapver: int, blocks) -> bytes:
    """Re-emit the file: directory, blocks, then Cksum computed over all of it."""
    if not blocks or blocks[-1].name != b'Cksum' or len(blocks[-1].body) != 2:
        raise ValueError('Cksum must be the last block with a 2-byte body')
    mapsize = 12 + sum(len(b.name) + 1 + 6 for b in blocks)
    hdr = b'Map\x00' + struct.pack('<HIH', mapver, mapsize, len(blocks) + 1)
    for b in blocks:
        hdr += b.name + b'\x00' + struct.pack('<HI', b.ver, len(b.name) + 1 + len(b.body))
    out = bytearray(hdr)
    for b in blocks[:-1]:
        out += b.name + b'\x00' + b.body
    out += b'Cksum\x00' + struct.pack('<H', sor_crc(bytes(out)))
    return bytes(out)


def roundtrip_ok(data: bytes) -> bool:
    mv, bl = split(data)
    return build(mv, bl) == data


def _find(blocks, name: bytes) -> Block:
    for b in blocks:
        if b.name == name:
            return b
    raise KeyError(name.decode())


# ─── field maps (verified byte-exact against real files, probe2.py) ───────

def _cstr_end(b: bytes, p: int) -> int:
    return b.index(b'\x00', p) + 1


def genparams_offsets(body: bytes) -> dict:
    """Offsets INTO the GenParams body of the fields that carry distance."""
    p = 2                                   # language
    p = _cstr_end(body, p)                  # cable id
    p = _cstr_end(body, p)                  # fiber id
    p += 4                                  # fiber type, wavelength
    p = _cstr_end(body, p)                  # location A
    p = _cstr_end(body, p)                  # location B
    p = _cstr_end(body, p)                  # cable code
    p += 2                                  # build condition
    return {'user_offset': p, 'user_offset_dist': p + 4}


def fxdparams_offsets(body: bytes) -> dict:
    """Offsets INTO the FxdParams body: group index and the distance twins."""
    p = 4 + 2 + 2                           # date, units, wavelength
    acq_offset, acq_offset_dist = p, p + 4
    p += 8
    npw = struct.unpack_from('<H', body, p)[0]
    p += 2
    p += 2 * npw                            # pulse widths
    p += 4 * npw                            # data spacing
    p += 4 * npw                            # points per width
    group_index = p
    p += 4                                  # group index
    p += 2 + 4 + 2                          # backscatter, averages, avg time
    acq_range, acq_range_dist = p, p + 4
    return {'acq_offset': acq_offset, 'acq_offset_dist': acq_offset_dist,
            'group_index': group_index,
            'acq_range': acq_range, 'acq_range_dist': acq_range_dist}


def read_ior(data: bytes) -> float:
    _, bl = split(data)
    fx = _find(bl, b'FxdParams').body
    return struct.unpack_from('<I', fx, fxdparams_offsets(fx)['group_index'])[0] / 1e5


# ─── the proprietary block ────────────────────────────────────────────────
#
# ExfoNewProprietaryBlock: a 36-byte header, then [uint32 length][zlib chunk]
# repeated.  The decompressed chunks concatenate into one field stream.  To
# patch a value we find which CHUNK holds it, patch inside that chunk's
# decompressed bytes, recompress that chunk only, and fix its length prefix --
# every other chunk stays byte-identical.  Re-chunking the whole stream would
# work too, but would touch bytes we have no reason to touch.

_PROP_HDR = 36


def _prop_chunks(body: bytes):
    """-> (header, [(raw_len_prefix_offset, decompressed_bytes), ...], tail)"""
    pos, out = _PROP_HDR, []
    while pos + 4 <= len(body):
        sz = struct.unpack_from('<I', body, pos)[0]
        if sz < 2 or pos + 4 + sz > len(body):
            break
        chunk = body[pos + 4:pos + 4 + sz]
        if chunk[:1] != b'\x78':
            break
        out.append((pos, zlib.decompress(chunk)))
        pos += 4 + sz
    return body[:_PROP_HDR], out, body[pos:]


def _prop_rebuild(header: bytes, chunks_dec, tail: bytes) -> bytes:
    out = bytearray(header)
    for dec in chunks_dec:
        comp = zlib.compress(dec)
        out += struct.pack('<I', len(comp)) + comp
    out += tail
    return bytes(out)


def _prop_find_float64(chunks_dec, name: bytes, current: float):
    """Locate the float64 field `name` -> (chunk_idx, offset_in_chunk).

    The stream's record layout is EXFO's, not documented; rather than assume
    it, find the field NAME and then the exact 8-byte little-endian double of
    the value the Bellcore side currently holds, within the next 64 bytes.
    That pair -- name, then this precise value -- cannot be a coincidence, and
    it cannot be a pseudo-field the decoder invents inside RawSamples because
    the search stops at that payload.  Exactly one hit is required."""
    needle = name + b'\x00'
    pat = struct.pack('<d', float(current))
    found = []
    for ci, dec in enumerate(chunks_dec):
        lo, hi = _rawsamples_span(dec)
        start = 0
        while True:
            i = dec.find(needle, start)
            if i < 0:
                break
            if lo is not None and lo <= i < hi:
                start = i + 1
                continue
            j = dec.find(pat, i, i + 64)
            if j >= 0:
                found.append((ci, j))
            start = i + 1
    if len(found) != 1:
        raise ValueError(f'expected exactly one {name!r} float64 = {current}, found {len(found)}')
    return found[0]


# Which proprietary float64 fields are DISTANCES IN METRES and therefore go
# stale when the IOR changes.  Enumerated by probe4.py against five real files;
# the uint32 twins of the cursors are sample INDICES and are not touched.
# Established in FastReporter, not assumed: with only the Bellcore group index
# changed FR still printed the old distances, and with the float64 `Ior` also
# changed it STILL printed the old distances -- FR reads these stored metres,
# it does not recompute them.
_PROP_METRE_FIELDS = (b'Position', b'Length', b'SpansLength',
                      b'CursorAPosition', b'CursorBPosition',
                      b'SubCursorAPosition', b'SubCursorBPosition',
                      b'CursorA', b'CursorB', b'SubCursorA', b'SubCursorB',
                      b'ManualZoomXMin', b'ManualZoomXMax')


def _rawsamples_span(stream: bytes):
    """[lo, hi) of the RawSamples PAYLOAD in the stream, or (None, None).

    Its record header (name_off, type, size) sits 12 bytes before the name;
    the payload is `size` bytes after the name's NUL.  Real geometry fields
    live AFTER this span -- `Ior` at ~144 k, `SpansLength` at ~50 k on a
    Defuniak file -- so the exclusion has to be this exact byte range, not
    'everything past the name'.  Inside it the decoder invents pseudo-fields
    (probe5.py); that is what we must never touch."""
    i = stream.find(b'RawSamples\x00')
    if i < 0 or i < 16:
        return None, None
    self_off, tc, sz = struct.unpack_from('<III', stream, i - 16)
    if self_off != i:
        return None, None
    lo = i + len(b'RawSamples\x00')
    return lo, lo + sz


def _prop_float64_payloads(stream: bytes, names):
    """-> [(stream_offset_of_payload, current_value)] for every float64 record
    named in `names`, skipping anything inside the RawSamples payload.

    Record layout (secretsauce/exfo_proprietary_decoder.py, confirmed on real
    bytes): a 16-byte descriptor [self_off][type][size][next_ref] then
    `name\\0`, then the payload.  self_off must equal the name's own offset;
    a wrong layout guess therefore yields nothing rather than patching the
    wrong bytes -- which is exactly what happened the first time."""
    lo, hi = _rawsamples_span(stream)
    out = []
    for name in names:
        needle = name + b'\x00'
        start = 0
        while True:
            i = stream.find(needle, start)
            if i < 0:
                break
            start = i + 1
            if lo is not None and lo <= i < hi:
                continue
            # descriptor precedes the name by 16 bytes:
            #   [self_off][type][size][next_ref] Name\0 payload
            # self_off must point back at THIS name.  That also rejects a
            # substring hit -- `CursorAPosition\0` occurs inside
            # `SubCursorAPosition\0`, and for that hit self_off will not match.
            if i >= 16:
                self_off, tc, sz = struct.unpack_from('<III', stream, i - 16)
                if self_off == i and tc == 3 and sz == 8:
                    pay = i + len(needle)
                    out.append((pay, struct.unpack_from('<d', stream, pay)[0]))
    return out


def _stream_to_chunk(chunk_lens, off):
    """Absolute stream offset -> (chunk index, offset within that chunk)."""
    base = 0
    for ci, ln in enumerate(chunk_lens):
        if off < base + ln:
            return ci, off - base
        base += ln
    raise IndexError(off)


# ─── the proprietary stream as a RECORD TREE (for size-changing edits) ─────
#
# Layout, read off real bytes and confirmed on four files (574 / 574 / 642 /
# 1047 records):  a 16-byte descriptor [self_off][type][size][payload_off],
# then `name\0`, then `size` bytes of payload.  Every offset is ABSOLUTE in
# the decompressed stream.  Type 0 records are not containers -- their
# payload is an array of uint32 child offsets (OtdrFile -> one child at 41,
# Fiber0 -> sixteen).  Type 4 is a UTF-16LE string, NUL-terminated, `size`
# counting the bytes.  The 36-byte block header carries the stream length at
# +24, and the stream is chunked at exactly 32 KiB before compression.
#
# So changing a string's length means: rewrite that record's size and payload,
# add the delta to every descriptor offset and every child pointer that lies
# beyond the edit, re-chunk, recompress, and fix the header length.  Anything
# less and FR reads a stream whose pointers no longer land on records.

_PROP_CHUNK = 32768
_PROP_HDR_LEN_OFF = 24


def _prop_records(stream: bytes):
    """Every record, found by its descriptor pointing back at its own name."""
    recs, i, n = [], 16, len(stream)
    while i < n - 1:
        j = stream.find(b'\x00', i)
        if j < 0:
            break
        nm = stream[i:j]
        if 2 <= len(nm) < 100 and nm[:1].isalpha() and all(32 <= c < 127 for c in nm):
            so, tc, sz, pay = struct.unpack_from('<IIII', stream, i - 16)
            if so == i and pay == j + 1:
                recs.append({'desc': i - 16, 'name': nm.decode(), 'tc': tc,
                             'size': sz, 'pay': pay})
                i = j + 1
                continue
        i += 1
    return recs


def _prop_strings(stream: bytes):
    """-> [(record, value)] for the UTF-16 string records."""
    out = []
    for r in _prop_records(stream):
        if r['tc'] == 4:
            v = stream[r['pay']:r['pay'] + r['size']].decode('utf-16-le', 'replace').rstrip('\x00')
            out.append((r, v))
    return out


def _prop_set_string_payloads(stream: bytes, edits) -> bytes:
    """Apply {payload_offset: new_text} edits with full pointer rebasing.

    Edits are applied from the END of the stream backwards, so each delta only
    disturbs offsets past a point every earlier edit has already been placed
    before.  After each, every descriptor offset and every type-0 child pointer
    greater than the edit point moves by the delta."""
    recs = _prop_records(stream)
    by_pay = {r['pay']: r for r in recs}
    starts = {r['desc'] for r in recs}
    for r in recs:                        # the pointer hypothesis is a hard guard
        if r['tc'] == 0:
            for k in range(0, r['size'], 4):
                v = struct.unpack_from('<I', stream, r['pay'] + k)[0]
                if v not in starts and v != len(stream):
                    raise ValueError(f'type-0 {r["name"]!r} holds {v}, not a record offset; '
                                     'refusing a size-changing edit on this layout')
    s = bytearray(stream)
    for pay in sorted(edits, reverse=True):
        r = by_pay[pay]
        if r['tc'] != 4:
            raise ValueError(f'{r["name"]!r} is not a string record')
        new = edits[pay].encode('utf-16-le') + b'\x00\x00'
        delta = len(new) - r['size']
        s[pay:pay + r['size']] = new
        struct.pack_into('<I', s, r['desc'] + 8, len(new))         # its own size
        if delta:
            cut = pay + r['size']                                    # old end of payload
            # Rebase every descriptor past the cut.  Descriptors at or beyond
            # `cut` have physically moved by delta in the buffer; address them
            # through that shift, then fix the offsets they carry.
            shifted = []
            for q in recs:
                d = q['desc']
                if d >= cut:
                    d += delta
                so, tc, sz, pf = struct.unpack_from('<IIII', s, d)
                if so > pay:  so += delta
                if pf > pay:  pf += delta
                struct.pack_into('<IIII', s, d, so, tc, sz, pf)
                if tc == 0:
                    for k in range(0, sz, 4):
                        v = struct.unpack_from('<I', s, pf + k)[0]
                        if v > pay:
                            struct.pack_into('<I', s, pf + k, v + delta)
                shifted.append((q, d))
            # keep `recs` addressable for the next (earlier) edit: only descriptors
            # past this cut moved, and later edits are all EARLIER than this one,
            # so their own descriptors did not move.  Update the map anyway.
            for q, d in shifted:
                q['desc'] = d
                if q['pay'] > pay:
                    q['pay'] += delta
            by_pay = {q['pay']: q for q in recs}
    return bytes(s)


def _prop_rebuild_stream(header: bytes, stream: bytes) -> bytes:
    """Re-chunk at 32 KiB, recompress, and write the stream length into the header."""
    h = bytearray(header)
    struct.pack_into('<I', h, _PROP_HDR_LEN_OFF, len(stream))
    out = bytearray(h)
    for i in range(0, len(stream), _PROP_CHUNK):
        comp = zlib.compress(stream[i:i + _PROP_CHUNK])
        out += struct.pack('<I', len(comp)) + comp
    return bytes(out)


# What FastReporter's Identification tab actually reads.  GenParams is NOT it:
# an edit there changed nothing on screen.  These are the UTF-16 records it
# shows, found by matching every displayed string to the stream.  The
# "Name"/"Value" pairs are the Identifiers list; Cable ID's Value is EMPTY on
# every file seen, which is why FR showed it blank -- not an edit failing.
_PROP_ID_MAP = {
    'cable_id':   [('Cable', None), ('Value', 'Cable ID')],
    'fiber_id':   [('Identifier', 'first'), ('Value', 'Fiber ID')],
    'loc_a':      [('LocationA', None), ('Value', 'Location A')],
    'loc_b':      [('LocationB', None), ('Value', 'Location B')],
    'operator':   [('UserNameA', None)],
    'operator_b': [('UserNameB', None)],
    'customer':   [('CustomerName', None)],
    'company':    [('CompanyName', None)],
    'job_id':     [('Identifier', 'job')],
    'comment':    [('Comment', 'nonempty')],
}


def _prop_id_targets(stream: bytes, field: str):
    """Payload offsets of every proprietary record that carries `field`."""
    strs = _prop_strings(stream)
    out = []
    for rec_name, rule in _PROP_ID_MAP[field]:
        if rec_name == 'Value':
            # the Identifiers list: Value follows the Name record naming it
            for k, (r, v) in enumerate(strs):
                if r['name'] == 'Name' and v == rule and k + 1 < len(strs) \
                        and strs[k + 1][0]['name'] == 'Value':
                    out.append(strs[k + 1][0]['pay'])
        elif rec_name == 'Identifier':
            hits = [r for r, v in strs if r['name'] == 'Identifier']
            if rule == 'first' and hits:
                out.append(hits[0]['pay'])
            elif rule == 'job' and len(hits) > 1:
                out.append(hits[1]['pay'])
        elif rec_name == 'Comment':
            out.extend(r['pay'] for r, v in strs if r['name'] == 'Comment' and v)
        else:
            out.extend(r['pay'] for r, v in strs if r['name'] == rec_name)
    return out


# ─── edits ────────────────────────────────────────────────────────────────

def set_ior(data: bytes, new_ior: float, proprietary: bool = True) -> bytes:
    """Return a new file with the group index changed to `new_ior`.

    Bellcore side: the FxdParams group index, and the three distance twins
    (user_offset_dist, acq_offset_dist, acq_range_dist) scaled by old/new so
    they keep agreeing with their time fields.

    `proprietary=True` also patches EXFO's own float64 `Ior`.  Whether that is
    necessary for FastReporter is the experiment this flag exists to run.
    """
    if not (1.40 <= new_ior <= 1.55):
        raise ValueError(f'group index {new_ior} outside the sane band 1.40-1.55')
    mv, bl = split(data)
    fx = _find(bl, b'FxdParams')
    fo = fxdparams_offsets(fx.body)
    old_raw = struct.unpack_from('<I', fx.body, fo['group_index'])[0]
    old_ior = old_raw / 1e5
    new_raw = int(round(new_ior * 1e5))
    ratio = old_ior / new_ior                # distance scales by old/new

    body = bytearray(fx.body)
    struct.pack_into('<I', body, fo['group_index'], new_raw)
    for k in ('acq_offset_dist', 'acq_range_dist'):
        v = struct.unpack_from('<i', body, fo[k])[0]
        struct.pack_into('<i', body, fo[k], int(round(v * ratio)))
    fx.body = bytes(body)

    gp = _find(bl, b'GenParams')
    go = genparams_offsets(gp.body)
    body = bytearray(gp.body)
    v = struct.unpack_from('<i', body, go['user_offset_dist'])[0]
    struct.pack_into('<i', body, go['user_offset_dist'], int(round(v * ratio)))
    gp.body = bytes(body)

    if proprietary:
        for b in bl:
            if b.name.startswith(b'ExfoNewProprietaryBlock'):
                hdr, chunks, tail = _prop_chunks(b.body)
                decs = [d for _, d in chunks]
                ci, off = _prop_find_float64(decs, b'Ior', old_ior)
                d = bytearray(decs[ci])
                struct.pack_into('<d', d, off, float(new_ior))
                decs[ci] = bytes(d)
                # the stored metres: every one scales by old/new, and the
                # RawSamples payload must come through byte-identical
                stream = b''.join(decs)
                lo, hi = _rawsamples_span(stream)
                raw_before = stream[lo:hi] if lo is not None else b''
                lens = [len(x) for x in decs]
                bufs = [bytearray(x) for x in decs]
                n_scaled = 0
                for pay, val in _prop_float64_payloads(stream, _PROP_METRE_FIELDS):
                    if val != val or val == 0.0:      # NaN / unset: leave
                        continue
                    cj, o = _stream_to_chunk(lens, pay)
                    if o + 8 > lens[cj]:
                        raise ValueError('float64 straddles a chunk boundary')
                    struct.pack_into('<d', bufs[cj], o, val * ratio)
                    n_scaled += 1
                decs = [bytes(x) for x in bufs]
                after = b''.join(decs)
                if lo is not None and after[lo:hi] != raw_before:
                    raise ValueError('RawSamples payload changed; refusing to write')
                if n_scaled == 0:
                    raise ValueError('no proprietary metre fields found to scale')
                b.body = _prop_rebuild(hdr, decs, tail)
                b.scaled_fields = n_scaled
                break
    return build(mv, bl)


# ─── identifiers (the "trace name") ───────────────────────────────────────
#
# GenParams carries the human-facing text: cable id, fiber id, the two location
# codes, cable code, operator, comment.  They are NUL-terminated strings, so an
# edit changes the block's SIZE and the directory has to be re-laid -- which
# `build()` does.  They appear NOWHERE else: searched the proprietary stream
# for every one of them on a real file and found none, so this is a Bellcore-
# only edit with no second copy to go stale.

GENPARAMS_STRINGS = ('cable_id', 'fiber_id', 'loc_a', 'loc_b', 'cable_code',
                     'operator', 'comment')
# FR-side extras that GenParams has no slot for.  Editable, proprietary only.
EXTRA_STRINGS = ('operator_b', 'customer', 'company', 'job_id')
ALL_STRINGS = GENPARAMS_STRINGS + EXTRA_STRINGS


def read_identifiers(data: bytes) -> dict:
    _, bl = split(data)
    b = _find(bl, b'GenParams').body
    out, q = {}, 2
    for k in ('cable_id', 'fiber_id'):
        e = b.index(b'\x00', q); out[k] = b[q:e].decode('latin-1'); q = e + 1
    q += 4                                          # fiber type, wavelength
    for k in ('loc_a', 'loc_b', 'cable_code'):
        e = b.index(b'\x00', q); out[k] = b[q:e].decode('latin-1'); q = e + 1
    q += 2 + 8                                      # build condition, user offsets
    for k in ('operator', 'comment'):
        e = b.index(b'\x00', q); out[k] = b[q:e].decode('latin-1'); q = e + 1
    return out


def _enc(k: str, v: str) -> bytes:
    try:
        raw = v.encode('latin-1')
    except UnicodeEncodeError:
        raise ValueError(f'{k}: only Latin-1 text fits in a .sor')
    if b'\x00' in raw:
        raise ValueError(f'{k}: NUL is the string terminator and cannot be in the text')
    if len(raw) > 255:
        raise ValueError(f'{k}: {len(raw)} bytes is longer than any reader expects')
    return raw


def set_identifiers(data: bytes, **fields) -> bytes:
    """Return a new file with the given GenParams strings replaced.

    Any of GENPARAMS_STRINGS may be passed.  Everything else in the block --
    fiber type, wavelength, build condition, the user offsets -- is copied
    through untouched, byte for byte.

    A word on `fiber_id`: the suite identifies a fiber by its FILENAME, and the
    completeness auditor cross-checks that against this field.  Changing one
    without the other is allowed here, because a tech correcting a mislabelled
    file may need exactly that -- but it is the one identifier whose edit can
    make another tool disagree with the name on disk.
    """
    bad = set(fields) - set(ALL_STRINGS)
    if bad:
        raise ValueError(f'not editable identifiers: {sorted(bad)}')
    for k, v in fields.items():
        _enc(k, v)
    mv, bl = split(data)
    # FastReporter reads its Identification tab from the proprietary stream,
    # so that is rewritten first; GenParams below keeps the Bellcore side in
    # step for every reader that speaks it.
    for pb in bl:
        if pb.name.startswith(b'ExfoNewProprietaryBlock'):
            hdr, chunks, tail = _prop_chunks(pb.body)
            stream = b''.join(d for _, d in chunks) + tail
            edits = {}
            for k, v in fields.items():
                for pay in _prop_id_targets(stream, k):
                    edits[pay] = v
            if edits:
                stream = _prop_set_string_payloads(stream, edits)
                pb.body = _prop_rebuild_stream(hdr, stream)
            break
    fields = {k: v for k, v in fields.items() if k in GENPARAMS_STRINGS}
    if not fields:
        return build(mv, bl)
    gp = _find(bl, b'GenParams')
    b = gp.body
    cur = read_identifiers(data)
    new = {k: (_enc(k, fields[k]) if k in fields else cur[k].encode('latin-1'))
           for k in GENPARAMS_STRINGS}

    # re-emit the block around the fixed-width fields, which are copied verbatim
    q = 2
    e = b.index(b'\x00', q); e = b.index(b'\x00', e + 1)          # past cable, fiber
    q_fixed1 = e + 1                                              # fiber type + wl
    e = b.index(b'\x00', q_fixed1 + 4)
    e = b.index(b'\x00', e + 1); e = b.index(b'\x00', e + 1)     # past locA, locB, code
    q_fixed2 = e + 1                                              # build + offsets
    e = b.index(b'\x00', q_fixed2 + 10); e = b.index(b'\x00', e + 1)   # past op, comment
    tail = b[e + 1:]                                              # anything after

    out = b[:2]
    out += new['cable_id'] + b'\x00' + new['fiber_id'] + b'\x00'
    out += b[q_fixed1:q_fixed1 + 4]
    out += new['loc_a'] + b'\x00' + new['loc_b'] + b'\x00' + new['cable_code'] + b'\x00'
    out += b[q_fixed2:q_fixed2 + 10]
    out += new['operator'] + b'\x00' + new['comment'] + b'\x00'
    out += tail
    gp.body = bytes(out)
    return build(mv, bl)


# ─── safe write ───────────────────────────────────────────────────────────

def write(data: bytes, dst: str, src: str | None = None, overwrite: bool = False) -> str:
    """Write `data` to `dst`.  Refuses the source path and existing files."""
    dst_abs = os.path.abspath(dst)
    if src is not None and os.path.abspath(src) == dst_abs:
        raise ValueError('refusing to write over the source file')
    if os.path.exists(dst_abs) and not overwrite:
        raise FileExistsError(dst_abs)
    tmp = dst_abs + '.part'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, dst_abs)
    return dst_abs


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='edit a .sor and write a COPY')
    ap.add_argument('src')
    ap.add_argument('dst')
    ap.add_argument('--ior', type=float, default=None)
    for k in ALL_STRINGS:
        ap.add_argument('--' + k.replace('_', '-'), default=None)
    ap.add_argument('--bellcore-only', action='store_true',
                    help='leave the proprietary Ior untouched (experiment)')
    a = ap.parse_args()
    raw = open(a.src, 'rb').read()
    assert roundtrip_ok(raw), 'source does not round-trip byte-exact; refusing'
    out = raw
    if a.ior is not None:
        out = set_ior(out, a.ior, proprietary=not a.bellcore_only)
    ids = {k: getattr(a, k) for k in ALL_STRINGS if getattr(a, k) is not None}
    if ids:
        out = set_identifiers(out, **ids)
    if out is raw:
        ap.error('nothing to change')
    write(out, a.dst, src=a.src)
    print(f'{a.src} -> {a.dst}: IOR {read_ior(raw):.5f} -> {read_ior(out):.5f}, '
          f'{len(raw)} -> {len(out)} bytes, cksum ok={roundtrip_ok(out)}')
    if ids:
        print('  identifiers (GenParams):', {k: v for k, v in read_identifiers(out).items() if k in ids})
