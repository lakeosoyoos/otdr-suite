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

import struct
import os
import sys
import glob
import argparse
import zlib
from typing import Any, Optional

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
    # Backscatter coefficient (uint16 @ +32, tenths of a dB, stored as a
    # positive magnitude -> negative dB) and the pulse width actually used
    # (uint16 @ +18, ns).  SR-4731 quotes the coefficient for a 1 ns pulse,
    # so the level that governs a reflection's spike height at THIS
    # acquisition is bs_1ns + 10*log10(pulse_ns).  Both are needed by
    # `measure_reflectance_from_sor`; None when the block is short.
    #
    # Field map verified byte-for-byte on WSC_SUIsh (10 ns, 1546 nm):
    #   +16 n pulse widths=1   +18 pulse width=10 ns   +20 data spacing
    #   +24 n data points=15670 (== DataPts length)    +28 group index=146833
    #   +32 backscatter=819 (-81.9 dB)                 +38 averaging time=15 s
    # With -81.9 + 10*log10(10) = -71.9 dB the spike-height formula
    # reproduces FastReporter's reflectance on that span to ~0.5 dB
    # (24/24 launch connectors, median offset -0.48 dB, sd 0.14).
    try:
        bs_raw = struct.unpack_from('<H', data, body + 32)[0]
        backscatter_db = -(bs_raw / 10.0) if 0 < bs_raw < 2000 else None
    except struct.error:
        backscatter_db = None
    try:
        pulse_ns = float(struct.unpack_from('<H', data, body + 18)[0]) or None
    except struct.error:
        pulse_ns = None
    # Group index (IOR) — uint32 @ +32 of the field map above, scaled by
    # 100000 (147000 -> 1.47000).  This is the Bellcore-standard copy of the
    # refractive index; the EXFO proprietary block carries the same number as
    # a full float64 (`Ior`) and is preferred when present because it keeps
    # the sub-1e-5 digits FastReporter prints (1.468325 vs 1.46832).  Kept
    # here as the fallback for files with no readable proprietary block.
    try:
        gi_raw = struct.unpack_from('<I', data, body + 28)[0]
        group_index = gi_raw / 100000.0 if 100000 <= gi_raw <= 200000 else None
    except struct.error:
        group_index = None
    return {
        'date_time': date_time, 'units': units,
        'wavelength': wavelength / 10.0, 'acq_range': acq_range,
        'duration_sec': duration_sec,
        'backscatter_db': backscatter_db,
        'fxd_pulse_ns': pulse_ns,
        'group_index': group_index,
    }


def _parse_sup_params(data, blocks):
    """Parse the SR-4731 SupParams block (Supplier / OTDR module / mainframe
    identifying strings).  All fields are null-terminated latin-1 strings
    in this fixed order — what we surface as 'otdr_model' / 'otdr_serial'
    matches what the EXFO viewer shows under Test Parameters → OTDR Info.

    Field order per SR-4731:
        SupplierName, MainframeID, MainframeSN, ModuleID, ModuleSN,
        SoftwareRev, OtherInfo

    The module ID / serial is the right answer for both EXFO single-frame
    rigs (where they're populated) and benchtop systems (where they match
    the mainframe).  We fall back to mainframe when the module fields are
    blank.
    """
    if 'SupParams' not in blocks:
        return {}
    body = blocks['SupParams']['body']
    fields = []
    o = body
    # Read up to 7 null-terminated strings starting at body.
    for _ in range(7):
        try:
            e = data.index(b'\x00', o)
        except ValueError:
            break
        if e - o > 256:        # runaway / not actually a string
            break
        s = data[o:e].decode('latin-1', errors='replace').strip()
        fields.append(s)
        o = e + 1
    while len(fields) < 7:
        fields.append('')
    supplier, mainframe_id, mainframe_sn, module_id, module_sn, sw_rev, other = fields[:7]
    return {
        'sup_supplier':     supplier,
        'sup_mainframe_id': mainframe_id,
        'sup_mainframe_sn': mainframe_sn,
        'sup_module_id':    module_id,
        'sup_module_sn':    module_sn,
        'sup_software_rev': sw_rev,
        'sup_other':        other,
        # Promoted fields (module preferred, fallback to mainframe).
        'otdr_model':  module_id or mainframe_id or '',
        'otdr_serial': module_sn or mainframe_sn or '',
    }


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


def read_genparams_fiber_type(src):
    """Return GenParams' `fiber type` code (e.g. 652 for ITU-T G.652), or None.

    Deliberately NOT folded into parse_genparams(): that function's return
    dict is asserted key-for-key by desktop/tests/test_genparams_identity.py,
    and this field is identity metadata of a different kind (what glass, not
    which fiber).  Layout is the one documented in parse_genparams — the
    int16 immediately after the cable-id and fiber-id strings.
    """
    try:
        if isinstance(src, (bytes, bytearray, memoryview)):
            data = bytes(src)
        else:
            with open(src, 'rb') as f:
                data = f.read()
    except (OSError, TypeError, ValueError):
        return None
    try:
        i = data.find(b'GenParams')
        if i < 0:
            return None
        i = data.find(b'GenParams', i + 1)     # 2nd occurrence = the block
        if i < 0:
            return None
        o = i + len(b'GenParams') + 1 + 2      # block name + NUL + language
        for _ in range(2):                     # cable id, fiber id
            e = data.index(b'\x00', o)
            if e - o > _GENPARAMS_MAX_STR:
                return None
            o = e + 1
        return struct.unpack_from('<h', data, o)[0]
    except (ValueError, IndexError, struct.error):
        return None


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

# Metres of fiber per unit of stored time-of-travel, at IOR 1 — so
# dist_m = tot * _TOT_M_PER_UNIT / ior.  Exactly c / 1e10.
#
# The measurement paths carried the rounded 0.02998, which is 25.6 ppm long.
# That was INVISIBLE while the IOR they used was itself back-derived through
# the same constant: the round trip cancelled it exactly.  #247 made dist_km
# EXFO's own float64 Position, so there is no round trip left to cancel and
# the rounding became a real 25.6 ppm scale error.  Measured non-circularly
# on a .sor at the time: 0.02998 -> +25.6 ppm, this -> +0.4 ppm.
_TOT_M_PER_UNIT = 0.0299792458


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
        # SIGNED.  A time-of-travel can be NEGATIVE — an event the OTDR
        # places just BEFORE the trace origin, which is what a tie-panel
        # shot writes for its instrument-port event.  Read as unsigned, a
        # tot of -750 (about -15 m) becomes 4294966546, i.e. 87,593.94 km,
        # which then sorts first, poisons every span estimate, and puts the
        # event table out of order.  Measured across 918 files in 155
        # folders on disk: no legitimate acquisition comes within four
        # orders of magnitude of 2**31, and the ONLY files with a tot above
        # it are tie-panel / panel-to-panel sets (FTH01_FTH06,
        # Reubensville ILA, Tie Panel Sacrificial Jumpers) — 286 of 288
        # files per folder.  So this is a no-op everywhere else by
        # construction.  The three fields below it were already signed.
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
            # Bellcore/Telcordia SR-4731 KeyEvents code, `xy9999` (EXFO
            # appends `LS`).  The FIRST character is the reflection class:
            #     '0' = non-reflective
            #     '1' = reflective
            #     '2' = SATURATED reflective — the return was strong enough
            #           to drive the receiver to its ceiling, so the stored
            #           reflectance is a floor, not a measurement.
            # '2' is a REFLECTIVE class; reading it as non-reflective (the
            # pre-2026-08-19 `== '1'`) silently hid the worst connectors on
            # the cable.  Measured census over 12,960 .sor across 7 spans
            # (WSC↔SUI, ONT↔BOI, SAN↔DUR, SEA↔NOR, MIL↔TOP, TUL↔ORO,
            # TUL↔BAR) — 125,468 events, first character ∈ {0,1,2} only:
            #     '0'  89,385 events —      2 carry a reflectance ( 0.00%)
            #     '1'  34,393 events — 30,185 carry a reflectance (87.76%)
            #     '2'   1,690 events —  1,690 carry a reflectance (100.0%)
            # and the '2' population is pinned at the physical ceiling
            # (p10 −15.82, median −15.64, p90 −15.45, max −15.18 dB — the
            # glass/air Fresnel limit is ≈ −14.7 dB), while '1' spans
            # −79.8 … −11.6 dB.  A code that ALWAYS carries a reflectance
            # and only ever lands at the top of the scale cannot belong
            # with '0', which records one essentially never.
            # Corroborating: `2E` is common (828 of MIL↔TOP's and 858 of
            # TUL↔BAR's end-of-fiber events) and `is_end` below already
            # treats it as a genuine fiber end — an end-of-fiber event that
            # is not reflective is a contradiction.
            # The bug that found this: WSC↔SUI fiber 34's Suisun launch
            # connector is stored `2F9999LS` at −25.072 dB, the worst
            # reflectance on the cable and the only 2F in that span's 4,608
            # files; the field team flagged it, we did not.  Its twin F491
            # sits at the same position as a plain `1F` and flagged fine.
            # Membership is spelled out rather than `!= '0'` so an
            # unrecognised future code fails closed instead of defaulting
            # to reflective.
            'is_reflective': evt_type[:1] in ('1', '2'),
            'is_end':        evt_type[1:2] == 'E',
            # EXFO LSA marker time-of-travel values (0.1 ns units, same
            # scale as `time_of_travel`).  Convert to km with
            # `_TOT_M_PER_UNIT` and the file's STATED IOR — km =
            # tot × _TOT_M_PER_UNIT / ior / 1000 — which is the scale
            # `dist_km` ends up on once the float64 Position upgrade below
            # has run.  The 0.02998 above is the pre-upgrade seed and is
            # overwritten; do not copy it into a measurement path.
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
    """Read a named scalar from the proprietary stream, anchored on a real
    field boundary.

    WHY NOT _prop_f64.  That helper does a bare `find(name + NUL)`, which is
    safe only for long distinctive names.  Short ones collide with the TAIL of
    a longer field: `Rbs` matches inside `PeakReflectionToRbs\\0`, and because
    the bytes 12 back from that tail are not a descriptor, the read silently
    returns None — the backscatter row came out blank on files that plainly
    contain the field.

    Field names always begin immediately after a NUL (the descriptor's
    next_ref is a small int, so its high byte reads as the terminator that
    ends the previous record — this is the same boundary decode_all_fields
    walks).  Anchoring on that NUL rejects mid-name matches, and the descriptor
    type/size check then confirms the hit.  Every occurrence is tried, so a
    decoy earlier in the stream cannot mask the real field.

    _prop_f64 is deliberately left alone: its callers are calibration fields
    feeding measurement code, and this is a reporting change.
    """
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
        fmt = '<d' if want_type == 3 else '<I'
        return struct.unpack_from(fmt, stream, val_off)[0]


# ── FastReporter "Test Settings" panel ────────────────────────────────
# The eight rows FastReporter shows under Test Settings, in FR's own order.
# Seven of them are stored verbatim in the EXFO proprietary block and are
# read back here; the eighth (Fiber core size) is NOT stored anywhere in
# the file — see _parse_test_settings.
#
# Provenance — every name below was located by dumping the decompressed
# proprietary stream's 1,096 named fields and matching values against a
# FastReporter screenshot of the same acquisition class:
#
#   FR panel row                      field                       screenshot
#   IOR                               Ior                     1.470000  ✓
#   Backscatter                       Rbs                      -83.00 dB ✓
#   Helix factor                      HelixFactor                0.00 %  ✓
#   Splice loss detection threshold   SpliceLossThreshold       0.020 dB ✓
#   Splitter loss (SM Only) [ ]       SplitterDetection(=0)    unchecked ✓
#                                     SplitterDetectionThreshold 2.500 dB ✓
#   Reflectance detection threshold   ReflectanceThreshold     -78.00 dB ✓
#   End-of-fiber detection threshold  EndOfFiberThreshold       5.000 dB ✓
#
# `Ior` / `Rbs` / `HelixFactor` live together in the block's
# FiberSectionCharacteristics → Section0 group; the four thresholds live in
# its Thresholds group.  Each name occurs exactly once per file (census over
# 1,106 traces from 232 archives), so a plain first-match read is unambiguous.
#
# Not every field is constant across the estate — Ior (1.47 / 1.468325),
# SpliceLossThreshold (0.020 / 0.010) and ReflectanceThreshold (-78 / -72)
# all vary on real spans, which is precisely why the A/B comparison is worth
# printing.
_TEST_SETTING_F64 = ('Ior', 'Rbs', 'HelixFactor', 'SpliceLossThreshold',
                     'SplitterDetectionThreshold', 'ReflectanceThreshold',
                     'EndOfFiberThreshold')
_TEST_SETTING_U32 = ('SplitterDetection', 'FiberCode')


