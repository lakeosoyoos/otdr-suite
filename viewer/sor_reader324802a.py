#!/usr/bin/env python3
"""
SOR Trace Reader 324741a
=====================
Parses Bellcore SR-4731 (.sor) OTDR trace files and finds duplicates
by comparing the three EXFO event fields at each matched position:

    1. Splice loss (dB)       — must match within 0.005 dB
    2. Attenuation (dB/km)    — must match within 0.005 dB/km
    3. Distance (km)          — used to align events (0.06 km tolerance)

Every matched event must pass all three checks.  At least 75% of
events must match.  No statistics, no correlation — just direct
comparison of EXFO's own measurements at each splice point.

Usage:
    python sor_reader755.py /path/to/folder/
    python sor_reader755.py --compare fileA.sor fileB.sor
    python sor_reader755.py --scan /path/to/folder/
"""

import re
import struct
import os
import sys
import glob
import argparse
import numpy as np


# ─────────────────────────────────────────────────────────────────────
#  SOR binary parsing
# ─────────────────────────────────────────────────────────────────────

def _parse_block_directory(data):
    off = 0
    name_end = data.index(b'\x00', off) + 1
    off = name_end + 6
    num_blocks = struct.unpack_from('<H', data, off)[0]
    off += 2
    block_list = []
    for _ in range(num_blocks):
        ne = data.index(b'\x00', off) + 1
        nm = data[off:ne - 1].decode('latin-1')
        bv = struct.unpack_from('<H', data, ne)[0]
        bs = struct.unpack_from('<I', data, ne + 2)[0]
        block_list.append((nm, bv, bs))
        off = ne + 6
    seen = set()
    blocks = {}
    search_from = name_end + 2 + 4
    for nm, bv, bs in block_list:
        if nm in seen:
            continue
        seen.add(nm)
        needle = nm.encode('latin-1') + b'\x00'
        idx = data.find(needle, search_from)
        if idx >= 0:
            blocks[nm] = {
                'offset': idx, 'size': bs, 'ver': bv,
                'body': idx + len(needle),
            }
            search_from = idx + bs
    return blocks


def _parse_fxd_params(data, blocks):
    if 'FxdParams' not in blocks:
        return {}
    body = blocks['FxdParams']['body']
    date_time   = struct.unpack_from('<I', data, body)[0]
    units       = data[body + 4:body + 6].decode('latin-1')
    wavelength  = struct.unpack_from('<H', data, body + 6)[0]
    num_pw      = struct.unpack_from('<H', data, body + 16)[0]
    pw_end      = body + 18 + num_pw * 2
    acq_range   = struct.unpack_from('<I', data, pw_end)[0]
    # Pulse width actually used, in ns (uint16 per entry from +18; EXFO writes
    # exactly one).  Field map verified byte-for-byte on this span's own files:
    # WSCSUI long = 500 ns, WSCSUIsh short = 10 ns, both num_pw 1.  Mirrors
    # splicereport/sor_reader324802a.py's `fxd_pulse_ns`, which reads the same
    # bytes -- the two readers are deliberately isolated copies.
    #
    # The FR grid's column tolerance is derived from this: an OTDR cannot
    # resolve two events closer than a pulse length, so the pulse is the
    # natural scale for "could these two readings be the same point".
    pulse_ns = None
    if num_pw >= 1:
        try:
            pulse_ns = float(struct.unpack_from('<H', data, body + 18)[0]) or None
        except struct.error:
            pulse_ns = None
    # Duration: uint16 at +38 of FxdParams body, stored in whole
    # seconds (not deciseconds as SR-4731 nominally specifies — EXFO
    # writes the raw second-count here).  This is the "Duration"
    # field shown under Test Parameters → Summary in the EXFO viewer.
    # Field sits between NumberOfAverages (uint32 @ +34) and the next
    # block of acquisition metadata.  Verified against viewer: a SOR
    # whose viewer shows "15 s" has 15 at this offset (not 150).
    try:
        duration_sec = float(struct.unpack_from('<H', data, body + 38)[0])
    except struct.error:
        duration_sec = None
    # Acquisition offset: where sample 0 sits relative to the front panel,
    # int32 at +8 in the SAME time units as a KeyEvent's time of travel, with
    # its distance twin (0.1 m) at +12.  This is the file's own statement of
    # the trace origin; the viewer draws its x axis from it.  Every production
    # file seen stores 0 (sample 0 IS the port), which is exactly what the old
    # first-500-sample minimum hunt could not reproduce on a long-pulse shot.
    try:
        acq_offset, acq_offset_dist = struct.unpack_from('<ii', data, body + 8)
    except struct.error:
        acq_offset, acq_offset_dist = 0, 0
    return {
        'date_time': date_time, 'units': units,
        'wavelength': wavelength / 10.0, 'acq_range': acq_range,
        'acq_offset': acq_offset, 'acq_offset_dist': acq_offset_dist,
        'duration_sec': duration_sec,
        'fxd_pulse_ns': pulse_ns,
    }


# The group index lives at FxdParams body + 28, uint32 = IOR * 100000, and is
# only sane between these.  Same three numbers helixcal/sor_fields.py already
# uses for `read_stored_ior_from_blocks`; that module is the one place in the
# suite that had this right, and it says so in its own docstring -- it calls
# `_read_ior` "an unanchored scan of the first 1000 bytes" and keeps it as a
# last-resort fallback only.  These readers now agree with it.
_FXD_IOR_OFFSET = 28
_IOR_U32_SCALE = 100000.0
_IOR_SANE_MIN = 1.40
_IOR_SANE_MAX = 1.55


def _read_ior(data, blocks=None):
    """The group index the instrument actually used.

    ANCHORED to FxdParams, with the old scan kept only as a fallback.

    The scan walked the first 1000 bytes for any uint32 between 145000 and
    149000 and took the first hit, else returned 1.46820.  Two ways that is
    wrong, and correcting an IOR in software is exactly what exposes both:

      OUTSIDE THE WINDOW IT LIES QUIETLY.  A trace corrected to 1.440 or 1.496
      falls outside 1.45-1.49, so the scan returns 1.46820 and every distance
      is silently mis-scaled.  Measured on two probe files written at 1.440 and
      1.496: both read back as 1.46820 and produced IDENTICAL event distances
      (1.0061 / 1.0373 / 2.0440 km), which is the tell -- two different files
      cannot agree to four decimals by accident.

      IT TAKES THE FIRST PLAUSIBLE-LOOKING UINT32.  Nothing anchors it to the
      IOR field; any other value that happens to land in the band wins if it
      comes first.

    Benign on today's data and verified so: across 72 production files from 12
    folders the anchored read and the scan agree 72 out of 72, and every one of
    them is 1.47000.  Which is itself the point -- 1.47 against a true 1.467 is
    about 148 m over 74 km, and correcting that is the whole reason this had to
    stop being a guess.
    """
    if blocks is None:
        try:
            blocks = _parse_block_directory(data)
        except Exception:                       # noqa: BLE001 - not a SOR, or
            blocks = None                       # truncated; fall through
    if blocks:
        fxd = blocks.get('FxdParams') or {}
        body = fxd.get('body')
        if body is not None:
            try:
                ior = struct.unpack_from('<I', data, body + _FXD_IOR_OFFSET)[0] / _IOR_U32_SCALE
                if _IOR_SANE_MIN <= ior <= _IOR_SANE_MAX:
                    return ior
            except struct.error:
                pass
    # Fallback: the old scan.  Kept so a file this cannot anchor is no worse
    # off than before, never better -- it is a guess and stays one.
    for off in range(0, min(len(data), 1000)):
        try:
            val = struct.unpack_from('<I', data, off)[0]
            if 145000 <= val <= 149000:
                return val / 100000.0
        except struct.error:
            pass
    return 1.46820  # fallback


