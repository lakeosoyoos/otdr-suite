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
    ap.add_argument('--ior', type=float, required=True)
    ap.add_argument('--bellcore-only', action='store_true',
                    help='leave the proprietary Ior untouched (experiment)')
    a = ap.parse_args()
    raw = open(a.src, 'rb').read()
    assert roundtrip_ok(raw), 'source does not round-trip byte-exact; refusing'
    out = set_ior(raw, a.ior, proprietary=not a.bellcore_only)
    write(out, a.dst, src=a.src)
    print(f'{a.src} -> {a.dst}: IOR {read_ior(raw):.5f} -> {read_ior(out):.5f}, '
          f'{len(raw)} -> {len(out)} bytes, cksum ok={roundtrip_ok(out)}')