def _parse_test_settings(stream):
    """Read FastReporter's Test Settings panel out of the proprietary stream.

    Returns a dict holding whichever of the fields above are present.  Keys
    are the EXFO field names, so a missing key means "not stored in this
    file" — never a substituted default.  Callers must render an absent key
    as blank / n-a rather than inventing a value: this table exists so a
    tech can VERIFY the two directions were shot alike, and a fabricated
    number in it is worse than an empty cell.

    NOTE ON FIBER CORE SIZE.  FastReporter's eighth row ("Fiber core size
    9 µm") has no counterpart in the file.  The full field dump contains no
    core-size / mode-field-diameter field at all.  The two nearest stored
    proxies are carried here so the renderer can still compare them
    A-against-B, but NEITHER is translated into microns:

      * `FiberCode` (proprietary, uint32) — 0 on all 4,392 occurrences
        across the 1,106-trace census, so its mapping to a core size is
        untestable: there is no second value on disk to calibrate against.
      * GenParams `fiber_type` (see parse_genparams) — 652 on all 1,106,
        i.e. ITU-T G.652 standard single-mode.

    G.652 does imply a ~9 µm core, but that is a lookup we cannot verify
    against a counter-example, so the value is reported as the stored
    designation and the micron figure is left to FastReporter.
    """
    if not stream:
        return {}
    out = {}
    for name in _TEST_SETTING_F64:
        v = _prop_scalar(stream, name, 3, 8)
        if v is not None:
            out[name] = v
    for name in _TEST_SETTING_U32:
        v = _prop_scalar(stream, name, 1, 4)
        if v is not None:
            out[name] = v
    return out


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

    # ── Parse EventTable entries (full-stream walk, metre units) ──
    # Three extraction bugs lived here and each one cost real coverage:
    #   * an arbitrary 80 KB scan cap sliced the record list mid-event
    #     (SEANOR109: names at 1.3 KB, records at 67-89 KB);
    #   * the plausibility filter read Position as KILOMETRES but the block
    #     stores METRES, silently dropping every event past 500 m;
    #   * records were grouped from a bounded window, losing the tail.
    # The list matters doubly: it is the truck's FULL analysis — KeyEvents is
    # a filtered view that SUPPRESSES near-end events (SEANOR: a real splice
    # metres before EOF present here on every fiber, absent from KeyEvents on
    # all of them) — and its per-event marker Lengths are the exact LSA
    # windows FastReporter fits with (248/248 .bdr events machine-exact).
    exfo_events = []
    cur = None
    pos_scan = 0
    _KEEP = ('Length', 'Loss', 'Type', 'Status', 'CurveLevel', 'Reflectance',
             'PeakReflectionToRbs', 'LocalNoise',
             'SubCursorAPosition', 'CursorAPosition',
             'CursorBPosition', 'SubCursorBPosition')
    while pos_scan < len(stream) - 1:
        end_scan = stream.find(b'\x00', pos_scan)
        if end_scan < 0:
            break
        ln = end_scan - pos_scan
        if 2 <= ln < 100:
            try:
                nm = stream[pos_scan:end_scan].decode('ascii')
            except UnicodeDecodeError:
                nm = None
            if nm and nm.isprintable() and nm[0].isalpha():
                tc = dsz = 0
                if pos_scan >= 16:
                    tc = struct.unpack_from('<I', stream, pos_scan - 12)[0]
                    dsz = struct.unpack_from('<I', stream, pos_scan - 8)[0]
                voff = end_scan + 1
                val = None
                if tc == 3 and dsz == 8 and voff + 8 <= len(stream):
                    val = struct.unpack_from('<d', stream, voff)[0]
                elif tc == 1 and dsz == 4 and voff + 4 <= len(stream):
                    val = struct.unpack_from('<I', stream, voff)[0]
                if nm == 'Position' and val is not None:
                    if cur is not None and len(cur) > 2:
                        exfo_events.append(cur)
                    cur = {'Position': val}
                elif cur is not None and nm in _KEEP and val is not None:
                    cur[nm] = val
        pos_scan = end_scan + 1
    if cur is not None and len(cur) > 2:
        exfo_events.append(cur)

    # Positions/lengths are METRES.  A record is an EVENT iff the truck wrote
    # a CurveLevel for it; the interleaved section records (no CurveLevel)
    # are kept too, tagged, for span accounting.
    kept = []
    for e in exfo_events:
        p_m = e.get('Position')
        if not isinstance(p_m, float) or not (-1.0 <= p_m <= 500_000.0):
            continue
        e['_is_section'] = 'CurveLevel' not in e
        kept.append(e)
    exfo_events = kept

    # Exact sample pitch: marker Lengths are integer sample multiples, so the
    # population pins the pitch far more precisely than the IOR-derived
    # estimate (which drifts ~0.3 permil — whole samples at 100+ km).
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
        else:
            # Fewer than three markers to vote -- a single-direction .sor
            # with a handful of events, which is most of them: 689 of the
            # 864 ZAYO BETA 432 files.  The stated IOR carries the same
            # pitch: c x SamplingPeriod / (2 x Ior), with Ior the
            # proprietary block's own 6-dp value.  On every file that has
            # both, the two agree to 5e-14 relative (3,256 files) -- the
            # marker vote and the stated pitch are the same number.
            #
            # Leaving this None was not a safe default.  measure_fr_exact_loss
            # refuses without it, so the FR-exact silent-side transplant
            # never ran on those files and the customer's .sor pair fell to
            # the legacy reconstruction: 606 of 1,789 silent legs matched
            # FastReporter (33.9%) against 1,761 (98.4%) with the pitch.
            _ior_p = _parse_test_settings(stream).get('Ior')
            if isinstance(_ior_p, (int, float)) and 1.3 < float(_ior_p) < 1.7:
                res_m_exact = 299_792_458.0 * float(_sp) / 2.0 / float(_ior_p)

    # ── RawSamples: the trace FastReporter actually fits on ──
    # dB = 64.0 - raw/1024.  Kept as the raw uint16 array (54 KB/fiber, vs
    # 216 KB as float64 — an 864-fiber span must stay loadable); convert the
    # window you need at measurement time.
    raw_trace = None
    _ri = stream.find(b'RawSamples\x00')
    if _ri >= 16:
        _tc = struct.unpack_from('<I', stream, _ri - 12)[0]
        _dsz = struct.unpack_from('<I', stream, _ri - 8)[0]
        _voff = _ri + len(b'RawSamples\x00')
        if _tc == 2 and _dsz >= 4 and _voff + _dsz <= len(stream):
            raw_trace = np.frombuffer(stream, dtype='<u2',
                                      count=_dsz // 2, offset=_voff).copy()

    exact_wl = cal.get('ExactWavelength')
    return {
        'calibration':       cal,
        'test_settings':     _parse_test_settings(stream),
        'exfo_events':       exfo_events,
        'res_m_exact':       res_m_exact,
        'raw_trace':         raw_trace,
        'spans_loss':        cal.get('SpansLoss'),
        'spans_length':      cal.get('SpansLength'),
        'total_orl':         cal.get('TotalOrl'),
        'sampling_period':   cal.get('SamplingPeriod'),
        'exact_wavelength_nm': exact_wl * 1e9 if exact_wl else None,
        'injection_level':   cal.get('InjectionLevel'),
        'saturation_level':  cal.get('SaturationLevel'),
    }


# ─────────────────────────────────────────────────────────────────────
#  Public parse API
# ─────────────────────────────────────────────────────────────────────

def _trim_end_floor(events, res_m, n_full):
    """Lowest end-index the span trim may use without discarding stored events.

    The trim maps a time-of-travel to a sample index with
    `tot * pts / (2 * acq_range)`, but `acq_range` here is FxdParams+20 —
    the DATA SPACING field, not the acquisition range (which lives at +40).
    On long-pulse acquisitions the two errors roughly cancel; on short-pulse
    ones they do not, and the trace is cut short of its own event table:
    WSC_SUIsh keeps 3.92 km of a 5.00 km fiber, Cle Elum Tray A-F 163 m of
    1.13 km, Dinwiddie ILA5 49 m of 1.03 km.  Every trace measurement then
    runs on a stub.

    Rather than re-point the trim at +40 (which moves the frame under every
    span in the app), this is a floor: never end before the last stored
    event, in the km/res_m frame the rest of the engine already indexes
    with, plus a small tail so the event's own flanks fit.  It can only ADD
    samples, never remove them.  Returns None when the geometry is unknown.
    """
    if not events or not res_m or res_m <= 0 or not n_full:
        return None
    try:
        last_km = max((e.get('dist_km') or 0.0) for e in events)
    except (TypeError, ValueError):
        return None
    if last_km <= 0:
        return None
    floor = int((last_km * 1000.0 + TRIM_END_TAIL_M) / res_m)
    return max(0, min(floor, n_full - 1))


TRIM_END_TAIL_M = 60.0     # keep this much fiber past the last stored event


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
    prop = _parse_proprietary_block(data, blocks)
    sp_s = (prop or {}).get('sampling_period')
    if sp_s and sp_s > 0:
        res_m = 299_792_458.0 * float(sp_s) / 2.0 / _read_ior(data)
        floor = _trim_end_floor(events, res_m, len(trace))
        if floor is not None:
            ei = max(ei, min(floor, len(trace) - 1))
    return trace[si:ei + 1]


def measure_grey_loss_from_sor_event(sor_data, event,
                                      min_valid_samples=10,
                                      ior=None):
    """EXFO-exact splice-loss recomputation for a known event.

    Uses the event's own per-event LSA markers (tot_end_prev,
    tot_start_curr, tot_end_curr) as the fit window boundaries —
    matching the exact algorithm EXFO ran when it produced the
    event-table splice_loss value.

    Compared to ``measure_grey_loss_from_sor`` (which uses fixed
    5000 m / 60 m windows), this function:
      • Uses the OTDR's own per-event window placements (which adapt
        to local SNR, neighbor proximity, slope stability, etc.).
      • Returns values that match the event-table splice_loss to
        within float / quantization precision (~0.001 dB).

    Returns the loss in dB (positive = real loss) or None when
    markers aren't present (older SOR exports) or the LSA can't fit.

    The off-event grey-LSA path (used by Pass 1's missing-B fallback,
    Test 2 narrow-LSA at predicted km, etc.) still uses the fixed-
    window function — there's no event to read markers from at those
    positions.
    """
    trace = sor_data.get('trace')
    if trace is None or len(trace) < 50:
        return None
    if event is None:
        return None
    end_prev   = event.get('tot_end_prev')
    start_curr = event.get('tot_start_curr')
    end_curr   = event.get('tot_end_curr')
    start_next = event.get('tot_start_next')
    if not (end_prev and start_curr and end_curr and start_next):
        return None

    # IOR and pitch — what the file STATES, not what inverting its own
    # event distances implies (see _sor_ior / _sor_res_m).
    if ior is None:
        ior = _sor_ior(sor_data)
    res_m = _sor_res_m(sor_data, ior)
    if res_m <= 0:
        return None

    # Convert each marker's time-of-travel to fiber km.  The exact
    # constant, not the 0.02998 `_parse_key_events` still carries: since
    # #247 the event's own `dist_km` is EXFO's float64 Position, so the
    # markers have to be put on the SAME scale as that, not on the scale of
    # the tot-derived distance they used to share.
    def tot_to_km(tot):
        return (tot * _TOT_M_PER_UNIT / ior) / 1000.0

    km_end_prev   = tot_to_km(end_prev)
    km_start_curr = tot_to_km(start_curr)
    km_end_curr   = tot_to_km(end_curr)
    km_start_next = tot_to_km(start_next)

    # Sample index from a fiber km.  The markers and the raw trace share
    # the OTDR's digitizer clock, so the index is simply km/res_m with
    # NO pre-launch (first_pos_m) offset.  Applying that offset shifts
    # the fit windows off the event and is what made this function
    # disagree with EXFO's event-table value.  Confirmed empirically on
    # Seattle (532 events): WITH the offset, median |err| 0.034 dB / 10%
    # within 0.01; WITHOUT it, median 0.002 dB / 94% within 0.01.
    def km_to_idx(km_val):
        return int(km_val * 1000.0 / res_m)

    # Before-splice window: [end_prev, start_curr]
    before_lo = max(0, km_to_idx(km_end_prev))
    before_hi = km_to_idx(km_start_curr)
    # After-splice window: [end_curr, start_next]
    after_lo = km_to_idx(km_end_curr)
    after_hi = min(len(trace) - 1, km_to_idx(km_start_next))

    if before_hi - before_lo < min_valid_samples or after_hi - after_lo < min_valid_samples:
        return None

    before = trace[before_lo:before_hi]
    after  = trace[after_lo:after_hi]
    SAT = 63.5
    MINV = 0.5
    mb = (before > MINV) & (before < SAT)
    ma = (after  > MINV) & (after  < SAT)
    if mb.sum() < min_valid_samples or ma.sum() < min_valid_samples:
        return None

    x_b = np.arange(before_lo, before_hi)[mb].astype(float)
    x_a = np.arange(after_lo,  after_hi )[ma].astype(float)
    y_b = before[mb].astype(float)
    y_a = after[ma].astype(float)

    cb = np.polyfit(x_b, y_b, 1)
    ca = np.polyfit(x_a, y_a, 1)

    # Splice position is the event's own position (same no-offset frame).
    splice_idx = event['dist_km'] * 1000.0 / res_m
    raw = float(np.polyval(ca, splice_idx) - np.polyval(cb, splice_idx))
    return raw


def _sor_ior_from_events(sor_data, default=1.46820):
    """Derive the exact IOR used by the OTDR from any event with a
    known time_of_travel and dist_km.  The Bellcore formula is

        dist_km = (tot * 0.02998 / IOR) / 1000

    so IOR = tot × 0.02998 / dist_km / 1000 (assuming a non-launch
    event with non-zero tot and non-zero dist_km).  Falls back to
    `default` (1550 nm typical) when no usable event is available.

    LAST RESORT ONLY — call `_sor_ior`, which reads the value the file
    STATES and comes here only when there is none (no such file has been
    seen: 115 of 115 fixtures and 4,032 of 4,032 production traces carry
    one).  Left byte-identical, rounded constant included, because it is a
    guess and this is what the guess has always returned; the reasons not to
    reach it are in `_sor_ior`."""
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


def _sor_ior(sor_data, default=1.46820):
    """The group index the FILE STATES, not one back-derived from it.

    `parse_sor_full` puts it in `sor_data['ior']`: EXFO's float64 `Ior` out of
    the proprietary block when the file carries one — it does on every file we
    have, 115 of 115 fixtures and 4,032 of 4,032 production traces — else the
    Bellcore FxdParams group index, read anchored (see _read_ior).

    WHY NOT `_sor_ior_from_events`, which this replaces.  That inverts the
    Bellcore formula over the first usable event, and it is wrong twice:

      * it inverts through the ROUNDED 0.02998, which is 25.6 ppm long.
        Before #247 that cancelled exactly, because `dist_km` had been
        produced by the same constant; #247 made `dist_km` EXFO's float64
        Position, so there is no round trip left and the rounding became a
        real bias.  It is visible bare on a long first event, where nothing
        else matters: +25.2 ppm off a 117 km event, +26.9 off a 17 km one.
      * `dist_km` is rounded to 4 dp, ±0.05 m, and it is the DENOMINATOR.
        On a first usable event 1 km out that is ±50 ppm on its own, so the
        error stops being a bias and starts being noise — measured over 2,419
        files it runs from −16 to +76 ppm, and which way is a property of
        where the fiber's first event happens to sit.
      * it needs a usable event at all.  A fiber reflective-dead AT the
        launch connector has one event, at tot 0, so the loop finds nothing
        and returns the hardcoded 1.46820 — against a stated 1.47000 that is
        1,224 ppm, which on the Viewer side drew the trace 171 m off the very
        files a tech opens it to look at (fixtures endlaunch/HOWLAN309_1550,
        endlaunch/LAGDUR0036).

    Scored against FastReporter's own marker Lengths — whole numbers of
    samples, so the population pins the pitch independently of any IOR field
    — over the 88 fixtures that carry enough markers to pin it:

        stated Ior (float64)     0.00 ppm median,    0.00 worst
        Bellcore group index     0.00 ppm median,    3.41 worst
        back-derivation         18.58 ppm median,   65.78 worst

    The derivation stays as the last resort, so a file carrying neither
    stated value is no worse off than it was.
    """
    ior = sor_data.get('ior')
    try:
        ior = float(ior)
    except (TypeError, ValueError):
        ior = None
    if ior is not None and _IOR_SANE_MIN <= ior <= _IOR_SANE_MAX:
        return ior
    return _sor_ior_from_events(sor_data, default=default)


def _sor_res_m(sor_data, ior=None):
    """Metres of fiber per trace sample.

    `exfo_res_m` first — FastReporter's OWN pitch, pinned by its marker
    Lengths, and the same input `measure_fr_exact_loss` indexes on.  Sharing
    it is the point: the legacy measurement paths and the FR-exact path can no
    longer disagree about where a sample sits.

    It needs three usable markers, so it is absent on 27 of 115 fixtures.
    There the stated IOR carries the pitch, and which of the two answers is
    never visible in a result: on all 3,256 files carrying both they agree to
    5e-14 relative — float rounding in a median-of-candidates, 0.00005 ppm,
    against the 18.58 ppm the derivation was out by.

    `ior` is the LAST-RESORT group index, for a file that states neither a
    marker pitch nor an IOR — the same role `default` plays in `_sor_ior`.
    It does not override `exfo_res_m` or the stated IOR, and no shipped
    caller passes one.
    """
    res = sor_data.get('exfo_res_m')
    try:
        res = float(res)
    except (TypeError, ValueError):
        res = None
    if res and res > 0:
        return res
    sp_s = sor_data.get('exfo_sampling_period')
    if not sp_s or sp_s <= 0:
        # EXFO default; matches every file we have seen in practice.
        sp_s = 5e-08
    ior = _sor_ior(sor_data, default=(ior or 1.46820))
    return 299_792_458.0 * float(sp_s) / 2.0 / float(ior)


def _sor_first_pos_m(sor_data, res_m):
    """Find the fiber-km position of trace[0] for an untrimmed SOR file.

    EXFO acquisitions begin sampling about 1 km BEFORE the cable's
    launch event (an instrument-side dead zone).  The events list
    has dist_km measured from the launch event itself, so to map an
    events-frame km to a sample index we need to know where the
    launch sits in the raw trace.

    Two-stage detection:
      1. Coarse: find the trace MINIMUM in samples [10:500].  In SOR
         convention high values = more accumulated attenuation, so the
         minimum is the strongest backscatter — the just-past-launch
         position.
      2. Refined: walk a few samples either side of the coarse min
         and find the inflection where the trace transitions from
         "instrument startup" (rapidly varying, noisy) to "fiber"
         (smooth, slowly rising).  Specifically, find the first
         sample after the minimum whose immediate-neighbor delta
         drops below a small threshold — that's where the fiber
         signal begins.

    Returns the fiber-km position (in events frame) of trace[0],
    which is negative (~-1 km).
    """
    trace = sor_data.get('trace')
    if trace is None or len(trace) < 50:
        return 0.0
    try:
        cached = sor_data.get('_sor_first_pos_m')
        if cached is not None:
            return cached
    except Exception:
        pass
    import numpy as _np
    # Stage 1: coarse min in [10:500]
    head_lo, head_hi = 10, min(500, len(trace) - 1)
    head = trace[head_lo:head_hi]
    if len(head) < 20:
        return 0.0
    coarse_idx = int(_np.argmin(head)) + head_lo
    # Stage 2: refine by walking forward from coarse_idx until the
    # local 5-sample slope settles (transition from instrument zone
    # to fiber zone).
    refined_idx = coarse_idx
    look_ahead = min(coarse_idx + 50, len(trace) - 6)
    for i in range(coarse_idx, look_ahead):
        # 5-sample delta
        win = trace[i:i+5]
        if len(win) < 5:
            break
        delta_mean = float(_np.mean(_np.abs(_np.diff(win))))
        if delta_mean < 0.005:  # very smooth — fiber signal stabilized
            refined_idx = i
            break
    first_pos_m = -float(refined_idx) * res_m
    try:
        sor_data['_sor_first_pos_m'] = first_pos_m
    except Exception:
        pass
    return first_pos_m


# Residual scatter (dB, RMS about the window's own LSA line) above which a
# window is the noise floor rather than glass.  See the liveness block in
# measure_grey_loss_from_sor for the measurement this sits on: live windows
# top out at 1.19 dB, windows past a confirmed break bottom out at 1.51, and
# 1.30 sits in the gap — biased toward the live side, because a false "dead"
# verdict silently DROPS a real cell while a false "live" one only leaves
# today's behaviour in place.
LIVENESS_MAX_RESID_DB = 1.30


def _win_residual(x, y, coeffs):
    """RMS residual of `y` about the fitted line — the window's roughness.

    Backscatter is a straight line carrying shot noise; the noise floor is
    not a line at all.  Returns 0.0 for a degenerate window so an empty fit
    can never be read as 'dead' (the sample-count guards upstream own that
    case)."""
    if len(x) < 2:
        return 0.0
    return float(np.sqrt(np.mean((y - np.polyval(coeffs, x)) ** 2)))


def measure_grey_loss_from_sor(sor_data,
                                splice_km,
                                outer_m=5000,
                                inner_m=60,
                                min_valid_samples=10,
                                ior=1.46820,
                                neighbor_buffer_m=80):
    """Measure splice loss at `splice_km` from a SOR fiber's raw trace
    using the same wide-LSA approach as the JSON version.  Mirrors
    `measure_grey_loss_from_json` but reads the SOR raw-sample array.

    Returns the loss in dB (positive = real loss, matching the JSON
    function's sign convention) or None when there aren't enough valid
    samples on either side.

    Sample resolution is the pitch the file itself states — EXFO's own
    marker-pinned `exfo_res_m`, else c × sampling period / (2 × stated IOR);
    see `_sor_res_m`.  Pre-launch offset is detected from the trace's
    first-500-sample minimum (the launch reflection peak); see
    `_sor_first_pos_m`.

    Trace convention: raw SOR samples store accumulated attenuation
    such that HIGH values = more loss (less backscatter), LOW values
    = strong signal (just past launch).  A splice with positive loss
    shows as an UPWARD step in the trace; we return that difference
    directly so positive loss → positive return value.
    """
    trace = sor_data.get('trace')
    if trace is None or len(trace) < 50:
        return None

    # ── Sample resolution (m/sample) — the file's own stated pitch ───
    res_m = _sor_res_m(sor_data, ior)
    if res_m <= 0:
        return None

    # ── Pre-launch offset: trace[0] sits ~1 km BEFORE the launch event ──
    first_pos_m = _sor_first_pos_m(sor_data, res_m)

    splice_m = float(splice_km) * 1000.0

    # ── Neighbor clamping (don't let outer windows reach into adjacent events) ──
    prev_m = None
    next_m = None
    for e in (sor_data.get('events') or []):
        ep = e['dist_km'] * 1000.0
        if ep < splice_m - 10:
            prev_m = ep if prev_m is None else max(prev_m, ep)
        elif ep > splice_m + 10:
            next_m = ep if next_m is None else min(next_m, ep)

    outer_a_m = splice_m - outer_m
    outer_b_m = splice_m + outer_m
    if prev_m is not None:
        outer_a_m = max(outer_a_m, prev_m + neighbor_buffer_m)
    if next_m is not None:
        outer_b_m = min(outer_b_m, next_m - neighbor_buffer_m)

    # ── Sample indices (events-frame km → trace samples) ──
    # idx = km_m / res_m, with NO first_pos_m launch offset.  The event
    # positions and the raw trace share the OTDR's digitizer clock, so the
    # offset must NOT be applied — and the fit windows must use the SAME frame
    # as splice_idx below (which already omits it).  Applying the offset to the
    # windows but not splice_idx was a frame mismatch that evaluated the fit
    # lines ~1 km off the event (the same bug fixed in the marker-based path).
    oa = max(0, int(outer_a_m / res_m))
    ia = int((splice_m - inner_m) / res_m)
    ib = int((splice_m + inner_m) / res_m)
    ob = min(len(trace) - 1, int(outer_b_m / res_m))

    if ia - oa < min_valid_samples or ob - ib < min_valid_samples:
        return None

    before = trace[oa:ia]
    after  = trace[ib:ob]
    # SOR saturation cap is 64.0 (raw uint16/1024 - 64).  Drop saturated
    # samples and obvious noise (negative dB) from the fit.
    SAT = 63.5
    MINV = 0.5
    mb = (before > MINV) & (before < SAT)
    ma = (after  > MINV) & (after  < SAT)
    if mb.sum() < min_valid_samples or ma.sum() < min_valid_samples:
        return None

    x_b = np.arange(oa, ia)[mb].astype(float)
    x_a = np.arange(ib, ob)[ma].astype(float)
    y_b = before[mb].astype(float)
    y_a = after[ma].astype(float)

    cb = np.polyfit(x_b, y_b, 1)
    ca = np.polyfit(x_a, y_a, 1)

    # ── Liveness: is there actually glass under both windows? ────────────
    # The masks above are a SATURATION cap, not a liveness test.  A fiber's
    # noise floor sits around 62-63 dB, just under SAT, so every sample past
    # a break survives the mask and fits a line as happily as backscatter
    # does — and the caller gets a confident number measured out of noise.
    # Swept across the dead 55 km of SUI↔EMR F908 (a real break at 21.36 km),
    # 66% of positions cleared the 0.160 report threshold, peaking at
    # +0.73 dB.  Those are the readings that put phantom losses on the dead
    # side of broken fibers.
    #
    # Backscatter is a straight line with a little shot noise on it; the
    # noise floor is not.  Residual scatter about the fit separates them by
    # an order of magnitude and needs no reference to the event table — which
    # matters, because the table is exactly what cannot be trusted here (the
    # firmware writes `0E` end-of-fiber on high-loss points that still have
    # tens of km of live glass past them, so gating on the stored EOF would
    # drop real measurements on precisely the damaged fibers we care about).
    #
    # Measured over 2 642 live windows across four 1152-fiber sets (SUI↔EMR
    # both ways, Miller↔Elmdale both ways): residual p50 0.017, p99 0.30,
    # max 1.19.  Over 328 windows past confirmed breaks: min 1.51, p50 1.98.
    # The threshold sits in that gap.
    if _win_residual(x_b, y_b, cb) > LIVENESS_MAX_RESID_DB:
        return None
    if _win_residual(x_a, y_a, ca) > LIVENESS_MAX_RESID_DB:
        return None

    splice_idx = splice_m / res_m
    raw = float(np.polyval(ca, splice_idx) - np.polyval(cb, splice_idx))
    return raw


SILENT_HALF_WIN_KM   = 5.0016   # EXFO's default LSA half-window (the SubCursor→
                                #   Cursor span; measured 5001.6 m on the bidi
                                #   .bdr fit cursors, ~981 samples at 1550/2.5us)
SILENT_DEFAULT_EVT_KM = 0.34    # fallback dead-zone length past the event when
                                #   the loud-side event length isn't supplied.
                                #   = EXFO's median event length (CursorB-CursorA)
                                #   on the .bdr cursors (range ~0.17-0.45 km); the
                                #   old 0.30 sat the after-window 41 m short of
                                #   EXFO's (~0.14 mdB).
SILENT_NEIGHBOUR_GAP_KM = 0.43  # FALLBACK gap when the previous event has no
                                #   stored markers: the before-window's outer edge
                                #   clamps to the PREVIOUS event's marker-end
                                #   (its CursorB = position + that event's own
                                #   length, threaded per-event below), matching
                                #   where EXFO puts SubCursorA.  0.43 km is the
                                #   median marker-end gap on the .bdr (≈ the launch
                                #   connector's own length) — used only when the
                                #   neighbour's tot_start/end_curr markers are absent.
SILENT_LAUNCH_CLEAR_KM = 0.43   # never start the before-window within this much of
                                #   the launch (normalized origin = 0).  EXFO clamps
                                #   SubCursorA to the launch connector's marker-end,
                                #   a consistent 0.429 km in our frame on all 12
                                #   .bdr fibers — our old 0.30 floor sat 129 m too
                                #   close to the launch (~+5 mdB bias near launch on
                                #   a SILENT side, whose true loss is <0.02 dB).


# ── Trace-measured reflectance ────────────────────────────────────────────
# The firmware's reflective/non-reflective verdict is an OPINION, and on weak
# glints it is wrong.  WSC_SUIsh F19 stores its 3.8937 km event as `0F9999LS`,
# non-reflective, reflectance 0.0 — while FastReporter re-analyses the same
# file and reports Reflective, -75.0 dB.  The glint really is in the glass: a
# 0.94 dB spike over a 63.55 dB baseline.  These helpers measure it, so the
# reflective finders stop depending on a table that can be silent (5th
# instance of the stored-table-trust class).
#
# Height -> reflectance is the standard relation
#     R = bs_level + 10*log10(10**(H/5) - 1)
# where H is the glint's height over the local backscatter baseline.
#
# bs_level is NOT taken from the FxdParams backscatter coefficient.  That
# field plus the textbook 10*log10(pulse_ns) scaling was measured against the
# stored reflectances on six real sets and lands 0.5 dB out on WSC_SUIsh but
# 3 dB out on the 5 ns Cle Elum trays and 8 dB out on Dinwiddie ILA5 — the
# absolute constant is not portable across instruments and pulse widths.
# Instead each acquisition SELF-CALIBRATES: `solve_backscatter_anchors`
# inverts the same relation on that file's own stored reflective events, and
# the caller takes the median over a folder.  Per-folder spread of that
# solution is IQR 0.15 dB (WSC_SUIsh), 0.69 (Dinwiddie PANEL A), 0.84-1.37
# (Cle Elum) — tight enough to publish a number, and it cancels the
# backscatter coefficient, the pulse-width scaling, and the detector
# response in one step.
#
# DETECTION does not depend on any of this: `measure_reflective_spike`
# returns height and SNR against the fiber's own flank noise, which is all
# the reflective finders need to decide that a glint exists.
REFLM_CORE_M          = 6.0    # +/- half-width of the peak search window
REFLM_FLANK_IN_M      = 9.0    # baseline flanks start this far out ...
REFLM_FLANK_OUT_M     = 45.0   #   ... and run to here, both sides
REFLM_MIN_FLANK_PTS   = 10     # clean samples needed to fit the baseline
REFLM_MIN_HEIGHT_DB   = 0.10   # ...and an ABSOLUTE floor as well.  SNR alone is
                               # not enough on a very quiet trace: Eugene (1000 ns,
                               # long averaging) has flank noise near 2 mdB, so
                               # 12-17 mdB backscatter ripples cleared SNR 7-8 and
                               # produced three phantom "reflections".  Physics puts
                               # a real -80 dB glint at ~0.28 dB of height for these
                               # backscatter levels; F19's genuine glint is 0.963 dB
                               # and connectors run 5-8 dB.  0.10 sits an order of
                               # magnitude above the ripples and 9x below the
                               # weakest real event measured.
REFLM_MIN_SNR         = 6.0    # peak must clear this multiple of flank noise.
                               # Calibrated on WSC_SUIsh: 1,578 bare-glass
                               # probes (>=150 m from any stored event, all 24
                               # fibers) peak at SNR 5 and produce ZERO hits at
                               # 6, while F19's real glint sits at 22 and the
                               # launch connectors at 59-199.  At the old 3.0 the
                               # same sweep returned 4.3% false spikes.
REFLM_EDGE_GUARD_KM   = (REFLM_FLANK_OUT_M + 15.0) / 1000.0   # both flanks must
                               # fit inside the trace.  Derived from the flank
                               # geometry, not a round number: a flat 250 m
                               # guard excluded every Cle Elum tie-panel anchor
                               # (connectors at 1.03-1.13 km on a 1.19 km trace)
                               # and left those folders with no calibration.
# Anchor band for self-calibration.  Strong reflections distort the peak
# (Dinwiddie ILA5's -46 dB connectors solve 8 dB high — the tip is clipped),
# and near-floor ones carry no information, so only mid-range stored values
# calibrate.
REFLM_ANCHOR_MIN_DB   = -78.0
REFLM_ANCHOR_MAX_DB   = -52.0
REFLM_ANCHOR_MIN_N    = 3      # anchors needed before a folder level is used
REFLM_ANCHOR_MAX_IQR  = 3.0    # dB — wider than this, the solution is noise


def _reflm_pulse_m(sor_data):
    """Length of fiber the pulse smears a glint over, in metres."""
    pulse_ns = sor_data.get('fxd_pulse_ns')
    try:
        pulse_ns = float(pulse_ns or 0)
    except (TypeError, ValueError):
        pulse_ns = 0.0
    if not (1.0 <= pulse_ns <= 20000.0):
        pulse_ns = 10.0
    return pulse_ns * 0.1022          # ns -> metres of fiber at ior 1.468


def _reflm_windows(sor_data):
    """(core_m, flank_in_m, flank_out_m) scaled to the acquisition's pulse.

    A glint occupies ~one pulse length of fiber, so fixed windows only work at
    one pulse width.  At 275 ns the smear is 28 m and at 2500 ns it is 255 m —
    a fixed +/-6 m core sits INSIDE the glint and the 9-45 m flanks sit ON it,
    which is why the fixed geometry silently declined to measure anything on
    every long-pulse span.  Short pulses keep the original numbers: at 10 ns
    1.5 * 1.02 m is well under the 6 m floor.
    """
    pm = _reflm_pulse_m(sor_data)
    core = max(REFLM_CORE_M, 1.5 * pm)
    flank_in = max(REFLM_FLANK_IN_M, core * 1.5)
    flank_out = flank_in + max(REFLM_FLANK_OUT_M - REFLM_FLANK_IN_M, 3.0 * pm)
    return core, flank_in, flank_out


def _reflm_geometry(sor_data, ior=None):
    """(trace, res_m) for the reflectance helpers, or (None, None)."""
    trace = sor_data.get('trace')
    if trace is None or len(trace) < 50:
        return None, None
    res_m = _sor_res_m(sor_data, ior or 1.468)
    if res_m <= 0:
        return None, None
    return np.asarray(trace, float), res_m


def measure_reflective_spike(sor_data, position_km, ior=None):
    """Measure the reflective glint at `position_km` from this fiber's own
    raw samples.

    Returns {'height_db', 'noise_db', 'snr'} or None.  None means the
    measurement could not be made honestly — no trace, windows off the end
    of the array, too few clean samples, or no peak clearing REFLM_MIN_SNR
    times the local flank noise.  None is "unknown", NOT "no reflection":
    callers must fall back to their existing behaviour rather than treat it
    as a refutation.
    """
    y, res_m = _reflm_geometry(sor_data, ior)
    if y is None:
        return None
    try:
        n = len(y)
        pos_m = float(position_km) * 1000.0

        def _flank(lo_m, hi_m):
            a = int((pos_m + lo_m) / res_m)
            b = int((pos_m + hi_m) / res_m)
            if a < 0 or b > n:
                return None, None       # a clipped flank biases the baseline
            if b - a < 4:
                return None, None
            return np.arange(a, b, dtype=float), y[a:b]

        core_m, flank_in_m, flank_out_m = _reflm_windows(sor_data)
        xl, yl = _flank(-flank_out_m, -flank_in_m)
        xr, yr = _flank(flank_in_m, flank_out_m)
        if xl is None or xr is None:
            return None
        x = np.concatenate([xl, xr])
        v = np.concatenate([yl, yr])
        clean = (v > 0.5) & (v < 63.9)          # drop saturated / dead samples
        if int(clean.sum()) < REFLM_MIN_FLANK_PTS:
            return None
        slope, intercept = np.polyfit(x[clean], v[clean], 1)
        noise = float(np.std(v[clean] - (intercept + slope * x[clean])))

        core = max(3, int(core_m / res_m))
        i = int(pos_m / res_m)
        lo, hi = max(0, i - core), min(n, i + core)
        if hi - lo < 3:
            return None
        base = intercept + slope * np.arange(lo, hi, dtype=float)
        # Raw SOR samples run "high = more loss", so a reflection is a DIP
        # below the fitted baseline; height is baseline minus trace.
        height = float(np.max(base - y[lo:hi]))
        if height < REFLM_MIN_HEIGHT_DB or noise <= 0:
            return None
        if height < REFLM_MIN_SNR * noise:
            return None
        return {'height_db': height, 'noise_db': noise,
                'snr': height / noise}
    except Exception:
        return None


def reflectance_from_height(height_db, bs_level):
    """Invert the spike-height relation.  None when the inputs cannot give a
    real answer."""
    try:
        ratio = 10.0 ** (float(height_db) / 5.0) - 1.0
        if ratio <= 0:
            return None
        return float(bs_level) + 10.0 * float(np.log10(ratio))
    except Exception:
        return None


def solve_backscatter_anchors(sor_data, ior=None, offset_km=None):
    """Backscatter levels implied by THIS file's own stored reflective events.

    Each stored event whose firmware reflectance sits in the anchor band and
    whose glint is measurable contributes one solved level.  Callers collect
    these across a folder and take the median (see
    `folder_backscatter_level`); a single file rarely has more than one.

    `offset_km` converts event km into the RAW trace frame.  Both engines
    re-reference events to the launch connector before analysis, so without
    it every anchor is looked up ~1 km upstream of its own glint and the
    folder silently falls back to the uncalibrated FxdParams level.  Defaults
    to the record's own `_trace_offset_km`, which both loaders set.
    """
    y, res_m = _reflm_geometry(sor_data, ior)
    if y is None:
        return []
    if offset_km is None:
        offset_km = sor_data.get('_trace_offset_km') or 0.0
    span_km = len(y) * res_m / 1000.0
    out = []
    for e in (sor_data.get('events') or []):
        if e.get('is_end'):
            continue
        refl = e.get('reflection')
        if refl is None or not (REFLM_ANCHOR_MIN_DB <= refl <= REFLM_ANCHOR_MAX_DB):
            continue
        km = e.get('dist_km')
        if km is None:
            continue
        km = km + float(offset_km)
        guard_km = (_reflm_windows(sor_data)[2] + 15.0) / 1000.0
        if km < guard_km or km > span_km - guard_km:
            continue
        spike = measure_reflective_spike(sor_data, km, ior=ior)
        if spike is None:
            continue
        ratio = 10.0 ** (spike['height_db'] / 5.0) - 1.0
        if ratio <= 0:
            continue
        out.append(float(refl) - 10.0 * float(np.log10(ratio)))
    return out


def folder_backscatter_level(records, ior=None):
    """Median backscatter level over a folder of parsed records, or None when
    the anchors are too few or too scattered to trust.  `records` is any
    iterable of parse_sor_full dicts."""
    anchors = []
    for r in records:
        try:
            anchors.extend(solve_backscatter_anchors(r, ior=ior))  # offset from the record
        except Exception:
            continue
    if len(anchors) < REFLM_ANCHOR_MIN_N:
        return None
    a = np.asarray(anchors, float)
    iqr = float(np.percentile(a, 75) - np.percentile(a, 25))
    if iqr > REFLM_ANCHOR_MAX_IQR:
        return None
    return float(np.median(a))


def measure_reflectance_from_sor(sor_data, position_km, bs_level=None, ior=None):
    """Reflectance (signed dB, less negative = stronger) of the glint at
    `position_km`, measured from this fiber's own raw samples.

    `bs_level` is the self-calibrated backscatter level from
    `folder_backscatter_level`.  Without one this falls back to the file's
    own anchors, and returns None when neither is available — a measured
    height with no calibration is a height, not a reflectance.
    """
    spike = measure_reflective_spike(sor_data, position_km, ior=ior)
    if spike is None:
        return None
    if bs_level is None:
        own = solve_backscatter_anchors(sor_data, ior=ior)
        if not own:
            return None
        bs_level = float(np.median(own))
    return reflectance_from_height(spike['height_db'], bs_level)

def measure_silent_grey_from_sor(sor_data, position_km, ior=None,
                                 min_valid_samples=8, require_clean=False,
                                 event_len_km=None):
    """Reconstruct the grey/splice loss at a SILENT matched position — one where
    THIS direction detected no event (loss below EXFO's 0.02 threshold) so there
    are no stored LSA markers, but EXFO's bidirectional analysis still measures a
    value there.

    Reproduces EXFO's EXACT silent-side LSA geometry, reverse-engineered from the
    bidirectional .bdr fit cursors (SubCursorA/CursorA/CursorB/SubCursorB) on the
    Seattle SEANOR109-120 fibers:

        before window = [P - 5.0016 km .. P]          (CursorA = P exactly)
        after  window = [P + L_event .. P + L_event + 5.0016 km]
                                                       (CursorB = P + event length)

    where L_event is the loud-side event length (~0.17-0.50 km; F111 0.30).  Both
    windows are clamped to this fiber's neighbouring events / EOL.  Fit an OLS line
    to each, extrapolate BOTH to the event index P, loss = after_level - before_level
    (signed, += loss).

    This REPLACES the earlier learned-marker recipe ([P-3.99..P+1.012] /
    [P+1.356..P+6.35] km), whose before-window ran 1 km PAST the event (the +1.012 /
    +1.356 km offsets were the export's launch-reference artifact, not real window
    geometry).  Validated on the .bdr cursors: the EXFO geometry reproduces 186
    silent-side events to median 1.7 mdB (F111 0.0143 vs EXFO 0.0145); the old
    recipe gave median 22 mdB with gross blow-ups (±14 dB) where its windows
    straddled the step or reached into neighbours.

    `event_len_km` is the loud-side event length (CursorB - CursorA); when None we
    fall back to SILENT_DEFAULT_EVT_KM.  A shorter-half-window fallback handles
    tight-neighbour / near-EOL positions.  Returns the loss in dB or None when no
    window fits.
    """
    trace = sor_data.get('trace')
    if trace is None or len(trace) < 50:
        return None
    if ior is None:
        ior = _sor_ior(sor_data)
    res_m = _sor_res_m(sor_data, ior)
    if res_m <= 0:
        return None

    P = float(position_km)
    n = len(trace)
    L_ev = float(event_len_km) if (event_len_km and event_len_km > 0) \
        else SILENT_DEFAULT_EVT_KM
    # Event distances are launch-referenced but the trace is not (sample 0 = OTDR
    # port).  Add the launch offset so km maps to the right trace sample.  All
    # window edges + neighbour/EOL clamps go through idx(), so the offset applies
    # uniformly; it is 0 for already-trimmed fibers / standalone callers.
    off = float(sor_data.get('_trace_offset_km') or 0.0)

    def idx(km):
        return int((km + off) * 1000.0 / res_m)

    # Neighbouring real events on this fiber + end-of-fiber, for window clamping
    # (EXFO clamps both windows to the immediate neighbours).  Carry each
    # neighbour's own event length so the before-window can clamp to the prev
    # event's EXACT marker-end (its CursorB = position + length), not a fixed
    # gap.  EXFO's CursorB - CursorA == the SOR marker span tot_end_curr -
    # tot_start_curr (verified 0 m on the .bdr); it's a tot DIFFERENCE so the
    # launch-normalization shift cancels and no offset is needed.
    def _evt_len_km(e):
        s = e.get('tot_start_curr'); t = e.get('tot_end_curr')
        if s and t and t > s:
            return (t - s) * _TOT_M_PER_UNIT / ior / 1000.0
        return None
    evs = sorted(((e['dist_km'], _evt_len_km(e))
                  for e in (sor_data.get('events') or [])
                  if not e.get('is_end') and e['dist_km'] >= 1.0),
                 key=lambda kv: kv[0])
    eol = None
    for e in (sor_data.get('events') or []):
        if e.get('is_end'):
            eol = e['dist_km']
    prevs = [kv for kv in evs if kv[0] < P - 0.05]
    nexts = [kv[0] for kv in evs if kv[0] > P + 0.05]
    pk_evt = max(prevs, key=lambda kv: kv[0]) if prevs else None
    pk = pk_evt[0] if pk_evt is not None else None
    pk_len = pk_evt[1] if pk_evt is not None else None
    nk = min(nexts) if nexts else None
    Pidx = idx(P)

    def fit(lo, hi):
        lo = max(0, lo); hi = min(n - 1, hi)
        if hi - lo < min_valid_samples:
            return None
        seg = trace[lo:hi]
        m = (seg > 0.5) & (seg < 63.5)
        if m.sum() < min_valid_samples:
            return None
        x = np.arange(lo, hi)[m].astype(float)
        return np.polyfit(x, seg[m].astype(float), 1)

    def measure(half):
        # EXFO geometry: before = [P-half .. P]; after = [P+L_ev .. P+L_ev+half],
        # clamped to neighbours / EOL.
        bstart = P - half
        if pk is not None:
            # EXFO starts the before-window at the PREVIOUS event's marker-end
            # (its CursorB = position + that event's own length); fall back to the
            # SILENT_NEIGHBOUR_GAP_KM median only if it has no stored markers.
            gap = pk_len if (pk_len and pk_len > 0) else SILENT_NEIGHBOUR_GAP_KM
            bstart = max(bstart, pk + gap)                       # prev marker-end
        bstart = max(bstart, SILENT_LAUNCH_CLEAR_KM)   # stay clear of the launch
        aend = P + L_ev + half
        if nk is not None:
            # Mirror of the before-window: EXFO's after-window OUTER edge
            # (SubCursorB) == the NEXT event's CursorA (== its position /
            # marker-start), verified to 0.0 m median on the .bdr cursors.  The
            # fit range is exclusive of idx(aend) so the next event's own step
            # isn't included.
            aend = min(aend, nk)
        if eol is not None:
            aend = min(aend, eol)         # EXFO clamps right to the end event's CursorA
        cb = fit(idx(bstart), Pidx)
        ca = fit(idx(P + L_ev), idx(aend))
        if cb is None or ca is None:
            return None, bstart, aend
        return float(np.polyval(ca, Pidx) - np.polyval(cb, Pidx)), bstart, aend

    # ── EXFO-exact geometry, full half-window ──
    r, bstart, aend = measure(SILENT_HALF_WIN_KM)
    if r is not None:
        if require_clean and not (
                P >= 0.5                          # clear of the launch zone
                and (P - bstart) >= 1.0           # >=1 km clean fiber BEFORE
                and (aend - (P + L_ev)) >= 1.0):   # >=1 km clean fiber AFTER
            # Tight-neighbour / near-launch: too little clean fiber to trust the
            # extrapolation — refuse rather than feed a shaky value into the
            # bidir average (the recon's unreliable tail).
            return None
        return r

    if require_clean:
        # Only the clean full-window fit is trusted for a hard silent-side value;
        # the shrink fallbacks below are the noisy tail.
        return None

    # ── shrink fallback: pull the half-window in for tight neighbours / EOL ──
    for half in (3.0, 1.5, 0.8, 0.4):
        r, bstart, aend = measure(half)
        if r is not None:
            return r
    return None


# ── END-ZONE / NEAR-LAUNCH silent-side reconstruction ──────────────────
# measure_silent_grey_from_sor reproduces EXFO's LSA geometry, which needs
# ~5 km (and at minimum 1 km) of clean fiber on BOTH sides of the event.  A
# closure sitting a few tens of metres from a cable end has neither: the
# after-window's own start (P + event length) already lands past the far-end
# connector, and the before-window's launch clamp already sits past the
# event.  Both directions then read None and the bidirectional average is
# never formed — the WSC↔SUI Splice 12 miss (63.9675 km, 80 m before EOF;
# the same closure sits 80 m past the B launch in the B frame).
#
# The windows here are anchored on the cable end instead of on the event:
# whatever clean glass exists between the launch connector's marker-end and
# the far-end connector is used, and the SLOPE is taken from the LONG side
# and shared with the short side.  Sharing the slope is the whole point —
# a free two-line fit over the ~10-30 samples that fit next to a cable end
# reads ±0.35 dB of pure noise (measured: F620 -0.323, F525 +0.379), while
# the shared-slope level fit lands within 0.008 dB (median, n=18) of the
# reviewer's bidirectional values.  Physically the backscatter slope cannot
# change across a splice, so only the LEVEL is in question.
ENDZONE_REACH_KM          = 0.50   # only fire this close to a cable end — the
                                   #   normal EXFO geometry owns everything else
ENDZONE_EOF_GUARD_KM      = 0.010  # stop short of the far-end connector's
                                   #   reflection onset (4 samples at 2.5 m/sample)
ENDZONE_DEAD_ZONE_KM      = 0.030  # skip the event's own pulse smear before the
                                   #   after-window starts (275 ns ≈ 28 m)
ENDZONE_LONG_WIN_KM       = 1.0    # length of the long (slope-bearing) window
ENDZONE_MIN_LEVEL_SAMPLES = 6      # samples needed to average a level
ENDZONE_MIN_SLOPE_SAMPLES = 20     # samples needed to trust a fitted slope
ENDZONE_LAUNCH_CLEAR_KM   = 0.43   # fallback launch clearance when the launch
                                   #   event carries no LSA markers (= EXFO's
                                   #   SILENT_LAUNCH_CLEAR_KM)
ENDZONE_ANCHOR_TOL_SAMPLES = 12    # far-end connector must land this close to
                                   #   idx(EOL) for the frame to be trusted
ENDZONE_ANCHOR_DIP_DB      = 1.0   # ... and its reflection must be this deep

# ── CORRECTED-ANCHOR (mirror) end-zone geometry ────────────────────────
# The shared-slope fit above is our own construction; EXFO's own end-zone
# cursors are recoverable, and they are NOT event-anchored on the silent
# side — they are anchored on the LOUD (mirror) direction's stored event,
# mapped into this direction's frame through the cable end:
#
#     CursorA    = idx(EOL - mirror_event.dist)   (mirror's own launch frame)
#     CursorB    = CursorA + the mirror event's OWN width in samples
#     SubCursorA = max(previous event's marker-end, CursorA - 5001.6 m)
#     SubCursorB = idx(EOL)  exactly
#     loss       = after_line(CursorB) - before_line(CursorB)
#
# Both lines are evaluated AT CursorB (not at CursorA, not at the event
# position) — that is what the calibration converged on and it is the one
# degree of freedom that must not be "tidied up".
#
# Calibrated against 918 FastReporter grey values on WSC↔SUI (train
# 1-864 / test 865-1152, two different B units): median |Δ| 0.0180 dB for
# the shared-slope fit -> 0.0125 here, p95 0.0539 -> 0.0438.  The four
# fibers the reviewer hand-checked move from -0.092/-0.095/-0.029/-0.011
# (a systematic UNDER-read) to -0.019/+0.035/-0.024/+0.013.  On the
# Splice-12 column that is 23 TP / 1 FP / 4 misses -> 26 / 2 / 1.
#
# What says this is EXFO's geometry rather than a fitted fudge: on the 152
# WSC↔SUI fibers that DO store the event, running this same recipe off
# their OWN stored cursors reproduces FastReporter to 0.0022 dB median.
# SubCursorB must be idx(EOL) exactly (±1 sample degrades the median).
#
# INDEXING (corrected 2026-09-21).  This used to truncate, and the truncation
# was measured to beat rounding — on a pitch that ran 25 ppm long.  At 64 km
# that bias is +0.6 of a sample, so int() was subtracting it back out and the
# two errors cancelled.  Read the pitch the file states and the cancellation
# goes with it: over 1,152 WSC↔SUI traces the cable end lands within 0.05 of
# a whole sample on 1152/1152 under the stated pitch and on 0/1152 under the
# back-derived one, and int(back-derived) picks the SAME sample as
# round(stated) on 1152/1152.  So the calibrated convention was always "the
# nearest sample"; round() is how you write that without an assumption about
# the pitch, and truncation now lands one sample early (see
# desktop/tests/test_endzone_subsample.py).
ENDZONE_HALF_WIN_M        = 5001.6 # EXFO's SubCursorA half-window (same
                                   #   5.0016 km the mid-span geometry uses)
ENDZONE_MIRROR_MIN_WIDTH  = 4      # floor on the mirror event's width, samples
ENDZONE_MIRROR_MIN_BEFORE = 30     # samples of glass needed before CursorA
ENDZONE_MIRROR_TOL_KM     = 0.10   # mirror-derived anchor must agree with the
                                   #   caller's position this closely, else the
                                   #   mirror is the wrong event -> fall back


def _endzone_ols(trace, lo, hi):
    """OLS line over the INCLUSIVE sample range [lo, hi], or None.

    Deliberately raw: the corrected-anchor windows sit in clean glass
    between two known cursors, and dropping samples by value would move
    the fit away from the geometry the calibration measured."""
    if lo < 0 or hi >= len(trace) or hi < lo + 3:
        return None
    x = np.arange(lo, hi + 1, dtype=float)
    return np.polyfit(x, np.asarray(trace[lo:hi + 1], dtype=float), 1)


def _endzone_mirror_grey(trace, res_m, off, eol_km, prev_marker_end_km,
                         mirror_dist_km, mirror_start_km, mirror_end_km):
    """EXFO's corrected-anchor end-zone loss, or None when it doesn't fit.

    `mirror_dist_km` is the LOUD direction's stored event distance from ITS
    OWN launch; EOL minus that is the closure's distance from THIS side's
    launch, because both directions normalize to the same two connectors.
    `mirror_start_km` / `mirror_end_km` are that event's RAW LSA marker kms
    (tot_start_curr / tot_end_curr through the loud side's IOR) — only their
    difference is used, so the launch offset cancels.

    ONE absolute index, and it is the cable end.  Everything else is a
    DISTANCE — how far back the closure sits, how wide the mirror event is,
    how far back the previous marker is — converted to samples once, from the
    distance itself.  It used to convert each endpoint's own km separately and
    subtract the two whole numbers, which is not the same arithmetic: the
    kilometres are absolute, so the leftovers of two truncations 25,000
    samples out decided a width, and a pitch change far too small to move any
    real boundary could still flip it.  Measured over 1,152 WSC↔SUI traces, a
    25 ppm pitch change (1.6 m against a 2.55 m sample) reshaped the windows
    on 563 fibers — the mirror width moved on 61, the after-window's LENGTH on
    345 and the before-window's on 290.  Written as distances the same change
    reshapes nothing on any of the 1,152: the geometry either does not move at
    all, or slides rigidly by the one sample the cable end moved."""
    n = len(trace)

    def nsamp(km):                     # a DISTANCE in km -> whole samples
        return int(round(float(km) * 1000.0 / res_m))

    # The cable end is a sample — the file's own marker grid says so — so this
    # is a nearest-sample lookup, not a floor.  See the block comment above.
    ieol = int(round((float(eol_km) + off) * 1000.0 / res_m))
    if ieol >= n:
        return None
    cur_a = ieol - nsamp(mirror_dist_km)
    width = ENDZONE_MIRROR_MIN_WIDTH
    if mirror_start_km is not None and mirror_end_km is not None:
        width = max(width, nsamp(float(mirror_end_km) - float(mirror_start_km)))
    cur_b = cur_a + width
    if cur_b >= ieol - 1 or cur_b <= cur_a + 3:
        return None
    # (a metres constant, so it converts directly — no km round trip)
    sub_a = cur_a - int(round(ENDZONE_HALF_WIN_M / res_m))
    if prev_marker_end_km is not None:
        # The marker km is RAW; eol_km is in the event frame, so the distance
        # between them carries the launch offset (this is ridx()'s old job,
        # written as a distance back from the end).
        sub_a = max(sub_a, ieol - nsamp(float(eol_km) + off
                                        - float(prev_marker_end_km)))
    if cur_a - sub_a < ENDZONE_MIRROR_MIN_BEFORE:
        return None
    before = _endzone_ols(trace, sub_a, cur_a - 1)
    after = _endzone_ols(trace, cur_b, ieol)      # SubCursorB = idx(EOL), inclusive
    if before is None or after is None:
        return None
    # BOTH lines are read at CursorB — the calibrated evaluation point.
    return float(np.polyval(after, cur_b) - np.polyval(before, cur_b))


def _endzone_prev_marker_end_km(sor_data, ior, eol_km):
    """Highest LSA marker-end (raw km) among this fiber's own events that sit
    clear of the cable end — EXFO's SubCursorA clamp."""
    best = None
    for e in (sor_data.get('events') or []):
        if e.get('is_end'):
            continue
        dk = e.get('dist_km')
        if dk is None or dk >= eol_km - 0.2:
            continue
        tot = e.get('tot_end_curr')
        if not tot or tot <= 0:
            continue
        km = (tot * _TOT_M_PER_UNIT / ior) / 1000.0
        if best is None or km > best:
            best = km
    return best


def _endzone_frame_ok(trace, eol_idx):
    """Does the EVENT frame actually line up with the trace samples?

    A cable-end measurement lives or dies on the km→sample mapping, and the
    mapping's launch offset (_trace_offset_km) is inferred from the EVENT
    LIST — which can disagree with the trace.  Real case (Span 3 "Mecca V2"):
    the tech picked start/stop so the exported event distances are launch-
    referenced (offset reads 0), but the data-points block was NOT re-cut and
    still carries the 1.04 km pre-launch lead.  The V1 and V2 exports of the
    same fiber are byte-identical traces with different event framing, and
    indexing V2 with offset 0 measures 1.04 km INTO the fiber while believing
    it is 79 m past the launch (F284: +0.131 dB of pure frame error vs the
    correctly-framed -0.003).  Mid-span the shifted window still lands in
    fiber and reads plausible; at a cable end it fabricates flags.

    Cheap, decisive check: the far-end connector is a strong reflection (a
    DOWNWARD spike in the SOR convention) and the end event marks it, so a
    correct frame puts that reflection at idx(EOL).  One scalar offset covers
    the whole trace, so validating the far end validates the launch end too.
    No reflection there → the frame is not trustworthy → the caller refuses
    (fail closed: no measurement, no flag)."""
    n = len(trace)
    if eol_idx <= 130 or eol_idx >= n:
        return False
    tol = ENDZONE_ANCHOR_TOL_SAMPLES
    base = trace[max(0, eol_idx - 120):eol_idx - 10]
    if len(base) < 20:
        return False
    baseline = float(np.median(base))
    win = trace[max(0, eol_idx - tol):min(n, eol_idx + tol + 1)]
    if len(win) < 3:
        return False
    return float(np.min(win)) <= baseline - ENDZONE_ANCHOR_DIP_DB


def _endzone_launch_clear_km(sor_data, ior, off):
    """Where the launch connector's dead zone ends, in the EVENT frame.

    EXFO stores the launch event's own LSA markers; its CursorB
    (tot_end_curr) is exactly where the connector's recovery tail ends —
    45.9 m at 275 ns on WSC↔SUI, 429 m at 2500 ns on the Seattle .bdr
    fibers the fixed SILENT_LAUNCH_CLEAR_KM was calibrated on.  Reading it
    per file makes the clearance track the pulse width instead of pinning
    a long-pulse constant onto short-pulse traces.  Marker tots are RAW
    (normalization shifts dist_km/time_of_travel but not the markers), so
    subtract the launch offset to land back in the event frame."""
    evs = sor_data.get('events') or []
    if not evs:
        return ENDZONE_LAUNCH_CLEAR_KM
    e0 = evs[0]
    if not (e0.get('is_reflective') and not e0.get('is_end')):
        return ENDZONE_LAUNCH_CLEAR_KM
    tot_end = e0.get('tot_end_curr')
    if not tot_end or tot_end <= 0:
        return ENDZONE_LAUNCH_CLEAR_KM
    return max(0.0,
               (tot_end * _TOT_M_PER_UNIT / ior) / 1000.0 - float(off or 0.0))


# FastReporter refuses to believe a fit slope below 0.100 dB/km.  Read
# directly off its Markers tab: driving one event through 12 left-window
# lengths (20 -> 3918 samples) and a second event through 3, our plain OLS
# matched FR at every length whose fitted slope was >= 0.1 and diverged at
# every length below it, by 2.7 to 24.5 mdB, growing as the slope fell.
# Inverting FR's answer for the slope it must have used gives 0.101, 0.098,
# 0.103, 0.096, 0.095 dB/km on those five - a constant, not a trend.  With
# the floor applied, all 15 readings reproduce to <= 0.46 mdB, which is FR's
# own display resolution.
#
# Physically it is a sanity floor: real SMF at 1550 nm runs ~0.19 dB/km, so a
# window fitting flatter than 0.1 is measuring noise or an event tail, not
# glass.  It binds on 0.9% of records and is a pure improvement there -
# 42 records newly exact, 0 regressions, SEANOR still 496/496.
FR_SLOPE_FLOOR_DB_KM = 0.100

# ...and a ceiling at 0.500 dB/km, the same rule at the other end.  Found by
# inverting FR's own answers on the records the floor did not explain: every
# silent-side record whose window fits STEEPER than 0.5 implies a slope of
# 0.500000 dB/km, five of them to within 1e-12 — float-exact, not a fit.  A
# corpus sweep peaks sharply at 0.500 (0.49 and 0.51 are both worse).
# Physically the mirror of the floor: 0.5 dB/km is ~2.6x real SMF, so a window
# fitting that steep is sitting on an event, not on fiber.
FR_SLOPE_CEIL_DB_KM = 0.500

# ...and the slope a before-line is carried at when it has to be read past
# its own reach (see the conflicting-reach case in measure_fr_exact_loss):
# a nominal 0.2 dB/km, the textbook attenuation of SMF at 1550 nm, not the
# line's own slope and not a band edge.  Solved on 42 of 46 conflicting
# WSC<->SUI legs to four decimals; three probe files confirm it.
FR_EXT_SLOPE_DB_KM = 0.200
# Two windows whose raw slopes differ by less than this fraction of their mean
# agree on the attenuation, and the carry uses it (see measure_fr_exact_loss).
FR_CARRY_AGREE_FRAC = 0.10


def measure_fr_exact_loss(sor_data, cursor_a_m, cursor_b_m, sub_a_m, sub_b_m):
    """FastReporter's event loss, computed EXACTLY as FastReporter computes it.

    Reverse-engineered against 12 SEANOR .bdr ground-truth files and verified
    machine-exact on all 248 events (max error 0.000000 mdB).  Every piece is
    load-bearing:
      * fit on the PROPRIETARY RawSamples trace (dB = 64 − raw/1024), not the
        Bellcore DataPts — same signal, different quantisation grid, and the
        ~0.3 mdB structured difference does not cancel in a fit;
      * both OLS windows are INCLUSIVE of their boundary cursors:
        [SubCursorA .. CursorA] and [CursorB .. SubCursorB], in samples;
      * both fitted lines are evaluated at the MIDPOINT
        (CursorA_idx + CursorB_idx) / 2 — not at the event onset — pulled
        back toward a cursor when that cursor's outer window is shorter
        than the reach to the midpoint (see the evaluation point below);
      * indices come from the file's EXACT pitch (`exfo_res_m`, pinned by the
        marker lengths), not the IOR-derived estimate.

    Cursor inputs are METRES in the raw (untrimmed) frame, exactly as stored
    in the proprietary event list / KeyEvents markers.  Returns the loss in
    dB (positive = loss) or None when inputs are missing or windows are
    degenerate -- with one exception FastReporter itself makes: an after-
    window of exactly ONE sample (SubCursorB == CursorB) is fitted with the
    before-window's slope, see below."""
    raw = sor_data.get('exfo_raw')
    res = sor_data.get('exfo_res_m')
    if raw is None or not res or res <= 0:
        return None
    def idx(m):
        return int(round(float(m) / res))
    i1, i2 = idx(sub_a_m), idx(cursor_a_m)
    i3, i4 = idx(cursor_b_m), idx(sub_b_m)
    # ONE-SAMPLE BEFORE-WINDOW: the mirror of the one-sample after-window
    # below.  When a transplant's SubCursorA clamps onto its CursorA -- the
    # projection lands just past a panel connector inside the silent fiber's
    # launch reel -- FR draws the before-line through that one sample with
    # the AFTER-window's slope.  North 288f (5 ns panel, CHM3<->CHM4) fiber
    # 47: FR -0.040236006, this rule -0.040236006.  A sound measurement, not
    # an FR artefact, so both modes use it (Robert, 2026-09-23); the window
    # used to be refused and OTDR Suite mode fell back to its wide-LSA
    # reconstruction there.
    if 0 <= i1 == i2 < i3 < i4 < len(raw):
        wa, wb = 0, i4 - i3
        x2 = np.arange(i3, i4 + 1, dtype=float)
        y2 = 64.0 - raw[i3:i4 + 1].astype(float) / 1024.0
        m2, b2 = np.polyfit(x2, y2, 1)
        m1 = m2
        b1 = (64.0 - float(raw[i1]) / 1024.0) - m1 * i1
    else:
        if not (0 <= i1 < i2 < i3 <= i4 < len(raw)):
            return None
        wa, wb = i2 - i1, i4 - i3
        # Two samples are enough for FastReporter: it fits a before-window of
        # one sample-pair (fiber 0057 A, wa = 1) and after-windows of two to ten
        # samples (0017 B, wb = 1; 0237 B, wb = 9) and its stored losses come
        # back to 0.000000 mdB from exactly those samples -- see the evaluation
        # point below, which is what makes a short window well-behaved.  The
        # 8-sample floor this function shipped with was ours, not FR's.
        x1 = np.arange(i1, i2 + 1, dtype=float)
        y1 = 64.0 - raw[i1:i2 + 1].astype(float) / 1024.0
        m1, b1 = np.polyfit(x1, y1, 1)
    if wa == 0:
        pass                              # both lines already set above
    elif i4 == i3:
        # A ONE-SAMPLE after-window: SubCursorB == CursorB.  FastReporter
        # writes this for a transplanted (silent-side) event whose window ran
        # into the silent direction's own next event -- it pulls CursorB back
        # to that event's position and SubCursorB with it (see
        # _fr_exact_silent_loss).  One sample cannot carry a slope, so the
        # after-line is that sample with the BEFORE-window's fitted slope:
        # verified 0.000000 mdB on all 13 such records in the Zayo 432 .bdr
        # set, against FR's own stored float64 loss.  With the evaluation
        # point pulled to CursorB (below) the slope never enters the answer.
        m2 = m1
        b2 = (64.0 - float(raw[i3]) / 1024.0) - m1 * i3
    else:
        x2 = np.arange(i3, i4 + 1, dtype=float)
        y2 = 64.0 - raw[i3:i4 + 1].astype(float) / 1024.0
        m2, b2 = np.polyfit(x2, y2, 1)
    mid = (i2 + i3) / 2.0
    # THE EVALUATION POINT.  Both lines are read at the event midpoint --
    # but FastReporter never extrapolates a fitted line further past its
    # cursor than the window it was fitted on is long.  A before-window of
    # wa samples is read no further than wa samples past CursorA; an
    # after-window of wb samples no further than wb samples before CursorB:
    #
    #     x = clamp(mid,  CursorB - wb,  CursorA + wa)
    #
    # and BOTH lines are evaluated there.  Read off the Zayo 432 .bdr set,
    # where FR's stored losses on 28 silent-side records with a clamped
    # outer window sat a half-integer number of samples off our midpoint
    # answer, and that number was (mid - CursorA) - wa on every one.  With
    # x in place all 28 reproduce to 0.000000 mdB (27 from the geometry
    # alone, one more with the merge rule in _fr_exact_silent_loss).  On a
    # full-width window (thousands of samples against a ~25-sample half
    # inner width) x is the midpoint and nothing changes: the 4,709 loud
    # legs of that set were bit-identical before and after.
    x = mid
    x = min(x, i2 + wa)
    x = max(x, i3 - wb)
    xa = xb = x
    # WHEN THE TWO REACHES CONFLICT -- CursorB - wb lies PAST CursorA + wa,
    # both windows too short to meet at the midpoint -- the read point is
    # the after-window's limit, CursorB - wb, and the before-line is carried
    # from its own limit, CursorA + wa, to that point at a NOMINAL
    # 0.2 dB/km (FR_EXT_SLOPE_DB_KM): neither its fitted slope nor the
    # band edge it was rotated to.  Read off WSC<->SUI Splice 12 (275 ns,
    # 40-90 m from the far connector, both windows short), where 46 of
    # FastReporter's own synthesised legs conflict: solving each one for
    # the slope FR used over the conflicting stretch gave 0.2000 dB/km on
    # 42 of them, whatever the line's own slope (0.06 to 3.5 dB/km) and
    # whichever way it was clamped, and three probe files with the same
    # geometry and clean 0.05, 0.2 and 1.0 dB/km lines written into the
    # window agreed.  41 of the 46 reproduce to 0.000000 mdB with it; no
    # Zayo or SEANOR record has conflicting reaches, so nothing there moves.
    # On flat windows this is what the earlier 'read each line at the
    # other's limit' reading was measuring: the two agree there exactly.
    ext_c = 0.0
    if (i3 - wb) > (i2 + wa):
        xa = i2 + wa
        ext_c = (i3 - wb) - (i2 + wa)
    # Slope band (see FR_SLOPE_FLOOR_DB_KM / FR_SLOPE_CEIL_DB_KM).  A window
    # fitting outside it is not measuring glass, so FastReporter rotates the
    # line to the nearest edge, holding the fitted value at the window's FIRST
    # sample fixed.
    floor = FR_SLOPE_FLOOR_DB_KM * res / 1000.0        # dB per sample
    ceil = FR_SLOPE_CEIL_DB_KM * res / 1000.0

    def _level(m, b, anchor, x):
        s = floor if m < floor else (ceil if m > ceil else m)
        return (m * anchor + b) + s * (x - anchor) if s != m else m * x + b

    # THE CARRY SLOPE.  Across the conflict stretch FR carries the before-line
    # at the nominal FR_EXT_SLOPE_DB_KM -- unless the two windows AGREE on the
    # fibre's attenuation, in which case it carries at that measured
    # attenuation: the magnitude of the mean of the two RAW fitted slopes,
    # taken when they differ by less than FR_CARRY_AGREE_FRAC of that mean.
    # Pinned by 15 clean-line probes on WSC<->SUI fibre 34 written with
    # setline.py and run through FastReporter (2026-09-22): -2/-2, -1/-1,
    # +0.3/+0.3, +1/+1, -2/-1.85 (7.6 %), -4/-3.7 (8.4 %) and -2/-1.81
    # (9.7 % of the mean, 10.2 % of the smaller) all carry at |mean|;
    # -2/-1.8 (10.4 % of the mean, 9.9 % of the larger), -0.5/-0.4 (14 %),
    # -2/-1.7, -2/-1.5, -2/-1, -1/-2, -0.7/-3.3 and -2/+0.3 all carry at 0.2.
    # The sign is dropped: an uphill fit (negative slope into the far-end
    # reflection) still carries as a loss.  The real cases that found it:
    # WSC fibres 324 (-2.14/-1.98) and 437 (-1.01/-0.92), 4.7 and 15.6 mdB
    # off under the nominal carry, exact under this one, and the 0.2 and
    # 0.03 mdB residuals on fibres 145 and 34 (in-band slopes 0.16/0.16 and
    # 0.196/0.192) that the nominal carry had left.
    ext = FR_EXT_SLOPE_DB_KM * res / 1000.0                 # dB per sample
    if ext_c > 0 and i4 > i3:
        mean_m = (m1 + m2) / 2.0
        if mean_m != 0.0 and abs(m1 - m2) < FR_CARRY_AGREE_FRAC * abs(mean_m):
            ext = abs(mean_m)
    return float(_level(m2, b2, i3, xb) - (_level(m1, b1, i1, xa) + ext * ext_c))


def measure_endzone_grey_from_sor(sor_data, position_km, ior=None,
                                  mirror_dist_km=None,
                                  mirror_marker_start_km=None,
                                  mirror_marker_end_km=None):
    """Reconstruct the splice loss at a SILENT position that sits within
    ENDZONE_REACH_KM of a cable end — the geometry measure_silent_grey_from_sor
    structurally cannot fit (see the block comment above).

    Preferred: EXFO's CORRECTED-ANCHOR geometry, used when the caller can name
    the loud (mirror) direction's stored event for this closure — pass its
    distance from its own launch plus its raw LSA marker kms.  That path only
    applies at the FAR end, where SubCursorB = idx(EOL) means what it says.

    Fallback (no mirror, near-launch, or the corrected windows don't fit):
    the shared-slope two-line LSA this function shipped with —
      before = [launch-clear .. P]          (clamped to the previous event)
      after  = [P + dead zone .. EOF-guard] (clamped to the next event)
    the slope comes from whichever window is longer, both levels are that
    slope's residual mean, and the loss is after-level − before-level.

    Returns the loss in dB (positive = real loss), or None when the position
    isn't near a cable end, when either window is too short to average, or
    when no window is long enough to carry a trustworthy slope."""
    trace = sor_data.get('trace')
    if trace is None or len(trace) < 50:
        return None
    if ior is None:
        ior = _sor_ior(sor_data)
    res_m = _sor_res_m(sor_data, ior)
    if res_m <= 0:
        return None

    P = float(position_km)
    n = len(trace)
    off = float(sor_data.get('_trace_offset_km') or 0.0)

    def idx(km):
        return int((km + off) * 1000.0 / res_m)

    eol = None
    for e in (sor_data.get('events') or []):
        if e.get('is_end'):
            eol = e['dist_km']
            break
    if eol is None or P <= 0 or P >= eol:
        return None

    launch_clear = _endzone_launch_clear_km(sor_data, ior, off)
    hard_end = eol - ENDZONE_EOF_GUARD_KM

    # Scope guard: this reconstruction exists for cable-end geometry only.
    # Anywhere else the EXFO-exact windower is the right (and calibrated)
    # instrument and a None from it means "don't trust this position".
    if (P - launch_clear) > ENDZONE_REACH_KM and (hard_end - P) > ENDZONE_REACH_KM:
        return None

    tr = np.asarray(trace, float)
    if not _endzone_frame_ok(tr, idx(eol)):
        return None                    # event frame ≠ trace frame — see above

    # ── EXFO's corrected-anchor geometry (preferred) ──
    # Needs the loud direction's stored event, and only means anything at the
    # FAR end: SubCursorB is idx(EOL), so a near-launch position would make
    # the after-window swallow the whole cable.  Anything it can't fit falls
    # through to the shared-slope reconstruction below.
    if mirror_dist_km is not None:
        anchor_km = eol - float(mirror_dist_km)
        if (abs(anchor_km - P) <= ENDZONE_MIRROR_TOL_KM
                and 0.0 < (eol - anchor_km) <= ENDZONE_REACH_KM):
            g = _endzone_mirror_grey(
                tr, res_m, off, eol,
                _endzone_prev_marker_end_km(sor_data, ior, eol),
                mirror_dist_km, mirror_marker_start_km, mirror_marker_end_km)
            if g is not None:
                return g

    # Neighbouring real events clamp both windows (same rule as the EXFO
    # geometry — never fit across another event's step).
    kms = sorted(e['dist_km'] for e in (sor_data.get('events') or [])
                 if not e.get('is_end') and e['dist_km'] > 0.001)
    prevs = [k for k in kms if k < P - ENDZONE_DEAD_ZONE_KM]
    nexts = [k for k in kms if k > P + ENDZONE_DEAD_ZONE_KM]
    b_lo = max(P - ENDZONE_LONG_WIN_KM, launch_clear)
    if prevs:
        b_lo = max(b_lo, max(prevs) + ENDZONE_DEAD_ZONE_KM)
    a_lo = P + ENDZONE_DEAD_ZONE_KM
    a_hi = min(a_lo + ENDZONE_LONG_WIN_KM, hard_end)
    if nexts:
        a_hi = min(a_hi, min(nexts))
    if b_lo >= P or a_lo >= a_hi:
        return None

    def window(lo_km, hi_km):
        lo = max(0, idx(lo_km)); hi = min(n - 1, idx(hi_km))
        if hi - lo < ENDZONE_MIN_LEVEL_SAMPLES:
            return None
        seg = tr[lo:hi]
        m = (seg > 0.5) & (seg < 63.5)
        if int(m.sum()) < ENDZONE_MIN_LEVEL_SAMPLES:
            return None
        return np.arange(lo, hi)[m].astype(float), seg[m]

    wb = window(b_lo, P)
    wa = window(a_lo, a_hi)
    if wb is None or wa is None:
        return None

    # Slope from the LONG side; the short side only contributes a level.
    long_x, long_y = wb if len(wb[0]) >= len(wa[0]) else wa
    if len(long_x) < ENDZONE_MIN_SLOPE_SAMPLES:
        return None
    slope = float(np.polyfit(long_x, long_y, 1)[0])

    Pi = float(idx(P))
    lvl_b = float(np.mean(wb[1] - slope * (wb[0] - Pi)))
    lvl_a = float(np.mean(wa[1] - slope * (wa[0] - Pi)))
    return lvl_a - lvl_b


def parse_sor_full(filepath, trim=True):
    with open(filepath, 'rb') as f:
        data = f.read()
    blocks = _parse_block_directory(data)
    full_trace, pts_trace, scale = _parse_data_pts(data, blocks)
    if full_trace is None:
        return None
    fxd    = _parse_fxd_params(data, blocks)
    sup    = _parse_sup_params(data, blocks)
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
        # Never cut short of the file's own event table — see _trim_end_floor.
        _prop_sp = (_parse_proprietary_block(data, blocks) or {}).get('sampling_period')
        if _prop_sp and _prop_sp > 0:
            _res = 299_792_458.0 * float(_prop_sp) / 2.0 / _read_ior(data)
            _floor = _trim_end_floor(events, _res, len(full_trace))
            if _floor is not None:
                ei = max(ei, min(_floor, len(full_trace) - 1))
    trace = full_trace[si:ei + 1]
    # GenParams identity metadata — the file's INTERNAL cable/fiber/route ids,
    # independent of whatever the filename says.  Cheap (scans a few hundred
    # bytes of the already-read buffer), always on; {} on structural surprise
    # keeps every gen_* field a plain '' string.
    gp = parse_genparams(data)
    result = {
        'filename': os.path.basename(filepath), 'filepath': filepath,
        'gen_cable_id': gp.get('cable_id', ''),
        'gen_fiber_id': gp.get('fiber_id', ''),
        'gen_loc_a':    gp.get('loc_a', ''),
        'gen_loc_b':    gp.get('loc_b', ''),
        'num_points': len(trace), 'trace': trace,
        'min_db': float(trace.min()), 'max_db': float(trace.max()),
        'mean_db': float(trace.mean()), 'wavelength': fxd.get('wavelength'),
        'acq_range': acq_range, 'events': events,
        'start_index': si, 'end_index': ei,
        'full_points': len(full_trace),
        'date_time': fxd.get('date_time', 0),
        'duration_sec': fxd.get('duration_sec'),
        # OTDR model / serial from SupParams (used by the acquisition-
        # parameters audit sheet).  Empty strings when the block is
        # missing or the supplier left the fields blank.
        'otdr_model':  sup.get('otdr_model', ''),
        'otdr_serial': sup.get('otdr_serial', ''),
        'sup_params':  sup,
        # Reflectance calibration inputs (see _parse_fxd_params).
        'backscatter_db': fxd.get('backscatter_db'),
        'fxd_pulse_ns':   fxd.get('fxd_pulse_ns'),
        # FastReporter "Test Settings" inputs (see _parse_test_settings).
        # `ior` prefers the proprietary float64 and falls back to the
        # Bellcore group index; both are filled in below.  `fiber_type` is
        # GenParams' glass designation (652 = ITU-T G.652).
        'ior':         fxd.get('group_index'),
        'fiber_type':  read_genparams_fiber_type(data),
    }
    # ── Augment with EXFO proprietary block data when present ──
    prop = _parse_proprietary_block(data, blocks)
    if prop:
        result['test_settings'] = prop['test_settings']
        # The proprietary Ior carries FastReporter's full 6-dp precision
        # (1.468325); the Bellcore group index quantises to 5 (1.46832).
        # Prefer the former, keep the latter as the fallback already set.
        if prop['test_settings'].get('Ior') is not None:
            result['ior'] = prop['test_settings']['Ior']
        result['exfo_calibration']    = prop['calibration']
        result['exfo_events']         = prop['exfo_events']
        result['exfo_raw']            = prop['raw_trace']
        result['exfo_res_m']          = prop['res_m_exact']
        result['exfo_spans_loss']     = prop['spans_loss']
        result['exfo_spans_length']   = prop['spans_length']
        result['exfo_total_orl']      = prop['total_orl']
        result['exfo_sampling_period']= prop['sampling_period']
        result['exfo_wavelength_nm']  = prop['exact_wavelength_nm']
        result['exfo_injection_level']= prop['injection_level']
        result['exfo_saturation_level']= prop['saturation_level']
    else:
        result['test_settings']        = {}
        result['exfo_calibration']     = None
        result['exfo_events']          = None
        result['exfo_raw']             = None
        result['exfo_res_m']           = None
        result['exfo_spans_loss']      = None
        result['exfo_spans_length']    = None
        result['exfo_total_orl']       = None
        result['exfo_sampling_period'] = None
        result['exfo_wavelength_nm']   = None
        result['exfo_injection_level'] = None
        result['exfo_saturation_level']= None

    # ── Declared span start (Bellcore GenParams user offset) ────────────────
    # When the tech sets the span start on the launch connector, EXFO writes
    # the event table RELATIVE TO THAT POINT while the DataPts samples stay
    # relative to the OTDR port.  Every table distance is therefore short of
    # its raw-trace sample by this much (1.0044 km on a 1 km launch reel;
    # 0.0 on an untrimmed file).  Any code that indexes the raw trace at a
    # table position must add it.  OGD->SLK 2026-09-09: without it the uni
    # tail-box probe read live glass 1 km BEFORE the cable cut, called the cut
    # a mated connector, and the cable end vanished from the report.
    result['user_offset_km'] = _read_user_offset_km(data, result.get('ior'))

    # ── Full-precision splice loss from EXFO's own event block ──────────────
    # The Bellcore KeyEvents block stores splice loss as an int16 in
    # MILLIDECIBELS, so every value it carries is quantized to 1 mdB.  EXFO's
    # proprietary block carries the same events at full float precision, and
    # FastReporter reads THAT block (see project-fr-reads-proprietary-block).
    # Two 0.5 mdB errors compound off the quantized read: averaging two
    # quantized legs into a bidirectional value can move the result by up to
    # 0.5 mdB, which is enough to carry a cell across the 0.160 reburn gate.
    #
    # Only applied when the two event lists align 1:1 AND each pair agrees on
    # position to <10 m, so a file whose proprietary block is truncated or
    # differently populated silently keeps the quantized value.
    _all = list(result.get('exfo_events') or [])
    _ex = [e for e in _all if not e.get('_is_section')]
    # The section that ENDS at event k is the one immediately before it in the
    # interleaved E,S,E,S stream — i.e. the section whose Position is event
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
    if _ex:
        for _key in ('events', '_raw_events'):
            _ke = result.get(_key) or []
            if len(_ke) != len(_ex):
                continue
            for _a, _b in zip(_ke, _ex):
                _p = _b.get('Position')
                # Alignment guard, evaluated against the ORIGINAL dist_km
                # before anything is upgraded.
                if (not isinstance(_p, float)
                        or abs(_a['dist_km'] * 1000.0 - _p) >= 10.0):
                    continue
                _l = _b.get('Loss')
                # NaN-safe: `_l != _l` catches a NaN written into the block.
                # A REFLECTIVE event legitimately has none — FR stores no loss
                # for one — so the loss upgrade is skipped while the position
                # and slope upgrades below still apply.
                if _l is not None and _l == _l:
                    _a['splice_loss'] = float(_l)
                    # Provenance stamp.  Downstream arithmetic has to know
                    # whether a leg carries EXFO's full float or a 1 mdB
                    # (KeyEvents) / 3-dp (JSON) quantized copy: a tie-break
                    # between two quantized legs is decided by IEEE-754
                    # representation, which is deterministic but arbitrary.
                    _a['loss_full_precision'] = True
                # Position: the KeyEvents time-of-travel is an integer with a
                # 0.0204 m quantum, so a tot-derived distance lands up to
                # 0.15 m from EXFO's own float64 — and the .bdr path already
                # uses the float64.  Same rounding as `_build_events` so a
                # fiber read from .sor and from .bdr gives the same number.
                _a['dist_km'] = round(_p / 1000.0, 4)
                # Section attenuation: KeyEvents stores it as int16 millibels,
                # so ours was exact only to 1 mdB (max 0.476 mdB observed on
                # ORPVL 212) while EXFO's float64 sat unused in this block.
                _s = _sec_for.get(_p)
                if _s is not None:
                    _a['slope'] = float(_s)
                # Reflectance, same story: KeyEvents quantizes it, and the
                # block carries EXFO's float.  Only upgraded where FR actually
                # recorded one — a NaN here means "not a reflective event",
                # which `is_reflective` already carries, and writing 0.0 over
                # it would be indistinguishable from a real reading.
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



# ═════════════════════════════════════════════════════════════════════
#  FastReporter .bdr — the bidirectional report as a Splice Report input
# ═════════════════════════════════════════════════════════════════════
#
#  bdr_reader.py — Parse EXFO FastReporter bidirectional report files (.bdr).
#
#  WHY THIS EXISTS
#  ---------------
#  A `.sor` is ONE direction of ONE fiber.  A `.bdr` is FastReporter's saved
#  bidirectional analysis: BOTH directions of one fiber in a single file, and
#  it carries strictly more than the two .sor files would:
#
#    * both full RawSamples traces (the proprietary trace FR itself fits on),
#    * both per-direction event lists with complete LSA cursor quintets,
#    * FR's own MERGED bidirectional table (MeanPosition / MeanLoss / MeanLength),
#    * the silent-side records FR synthesized by transplanting the detecting
#      direction's geometry (see project-fr-silent-window) — values we otherwise
#      have to reconstruct.
#
#  So a crew that hands us .bdr instead of .sor is handing us a SUPERSET.  This
#  module unpacks one .bdr into the two per-direction dicts the splice engine
#  already knows how to consume, so the whole pipeline downstream — gates,
#  grid, Excel, Zach's PDF, the Viewer — runs unchanged.
#
#  CONTAINER FORMAT
#  ----------------
#  A .bdr is the same "AppReg Format Ex" container the .sor proprietary block
#  uses, just nested one level deeper.  The SECOND container starts at byte 55,
#  its chunk stream at 55+36 = 91, then `[4B LE size][zlib chunk]` repeating to
#  EOF.  Concatenate the inflated chunks and the result is one flat field
#  stream with the familiar descriptor layout:
#
#      [self_offset 4B] [type_code 4B] [data_size 4B] [next_ref 4B] Name\0 [value]
#
#  Type codes: 1 = uint32, 2 = binary array, 3 = float64, 4 = UTF-16LE string.
#
#  STREAM LAYOUT (verified on 24 ORPVL fibers and 12 SEANOR fibers)
#  ----------------------------------------------------------------
#      fiber header      Identifier / Cable / LocationA / LocationB / Date
#      RawSamples  #1    TraceAB, followed by its acquisition settings
#      RawSamples  #2    TraceBA, followed by its acquisition settings
#      directories       Event0..EventN name tables + per-direction META
#                        (UserNameA/B, ModelName, SerialNumber)
#      record blocks     merged bidi table (MeanPosition records), then the
#                        A event list, then the B event list, then one
#                        two-record block per merged row (the A-frame and
#                        B-frame view of that row)
#
#  Record blocks are separated by POSITION RESETS.  Every list runs ascending
#  from 0 in its OWN frame, so a .bdr event list needs no reframing — it is
#  already what a .sor of that direction would carry.
#
#  DIRECTION BINDING — the one thing not to guess
#  ----------------------------------------------
#  Binding an event list to the right trace matters more than anything else
#  here: get it backwards and every silent-side measurement is fitted against
#  the wrong glass (that exact mistake cost a week on SEANOR — see
#  project-fr-silent-window's "mis-framing invents phantom silent events").
#
#  The binding is not inferred from order.  Each direction's first event record
#  carries a `CurveLevel` equal to that trace's own `InjectionLevel` — an
#  independently stored float64 that has matched to <1e-6 on 36/36 fibers
#  across two unrelated spans.  We match on that and cross-check that both
#  lists terminate within 50 m of each other (they must: it is one span
#  measured from both ends).
#
#  VALIDATION
#  ----------
#  Feeding each list's own stored cursors and its bound trace back through
#  `measure_fr_exact_loss` reproduces FR's own stored Loss on 259 of 278 ORPVL
#  events at max error 0.000000 mdB (median 0.000000).  The 19 residuals are
#  all A-direction, median ~3 mdB, and do not affect reported values: the
#  engine prefers the stored full-precision Loss whenever positions align, the
#  same as it does for .sor.
#
#  Public API
#  ----------
#      parse_bdr(filepath)        -> {'a': dict, 'b': dict, 'merged': [...]}
#      parse_bdr_side(fp, side)   -> dict  ('a' or 'b')
#      is_bdr(filename)           -> bool
#
#  LIVES HERE ON PURPOSE.  This is shipped engine code, and the installed
#  launcher rejects any update manifest whose file set differs from the
#  ENGINE_FILES baked into its exe — so a NEW engine module freezes fleet
#  hot-updates until every tech runs a fresh installer.  New engine code
#  folds into an existing engine file (see project-sor-writer, where the
#  SOR writer went into viewer/trace_server.py for the same reason).
#  sor_reader324802a.py is the right host: a .bdr is the same EXFO
#  container as the .sor proprietary block, and the FR-exact fitter this
#  reader's cursors feed already lives here.

# The .bdr's outer wrapper: second container at byte 55, chunk stream 36
# bytes into it.  Both constants are structural, not heuristic — they hold on
# every .bdr seen from FR3 (SEANOR 2026-06, ORPVL 2026-09).
_CHUNK_STREAM_OFFSET = 55 + 36

# Hard ceiling on the inflated stream.  A corrupt or hostile chunk can
# inflate ~1000x under DEFLATE; the hub parses uploads in-process, so an
# unbounded decompress is an OOM away from taking down every page.  Real
# files inflate to a few hundred KB per fiber.
_MAX_INFLATED_BYTES = 256 * 1024 * 1024

# Per-event record fields we keep.  Same set the .sor proprietary-block
# parser keeps, so `exfo_events` from a .bdr is shape-identical to
# `exfo_events` from a .sor and every downstream consumer is none the wiser.
_RECORD_FIELDS = (
    'Length', 'Loss', 'Type', 'Status', 'CurveLevel', 'Reflectance',
    'PeakReflectionToRbs', 'LocalNoise', 'SplitterRatio',
    'InvalidAttenuation',
    'SubCursorAPosition', 'CursorAPosition',
    'CursorBPosition', 'SubCursorBPosition',
)

# Bellcore group index fallback.  Only used when the file carries no `Ior`
# (never seen in practice — it is stored per direction).
_IOR_FALLBACK = 1.468325

# Bellcore KeyEvents distance constant, kept bit-identical to
# sor_reader324802a so a .bdr event's `time_of_travel` round-trips through
# the same formula the .sor path uses.
_TOT_C = 0.02998

# How far the two directions' measured end-of-fiber may disagree before the
# file is rejected as not-one-span.  Relative, because the disagreement is a
# property of length, not a fixed metre count (see parse_bdr).
_SPAN_END_TOL_FRACTION = 0.005     # 0.5% of span
_SPAN_END_TOL_MIN_M = 100.0        # absolute floor for short spans


# ═══════════════════════════════════════════════════════════════════════
#  Container / field decoding
# ═══════════════════════════════════════════════════════════════════════

def is_bdr(filename: str) -> bool:
    return str(filename).lower().endswith('.bdr')


def _inflate(path: str) -> bytes:
    """Concatenate the .bdr's zlib chunk stream into one flat field stream."""
    with open(path, 'rb') as fh:
        data = fh.read()
    off = _CHUNK_STREAM_OFFSET
    out, total = [], 0
    while off + 4 <= len(data):
        size = struct.unpack_from('<I', data, off)[0]
        off += 4
        if size == 0 or off + size > len(data):
            break
        try:
            chunk = zlib.decompress(data[off:off + size])
        except zlib.error:
            # A tail chunk that is not zlib ends the stream; everything
            # already inflated stays usable.  Partial reads are normal at
            # EOF and are not an error condition.
            break
        total += len(chunk)
        if total > _MAX_INFLATED_BYTES:
            raise ValueError(f"{os.path.basename(path)}: inflated stream "
                             f"exceeds {_MAX_INFLATED_BYTES} bytes")
        out.append(chunk)
        off += size
    return b''.join(out)


def _decode_fields(stream: bytes) -> list[dict]:
    """Walk the flat stream and return every named field, in stream order.

    Values are decoded for the four type codes that carry them; containers
    (type 0) come back with value None and are kept because their NAMES are
    the structure (FiberAB, TraceBA, EventAB, ...).
    """
    fields = []
    p, n = 0, len(stream)
    while p < n - 1:
        end = stream.find(b'\x00', p)
        if end < 0:
            break
        ln = end - p
        if 2 <= ln < 100 and p >= 16:
            try:
                name = stream[p:end].decode('ascii')
            except UnicodeDecodeError:
                name = None
            if name and name.isprintable() and name[0].isalpha():
                tc = struct.unpack_from('<I', stream, p - 12)[0]
                dsz = struct.unpack_from('<I', stream, p - 8)[0]
                voff = end + 1
                val: Any = None
                if tc == 3 and dsz == 8 and voff + 8 <= n:
                    val = struct.unpack_from('<d', stream, voff)[0]
                elif tc == 1 and dsz == 4 and voff + 4 <= n:
                    val = struct.unpack_from('<I', stream, voff)[0]
                elif tc == 4 and 0 < dsz <= 1024 and voff + dsz <= n:
                    val = (stream[voff:voff + dsz]
                           .decode('utf-16-le', errors='replace')
                           .split('\x00')[0])
                fields.append({'offset': p, 'name': name, 'type_code': tc,
                               'data_size': dsz, 'value': val})
        p = end + 1
    return fields


def _raw_traces(stream: bytes) -> list[np.ndarray]:
    """Every RawSamples blob, in stream order (AB first, then BA)."""
    out, off = [], 0
    needle = b'RawSamples\x00'
    while True:
        i = stream.find(needle, off)
        if i < 0:
            break
        if i >= 16:
            tc = struct.unpack_from('<I', stream, i - 12)[0]
            dsz = struct.unpack_from('<I', stream, i - 8)[0]
            voff = i + len(needle)
            if tc == 2 and dsz >= 4 and voff + dsz <= len(stream):
                out.append(np.frombuffer(stream, dtype='<u2',
                                         count=dsz // 2, offset=voff).copy())
        off = i + 1
    return out


def _scalars(fields: list[dict], name: str) -> list:
    return [f['value'] for f in fields
            if f['name'] == name and f['value'] is not None]


def _first(fields: list[dict], name: str, default=None):
    v = _scalars(fields, name)
    return v[0] if v else default


def _nth(fields: list[dict], name: str, i: int, default=None):
    v = _scalars(fields, name)
    return v[i] if len(v) > i else default


# ═══════════════════════════════════════════════════════════════════════
#  Record blocks
# ═══════════════════════════════════════════════════════════════════════

def _record_blocks(fields: list[dict]) -> list[list[dict]]:
    """Group the stream's event/section records and split on position resets.

    A record starts at each `Position` (own-direction lists and the paired
    per-frame views) or `MeanPosition` (FR's merged bidi table) field and
    collects the record fields that follow it.  `setdefault` matters: a
    record's fields appear once each, and a nested sub-object can repeat a
    name before the next Position — first write wins, which is the record's
    own value.
    """
    recs: list[dict] = []
    cur: Optional[dict] = None
    for f in fields:
        name, val = f['name'], f['value']
        if name in ('Position', 'MeanPosition'):
            if cur is not None:
                recs.append(cur)
            cur = {'Position': val, '_off': f['offset'],
                   '_merged': name == 'MeanPosition'}
        elif cur is not None:
            if name in _RECORD_FIELDS:
                cur.setdefault(name, val)
            elif name in ('MeanLength', 'MeanLoss'):
                cur.setdefault(name[4:], val)
    if cur is not None:
        recs.append(cur)

    blocks: list[list[dict]] = []
    block: list[dict] = []
    prev = None
    for r in recs:
        pos = r.get('Position')
        if prev is not None and isinstance(pos, float) and pos < prev - 500.0:
            blocks.append(block)
            block = []
        if isinstance(pos, float):
            prev = pos
        block.append(r)
    if block:
        blocks.append(block)
    return blocks


def _sub_record(stream: bytes, ptrs: list[int], depth: int = 0) -> dict:
    """Decode one nested sub-record from its pointer array.

    A container field (type 0) stores a list of 4-byte offsets, each pointing
    at a 16-byte field header laid out as
    ``[name_offset][type_code][data_size][value_offset]``.  This is the same
    data `_decode_fields` walks, reached by pointer instead of by scanning,
    which is what keeps a sub-record's fields attached to their own record
    instead of being flattened into the parent by `_record_blocks`.
    """
    out: dict = {}
    for off in ptrs:
        try:
            name_off, tc, dsz, voff = struct.unpack_from('<IIII', stream, off)
        except struct.error:
            continue
        end = stream.find(b'\x00', name_off)
        if end < 0 or end - name_off > 100:
            continue
        try:
            name = stream[name_off:end].decode('ascii')
        except UnicodeDecodeError:
            continue
        if tc == 0:
            if depth > 2:
                continue
            try:
                sub = [struct.unpack_from('<I', stream, voff + 4 * k)[0]
                       for k in range(dsz // 4)]
            except struct.error:
                continue
            out[name] = _sub_record(stream, sub, depth + 1)
        elif tc == 3 and dsz == 8 and voff + 8 <= len(stream):
            out[name] = struct.unpack_from('<d', stream, voff)[0]
        elif tc == 1 and dsz == 4 and voff + 4 <= len(stream):
            out[name] = struct.unpack_from('<I', stream, voff)[0]
        elif tc == 4 and 0 < dsz <= 1024 and voff + dsz <= len(stream):
            out[name] = (stream[voff:voff + dsz]
                         .decode('utf-16-le', errors='replace').split('\x00')[0])
    return out


def _merged_sub_records(stream: bytes,
                        fields: list[dict]) -> dict[float, dict]:
    """FR's per-direction legs for each merged bidi row, keyed by MeanPosition.

    Each merged row carries an `EventAB` and an `EventBA` container holding
    that direction's own Loss and its four cursors.  `_record_blocks`
    deliberately flattens the stream (first write wins), which keeps the
    merged row's own scalars but discards these two legs — so the split
    between the A and B measurements behind FR's mean is otherwise lost.

    Returns {stream offset of the MeanPosition field: {'EventAB': {...},
    'EventBA': {...}}}.  The key is the offset and NOT the position value:
    every merged position appears twice, once for the event row and once for
    the section row that follows it, so keying on the value collides and the
    section's legs overwrite the event's.  `_record_blocks` stamps the same
    offset on each merged record as `_off`, which makes the join exact.
    """
    out: dict[float, dict] = {}
    cur: dict = {}
    for f in fields:
        name = f['name']
        if name in ('EventAB', 'EventBA') and f['type_code'] == 0:
            voff = f['offset'] + len(name) + 1
            try:
                ptrs = [struct.unpack_from('<I', stream, voff + 4 * k)[0]
                        for k in range(f['data_size'] // 4)]
            except struct.error:
                continue
            if name == 'EventAB':
                cur = {}
            cur[name] = _sub_record(stream, ptrs)
        elif name == 'MeanPosition':
            if cur:
                out[f['offset']] = cur
            cur = {}
    return out


def _tag_sections(records: list[dict]) -> list[dict]:
    """A record is an EVENT iff the truck wrote a CurveLevel for it; the
    interleaved records without one are fiber SECTIONS (the span between two
    events).  Same rule the .sor proprietary parser uses."""
    for r in records:
        r['_is_section'] = 'CurveLevel' not in r
    return records


def _own_lists(blocks: list[list[dict]],
               injections: list[float]) -> list[list[dict]]:
    """Bind each direction's event list to its trace via CurveLevel.

    See DIRECTION BINDING in the module docstring: the first event's
    CurveLevel IS that trace's InjectionLevel.  Matching on the value rather
    than on block order is what keeps a file with extra record blocks (the
    3- and 5-record oddballs, and the reflectance-only tail) from being
    mistaken for an event list.
    """
    out = []
    for lvl in injections[:2]:
        for b in blocks:
            if not b or b[0].get('_merged') or len(b) < 3:
                continue
            if any(b is o for o in out):
                continue
            head = b[0].get('CurveLevel')
            start = b[0].get('Position')
            last = b[-1].get('Position')
            if (head is not None and abs(head - lvl) < 1e-6
                    and isinstance(start, float) and start < 1.0
                    and isinstance(last, float) and last > 1000.0):
                out.append(b)
                break
    return out


def _exact_pitch(records: list[dict], sampling_period: Optional[float],
                 ior: float) -> Optional[float]:
    """The exact sample pitch in metres.

    Marker Lengths are integer sample multiples, so the population pins the
    pitch far tighter than the IOR-derived estimate — which drifts ~0.3
    permil, i.e. whole samples at 50+ km, and whole samples move a fitted
    loss by millidecibels.
    """
    if not sampling_period or sampling_period <= 0:
        return None
    seed = 299_792_458.0 * float(sampling_period) / 2.0 / ior
    cands = []
    for r in records:
        L = r.get('Length')
        if isinstance(L, float) and 50.0 < L < 3000.0:
            n = round(L / seed)
            if n >= 10:
                cands.append(L / n)
    if len(cands) < 3:
        return seed
    cands.sort()
    return float(cands[len(cands) // 2])


# ═══════════════════════════════════════════════════════════════════════
#  Event normalization  (.bdr records -> the engine's event dicts)
# ═══════════════════════════════════════════════════════════════════════

def _is_finite(x) -> bool:
    return isinstance(x, float) and x == x and abs(x) != float('inf')


def _build_events(records: list[dict], ior: float) -> list[dict]:
    """Turn one direction's record list into the engine's event dicts.

    Field-for-field the same shape `_parse_key_events` produces from a
    Bellcore KeyEvents block, so nothing downstream needs a .bdr branch.
    """
    events = []
    # Section records carry the attenuation of the span that STARTS at their
    # own position, so the section preceding event i is the one recorded at
    # event i-1.  Stash it as we walk, and hand each event the slope of the
    # fiber leading INTO it — the convention parse_otdr_json uses
    # (PreviousFiberSection) and the engine reads.
    pending_slope = 0.0
    number = 0
    for r in records:
        if r.get('_is_section') or 'CurveLevel' not in r:
            L, ln = r.get('Loss'), r.get('Length')
            if _is_finite(L) and _is_finite(ln) and ln > 0:
                pending_slope = L / ln * 1000.0
            continue

        pos_m = r.get('Position')
        if not isinstance(pos_m, float):
            continue
        number += 1

        status = r.get('Status')
        # FR marks the end-of-fiber record with the high bit of Status
        # (128 / 132 observed; the launch connector is 64 / 72).  Proven on
        # the SEANOR set, where KeyEvents independently flags the same
        # records is_end.
        is_end = bool(int(status) & 0x80) if isinstance(status, (int, float)) else False
        refl = r.get('Reflectance')
        # A reflective event is one FR measured a reflectance for.  NaN is
        # how it records "non-reflective", so finiteness is the test — not
        # presence of the key.
        is_reflective = _is_finite(refl)

        loss = r.get('Loss')
        type_str = (('1' if is_reflective else '0')
                    + ('E' if is_end else 'F') + '9999LS')

        def _tot(metres):
            if not isinstance(metres, float):
                return 0
            return int(round(metres * ior / _TOT_C))

        events.append({
            'number':         number,
            'time_of_travel': _tot(pos_m),
            'dist_km':        round(pos_m / 1000.0, 4),
            'splice_loss':    float(loss) if _is_finite(loss) else 0.0,
            'reflection':     float(refl) if is_reflective else 0.0,
            'slope':          pending_slope,
            'type':           type_str,
            'is_reflective':  is_reflective,
            'is_end':         is_end,
            # FR's own LSA cursors, in the same time-of-travel units the .sor
            # KeyEvents markers use.  These ARE the fit windows FastReporter
            # used; carrying them means the engine's FR-exact path needs no
            # window prediction on a .bdr at all.
            'tot_end_prev':   _tot(r.get('SubCursorAPosition')),
            'tot_start_curr': _tot(r.get('CursorAPosition')),
            'tot_end_curr':   _tot(r.get('CursorBPosition')),
            'tot_start_next': _tot(r.get('SubCursorBPosition')),
            'tot_peak_curr':  _tot(r.get('CursorAPosition')),
            # Provenance: this loss came from FR's float64 record, not from a
            # 1 mdB-quantized KeyEvents int16.  Downstream tie-breaks care.
            'loss_full_precision': _is_finite(loss),
            '_bdr_record': r,
        })
        pending_slope = 0.0
    return events


def _date_epoch(date_str: Optional[str]) -> int:
    """'2026-09-08T22:40:00' -> unix epoch.  0 when absent or unparseable —
    the acquisition audit renders 0 as blank rather than inventing a date."""
    if not date_str:
        return 0
    import calendar
    import datetime as _dt
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d'):
        try:
            return calendar.timegm(_dt.datetime.strptime(date_str, fmt).timetuple())
        except ValueError:
            continue
    return 0


# ═══════════════════════════════════════════════════════════════════════
#  Public API
# ═══════════════════════════════════════════════════════════════════════

def parse_bdr(filepath: str) -> dict:
    """Unpack one .bdr into both directions.

    Returns {'a': <dict>, 'b': <dict>, 'merged': [records]} where each
    direction dict matches what `sor_reader324802a.parse_sor_full(trim=False)`
    returns for a .sor of that direction.  `merged` is FR's own bidirectional
    table, carried through untouched for comparison work — the engine does
    not read it and must not: the whole point of the report is OUR
    bidirectional arithmetic, not FR's.

    Raises ValueError when the file does not hold two traces and two
    bindable event lists — a .bdr that cannot supply both directions is not
    something to half-load, because a half-loaded fiber reads downstream as
    a dead direction.
    """
    stream = _inflate(filepath)
    if not stream:
        raise ValueError(f"{os.path.basename(filepath)}: no readable "
                         f"chunk stream (not a FastReporter .bdr?)")

    fields = _decode_fields(stream)
    traces = _raw_traces(stream)
    if len(traces) < 2:
        raise ValueError(f"{os.path.basename(filepath)}: found "
                         f"{len(traces)} trace(s), need 2 (A and B)")

    injections = [v for v in _scalars(fields, 'InjectionLevel')][:2]
    if len(injections) < 2:
        raise ValueError(f"{os.path.basename(filepath)}: only "
                         f"{len(injections)} InjectionLevel(s). Cannot bind "
                         f"event lists to traces")

    blocks = _record_blocks(fields)
    for b in blocks:
        _tag_sections(b)
    own = _own_lists(blocks, injections)
    if len(own) != 2:
        raise ValueError(f"{os.path.basename(filepath)}: matched {len(own)} "
                         f"event list(s) to traces, need 2")

    # Sanity: the two lists must describe ONE span measured from both ends.
    # They never agree exactly — each direction measures the other's end
    # connector through its own glass and its own IOR — and the disagreement
    # scales with length: 1 m on a 55 km ORPVL span, 51-61 m on a 110 km
    # SEANOR span (0.05%).  So the tolerance is RELATIVE, with an absolute
    # floor for short spans.  A genuinely mis-paired file differs by
    # kilometres, not by a fraction of a percent.
    end_a = own[0][-1].get('Position') or 0.0
    end_b = own[1][-1].get('Position') or 0.0
    tol = max(_SPAN_END_TOL_MIN_M,
              _SPAN_END_TOL_FRACTION * (end_a + end_b) / 2.0)
    # A receive reel on ONE end only: that direction's list carries the reel
    # end one reel-length past the other's last event, and its own second-
    # to-last event is the same connector the other ends on.  Tooele<->Knolls
    # Span 2 fibers 241-432: the Knolls shots end at the Tooele connector
    # (78.34 km, no receive reel), the Tooele shots see the Knolls reel too
    # (79.39 km, connector at 78.38).  FastReporter pairs these; a file
    # mis-paired across spans matches neither way.
    def _pen(lst):
        pos = [r.get('Position') for r in lst if r.get('Position') is not None]
        return pos[-2] if len(pos) >= 2 else None
    pen_a, pen_b = _pen(own[0]), _pen(own[1])
    one_reel = ((pen_b is not None and abs(end_a - pen_b) <= tol)
                or (pen_a is not None and abs(end_b - pen_a) <= tol))
    if abs(end_a - end_b) > tol and not one_reel:
        raise ValueError(
            f"{os.path.basename(filepath)}: the two event lists end "
            f"{abs(end_a - end_b):.0f} m apart ({end_a:.0f} / {end_b:.0f}, "
            f"tolerance {tol:.0f} m). They cannot be two directions of "
            f"one span")

    merged = [r for b in blocks for r in b if r.get('_merged')]

    # FR's per-direction legs behind each merged mean (see
    # `_merged_sub_records`).  Attached by position, so a row whose legs are
    # missing simply carries none rather than borrowing its neighbour's.
    legs = _merged_sub_records(stream, fields)
    for r in merged:
        pair = legs.get(r.get('_off'))
        if pair:
            r['_ab'] = pair.get('EventAB')
            r['_ba'] = pair.get('EventBA')

    # Shared fiber identity — one fiber, so these are not per-direction.
    identifier = _first(fields, 'Identifier', '') or ''
    cable = _first(fields, 'Cable', '') or ''
    loc_a = _first(fields, 'LocationA', '') or ''
    loc_b = _first(fields, 'LocationB', '') or ''

    sides = {}
    for i, key in enumerate(('a', 'b')):
        recs = own[i]
        raw = traces[i]
        # Per-direction scalars appear once per trace, in trace order.
        ior = _nth(fields, 'Ior', i) or _IOR_FALLBACK
        sampling = _nth(fields, 'SamplingPeriod', i)
        res_m = _exact_pitch(recs, sampling, ior)
        wl_m = _nth(fields, 'Wavelength', i)
        pulse_s = _nth(fields, 'Pulse', i)
        acq_range_m = _nth(fields, 'Range', i) or 0.0

        trace = 64.0 - raw.astype(np.float64) / 1024.0
        events = _build_events(recs, ior)

        # acq_range in the Bellcore sense: time-of-travel of the acquisition
        # window, which is what the engine's sample-spacing arithmetic
        # expects.  Derived from the file's own range so a 80 km / 140 km
        # acquisition is described correctly.
        acq_range_tot = int(round(acq_range_m * ior / _TOT_C))

        result = {
            'filename':  os.path.basename(filepath),
            'filepath':  filepath,
            # Identity.  LocationA/LocationB are stored once, oriented A->B;
            # the B direction is shot the other way, so its originating and
            # terminating locations are the swap — exactly what a real B .sor
            # carries in GenParams, which is what the direction detector and
            # the span-name reader read.
            'gen_cable_id': cable,
            'gen_fiber_id': identifier,
            'gen_loc_a':    loc_a if key == 'a' else loc_b,
            'gen_loc_b':    loc_b if key == 'a' else loc_a,

            'trace':       trace,
            'num_points':  len(trace),
            'full_points': len(trace),
            'start_index': 0,
            'end_index':   len(trace) - 1,
            'min_db':  float(trace.min()),
            'max_db':  float(trace.max()),
            'mean_db': float(trace.mean()),

            'wavelength': (wl_m * 1e9) if wl_m else None,
            'acq_range':  acq_range_tot,
            'events':     events,
            'date_time':  _date_epoch(_nth(fields, 'Date', i + 1)),
            'duration_sec': _nth(fields, 'Duration', i),

            'otdr_model':  _nth(fields, 'ModelName', i, '') or '',
            'otdr_serial': _nth(fields, 'SerialNumber', i, '') or '',
            'sup_params': {
                'sup_supplier':     'EXFO',
                'sup_mainframe_id': _nth(fields, 'ModelName', i, '') or '',
                'sup_mainframe_sn': _nth(fields, 'SerialNumber', i, '') or '',
                'sup_module_id':    _nth(fields, 'ModelName', i, '') or '',
                'sup_module_sn':    _nth(fields, 'SerialNumber', i, '') or '',
                'sup_software_rev': _nth(fields, 'InstrumentVersion', i, '') or '',
                'sup_other':        '',
                'otdr_model':       _nth(fields, 'ModelName', i, '') or '',
                'otdr_serial':      _nth(fields, 'SerialNumber', i, '') or '',
                'pulse_ns':         (pulse_s * 1e9) if pulse_s else None,
            },

            'backscatter_db': _nth(fields, 'Rbs', i),
            'fxd_pulse_ns':   (pulse_s * 1e9) if pulse_s else None,
            'ior':            ior,
            # GenParams glass designation.  A .bdr stores no fiber-type
            # field; leave it blank rather than assume G.652 — the Test
            # Settings table exists to VERIFY the two directions, and an
            # invented value there is worse than an empty cell.
            'fiber_type':     '',

            'test_settings': {
                k: v for k, v in (
                    ('Ior', ior),
                    ('Rbs', _nth(fields, 'Rbs', i)),
                    ('HelixFactor', _nth(fields, 'HelixFactor', i)),
                    ('SpliceLossThreshold', _nth(fields, 'SpliceLossThreshold', i)),
                    ('SplitterDetectionThreshold',
                     _nth(fields, 'SplitterDetectionThreshold', i)),
                    ('ReflectanceThreshold', _nth(fields, 'ReflectanceThreshold', i)),
                    ('EndOfFiberThreshold', _nth(fields, 'EndOfFiberThreshold', i)),
                    ('SplitterDetection', _nth(fields, 'SplitterDetection', i)),
                    ('FiberCode', _nth(fields, 'FiberCode', i)),
                ) if v is not None
            },

            'exfo_calibration': {
                k: v for k, v in (
                    ('SamplingPeriod', sampling),
                    ('InjectionLevel', injections[i]),
                    ('ScaleFactor', _nth(fields, 'ScaleFactor', i)),
                    ('SaturationLevel', _nth(fields, 'SaturationLevel', i)),
                    ('RmsNoise', _nth(fields, 'RmsNoise', i)),
                    ('NominalPulseWidth', _nth(fields, 'NominalPulseWidth', i)),
                    ('CalibratedPulseWidth', _nth(fields, 'CalibratedPulseWidth', i)),
                    ('NumberOfAverages', _nth(fields, 'NumberOfAverages', i)),
                    ('SpansLoss', _nth(fields, 'SpansLoss', i)),
                    ('SpansLength', _nth(fields, 'SpansLength', i)),
                ) if v is not None
            },
            'exfo_events':          _tag_sections(list(recs)),
            'exfo_raw':             raw,
            'exfo_res_m':           res_m,
            'exfo_spans_loss':      _nth(fields, 'SpansLoss', i),
            'exfo_spans_length':    _nth(fields, 'SpansLength', i),
            'exfo_total_orl':       _nth(fields, 'TotalOrl', i),
            'exfo_sampling_period': sampling,
            'exfo_wavelength_nm':   ((_nth(fields, 'ExactWavelength', i) or 0) * 1e9)
                                    or ((wl_m * 1e9) if wl_m else None),
            'exfo_injection_level': injections[i],
            'exfo_saturation_level': _nth(fields, 'SaturationLevel', i),

            # A .bdr event list is stored in the SAME frame as its trace:
            # sample 0 is Position 0, verified by fitting every event at
            # Position/pitch and reproducing FR's own losses exactly.  So
            # there is no declared-span-start shift to undo.
            'user_offset_km': 0.0,

            # .bdr-only extras.
            '_bdr_side':     key.upper(),
            '_bdr_merged':   merged,
            '_bdr_path':     filepath,
            # FastReporter's OWN synthesised value for every event this
            # direction never detected — see `_fr_synthetic_legs`.
            'fr_synthetic':  _fr_synthetic_legs(merged, key),
        }
        sides[key] = result

    return {'a': sides['a'], 'b': sides['b'], 'merged': merged}


def _fr_synthetic_legs(merged: list[dict], side: str) -> list[dict]:
    """FastReporter's own value for each event THIS direction never saw.

    Where one direction detected an event and the other did not, FR still
    needs two numbers to average, so it synthesises one for the silent side
    and stores it in that row's leg.  We can reproduce that synthesis from the
    trace for 99.4% of records; the remainder are squeezed between the silent
    side's own neighbouring event and the projected position, and FR's stored
    figure there is not the 4-point of the cursors stored beside it — it comes
    out of FR's pairing, from state the file does not carry.  Verified by
    driving FR's own Markers tab: at those cursors it returns OUR value, not
    its stored one.

    So when the input IS a .bdr, FR's answer is already in the file and there
    is nothing to re-derive.  Positions are in this direction's own raw frame,
    the same frame `measure_fr_exact_loss` takes.
    """
    want = 'EventAB' if str(side).lower() == 'a' else 'EventBA'
    key = '_ab' if want == 'EventAB' else '_ba'
    out = []
    for r in merged:
        if 'Type' not in r:                      # section row, not an event
            continue
        leg = r.get(key)
        if not leg:
            continue
        # FR marks the side it never detected with CurveLevel NaN.
        cl = leg.get('CurveLevel')
        if not (cl is None or (isinstance(cl, float) and cl != cl)):
            continue
        loss = leg.get('Loss')
        pos = leg.get('CursorAPosition')
        if not isinstance(loss, float) or loss != loss:
            continue
        if not isinstance(pos, float):
            continue
        out.append({
            'position_m': pos,
            'loss': float(loss),
            'cursors_m': (leg.get('SubCursorAPosition'), pos,
                          leg.get('CursorBPosition'),
                          leg.get('SubCursorBPosition')),
        })
    return out


def parse_bdr_side(filepath: str, side: str) -> dict:
    """One direction of a .bdr.  `side` is 'a' or 'b'."""
    s = str(side).lower()
    if s not in ('a', 'b'):
        raise ValueError(f"side must be 'a' or 'b', got {side!r}")
    return parse_bdr(filepath)[s]

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