# ── The DECLARED span's launch offset, as the file states it ─────────────
#
# When a span is declared, FastReporter re-bases every KeyEvent so the span
# start is 0 and records how far that start sits into the raw acquisition in
# GenParams, as a time of travel like any event.  It is exact, and it is the
# number FR's own Spans by Distance dialog shows as "Launch fiber length":
#
#     launch_set (span set in FR)   file 1036.04 m   FR 1036.03    +0.01 m
#     FTH01 tie panel               file 1044.87 m   FR 1044.90    -0.03 m
#     PTL1PTL6 Reubensville         file 1029.43 m   FR 1029.40    +0.03 m
#
# Present on 40 of 40 span-declared fibers across both tie-panel folders, and
# zero on 52 of 52 fibers from three folders with no span -- so it is also the
# reliable way to ASK whether a file carries one.
#
# NOT read through _parse_block_directory.  That resolves a block by searching
# for its name from inside the map, and GenParams is the FIRST block, so the
# search lands on the directory ENTRY rather than the data: measured wrong on
# 25 of 25 files (FxdParams, further down, happens to come out right on all
# 25).  helixcal/sor_fields.py hit this first and documents it.  Blocks are
# laid out contiguously from the end of the map in directory order, so walking
# the sizes is what the format actually specifies.
def _block_body_offsets(data):
    """{block name: body offset}, by walking the directory's sizes.

    Correct for EVERY block, including the first one."""
    p = data.index(b'\x00') + 1
    _rev, mapsize, nblocks = struct.unpack_from('<HIH', data, p)
    p += 8
    ents = []
    for _ in range(nblocks - 1):
        e = data.index(b'\x00', p)
        nm = data[p:e].decode('latin-1')
        p = e + 1
        _bv, bs = struct.unpack_from('<HI', data, p)
        p += 6
        ents.append((nm, bs))
    off, out = mapsize, {}
    for nm, bs in ents:
        out[nm] = off + len(nm) + 1          # skip the block's own name + NUL
        off += bs
    return out


def _read_user_offset_km(data, ior=None):
    """Where a declared span starts, in the raw acquisition, or 0.0.

    0.0 means no span is declared -- which is what every ordinary file says."""
    try:
        body = _block_body_offsets(data).get('GenParams')
        if body is None:
            return 0.0
        p = body + 2                          # language code
        for _ in range(2):                    # cable id, fiber id
            p = data.index(b'\x00', p) + 1
        p += 4                                # fiber type + wavelength
        for _ in range(3):                    # location A, location B, cable code
            p = data.index(b'\x00', p) + 1
        p += 2                                # build condition
        uo = struct.unpack_from('<i', data, p)[0]
    except (ValueError, struct.error, IndexError):
        return 0.0
    if ior is None:
        ior = _read_ior(data)
    return (uo * 0.0299792458 / ior) / 1000.0


def _parse_key_events(data, blocks):
    """Parse the SR-4731 KeyEvents block.

    Each event carries the OTDR's per-event LSA marker positions
    (end_prev, start_curr, end_curr, start_next, peak_curr) — five
    uint32 time-of-travel values that define EXFO's exact LSA fit
    windows for that specific event.  Recording them lets us recompute
    splice_loss using the same fit windows the OTDR did, matching
    event-table values to within float precision."""
    if 'KeyEvents' not in blocks:
        return []
    body = blocks['KeyEvents']['body']
    num_events = struct.unpack_from('<H', data, body)[0]
    pos = body + 2
    IOR = _read_ior(data, blocks)
    events = []
    for _ in range(num_events):
        evnum      = struct.unpack_from('<H', data, pos)[0];      pos += 2
        # SIGNED, like the splice-report reader ('<i' at its line 332).  A
        # KeyEvent's time of travel goes NEGATIVE once a span has been declared
        # on the file: FastReporter re-bases every event so the span start is
        # 0, and anything ahead of it — the OTDR port, a launch connector —
        # lands before zero.  Read unsigned, -1528 came back as 4,294,965,768
        # and the event landed 87,594 km out.
        #
        # That number is all over our own comments as "the time-of-travel
        # artifact the viewer's reader still emits".  It is not an artifact and
        # the instrument does not emit it; it is this one letter.  Checked
        # against FastReporter on a file where it set the span: FR prints the
        # same event at -0.0311 km, and signed gives -31.16 m.
        tot        = struct.unpack_from('<i', data, pos)[0];      pos += 4
        slope      = struct.unpack_from('<h', data, pos)[0];      pos += 2
        splice     = struct.unpack_from('<h', data, pos)[0];      pos += 2
        refl       = struct.unpack_from('<i', data, pos)[0];      pos += 4
        evt_raw    = data[pos:pos + 8];                           pos += 8
        # Per-event LSA markers — time-of-travel values defining
        # EXFO's exact fit windows for this event.
        end_prev   = struct.unpack_from('<I', data, pos)[0];      pos += 4
        start_curr = struct.unpack_from('<I', data, pos)[0];      pos += 4
        end_curr   = struct.unpack_from('<I', data, pos)[0];      pos += 4
        start_next = struct.unpack_from('<I', data, pos)[0];      pos += 4
        peak_curr  = struct.unpack_from('<I', data, pos)[0];      pos += 4
        pos += 2   # padding
        evt_type = evt_raw.split(b'\x00')[0].decode('latin-1', errors='replace')
        dist_km = (tot * 0.02998 / IOR) / 1000.0
        events.append({
            'number':        evnum,
            'time_of_travel': tot,
            'dist_km':       round(dist_km, 4),
            'splice_loss':   splice / 1000.0,
            'reflection':    refl / 1000.0,
            'slope':         slope / 1000.0,
            'type':          evt_type,
            # Bellcore/Telcordia SR-4731 KeyEvents code `xy9999` (EXFO
            # appends `LS`).  First character = reflection class: '0'
            # non-reflective, '1' reflective, '2' SATURATED reflective —
            # the return drove the receiver to its ceiling, so the stored
            # reflectance is a floor.  '2' is a REFLECTIVE class; the old
            # `== '1'` hid it.  Census over 12,960 .sor / 7 spans, 125,468
            # events (first char ∈ {0,1,2} only): '0' 89,385 events with 2
            # reflectances (0.00%); '1' 34,393 with 87.76%; '2' 1,690 with
            # 100.0%, pinned at the ceiling (p10 −15.82 / med −15.64 / max
            # −15.18 dB, vs the −14.7 dB glass/air Fresnel limit) while '1'
            # spans −79.8…−11.6.  Also `2E` is a common end-of-fiber code
            # (858 of TUL↔BAR's 862 end events) that `is_end` below already
            # honours — a non-reflective fiber end is a contradiction.
            # Found via WSC↔SUI F34, whose `2F9999LS` launch connector at
            # −25.072 dB (worst on the cable) went unflagged.  Kept as an
            # explicit set so an unknown future code fails closed.
            # Mirrors splicereport/ and secretsauce/, which carry their own
            # deliberately isolated copies of this reader.
            'is_reflective': evt_type[:1] in ('1', '2'),
            'is_end':        evt_type[1:2] == 'E',
            # EXFO LSA marker time-of-travel values (0.1 ns units, same
            # scale as `time_of_travel`).  Convert to km via the same
            # formula: km = tot × 0.02998 / IOR / 1000.
            'tot_end_prev':   end_prev,
            'tot_start_curr': start_curr,
            'tot_end_curr':   end_curr,
            'tot_start_next': start_next,
            'tot_peak_curr':  peak_curr,
        })
    return events


