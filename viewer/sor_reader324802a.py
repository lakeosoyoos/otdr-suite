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
import math
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
    # job R long = 500 ns, job R short = 10 ns, both num_pw 1.  Mirrors
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
            # Found via job R F34, whose `2F9999LS` launch connector at
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



# ── FastReporter's settings panel ────────────────────────────────────────
# What FR shows for one trace under Test Parameters and Test Settings, read
# from the same fields FR reads.  Every name was matched against an FR panel
# of the same acquisition (the Splice Report's _parse_test_settings carries
# the census): Range is metres, NominalWavelength / NominalPulseWidth /
# SamplingPeriod are SI, Duration is whole seconds.  Resolution is not stored;
# FR prints the sample pitch, c x SamplingPeriod / (2 x Ior), which is 0.319 m
# on a 3.125 ns / 1.47 shot as its panel shows.
#
# A missing field stays None: the panel exists so a tech can check what the
# OTDR was told, and a made-up number there is worse than a blank one.
_PANEL_F64 = ('Ior', 'Rbs', 'HelixFactor', 'SpliceLossThreshold',
              'SplitterDetectionThreshold', 'ReflectanceThreshold',
              'EndOfFiberThreshold', 'Range', 'NominalWavelength',
              'NominalPulseWidth', 'SamplingPeriod')
_PANEL_U32 = ('SplitterDetection', 'Duration')


def read_test_panel(data):
    """FR's Test Parameters + Test Settings for one .sor, from its bytes.

    Returns {'wavelength_nm', 'range_km', 'pulse_ns', 'duration_s',
    'resolution_m', 'ior', 'backscatter_db', 'helix_pct',
    'splice_thr_db', 'splitter_on', 'splitter_thr_db', 'refl_thr_db',
    'eof_thr_db', 'fiber_type'}, each None when the file does not carry it.

    FR has an eighth Test Settings row, Fiber core size, that no field in the
    file holds; `fiber_type` is the GenParams glass code (652 = G.652) so the
    dialog can say what IS stored instead of guessing a micron figure.
    """
    out = dict.fromkeys(('wavelength_nm', 'range_km', 'pulse_ns', 'duration_s',
                         'resolution_m', 'ior', 'backscatter_db', 'helix_pct',
                         'splice_thr_db', 'splitter_on', 'splitter_thr_db',
                         'refl_thr_db', 'eof_thr_db', 'fiber_type'))
    try:
        blocks = _parse_block_directory(data)
    except Exception:
        return out
    try:
        stream = _decompress_proprietary(data, blocks)
    except Exception:
        stream = None
    f = {n: _prop_scalar(stream, n, 3, 8) for n in _PANEL_F64} if stream else {}
    u = {n: _prop_scalar(stream, n, 1, 4) for n in _PANEL_U32} if stream else {}
    fxd = {}
    try:
        fxd = _parse_fxd_params(data, blocks)
    except Exception:
        pass
    # FxdParams is the fallback for the three it also stores: a file with no
    # EXFO block still has a wavelength, a pulse and a duration.
    if f.get('NominalWavelength'):
        out['wavelength_nm'] = round(f['NominalWavelength'] * 1e9, 1)
    elif fxd.get('wavelength'):
        out['wavelength_nm'] = fxd['wavelength']
    if f.get('NominalPulseWidth'):
        out['pulse_ns'] = round(f['NominalPulseWidth'] * 1e9, 3)
    elif fxd.get('fxd_pulse_ns'):
        out['pulse_ns'] = fxd['fxd_pulse_ns']
    if u.get('Duration') is not None:
        out['duration_s'] = float(u['Duration'])
    elif fxd.get('duration_sec'):
        out['duration_s'] = fxd['duration_sec']
    if f.get('Range'):
        out['range_km'] = f['Range'] / 1000.0
    ior = f.get('Ior')
    if ior is None:
        try:
            ior = _read_ior(data, blocks)
        except Exception:
            ior = None
    out['ior'] = ior
    if f.get('SamplingPeriod') and ior:
        out['resolution_m'] = 299_792_458.0 * f['SamplingPeriod'] / 2.0 / ior
    for key, name in (('backscatter_db', 'Rbs'), ('helix_pct', 'HelixFactor'),
                      ('splice_thr_db', 'SpliceLossThreshold'),
                      ('splitter_thr_db', 'SplitterDetectionThreshold'),
                      ('refl_thr_db', 'ReflectanceThreshold'),
                      ('eof_thr_db', 'EndOfFiberThreshold')):
        out[key] = f.get(name)
    if u.get('SplitterDetection') is not None:
        out['splitter_on'] = bool(u['SplitterDetection'])
    # GenParams: language (2), cable id, fiber id, then fiber type int16.
    try:
        i = data.find(b'GenParams', data.find(b'GenParams') + 1)
        if i >= 0:
            o = i + len(b'GenParams') + 1 + 2
            o = data.index(b'\x00', o) + 1
            o = data.index(b'\x00', o) + 1
            ft = struct.unpack_from('<H', data, o)[0]
            out['fiber_type'] = ft or None
    except (ValueError, struct.error):
        pass
    return out

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
    return _parse_proprietary_stream(_decompress_proprietary(data, blocks))


def _parse_proprietary_stream(stream):
    """`_parse_proprietary_block` on an already-inflated field stream, so a
    .trc can hand over one wavelength's stream (see parse_trc)."""
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
        # Seed with the IOR the file STATES.  The vote snaps each marker to a
        # whole number of samples, which only corrects the seed while the
        # seed is within half a sample over the marker: n < 1/(2 x error).
        # A hardcoded 1.4682 against a stated 1.4700 is 1225 ppm, so any
        # marker past ~400 samples kept the seed's error instead of fixing
        # it.  At 0.78 ns sampling (5 ns tie-panel shots, 8 cm/sample) every
        # marker is 600-13,000 samples long and the vote returned its seed:
        # SNARCAAH 1 East/West (2026-09-23) and Dinwiddie ILA1-6, 1,212 ppm
        # off, against event peaks that sit a constant pulse-rise after
        # their markers at the stated pitch.
        _ior_s = _prop_scalar(stream, 'Ior', 3, 8)
        if not (isinstance(_ior_s, (int, float)) and 1.3 < float(_ior_s) < 1.7):
            _ior_s = 1.4682
        _seed = 299_792_458.0 * float(_sp) / 2.0 / float(_ior_s)
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


# ═══════════════════════════════════════════════════════════════════════
#  EXFO .trc reader (the Viewer's copy)
# ═══════════════════════════════════════════════════════════════════════
#
#  The Splice Report engine's sor_reader324802a carries the full reader and
#  its evidence; this is the same method cut down to what the Viewer draws,
#  and kept in step with it by test_viewer_trc.py.
#
#  A .trc is ONE direction shot at several wavelengths, in the same EXFO
#  container and object tree as a .sor's proprietary block, one Trace per
#  wavelength.  Each Trace subtree (plus the shared fiber identity) is cut
#  into its own one-trace stream and read by _parse_proprietary_stream as a
#  .sor's block would be; the few Bellcore fields the Viewer uses are rebuilt
#  from the tree.  Events come from the trace's own records, as KeyEvents
#  would carry them:
#    * Type 3 is reflective ('1'), '2' when Status 0x10 (saturated); Type 5
#      is an end past the acquisition range ('1O'); Status 0x80 is the end.
#    * A declared span keeps the OTDR port as an extra record 1 km upstream,
#      Status 0x08 without the launch bit 0x40.  KeyEvents omits it and
#      GenParams states the length: it is `user_offset_km`, not an event.
#    * DataPts is (65535 - raw) x 1000 // 1024 thousandths of a dB.

import zlib

_TRC_TOT_M = 0.0299792458            # metres per 100 ps, as KeyEvents counts
_TRC_TYPE_REFLECTIVE = 3
_TRC_TYPE_PAST_RANGE = 5
_TRC_STATUS_START = 0x08
_TRC_STATUS_SATURATED = 0x10
_TRC_STATUS_LAUNCH = 0x40
_TRC_STATUS_END = 0x80


def is_trc(filename):
    return str(filename).lower().endswith('.trc')


def _trc_stream_of(data):
    """The field stream of a .trc's bytes: the chunks after its SECOND
    'AppReg Format Ex' header.  b'' when it is not one."""
    inner = data.find(b'AppReg Format Ex', 1)
    if inner < 0:
        return b''
    off, out, total = inner + 36, [], 0
    while off + 4 <= len(data):
        size = struct.unpack_from('<I', data, off)[0]
        off += 4
        if size < 2 or off + size > len(data):
            break
        try:
            chunk = _zlib_decompress_capped(data[off:off + size])
        except zlib.error:
            break
        total += len(chunk)
        if total > _MAX_DECOMPRESSED_BYTES:
            raise ValueError('inflated .trc stream exceeds the cap')
        out.append(chunk)
        off += size
    return b''.join(out)


def _trc_node(stream, header):
    name_off, tc, dsz, voff = struct.unpack_from('<IIII', stream, header)
    end = stream.find(b'\x00', name_off)
    if end < 0 or end - name_off > 100:
        raise ValueError('bad field name')
    return stream[name_off:end].decode('ascii', 'replace'), tc, dsz, voff