def _find_reflective_span(events):
    """Find the fiber span from launch connector to end-of-fiber.

    The start is the first reflective event (1F) at or near distance 0 — the launch connector.
    The end is the 1E (end-of-fiber) event — NOT mid-span 1F events (which are breaks/reflections).
    """
    # Find launch: first 1F event at distance 0
    launch = None
    for e in events:
        if e['is_reflective'] and not e['is_end'] and e['time_of_travel'] == 0:
            launch = e
            break
    if launch is None:
        # Fallback: first reflective event
        for e in events:
            if e['is_reflective'] and not e['is_end']:
                launch = e
                break

    # Find end: the 1E (end-of-fiber) event
    end = None
    for e in events:
        if e['is_end']:
            end = e
            break

    # Fallback: if no 1E, use the last reflective event
    if end is None:
        reflective_all = [e for e in events if e['is_reflective']]
        if reflective_all:
            end = reflective_all[-1]

    if launch is None or end is None:
        return None

    return launch, end


def _parse_data_pts(data, blocks):
    if 'DataPts' not in blocks:
        return None, 0, 0
    body = blocks['DataPts']['body']
    total_pts  = struct.unpack_from('<I', data, body)[0]
    pts_trace  = struct.unpack_from('<I', data, body + 6)[0]
    scale      = struct.unpack_from('<H', data, body + 10)[0]
    data_start = body + 12
    if pts_trace > 500_000 or pts_trace < 10 or scale == 0:
        pts_trace = total_pts
        scale = 1000
        data_start = body + 4
        block_end = blocks['DataPts']['offset'] + blocks['DataPts']['size']
        pts_trace = (block_end - data_start) // 2
    raw = np.frombuffer(data[data_start:data_start + pts_trace * 2], dtype='<u2')
    return raw.astype(np.float64) / scale, pts_trace, scale


# ─────────────────────────────────────────────────────────────────────
#  EXFO Proprietary Block – richer event and calibration data
# ─────────────────────────────────────────────────────────────────────

_MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024  # 256 MB cap (real traces: a few MB)


def _zlib_decompress_capped(chunk, max_bytes=_MAX_DECOMPRESSED_BYTES):
    """zlib.decompress with a hard OUTPUT ceiling.  A malicious/corrupt block can
    inflate ~1000x (DEFLATE) to multiple GB and OOM the process — the Viewer
    parses SOR in-process, so it would take down the whole hub.  Raises zlib.error
    past the cap; every caller here already treats a raised decompress as a bad
    block and skips it."""
    import zlib
    d = zlib.decompressobj()
    out = d.decompress(chunk, max_bytes)
    if d.unconsumed_tail:
        raise zlib.error("decompressed output exceeds %d-byte cap (possible zlib bomb)"
                         % max_bytes)
    out += d.flush()
    if not d.eof:                # truncated/incomplete stream: raise like the old
        raise zlib.error("incomplete or truncated zlib stream")  # zlib.decompress did,
    return out                   # so callers resync instead of taking partial bytes


def _decompress_proprietary(data, blocks):
    """Decompress ExfoNewProprietaryBlock streams into a single byte string."""
    import zlib
    blk_name = None
    for name in blocks:
        if 'ExfoNewProprietaryBlock' in name:
            blk_name = name
            break
    if blk_name is None:
        return None
    blk = blocks[blk_name]
    raw = data[blk['body']:blk['offset'] + blk['size']]
    chunks = []
    pos = 36  # skip "AppReg Format Ex  \0\0" header
    while pos < len(raw) - 4:
        sz = struct.unpack_from('<I', raw, pos)[0]
        if sz < 2 or sz > len(raw) - pos - 4:
            break
        chunk = raw[pos + 4:pos + 4 + sz]
        if len(chunk) >= 2 and chunk[0] == 0x78:
            try:
                dec = _zlib_decompress_capped(chunk)
                chunks.append(dec)
                pos += 4 + sz
                continue
            except Exception:
                pass
        pos += 1
    return b''.join(chunks) if chunks else None


def _prop_f64(stream, name):
    """Read a named float64 field from the decompressed proprietary stream."""
    nb = name.encode() + b'\x00'
    idx = stream.find(nb)
    if idx < 16:
        return None
    type_code = struct.unpack_from('<I', stream, idx - 12)[0]
    data_size = struct.unpack_from('<I', stream, idx - 8)[0]
    if type_code != 3 or data_size != 8:
        return None
    val_off = idx + len(nb)
    if val_off + 8 > len(stream):
        return None
    return struct.unpack_from('<d', stream, val_off)[0]