def _trc_children(stream, header):
    _n, tc, dsz, voff = _trc_node(stream, header)
    if tc != 0 or voff + dsz > len(stream):
        return []
    return [struct.unpack_from('<I', stream, voff + 4 * k)[0] for k in range(dsz // 4)]


def _trc_walk(stream, root, skip=frozenset()):
    out, seen, todo = [], set(), [root]
    while todo:
        h = todo.pop()
        if h in seen or h in skip or h < 0 or h + 16 > len(stream):
            continue
        seen.add(h)
        try:
            _n, tc, dsz, voff = _trc_node(stream, h)
        except (ValueError, struct.error):
            continue
        out.append((h, voff + dsz))
        if tc == 0:
            todo.extend(_trc_children(stream, h))
    return out


def _trc_substreams(stream):
    """One stream per Trace, in file order: the whole tree minus the other
    traces' subtrees, fields copied whole so every name keeps the NUL before
    it and its type/size 12 and 8 bytes back."""
    i = stream.find(b'\x00OtdrFile\x00')
    if i < 0:
        raise ValueError('no OtdrFile record')
    root = i + 1 - 16
    traces = []
    for h, _end in _trc_walk(stream, root):
        name, tc, _d, _v = _trc_node(stream, h)
        if name == 'Traces' and tc == 0:
            traces = [c for c in _trc_children(stream, h)
                      if _trc_node(stream, c)[0].startswith('Trace')]
            break
    out = []
    for k in range(len(traces)):
        others = frozenset(h for j, h in enumerate(traces) if j != k)
        buf, last = bytearray(), -1
        for h, end in sorted(_trc_walk(stream, root, others)):
            start = max(h, last)
            if end > start:
                buf += stream[start:end]
                last = end
        out.append(bytes(buf))
    return out


def _trc_text(stream, name):
    """First UTF-16 (type 4) field called `name`, stripped, or ''."""
    needle = b'\x00' + name.encode() + b'\x00'
    pos = 0
    while True:
        i = stream.find(needle, pos)
        if i < 0:
            return ''
        start = i + 1
        pos = start
        if start < 16:
            continue
        tc, size = struct.unpack_from('<II', stream, start - 12)
        voff = start + len(needle) - 1
        if tc == 4 and voff + size <= len(stream):
            return (stream[voff:voff + size].decode('utf-16-le', errors='replace')
                    .split('\x00')[0].strip())


def _trc_records(stream, injection):
    """The trace's own event/section records, ALL of them (the block parser
    drops those more than 1 m before the span start, which is where a
    declared span's port record sits).  Records split on a position reset;
    the list whose first record's CurveLevel IS the trace's InjectionLevel is
    the trace's own."""
    keep = ('Length', 'Loss', 'Type', 'Status', 'CurveLevel', 'Reflectance',
            'SubCursorAPosition', 'CursorAPosition', 'CursorBPosition',
            'SubCursorBPosition')
    recs, cur = [], None
    for m in _PROP_NAME_RE.finditer(stream):
        pos, end = m.start(), m.end() - 1
        name = stream[pos:end].decode('ascii', 'replace')
        if name != 'Position' and name not in keep:
            continue
        if pos < 16:
            continue
        tc, dsz = struct.unpack_from('<II', stream, pos - 12)
        voff = end + 1
        if tc == 3 and dsz == 8 and voff + 8 <= len(stream):
            val = struct.unpack_from('<d', stream, voff)[0]
        elif tc == 1 and dsz == 4 and voff + 4 <= len(stream):
            val = struct.unpack_from('<I', stream, voff)[0]
        else:
            continue
        if name == 'Position':
            if cur is not None:
                recs.append(cur)
            cur = {'Position': val}
        elif cur is not None:
            cur.setdefault(name, val)
    if cur is not None:
        recs.append(cur)
    blocks, block, prev = [], [], None
    for r in recs:
        p = r['Position']
        if prev is not None and p < prev - 500.0:
            blocks.append(block)
            block = []
        prev = p
        block.append(r)
    if block:
        blocks.append(block)
    for b in blocks:
        head = b[0].get('CurveLevel')
        if injection is not None and head is not None and abs(head - injection) < 1e-6:
            for r in b:
                r['_is_section'] = 'CurveLevel' not in r
            return b
    return []


def _trc_finite(x):
    return isinstance(x, float) and x == x and abs(x) != float('inf')


def _trc_events(records, ior):
    """(KeyEvents-shaped events, declared span offset km)."""
    offset_km, port, evs = 0.0, None, []
    for r in records:
        if r['_is_section']:
            continue
        st = int(r.get('Status') or 0)
        if st & _TRC_STATUS_START and not st & _TRC_STATUS_LAUNCH:
            offset_km, port = -r['Position'] / 1000.0, r
            continue
        evs.append(r)
    slope_into = {}
    for i, r in enumerate(records):
        if r['_is_section'] or i + 2 >= len(records) or r is port:
            continue
        sec, nxt = records[i + 1], records[i + 2]
        if nxt['_is_section'] or not sec['_is_section']:
            continue
        sl, sln = sec.get('Loss'), sec.get('Length')
        if _trc_finite(sl) and _trc_finite(sln) and sln > 0.0:
            slope_into[id(nxt)] = sl / sln * 1000.0
    events = []
    for n, r in enumerate(evs, start=1):
        pos, t = r['Position'], r.get('Type')
        st = int(r.get('Status') or 0)
        refl_cls = t in (_TRC_TYPE_REFLECTIVE, _TRC_TYPE_PAST_RANGE)
        code = (('2' if st & _TRC_STATUS_SATURATED else '1') if refl_cls else '0') + \
               ('O' if t == _TRC_TYPE_PAST_RANGE else ('E' if st & _TRC_STATUS_END else 'F')) + \
               '9999LS'
        loss, refl = r.get('Loss'), r.get('Reflectance')
        e = {
            'number': n,
            'time_of_travel': int(round(pos * ior / _TRC_TOT_M)),
            'dist_km': round(pos / 1000.0, 4),
            'splice_loss': float(loss) if _trc_finite(loss) else 0.0,
            'reflection': float(refl) if _trc_finite(refl) else 0.0,
            'slope': slope_into.get(id(r), 0.0),
            'type': code,
            'is_reflective': code[:1] in ('1', '2'),
            'is_end': code[1:2] == 'E',
        }
        # As parse_sor_full does from the block: FR's own kind and status,
        # full-precision loss, and an explicit "no reading" when FR stored NaN.
        if isinstance(t, int):
            e['fr_type'] = t
        if isinstance(r.get('Status'), int):
            e['fr_status'] = st
        if _trc_finite(loss):
            e['loss_full_precision'] = True
        elif isinstance(loss, float):
            e['fr_has_loss'] = False
        events.append(e)
    return events, offset_km


def _trc_record(stream, filepath):
    prop = _parse_proprietary_stream(stream)
    raw = None
    i = stream.find(b'\x00RawSamples\x00') + 1
    if i >= 16:
        tc, dsz = struct.unpack_from('<II', stream, i - 12)
        voff = i + len('RawSamples') + 1
        if tc == 2 and dsz >= 4 and voff + dsz <= len(stream):
            raw = np.frombuffer(stream, dtype='<u2', count=dsz // 2, offset=voff)
    if not prop or raw is None:
        raise ValueError(f'{os.path.basename(filepath)}: a trace has no samples')
    ior = prop.get('ior') or 1.468325
    events, offset_km = _trc_events(_trc_records(stream, prop['injection_level']), ior)
    trace = ((65535 - raw.astype(np.int64)) * 1000 // 1024) / 1000.0
    wl = _prop_scalar(stream, 'Wavelength', 3, 8)
    pulse = _prop_scalar(stream, 'Pulse', 3, 8)
    exact = prop['exact_wavelength_nm']
    return {
        'filename': os.path.basename(filepath), 'filepath': filepath,
        'num_points': len(trace), 'trace': trace, 'full_points': len(trace),
        'start_index': 0, 'end_index': len(trace) - 1,
        'wavelength': round(exact, 1) if exact else (round(wl * 1e9, 1) if wl else None),
        '_trc_nominal_nm': round(wl * 1e9) if wl else None,
        'events': events,
        'fxd_pulse_ns': (pulse * 1e9) if pulse else None,
        # The tree has no acquisition offset: every trace starts at the port.
        'fxd_acq_offset': 0,
        'user_offset_km': offset_km,
        'ior': ior,
        'gen_fiber_id': _trc_text(stream, 'Identifier'),
        'gen_loc_a': _trc_text(stream, 'LocationA'),
        'gen_loc_b': _trc_text(stream, 'LocationB'),
        'exfo_calibration': prop['calibration'],
        'exfo_events': prop['exfo_events'],
        'exfo_spans_loss': prop['spans_loss'],
        'exfo_spans_length': prop['spans_length'],
        'exfo_total_orl': prop['total_orl'],
        'exfo_sampling_period': prop['sampling_period'],
        'exfo_wavelength_nm': exact,
        'exfo_injection_level': prop['injection_level'],
        'exfo_saturation_level': prop['saturation_level'],
        'exfo_res_m': prop['res_m_exact'],
    }


def parse_trc(filepath):
    """Every wavelength in a .trc, in file order, shaped like this module's
    parse_sor_full(trim=False).  ValueError on anything that is not a
    readable .trc."""
    with open(filepath, 'rb') as fh:
        stream = _trc_stream_of(fh.read())
    if not stream:
        raise ValueError(f'{os.path.basename(filepath)}: not an EXFO .trc')
    subs = _trc_substreams(stream)
    if not subs:
        raise ValueError(f'{os.path.basename(filepath)}: holds no traces')
    return [_trc_record(s, filepath) for s in subs]


def parse_trc_wavelength(filepath, wavelength_nm=None):
    """The wavelength nearest `wavelength_nm`; 1550 nm when none is asked for
    (the one every report runs at), else the file's first."""
    sides = parse_trc(filepath)
    want = 1550.0 if wavelength_nm is None else float(wavelength_nm)
    have = [s for s in sides if s.get('_trc_nominal_nm')]
    if not have:
        return sides[0]
    best = min(have, key=lambda s: abs(s['_trc_nominal_nm'] - want))
    if wavelength_nm is None and abs(best['_trc_nominal_nm'] - want) > 5.0:
        return sides[0]
    return best


def trc_head(data):
    """{'fiber_id', 'loc_a', 'loc_b'} from the first chunk of a .trc's bytes
    -- the fields the listing and the direction split need, without reading
    the traces.  {} when it is not a .trc."""
    inner = data.find(b'AppReg Format Ex', 1)
    if inner < 0 or inner + 40 > len(data):
        return {}
    size = struct.unpack_from('<I', data, inner + 36)[0]
    try:
        stream = _zlib_decompress_capped(data[inner + 40:inner + 40 + size])
    except (zlib.error, ValueError):
        return {}
    return {'fiber_id': _trc_text(stream, 'Identifier'),
            'loc_a': _trc_text(stream, 'LocationA'),
            'loc_b': _trc_text(stream, 'LocationB')}


# ─────────────────────────────────────────────────────────────────────
#  EXFO .olts (FTB-940/945 OLTS: loss, ORL and length per fiber)
# ─────────────────────────────────────────────────────────────────────
# The boss, 2026-10-01: the Viewer has to take these.  An .olts has no trace,
# only numbers, and EXFO does not store the numbers it prints: it stores the
# raw lock-in readings (amplitude and phase of a 106 kHz modulated source,
# at the far detector and at the source's own monitor) and computes loss,
# ORL and length when the file is opened.  The formulas below reproduce
# EXFO's own library to floating-point precision on all 864 fibers of the
# first file (loss and its average exactly, ORL within 1e-14 dB, length
# within 1e-10 m), and every printed cell of EXFO's PDF report for it.
#
# Container: an OLE compound file; storage OltsMeasures holds one stream per
# fiber, each a gzip of a .NET BinaryFormatter graph (MS-NRBF) of
# Metrino.Oltsx.OltsMeasurement.  The readings sit in that class's own
# packed byte fields ('fs', 'results', 'sources'), read here by offset.
#
# Verified only for what that file holds: Loopback reference, bidirectional,
# 1550 nm, single-mode.  Anything else is refused with a reason rather than
# read on a guess.

_OLTS_MAX_STREAM = 64 * 1024 * 1024
_OLTS_C = 299792458.0
# EXFO's group index for single-mode fiber (PhysicalFiberCharacteristics in
# its library): 1310 / 1550 / 1625 nm, else 1.468.  Not stored in the file.
_OLTS_IOR = {1310: 1.4677, 1550: 1.468325, 1625: 1.468734}


def is_olts(filename):
    return str(filename).lower().endswith('.olts')


def _cfb_streams(data, want=None):
    """{path: bytes} for the streams of an OLE compound file.  `want(path)`
    picks which streams to read (all by default).  ValueError when it is not
    one or is damaged."""
    if len(data) < 512 or data[:8] != b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
        raise ValueError('not an OLE compound file')
    ss = 1 << struct.unpack_from('<H', data, 0x1E)[0]
    mss = 1 << struct.unpack_from('<H', data, 0x20)[0]
    if ss not in (512, 4096) or mss != 64:
        raise ValueError('unexpected compound-file sector size')
    (n_fat, dir_start, _tx, cutoff, minifat_start, n_minifat,
     difat_start, n_difat) = struct.unpack_from('<IIIIIIII', data, 0x2C)
    nsec = (len(data) - ss) // ss
    per = ss // 4

    def sector(sid):
        if not 0 <= sid < nsec:
            raise ValueError('compound-file sector out of range')
        o = ss + sid * ss
        return data[o:o + ss]

    fat_sids = list(struct.unpack_from('<109I', data, 0x4C))
    sid, seen = difat_start, set()
    while sid < 0xFFFFFFFA and len(seen) <= n_difat:
        if sid in seen:
            raise ValueError('compound-file DIFAT loop')
        seen.add(sid)
        ent = struct.unpack('<%dI' % per, sector(sid))
        fat_sids += ent[:-1]
        sid = ent[-1]
    fat = []
    for s in fat_sids[:n_fat]:
        fat += struct.unpack('<%dI' % per, sector(s))

    def chain(start, table, limit):
        out, s = [], start
        while s < 0xFFFFFFFA:
            if s >= len(table) or len(out) > limit:
                raise ValueError('compound-file chain is broken')
            out.append(s)
            s = table[s]
        return out

    def read(start, size):
        if size > _OLTS_MAX_STREAM:
            raise ValueError('compound-file stream too large')
        buf = b''.join(sector(s) for s in chain(start, fat, nsec))
        return buf[:size]

    dir_buf = read(dir_start, nsec * ss)
    ents = []
    for o in range(0, len(dir_buf) - 127, 128):
        e = dir_buf[o:o + 128]
        nlen = struct.unpack_from('<H', e, 64)[0]
        name = e[:max(0, min(nlen, 64) - 2)].decode('utf-16-le', 'replace')
        etype = e[66]
        left, right, child = struct.unpack_from('<III', e, 68)
        start, size = struct.unpack_from('<II', e, 116)
        ents.append((name, etype, left, right, child, start, size))
    if not ents or ents[0][1] != 5:
        raise ValueError('compound file has no root entry')
    root = ents[0]
    mini = read(root[5], root[6]) if root[6] else b''
    minifat = []
    if n_minifat:
        mf = read(minifat_start, n_minifat * ss)
        minifat = list(struct.unpack('<%dI' % (len(mf) // 4), mf))

    def read_mini(start, size):
        idx = chain(start, minifat, len(mini) // mss + 1)
        return b''.join(mini[i * mss:(i + 1) * mss] for i in idx)[:size]

    out, todo, visited = {}, [(root[4], '')], set()
    while todo:
        i, prefix = todo.pop()
        if i == 0xFFFFFFFF or i >= len(ents) or i in visited:
            continue
        visited.add(i)
        name, etype, left, right, child, start, size = ents[i]
        todo += [(left, prefix), (right, prefix)]
        path = prefix + name
        if etype == 1:
            todo.append((child, path + '/'))
        elif etype == 2 and (want is None or want(path)):
            out[path] = read_mini(start, size) if size < cutoff else read(start, size)
    return out


class _NrbfRef:
    __slots__ = ('id',)

    def __init__(self, i):
        self.id = i


_NRBF_PRIM = {1: '<?', 2: '<B', 6: '<d', 7: '<h', 8: '<i', 9: '<q', 10: '<b',
              11: '<f', 12: '<q', 13: '<Q', 14: '<H', 15: '<I', 16: '<Q'}


class _Nrbf:
    """Just enough MS-NRBF (.NET BinaryFormatter) to turn an OltsMeasurement
    into dicts and lists.  A class becomes {'__class': name, member: value};
    an enum is {'value__': n}; a byte array is a list of ints."""

    def __init__(self, data):
        self.d, self.p, self.objs, self.meta = data, 0, {}, {}

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def i32(self):
        v = struct.unpack_from('<i', self.d, self.p)[0]
        self.p += 4
        return v

    def count(self):
        n = self.i32()
        if not 0 <= n <= len(self.d):
            raise ValueError('bad .NET array length')
        return n

    def lps(self):
        n, shift = 0, 0
        while True:
            b = self.u8()
            n |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                break
            if shift > 35:
                raise ValueError('bad .NET string length')
        s = self.d[self.p:self.p + n].decode('utf-8', 'replace')
        self.p += n
        return s

    def prim(self, t):
        if t in (5, 18):
            return self.lps()
        if t == 3:
            b = self.d[self.p]
            n = 1 if b < 0x80 else 2 if b < 0xE0 else 3 if b < 0xF0 else 4
            s = self.d[self.p:self.p + n].decode('utf-8', 'replace')
            self.p += n
            return s
        if t == 17:
            return None
        f = _NRBF_PRIM.get(t)
        if f is None:
            raise ValueError('bad .NET primitive type %d' % t)
        v = struct.unpack_from(f, self.d, self.p)[0]
        self.p += struct.calcsize(f)
        return v & 0x3FFFFFFFFFFFFFFF if t == 13 else v     # DateTime: ticks

    def typeinfo(self, bt):
        if bt in (0, 7):
            return self.u8()
        if bt == 3:
            return self.lps()
        if bt == 4:
            return (self.lps(), self.i32())
        return None

    def values(self, oid, name, members, bts, extra):
        obj = {'__class': name}
        self.objs[oid] = obj
        for m, bt, ex in zip(members, bts, extra):
            obj[m] = self.prim(ex) if bt == 0 else self.record()
        return obj

    def items(self, n):
        arr = []
        while len(arr) < n:
            r = self.record()
            if isinstance(r, tuple) and r and r[0] == 'NULLS':
                arr.extend([None] * r[1])
            else:
                arr.append(r)
        return arr

    def record(self):
        rt = self.u8()
        if rt == 0:                                  # stream header
            self.p += 16
            return self.record()
        if rt == 12:                                 # library
            self.i32()
            self.lps()
            return self.record()
        if rt in (2, 3, 4, 5):                       # class with its own metadata
            oid, name = self.i32(), self.lps()
            members = [self.lps() for _ in range(self.count())]
            if rt in (4, 5):
                bts = [self.u8() for _ in members]
                extra = [self.typeinfo(bt) for bt in bts]
            else:
                bts, extra = [2] * len(members), [None] * len(members)
            if rt in (3, 5):
                self.i32()
            self.meta[oid] = (name, members, bts, extra)
            return self.values(oid, name, members, bts, extra)
        if rt == 1:                                  # class reusing metadata
            oid, mid = self.i32(), self.i32()
            if mid not in self.meta:
                raise ValueError('bad .NET metadata reference')
            self.meta[oid] = self.meta[mid]
            return self.values(oid, *self.meta[mid])
        if rt == 6:
            oid = self.i32()
            s = self.objs[oid] = self.lps()
            return s
        if rt == 8:
            return self.prim(self.u8())
        if rt == 9:
            return _NrbfRef(self.i32())
        if rt == 10:
            return None
        if rt == 13:
            return ('NULLS', self.u8())
        if rt == 14:
            return ('NULLS', self.count())
        if rt == 15:
            oid, n, t = self.i32(), self.count(), self.u8()
            if t == 2:                               # byte[]: one slice, not n calls
                arr = list(self.d[self.p:self.p + n])
                self.p += n
            else:
                arr = [self.prim(t) for _ in range(n)]
            self.objs[oid] = arr
            return arr
        if rt in (16, 17):
            oid, n = self.i32(), self.count()
            arr = self.objs[oid] = self.items(n)
            return arr
        if rt == 7:
            oid, at, rank = self.i32(), self.u8(), self.i32()
            if not 1 <= rank <= 8:
                raise ValueError('bad .NET array rank')
            lens = [self.count() for _ in range(rank)]
            if at in (3, 4, 5):
                self.p += 4 * rank
            bt = self.u8()
            ex = self.typeinfo(bt)
            n = 1
            for k in lens:
                n *= k
            if n > len(self.d):
                raise ValueError('bad .NET array size')
            arr = [self.prim(ex) for _ in range(n)] if bt == 0 else self.items(n)
            self.objs[oid] = arr
            return arr
        if rt == 11:
            return StopIteration
        raise ValueError('unsupported .NET record type %d' % rt)

    def parse(self):
        first = None
        while self.p < len(self.d):
            r = self.record()
            if r is StopIteration:
                break
            if first is None and isinstance(r, dict):
                first = r
        return self.resolve(first, 0)

    def resolve(self, o, depth):
        if depth > 200:
            raise ValueError('.NET object graph too deep')
        if isinstance(o, _NrbfRef):
            return self.resolve(self.objs.get(o.id), depth + 1)
        if isinstance(o, dict):
            return {k: self.resolve(v, depth + 1) for k, v in o.items()}
        if isinstance(o, list) and o and not isinstance(o[0], int):
            return [self.resolve(v, depth + 1) for v in o]
        return o


def _gunzip_capped(data):
    import zlib
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(data, _OLTS_MAX_STREAM)
    if d.unconsumed_tail:
        raise ValueError('.olts measurement inflates past the cap')
    return out + d.flush()


def _olts_d(b, o):
    return struct.unpack_from('<d', b, o)[0] if 0 <= o and o + 8 <= len(b) else float('nan')


def _olts_pairs(blob):
    """The (detector, monitor) readings of one TxRx 'results' field: a list of
    {wl_nm, freq, det_amp, det_ph, mon_amp, mon_ph}, one per wavelength or
    modulation frequency.  Layout: version, count (u16), then 177-byte items
    of [flag, wavelength, detector reading, monitor reading, modulation]."""
    b = bytes(blob or b'')
    if len(b) < 3:
        return []
    n = struct.unpack_from('<H', b, 1)[0]
    out = []
    for i in range(n):
        o = 3 + 177 * i
        if o + 153 > len(b) or b[o] != 1 or b[o + 9] != 2 or b[o + 76] != 2:
            raise ValueError('unsupported .olts reading layout')
        wl = _olts_d(b, o + 1)
        out.append({'wl_nm': int(round(wl * 1e9)) if wl == wl else None,
                    'freq': _olts_d(b, o + 145),
                    'det_amp': _olts_d(b, o + 60), 'det_ph': _olts_d(b, o + 52),
                    'mon_amp': _olts_d(b, o + 127), 'mon_ph': _olts_d(b, o + 119)})
    return out


def _olts_txrx(acq, side):
    """Every reading of one direction ('ab' / 'ba') of an acquisition."""
    coll = (acq or {}).get(side) or {}
    out = []
    for t in coll.get('items') or []:
        if t:
            out += _olts_pairs(t.get('results'))
    return out


def _olts_ratio(p):
    if not p or not p['mon_amp'] or p['det_amp'] != p['det_amp'] or p['det_amp'] <= 0:
        return None
    return p['det_amp'] / p['mon_amp']


def _olts_source_refl(acq, side, wl_nm):
    """(internal reflectance, far-end reflectance) the ORL reading of `side`
    at `wl_nm` is corrected for: the two doubles after the source's exact
    wavelength in its 'sources' field."""
    for t in ((acq or {}).get(side) or {}).get('items') or []:
        b = bytes((t or {}).get('sources') or b'')
        for i in range(len(b) - 32):
            if b[i + 8] == 2 and abs(_olts_d(b, i) * 1e9 - wl_nm) < 0.01:
                return _olts_d(b, i + 17), _olts_d(b, i + 25)
    return None, None


def _olts_db(x):
    return -10.0 * math.log10(x) if x and x > 0 else None


def _olts_lin_avg(a, b):
    """EXFO's bidirectional loss: the mean of the two transmittances, in dB."""
    if a is None or b is None:
        return a if b is None else b
    return -10.0 * math.log10((10 ** (-a / 10.0) + 10 ** (-b / 10.0)) / 2.0)


def _olts_delay_sum(freqs, psi):
    """tau_AB + tau_BA from the phases psi(f) = -2 pi f (tau_AB + tau_BA) + 2 pi k:
    a search for the sum, then each frequency's absolute delay with its own
    whole turns, averaged (what EXFO does).  Unique to 1/gcd(f) = 4 ms."""
    f = np.asarray(freqs, float)
    p = np.asarray(psi, float)
    taus = np.arange(0.0, 4e-3, 1e-7)
    z = np.exp(1j * (p[None, :] + 2 * np.pi * f[None, :] * taus[:, None]))
    t = taus[int(np.abs(z.mean(1)).argmax())]
    a = np.vstack([np.ones_like(f), -2 * np.pi * f]).T
    for _ in range(4):
        r = np.angle(np.exp(1j * (p + 2 * np.pi * f * t)))
        t += np.linalg.lstsq(a, r, rcond=None)[0][1]
    k = np.round((-t * 2 * np.pi * f - p) / (2 * np.pi))
    return float(np.mean(-(p + 2 * np.pi * k) / (2 * np.pi * f)))


def _olts_length_m(m, ref_a, ref_b):
    ab = _olts_txrx(m.get('lengthAcquisition'), 'ab')
    ba = {round(x['freq']): x for x in _olts_txrx(m.get('lengthAcquisition'), 'ba')}
    ra = {round(x['freq']): x for x in ref_a}
    rb = {round(x['freq']): x for x in ref_b}
    f, psi = [], []
    for x in ab:
        k = round(x['freq'])
        if k not in ba or k not in ra or k not in rb:
            continue
        ref = (ra[k]['det_ph'] - ra[k]['mon_ph']) + (rb[k]['det_ph'] - rb[k]['mon_ph'])
        psi.append((x['det_ph'] - x['mon_ph'] - ref) + (ba[k]['det_ph'] - ba[k]['mon_ph'] - ref))
        f.append(x['freq'])
    if len(f) < 3 or not all(v == v for v in psi):
        return None
    wl = ab[0]['wl_nm']
    v = _OLTS_C / _OLTS_IOR.get(wl, 1.468)
    return v * _olts_delay_sum(f, psi) / 2.0


def _olts_ticks(t):
    """A .NET DateTime's ticks as a UTC datetime, None when unset."""
    import datetime as _dt
    if not t:
        return None
    return (_dt.datetime(1, 1, 1, tzinfo=_dt.timezone.utc)
            + _dt.timedelta(microseconds=int(t) // 10))


def _olts_thresholds(cfg):
    """[{set, kind, fiber_types, wl_nm, fail, enabled}] from the test
    configuration's packed field.  Each set is '$ct' (custom) or '$mt'
    (manual): a count, then 17-byte entries [2, 1, kind, ?, fiber types (u16),
    wavelength nm (u16, 0 = all), fail (double), enabled]."""
    b = bytes((cfg or {}).get('fs') or b'')
    out = []
    for tag in (b'$ct', b'$mt'):
        i = b.find(b'\x03' + tag)
        if i < 0 or i + 8 > len(b):
            continue
        n = struct.unpack_from('<H', b, i + 6)[0]
        o = i + 8
        for _ in range(min(n, 64)):
            if o + 17 > len(b) or b[o] != 2:
                break
            ft, wl = struct.unpack_from('<HH', b, o + 4)
            out.append({'set': tag.decode(), 'kind': b[o + 2], 'fiber_types': ft,
                        'wl_nm': wl, 'fail': _olts_d(b, o + 8), 'enabled': bool(b[o + 16])})
            o += 17
    return out


# Kind 3 is the only one seen so far: EXFO calls it Link ORL (a minimum).
_OLTS_KIND_LINK_ORL = 3


def _olts_measurement(m, ref):
    """One fiber's rows: {'id', 'when', 'rows': [{wl_nm, loss_ab, loss_ba,
    loss_avg, orl_a, orl_b}], 'length_m'}."""
    rows = []
    loss_ab = {x['wl_nm']: x for x in _olts_txrx(m.get('lossAcquisition'), 'ab')}
    loss_ba = {x['wl_nm']: x for x in _olts_txrx(m.get('lossAcquisition'), 'ba')}
    orl_ab = {x['wl_nm']: x for x in _olts_txrx(m.get('orlAcquisition'), 'ab')}
    orl_ba = {x['wl_nm']: x for x in _olts_txrx(m.get('orlAcquisition'), 'ba')}
    for wl in sorted(set(loss_ab) | set(loss_ba) | set(orl_ab) | set(orl_ba)):
        r_a, r_b = ref['loss_a'].get(wl), ref['loss_b'].get(wl)
        both = (r_a + r_b) if r_a is not None and r_b is not None else None
        la = lb = None
        if both is not None:
            if _olts_ratio(loss_ab.get(wl)):
                la = _olts_db(_olts_ratio(loss_ab[wl])) - both
            if _olts_ratio(loss_ba.get(wl)):
                lb = _olts_db(_olts_ratio(loss_ba[wl])) - both

        def orl(side, reading, r_own, loss):
            rr = _olts_ratio(reading)
            if rr is None or r_own is None or both is None or loss is None:
                return None
            r_int, r_far = _olts_source_refl(m.get('orlAcquisition'), side, wl)
            if r_int is None:
                return None
            r_int = r_int if r_int == r_int else 0.0
            r_far = r_far if r_far == r_far else 0.0
            t2 = 10 ** (-2 * loss / 10.0)
            return _olts_db(10 ** (2 * r_own / 10.0)
                            * (rr - r_int - r_far * 10 ** (-2 * both / 10.0) * t2))

        rows.append({'wl_nm': wl, 'loss_ab': la, 'loss_ba': lb,
                     'loss_avg': _olts_lin_avg(la, lb),
                     'orl_a': orl('ab', orl_ab.get(wl), r_a, la),
                     'orl_b': orl('ba', orl_ba.get(wl), r_b, lb)})
    length = None
    if m.get('lengthAcquisition'):
        length = _olts_length_m(m, ref['len_a'], ref['len_b'])
    return {'id': m.get('testName') or (m.get('fiberInformation') or {}).get('id') or '',
            'when': _olts_ticks(m.get('dateTime')), 'rows': rows, 'length_m': length}


def _olts_reference(m):
    """The loopback reference every measurement carries: per-wavelength
    reference loss of each unit (dB) and the test cords' length phases."""
    ref = m.get('reference') or {}
    if 'fsSxLpbk' not in ref:
        raise ValueError('this .olts uses a reference method other than Loopback, '
                         'which the Viewer does not read yet')

    def unit_loss(u):
        out = {}
        for t in (((ref.get(u) or {}).get('tc1Loss') or {}).get('items') or []):
            for p in _olts_pairs((t or {}).get('results')):
                if _olts_ratio(p):
                    out[p['wl_nm']] = _olts_db(_olts_ratio(p))
        return out

    def unit_len(u):
        out = []
        for t in (((ref.get(u) or {}).get('tc1Length') or {}).get('items') or []):
            out += _olts_pairs((t or {}).get('results'))
        return out

    # Each unit's reference power (dBm) per wavelength, FastReporter's
    # "Ref. A->B" / "Ref. B->A": the loopback field's per-wavelength items,
    # [1, wavelength nm (u16), then 1 + 8 doubles per unit, A then B], the
    # first double of each unit's being it.
    power = {}
    sx = bytes(ref.get('fsSxLpbk') or b'')
    if len(sx) >= 4:
        for i in range(min(struct.unpack_from('<H', sx, 2)[0], 16)):
            o = 4 + 133 * i
            if o + 133 > len(sx) or sx[o] != 1:
                break
            a = _olts_d(sx, o + 4) if sx[o + 3] == 1 else float('nan')
            b = _olts_d(sx, o + 69) if sx[o + 68] == 1 else float('nan')
            power[struct.unpack_from('<H', sx, o + 1)[0]] = (
                a if a == a else None, b if b == b else None)

    fs = bytes(ref.get('fs') or b'')
    when = None
    if len(fs) > 2 and len(fs) >= fs[1] + 11:
        when = _olts_ticks(struct.unpack_from('<q', fs, fs[1] + 3)[0] & 0x3FFFFFFFFFFFFFFF)
    return {'loss_a': unit_loss('unitA'), 'loss_b': unit_loss('unitB'),
            'len_a': unit_len('unitA'), 'len_b': unit_len('unitB'), 'power': power,
            'key': fs[2:2 + fs[1]].decode('ascii', 'replace') if len(fs) > 2 else '',
            'when': when, 'method': 'Loopback'}


def _olts_ids(m):
    """{name: value} of the measurement's custom ID configuration (job,
    units, calibration dates, ...), as EXFO's report header reads them."""
    out = {}
    for it in ((m.get('customIDConfigurationCollection') or {}).get('items') or []):
        if it and it.get('name'):
            out.setdefault(it['name'], it.get('value') or '')
    return out


def parse_olts(filepath):
    """Read an EXFO .olts.  Returns
      {'file', 'job', 'customer', 'company', 'units': {'A': {...}, 'B': {...}},
       'fibers': [{id, when, rows: [{wl_nm, loss_ab, loss_ba, loss_avg,
                   orl_a, orl_b}], length_m, ref}],
       'references': [{key, when, method, rows: [{wl_nm, ref_ab, ref_ba,
                       power_ab, power_ba}]}],
       'thresholds': [{kind, wl_nm, fail, ...}], 'wavelengths': [nm]}
    Times are UTC datetimes; dB and metres as floats (None where the file has
    no reading).  ValueError on anything that is not a readable .olts."""
    with open(filepath, 'rb') as fh:
        data = fh.read()
    try:
        streams = _cfb_streams(data, want=lambda p: p.startswith('OltsMeasures/'))
    except (ValueError, struct.error) as e:
        raise ValueError(f'{os.path.basename(filepath)}: not an EXFO .olts ({e})')

    def num(path):
        tail = path.rsplit('/', 1)[-1]
        return int(tail) if tail.isdigit() else 1 << 30

    fibers, refs, first, ths = [], {}, None, []
    for path in sorted(streams, key=num):
        try:
            m = _Nrbf(_gunzip_capped(streams[path])).parse()
        except Exception as e:                       # noqa: BLE001 - name the stream
            raise ValueError(f'{os.path.basename(filepath)}: measurement {path} '
                             f'is not readable ({e})')
        if not m or m.get('__class') != 'Metrino.Oltsx.OltsMeasurement':
            continue
        ref = _olts_reference(m)
        key = ref['key'] or str(len(refs))
        if key not in refs:
            refs[key] = ref
        fibers.append(dict(_olts_measurement(m, refs[key]), ref=key))
        if first is None:
            first = m
            ths = _olts_thresholds(m.get('testConfiguration'))
    if first is None:
        raise ValueError(f'{os.path.basename(filepath)}: holds no OLTS measurements')
    ids = _olts_ids(first)
    job = first.get('jobInformation') or {}

    def unit(u, role, op):
        info = first.get(u) or {}
        serial = info.get('serialNumber') or ''
        cal = ''
        for r in ('Main', 'Remote'):
            if ids.get(f'{r} unit serial number') == serial:
                cal = ids.get(f'{r} unit calibration date') or ''
        return {'role': role, 'operator': first.get(op) or '',
                'model': info.get('modelName') or '', 'serial': serial,
                'calibration': cal}

    wls = sorted({r['wl_nm'] for f in fibers for r in f['rows']})
    references = [{'key': k, 'when': r['when'], 'method': r['method'],
                   'rows': [{'wl_nm': wl, 'ref_ab': r['loss_a'].get(wl),
                             'ref_ba': r['loss_b'].get(wl),
                             'power_ab': r['power'].get(wl, (None, None))[0],
                             'power_ba': r['power'].get(wl, (None, None))[1]} for wl in wls]}
                  for k, r in refs.items()]
    return {'file': os.path.basename(filepath),
            'job': job.get('id') or ids.get('Job ID') or '',
            'customer': job.get('customerName') or ids.get('Customer') or '',
            'company': job.get('companyName') or ids.get('Company') or '',
            'units': {'A': unit('unitA', 'Location A', 'operatorA'),
                      'B': unit('unitB', 'Location B', 'operatorB')},
            'fibers': fibers, 'references': references,
            'thresholds': ths, 'wavelengths': wls}


def olts_orl_min(parsed, wl_nm):
    """The Link ORL minimum (dB) EXFO grades this wavelength against, or None:
    the custom set's enabled entry for that wavelength, else for all."""
    best = None
    for t in parsed.get('thresholds') or []:
        if t['set'] != '$ct' or t['kind'] != _OLTS_KIND_LINK_ORL or not t['enabled']:
            continue
        if t['wl_nm'] == wl_nm:
            return t['fail']
        if t['wl_nm'] == 0 and best is None:
            best = t['fail']
    return best