def _prop_scalar(stream, name, want_type, want_size):
    """Read a named scalar, anchored on a real field boundary.

    WHY NOT _prop_f64 for a SHORT name.  That helper does a bare
    `find(name + NUL)`, which is safe only for long distinctive names.  A
    short one collides with the TAIL of a longer field -- the Splice Report
    engine hit this with `Rbs` matching inside `PeakReflectionToRbs\\0`, where
    the bytes 12 back from that tail are not a descriptor, so the read
    silently returned None.

    Field names always begin immediately after a NUL (the descriptor's
    next_ref is a small int, so its high byte reads as the terminator that
    ends the previous record).  Anchoring on that NUL rejects mid-name
    matches, and the descriptor type/size check then confirms the hit.  Every
    occurrence is tried, so a decoy earlier in the stream cannot mask the real
    field.

    `Ior` is three characters, which is exactly the risky case, and a silent
    None there sends the pitch back to a derivation that is wrong by up to
    1225 ppm.  _prop_f64 is left alone: its callers are long calibration
    names, and changing them is not this change's business.
    """
    if not stream:
        return None
    nb = name.encode() + b'\x00'
    needle = b'\x00' + nb
    pos = 0
    while True:
        idx = stream.find(needle, pos)
        if idx < 0:
            return None
        start = idx + 1                      # the name itself
        pos = start
        if start < 16:
            continue
        type_code = struct.unpack_from('<I', stream, start - 12)[0]
        data_size = struct.unpack_from('<I', stream, start - 8)[0]
        if type_code != want_type or data_size != want_size:
            continue
        val_off = start + len(nb)
        if val_off + want_size > len(stream):
            continue
        if want_type == 3 and want_size == 8:
            return struct.unpack_from('<d', stream, val_off)[0]
        if want_type == 1 and want_size == 4:
            return struct.unpack_from('<I', stream, val_off)[0]
        return None


# A proprietary-block field name: NUL-delimited, 2-79 chars, ASCII-printable,
# first character a letter.  The lookbehind is what makes this the same set of
# runs a NUL-by-NUL walk visits -- a match can only start just after a NUL, so
# a run longer than 79 cannot match on one of its own suffixes.
_PROP_NAME_RE = re.compile(rb'(?<=\x00)[A-Za-z][\x20-\x7E]{1,78}\x00')


def _parse_proprietary_block(data, blocks):
    """
    Decode the ExfoNewProprietaryBlock into calibration and event data.

    Field descriptor format in decompressed stream:
        [self_offset: 4B LE] [type_code: 4B LE] [data_size: 4B LE]
        [next_ref: 4B LE] FieldName\\0 [value_bytes]
    Type codes: 1=uint32, 2=binary array, 3=float64

    Trace encoding (RawSamples):
        loss_dB = 64.0 - raw_uint16 / 1024.0
        (ScaleFactor=1024, inverted vs standard DataPts which uses scale=1000)

    Returns None if the block is absent or undecodable.
    """
    stream = _decompress_proprietary(data, blocks)
    if not stream:
        return None

    # ── Scalar calibration / hardware fields ──
    cal = {}
    for name in ('SamplingPeriod', 'DisplayRange', 'InjectionLevel', 'ScaleFactor',
                 'SaturationLevel', 'BaseClockPeriod', 'NominalPulseWidth',
                 'CalibratedPulseWidth', 'PulseRiseTime', 'PulseFallTime',
                 'Bandwidth', 'TypicalApdGain', 'TypicalAnalogGain',
                 'NominalWavelength', 'ExactWavelength', 'InternalModuleReflection',
                 'FresnelCorrection', 'SaturationLevelLinear', 'RmsNoise',
                 'ModuleTemperature', 'ApdTemperature', 'NormalizationExponent',
                 'TimeToOutputConnector', 'UnfilteredRawDataRmsNoise',
                 'SpansLoss', 'SpansLength', 'TotalOrl'):
        v = _prop_f64(stream, name)
        if v is not None:
            cal[name] = v

    # NumberOfAverages is uint32
    nb = b'NumberOfAverages\x00'
    idx = stream.find(nb)
    if idx >= 16:
        tc = struct.unpack_from('<I', stream, idx - 12)[0]
        if tc == 1 and idx + len(nb) + 4 <= len(stream):
            cal['NumberOfAverages'] = struct.unpack_from('<I', stream, idx + len(nb))[0]

    # ── Parse EventTable entries (full-stream scan, metre units) ──
    # This walked only an 80 KB window starting at the 'EventTable' name and
    # filtered Position as KILOMETRES.  Both were wrong, and together they
    # left `exfo_events` holding one record on a real file: the names sit
    # near the head of the stream but the records themselves run tens of KB
    # further in (SEANOR109: names at 1.3 KB, records at 67-89 KB), and the
    # block stores Position in METRES, so `<= 500` dropped every event past
    # half a kilometre.  The Splice Report engine's copy was fixed for both;
    # this one was not, so the Viewer never had EXFO's own event values.
    #
    # The scan stays REGEX-based (PR #240, 46.5s -> 7.6s on a 1152-fibre
    # cable): a byte-at-a-time `stream.find(b'\x00', pos)` walk over the
    # whole stream lands in RawSamples' binary ~12,918 times per file and
    # costs a third of a trace load.  _PROP_NAME_RE is the same test in one
    # C-speed pass -- a NUL-preceded run of 2 to 79 printable ASCII bytes
    # starting with a letter.
    #
    # Widening its bounds from the 80 KB window to the whole stream costs
    # +30% here on its own, because RawSamples' payload is ~84% of the bytes
    # and the records sit BEYOND it (its name is at ~1.5 KB, the event
    # records run to ~138 KB).  The payload is a sized type-2 binary field,
    # so its extent is known exactly and can be stepped over rather than
    # scanned -- it holds no field names (0 regex hits measured), so nothing
    # is lost.  A malformed or out-of-range size falls back to scanning the
    # whole stream: slower, never wrong.
    _spans = [(0, len(stream))]
    _ri = stream.find(b'RawSamples\x00')
    if _ri >= 16:
        _tc = struct.unpack_from('<I', stream, _ri - 12)[0]
        _dsz = struct.unpack_from('<I', stream, _ri - 8)[0]
        _vo = _ri + len(b'RawSamples\x00')
        if _tc == 2 and _dsz > 0 and _vo + _dsz <= len(stream):
            _spans = [(0, _vo), (_vo + _dsz, len(stream))]

    exfo_events = []
    current = None
    _KEEP = ('Length', 'Loss', 'Type', 'Status', 'CurveLevel', 'Reflectance',
             'PeakReflectionToRbs', 'LocalNoise',
             'SubCursorAPosition', 'CursorAPosition',
             'CursorBPosition', 'SubCursorBPosition')

    for m in (m for _lo, _hi in _spans
              for m in _PROP_NAME_RE.finditer(stream, _lo, _hi)):
        pos, end = m.start(), m.end() - 1          # m.end() is past the NUL
        try:
            name = stream[pos:end].decode('ascii')
        except UnicodeDecodeError:
            continue
        if name != 'Position' and name not in _KEEP:
            continue

        type_code = data_size = 0
        if pos >= 16:
            type_code = struct.unpack_from('<I', stream, pos - 12)[0]
            data_size = struct.unpack_from('<I', stream, pos - 8)[0]

        val_off = end + 1
        value = None
        if type_code == 3 and data_size == 8 and val_off + 8 <= len(stream):
            value = struct.unpack_from('<d', stream, val_off)[0]
        elif type_code == 1 and data_size == 4 and val_off + 4 <= len(stream):
            value = struct.unpack_from('<I', stream, val_off)[0]
        if value is None:
            continue

        if name == 'Position':
            if current is not None and len(current) > 2:
                exfo_events.append(current)
            current = {'Position': value}
        elif current is not None:
            current[name] = value

    if current is not None and len(current) > 2:
        exfo_events.append(current)

    # Positions are METRES.  A record is an EVENT iff the truck wrote a
    # CurveLevel for it; the interleaved section records (no CurveLevel) are
    # kept too, tagged, so a consumer can pair each event with the section
    # that ends at it.  This replaces a "Loss arrived before Type" heuristic
    # that mis-tagged any event whose Type field the scan had not reached.
    kept = []
    for e in exfo_events:
        p_m = e.get('Position')
        if not isinstance(p_m, float) or not (-1.0 <= p_m <= 500_000.0):
            continue
        e['_is_section'] = 'CurveLevel' not in e
        kept.append(e)
    exfo_events = kept

    # Sample pitch, pinned by the file itself.  FastReporter's per-event
    # marker Lengths are whole numbers of samples, so the population fixes the
    # pitch far more precisely than any single field -- it is carried here as
    # an independent CHECK on the IOR-derived pitch, not as its replacement
    # (a file with too few usable markers yields nothing, and the caller must
    # still work).
    res_m_exact = None
    _sp = cal.get('SamplingPeriod')
    if _sp and _sp > 0:
        _seed = 299_792_458.0 * float(_sp) / 2.0 / 1.4682
        _cands = []
        for e in exfo_events:
            L = e.get('Length')
            if isinstance(L, float) and 50.0 < L < 3000.0:
                n = round(L / _seed)
                if n >= 10:
                    _cands.append(L / n)
        if len(_cands) >= 3:
            _cands.sort()
            res_m_exact = float(_cands[len(_cands) // 2])

    exact_wl = cal.get('ExactWavelength')
    return {
        'calibration':       cal,
        'exfo_events':       exfo_events,
        'spans_loss':        cal.get('SpansLoss'),
        'spans_length':      cal.get('SpansLength'),
        'total_orl':         cal.get('TotalOrl'),
        'sampling_period':   cal.get('SamplingPeriod'),
        'exact_wavelength_nm': exact_wl * 1e9 if exact_wl else None,
        'injection_level':   cal.get('InjectionLevel'),
        'saturation_level':  cal.get('SaturationLevel'),
        # FastReporter's own group index, float64.  The Bellcore FxdParams
        # copy is a uint32 x 1e5, so it quantises to 5 dp (1.46832) where this
        # carries 6 (1.468325).  Anchored read -- see _prop_scalar.
        'ior':               _prop_scalar(stream, 'Ior', 3, 8),
        'res_m_exact':       res_m_exact,
    }


# ─────────────────────────────────────────────────────────────────────
#  Public parse API
# ─────────────────────────────────────────────────────────────────────

def parse_sor(filepath, trim=True):
    with open(filepath, 'rb') as f:
        data = f.read()
    blocks = _parse_block_directory(data)
    trace, pts_trace, scale = _parse_data_pts(data, blocks)
    if trace is None:
        return None
    if not trim:
        return trace
    fxd = _parse_fxd_params(data, blocks)
    events = _parse_key_events(data, blocks)
    span = _find_reflective_span(events)
    if span is None or fxd.get('acq_range', 0) == 0:
        return trace
    start_evt, end_evt = span
    acq_range = fxd['acq_range']
    si = int(round(start_evt['time_of_travel'] * pts_trace / (2 * acq_range)))
    ei = int(round(end_evt['time_of_travel']   * pts_trace / (2 * acq_range)))
    si = max(0, min(si, len(trace) - 1))
    ei = max(si, min(ei, len(trace) - 1))
    return trace[si:ei + 1]




def _sor_ior_from_events(sor_data, default=1.46820):
    """Derive the exact IOR used by the OTDR from any event with a
    known time_of_travel and dist_km.  The Bellcore formula is

        dist_km = (tot * 0.02998 / IOR) / 1000

    so IOR = tot × 0.02998 / dist_km / 1000 (assuming a non-launch
    event with non-zero tot and non-zero dist_km).  Falls back to
    `default` (1550 nm typical) when no usable event is available."""
    for e in (sor_data.get('events') or []):
        tot = e.get('time_of_travel')
        dk  = e.get('dist_km')
        if tot and tot > 0 and dk and dk > 0.5:
            try:
                ior = (tot * 0.02998) / (dk * 1000.0)
                if 1.40 < ior < 1.55:
                    return float(ior)
            except Exception:
                continue
    return default






def parse_sor_full(filepath, trim=True):
    with open(filepath, 'rb') as f:
        data = f.read()
    blocks = _parse_block_directory(data)
    full_trace, pts_trace, scale = _parse_data_pts(data, blocks)
    if full_trace is None:
        return None
    fxd    = _parse_fxd_params(data, blocks)
    events = _parse_key_events(data, blocks)
    span   = _find_reflective_span(events)
    acq_range = fxd.get('acq_range', 0)
    si, ei = 0, len(full_trace) - 1
    if trim and span is not None and acq_range > 0:
        start_evt, end_evt = span
        si = int(round(start_evt['time_of_travel'] * pts_trace / (2 * acq_range)))
        ei = int(round(end_evt['time_of_travel']   * pts_trace / (2 * acq_range)))
        si = max(0, min(si, len(full_trace) - 1))
        ei = max(si, min(ei, len(full_trace) - 1))
    trace = full_trace[si:ei + 1]
    result = {
        'filename': os.path.basename(filepath), 'filepath': filepath,
        'num_points': len(trace), 'trace': trace,
        'min_db': float(trace.min()), 'max_db': float(trace.max()),
        'mean_db': float(trace.mean()), 'wavelength': fxd.get('wavelength'),
        'acq_range': acq_range, 'events': events,
        'start_index': si, 'end_index': ei,
        'full_points': len(full_trace),
        'date_time': fxd.get('date_time', 0),
        'duration_sec': fxd.get('duration_sec'),
        'fxd_pulse_ns': fxd.get('fxd_pulse_ns'),
        'fxd_acq_offset': fxd.get('acq_offset'),
        # 0.0 unless a span was declared on this file; see
        # _read_user_offset_km for why it is not read via the block dir.
        'user_offset_km': _read_user_offset_km(data, _read_ior(data, blocks)),
        # The group index the instrument used, read from FxdParams and
        # ANCHORED there (see _read_ior).  Upgraded to EXFO's float64 below
        # when the proprietary block carries it.  This exists so nothing has
        # to BACK-DERIVE the IOR out of an event's distance: that inversion
        # needs a usable event, and a fiber broken at the connector has none.
        'ior': _read_ior(data, blocks),
    }
    # ── Augment with EXFO proprietary block data when present ──
    prop = _parse_proprietary_block(data, blocks)
    if prop:
        result['exfo_calibration']    = prop['calibration']
        result['exfo_events']         = prop['exfo_events']
        result['exfo_spans_loss']     = prop['spans_loss']
        result['exfo_spans_length']   = prop['spans_length']
        result['exfo_total_orl']      = prop['total_orl']
        result['exfo_sampling_period']= prop['sampling_period']
        result['exfo_wavelength_nm']  = prop['exact_wavelength_nm']
        result['exfo_injection_level']= prop['injection_level']
        result['exfo_saturation_level']= prop['saturation_level']
        result['exfo_res_m']          = prop['res_m_exact']
        # EXFO's float64 Ior carries 6 dp (1.468325) where the Bellcore
        # group index quantises to 5 (1.46832).  Prefer it; the FxdParams
        # read above stays as the fallback already set.
        if prop.get('ior') is not None:
            result['ior'] = float(prop['ior'])
    else:
        result['exfo_calibration']     = None
        result['exfo_events']          = None
        result['exfo_spans_loss']      = None
        result['exfo_spans_length']    = None
        result['exfo_total_orl']       = None
        result['exfo_sampling_period'] = None
        result['exfo_wavelength_nm']   = None
        result['exfo_injection_level'] = None
        result['exfo_saturation_level']= None
        result['exfo_res_m']           = None
        # 'ior' keeps the FxdParams value set above -- never None here.

    # ── Full-precision event values from EXFO's own block ──────────────────
    # The Viewer's event table sits beside the Splice Report's and must print
    # the same numbers.  It did not: the Bellcore KeyEvents block stores loss
    # and reflectance as int16 (1 mdB quantum) and position as an integer
    # time-of-travel converted with 0.02998 m/unit, which is 25.6 ppm long
    # against the true 0.0299792458 -- 2.8 m at 110 km, growing with distance.
    # EXFO's proprietary block carries the same events at full float64 and
    # FastReporter reads THAT block, so the Splice Report engine upgrades
    # from it (PR #247).  This is the same upgrade, same guards.
    #
    # Only applied when the two event lists align 1:1 AND each pair agrees on
    # position to <10 m, so a file whose proprietary block is truncated or
    # differently populated silently keeps the quantized value.
    _all = list(result.get('exfo_events') or [])
    _ex = [e for e in _all if not e.get('_is_section')]
    # The section that ENDS at event k is the one immediately before it in the
    # interleaved E,S,E,S stream -- i.e. the section whose Position is event
    # k-1's.  Keyed by the event it feeds so a stream with a missing or extra
    # record cannot shift every later slope by one.
    _sec_for = {}
    for _i, _r in enumerate(_all):
        if _r.get('_is_section') or _i + 2 >= len(_all):
            continue
        _nxt, _sec = _all[_i + 2], _all[_i + 1]
        if _nxt.get('_is_section') or not _sec.get('_is_section'):
            continue
        _np_, _sl, _sln = _nxt.get('Position'), _sec.get('Loss'), _sec.get('Length')
        if (isinstance(_np_, float) and isinstance(_sl, float) and _sl == _sl
                and isinstance(_sln, float) and _sln > 0.0):
            _sec_for[_np_] = _sl / _sln * 1000.0
    _ke = result.get('events') or []
    if _ex and len(_ke) == len(_ex):
        for _a, _b in zip(_ke, _ex):
            _p = _b.get('Position')
            # Alignment guard, evaluated against the ORIGINAL dist_km before
            # anything is upgraded.
            if (not isinstance(_p, float)
                    or abs(_a['dist_km'] * 1000.0 - _p) >= 10.0):
                continue
            _l = _b.get('Loss')
            # NaN-safe: `_l != _l` catches a NaN written into the block.  A
            # REFLECTIVE event legitimately has none -- FR stores no loss for
            # one -- so the loss upgrade is skipped while the position and
            # slope upgrades below still apply.
            if isinstance(_l, float) and _l == _l:
                _a['splice_loss'] = float(_l)
                _a['loss_full_precision'] = True
            elif isinstance(_l, float):
                # An EXPLICIT NaN: FastReporter took no loss reading at this
                # event and shows the cell empty.  The Bellcore KeyEvents copy
                # cannot say "no reading" -- its loss is an int16, so absence
                # arrives as a 0 -- and printing 0.000 where FR prints nothing
                # is a reading the instrument never made.  1,728 of 6,455
                # events on the Zayo 432 span, nearly all reflective.
                #
                # Recorded as a FLAG rather than by nulling splice_loss.  The
                # Splice Report engine reads the same field from its own copy
                # of this parser and does arithmetic on it, and the Viewer is
                # required to carry the same values it does (PR #248); moving
                # the value here would split the two.  The flag adds what the
                # int16 could not say and leaves every number alone, so the
                # DISPLAY layer can blank the cell and the engine can adopt it
                # separately.
                #
                # Only an explicit NaN sets it.  A record that simply does not
                # carry the field is untouched: absent is not the same claim
                # as "measured nothing".
                _a['fr_has_loss'] = False
            # FastReporter's own event kind, and its status bits.  Both are
            # read rather than inferred: the Viewer derived the kind from
            # sign(loss), which disagrees with FR's own Type on 4 of 4,727
            # events where FR's Type and the sign of its stored Loss disagree
            # with each other.  Reproducing FR means following FR.
            _t = _b.get('Type')
            if isinstance(_t, int):
                _a['fr_type'] = _t
            _st = _b.get('Status')
            if isinstance(_st, int):
                _a['fr_status'] = _st
            # Same 4-dp rounding as the Splice Report engine, so a fiber read
            # here and there gives the same number.
            _a['dist_km'] = round(_p / 1000.0, 4)
            _s = _sec_for.get(_p)
            if _s is not None:
                _a['slope'] = float(_s)
            # Reflectance is only upgraded where FR actually recorded one: a
            # NaN here means "not a reflective event", which `is_reflective`
            # already carries, and writing 0.0 over it would be
            # indistinguishable from a real reading.
            _rf = _b.get('Reflectance')
            if _rf is not None and _rf == _rf and _a.get('is_reflective'):
                _a['reflection'] = float(_rf)
    return result


# ─────────────────────────────────────────────────────────────────────
#  Duplicate detection
# ─────────────────────────────────────────────────────────────────────

# Tolerances — based on OTDR measurement repeatability
DIST_TOL   = 0.120    # km  — position matching window
SPLICE_TOL = 0.005    # dB  — max splice loss difference per event
SLOPE_TOL  = 0.005    # dB/km — max attenuation difference per event
MIN_MATCH  = 0.75     # fraction of events that must match


def _interior_events(events):
    """Splice events only — skip launch (dist=0), end-of-fiber,
    the far-end connector (last reflective non-end event),
    and any events beyond the end-of-fiber."""
    # Find the end-of-fiber event distance
    end_dist = None
    for e in events:
        if e['is_end']:
            end_dist = e['dist_km']
            break
    # Find the last 1F event (far-end connector)
    last_1f = None
    for e in reversed(events):
        if e['is_reflective'] and not e['is_end'] and e['dist_km'] > 0:
            last_1f = e
            break
    return [e for e in events
            if e['dist_km'] > 0
            and not e['is_end']
            and e is not last_1f
            and (end_dist is None or e['dist_km'] < end_dist)]


def compare_traces(events_a, events_b):
    """
    Compare two traces using the three EXFO event fields.

    1. Match events by distance (within DIST_TOL km).
    2. At each matched event, check:
       - |splice_loss_A - splice_loss_B| <= SPLICE_TOL
       - |slope_A - slope_B| <= SLOPE_TOL
    3. Every matched event must pass both checks.
    4. At least MIN_MATCH of events must be matched.

    Returns a result dict.
    """
    sa = sorted(_interior_events(events_a), key=lambda e: e['dist_km'])
    sb = sorted(_interior_events(events_b), key=lambda e: e['dist_km'])

    # Match by distance
    used_b = set()
    matched = []
    for a in sa:
        best_j, best_d = None, DIST_TOL + 1
        for j, b in enumerate(sb):
            if j in used_b:
                continue
            d = abs(a['dist_km'] - b['dist_km'])
            if d < best_d:
                best_d = d
                best_j = j
        if best_j is not None and best_d <= DIST_TOL:
            matched.append((a, sb[best_j]))
            used_b.add(best_j)

    unmatched_a = len(sa) - len(matched)
    unmatched_b = len(sb) - len(used_b)

    # Check all three fields at each matched event
    details = []
    all_pass = True
    worst_splice = 0.0
    worst_slope = 0.0

    for ea, eb in matched:
        sd = abs(ea['splice_loss'] - eb['splice_loss'])
        ad = abs(ea['slope'] - eb['slope'])
        splice_ok = sd <= SPLICE_TOL
        slope_ok  = ad <= SLOPE_TOL
        event_ok  = splice_ok and slope_ok

        if sd > worst_splice:
            worst_splice = sd
        if ad > worst_slope:
            worst_slope = ad
        if not event_ok:
            all_pass = False

        details.append({
            'dist_a':      ea['dist_km'],
            'dist_b':      eb['dist_km'],
            'splice_a':    ea['splice_loss'],
            'splice_b':    eb['splice_loss'],
            'splice_diff': round(sd, 4),
            'slope_a':     ea['slope'],
            'slope_b':     eb['slope'],
            'slope_diff':  round(ad, 4),
            'pass':        event_ok,
        })

    # Match ratio based on the larger trace
    max_events = max(len(sa), len(sb))
    match_ratio = len(matched) / max_events if max_events > 0 else 0

    # Decision
    is_duplicate = (len(matched) >= 3
                    and match_ratio >= MIN_MATCH
                    and all_pass)

    reason = 'PASS'
    if len(matched) < 3:
        reason = f'only {len(matched)} matched events'
    elif match_ratio < MIN_MATCH:
        reason = f'match ratio {len(matched)}/{max_events} = {match_ratio:.0%}'
    elif not all_pass:
        failing = [d for d in details if not d['pass']]
        worst = failing[0]
        if worst['splice_diff'] > SPLICE_TOL:
            reason = (f"splice diff {worst['splice_diff']:.4f} dB at "
                      f"{worst['dist_a']:.3f} km > {SPLICE_TOL} dB")
        else:
            reason = (f"slope diff {worst['slope_diff']:.4f} dB/km at "
                      f"{worst['dist_a']:.3f} km > {SLOPE_TOL} dB/km")

    return {
        'is_duplicate':    is_duplicate,
        'reason':          reason,
        'num_events_a':    len(sa),
        'num_events_b':    len(sb),
        'num_matched':     len(matched),
        'num_unmatched_a': unmatched_a,
        'num_unmatched_b': unmatched_b,
        'max_splice_diff': round(worst_splice, 4),
        'max_slope_diff':  round(worst_slope, 4),
        'match_ratio':     round(match_ratio, 2),
        'details':         details,
    }


def find_duplicates(meta):
    """Compare all pairs. Returns list of (name_a, name_b, result)."""
    names = list(meta.keys())
    n = len(names)
    dups = []
    for i in range(n):
        for j in range(i + 1, n):
            r = compare_traces(meta[names[i]]['events'], meta[names[j]]['events'])
            if r['is_duplicate']:
                dups.append((names[i], names[j], r))
    return dups


# ─────────────────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────────────────

def _print_exfo_table(events, label=''):
    """Print the EXFO-style event table."""
    if label:
        print(f'\n  ═══ {label} ═══  ({len(events)} events)')
    cum = 0.0
    prev = 0.0
    print(f'  {"#":>3s}  {"Type":8s}  {"Dist(km)":>9s}  {"Span(km)":>9s}  '
          f'{"Splice(dB)":>10s}  {"Refl(dB)":>10s}  {"Atten(dB/km)":>12s}  {"CumLoss(dB)":>11s}')
    print(f'  {"─"*3}  {"─"*8}  {"─"*9}  {"─"*9}  {"─"*10}  {"─"*10}  {"─"*12}  {"─"*11}')
    for e in events:
        span = e['dist_km'] - prev
        cum += e['splice_loss']
        refl = f"{e['reflection']:10.3f}" if e['reflection'] != 0 else f"{'':>10s}"
        print(f"  {e['number']:3d}  {e['type']:8s}  {e['dist_km']:9.3f}  {span:9.3f}  "
              f"{e['splice_loss']:+10.3f}  {refl}  {e['slope']:12.3f}  {cum:11.3f}")
        prev = e['dist_km']


def _print_comparison(result, na, nb):
    """Print a comparison result."""
    status = "DUPLICATE" if result['is_duplicate'] else "NOT DUPLICATE"
    print(f"\n  {na}  vs  {nb}  →  {status}")
    if not result['is_duplicate']:
        print(f"  Reason: {result['reason']}")
    print(f"  Events: A={result['num_events_a']}  B={result['num_events_b']}  "
          f"matched={result['num_matched']}  ({result['match_ratio']:.0%})  "
          f"unmatched: A={result['num_unmatched_a']} B={result['num_unmatched_b']}")
    print(f"  Worst splice diff: {result['max_splice_diff']:.4f} dB  "
          f"Worst slope diff: {result['max_slope_diff']:.4f} dB/km")

    if result['details']:
        print(f"\n  {'Dist A':>8s}  {'Dist B':>8s}  "
              f"{'Spl A':>7s}  {'Spl B':>7s}  {'ΔSpl':>7s}  "
              f"{'Slp A':>7s}  {'Slp B':>7s}  {'ΔSlp':>7s}  {'OK':>3s}")
        print(f"  {'─'*8}  {'─'*8}  {'─'*7}  {'─'*7}  {'─'*7}  "
              f"{'─'*7}  {'─'*7}  {'─'*7}  {'─'*3}")
        for d in result['details']:
            ok = '✓' if d['pass'] else '✗'
            print(f"  {d['dist_a']:8.3f}  {d['dist_b']:8.3f}  "
                  f"{d['splice_a']:+7.3f}  {d['splice_b']:+7.3f}  {d['splice_diff']:7.4f}  "
                  f"{d['slope_a']:7.3f}  {d['slope_b']:7.3f}  {d['slope_diff']:7.4f}  {ok:>3s}")


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='SOR reader + duplicate finder (v324741a)')
    ap.add_argument('path', nargs='?', help='.sor file or folder')
    ap.add_argument('--compare', nargs=2, metavar=('A', 'B'), help='Compare two files')
    ap.add_argument('--scan', metavar='FOLDER', help='Scan folder for duplicates')
    ap.add_argument('--full', action='store_true')
    args = ap.parse_args()

    if args.compare:
        ra = parse_sor_full(args.compare[0])
        rb = parse_sor_full(args.compare[1])
        if not ra or not rb:
            print("Failed to parse"); sys.exit(1)
        na = os.path.basename(args.compare[0]).replace('_1550.sor','').replace('.sor','')
        nb = os.path.basename(args.compare[1]).replace('_1550.sor','').replace('.sor','')
        _print_exfo_table(ra['events'], na)
        _print_exfo_table(rb['events'], nb)
        result = compare_traces(ra['events'], rb['events'])
        _print_comparison(result, na, nb)
        sys.exit(0)

    if args.scan:
        folder = args.scan
        files = sorted(glob.glob(os.path.join(folder, '*.sor')) +
                        glob.glob(os.path.join(folder, '*.SOR')))
        if not files:
            print(f"No .sor files in {folder}"); sys.exit(1)
        print(f"Loading {len(files)} traces ...")
        meta = {}
        for f in files:
            r = parse_sor_full(f)
            if r:
                short = os.path.basename(f).replace('_1550.sor','').replace('.sor','').replace('.SOR','')
                meta[short] = r
        print(f"Loaded {len(meta)} traces")
        n = len(meta)
        print(f"Comparing {n*(n-1)//2} pairs ...")
        dups = find_duplicates(meta)
        if dups:
            print(f"\n  {len(dups)} duplicate(s) found:")
            for a, b, r in dups:
                print(f"    {a} <-> {b}  matched={r['num_matched']}/{max(r['num_events_a'],r['num_events_b'])}  "
                      f"max_splice_Δ={r['max_splice_diff']:.4f} dB  "
                      f"max_slope_Δ={r['max_slope_diff']:.4f} dB/km")
        else:
            print("\n  No duplicates found.")
        sys.exit(0)

    if args.path is None:
        ap.print_help(); sys.exit(1)

    if os.path.isdir(args.path):
        files = sorted(glob.glob(os.path.join(args.path, '*.sor')) +
                        glob.glob(os.path.join(args.path, '*.SOR')))
        if not files:
            print(f"No .sor files in {args.path}"); sys.exit(1)
        print(f"Found {len(files)} .sor files\n")
        for f in files:
            r = parse_sor_full(f, trim=not args.full)
            if r:
                interior = _interior_events(r['events'])
                print(f"  {r['filename']:40s}  {len(r['events']):>2d} events  "
                      f"{len(interior):>2d} splices  {r['num_points']:>6d} pts")
            else:
                print(f"  {os.path.basename(f):40s}  FAILED")
    else:
        r = parse_sor_full(args.path, trim=not args.full)
        if not r:
            print(f"Failed to parse {args.path}"); sys.exit(1)
        print(f"File: {r['filename']}  Wavelength: {r['wavelength']:.1f} nm")
        _print_exfo_table(r['events'])


# ── Ported from splicereport/sor_reader324802a.py (PR#17 GenParams identity).
# Keep byte-identical with that copy — desktop/tests/test_viewer_fiber_identity.py
# locks the two sources together.
_GENPARAMS_MAX_STR = 256   # runaway-string cap, same guard as _parse_sup_params


def parse_genparams(src):
    """Parse the Bellcore GR-196 / SR-4731 v2 GenParams block — the file's
    IDENTITY metadata (what cable / fiber / route the tech told the OTDR it
    was shooting).

    `src` is either the file's raw bytes or a filesystem path.  Reads only a
    few hundred bytes of already-loaded data, so it is cheap enough to run on
    every file.

    Field order per GR-196 / SR-4731 v2 (all strings latin-1):
        language code (2 chars, NOT null-terminated), then null-terminated
        cable ID, fiber ID, then fiber type (int16) + nominal wavelength
        (int16) BINARY — two fields that must be SKIPPED, not string-scanned —
        then null-terminated originating location, terminating location.
        (cable code / current-data flag / offsets / operator / comment follow
        but carry no identity value for us.)

    The 2nd occurrence of b'GenParams' in the file is the block itself; the
    1st is its entry in the block-directory map at the top of the file.

    Returns {'cable_id','fiber_id','loc_a','loc_b'} — stripped strings, ''
    when a field is absent/blank (EXFO writes ' ' for empty fields) — or {}
    on ANY structural surprise (missing block, runaway string, truncation).
    Verified against real field files: tie-panel PTL1PTL60145.sor →
    cable_id 'PLT1PLT6', fiber_id '0145', loc 'PLT1'→'PLT6'; span shots
    (HOWLAN559.sor) put the whole 'HOWLAN559' name in fiber_id with empty
    locations.
    """
    try:
        if isinstance(src, (bytes, bytearray, memoryview)):
            data = bytes(src)
        else:
            with open(src, 'rb') as f:
                data = f.read()
    except (OSError, TypeError, ValueError):
        return {}
    try:
        i = data.find(b'GenParams')
        if i < 0:
            return {}
        i = data.find(b'GenParams', i + 1)     # 2nd occurrence = the block
        if i < 0:
            return {}
        o = i + len(b'GenParams') + 1          # skip block name + its NUL
        if o + 2 > len(data):
            return {}
        o += 2                                  # language code (2 chars, no NUL)

        def _cstr(off):
            e = data.index(b'\x00', off)        # ValueError when unterminated
            if e - off > _GENPARAMS_MAX_STR:
                raise ValueError('runaway GenParams string')
            return data[off:e].decode('latin-1', errors='replace'), e + 1

        cable_id, o = _cstr(o)
        fiber_id, o = _cstr(o)
        o += 4                                  # fiber type + nominal λ (2×int16, binary)
        if o >= len(data):
            return {}
        loc_a, o = _cstr(o)
        loc_b, o = _cstr(o)
        return {
            'cable_id': cable_id.strip(),
            'fiber_id': fiber_id.strip(),
            'loc_a':    loc_a.strip(),
            'loc_b':    loc_b.strip(),
        }
    except (ValueError, IndexError):
        return {}
