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
    return {
        'date_time': date_time, 'units': units,
        'wavelength': wavelength / 10.0, 'acq_range': acq_range,
    }


def _read_ior(data):
    """Read the group index (IOR) from the SOR file. Stored as uint32 * 100000."""
    # The IOR is typically near offset 305 in EXFO files, stored as e.g. 147000 = 1.47000
    # Search for a plausible IOR value (1.45000 to 1.49000)
    for off in range(0, min(len(data), 1000)):
        try:
            val = struct.unpack_from('<I', data, off)[0]
            if 145000 <= val <= 149000:
                return val / 100000.0
        except struct.error:
            pass
    return 1.46820  # fallback


def _parse_key_events(data, blocks):
    if 'KeyEvents' not in blocks:
        return []
    body = blocks['KeyEvents']['body']
    num_events = struct.unpack_from('<H', data, body)[0]
    pos = body + 2
    IOR = _read_ior(data)
    events = []
    for _ in range(num_events):
        evnum      = struct.unpack_from('<H', data, pos)[0];      pos += 2
        tot        = struct.unpack_from('<I', data, pos)[0];      pos += 4
        slope      = struct.unpack_from('<h', data, pos)[0];      pos += 2
        splice     = struct.unpack_from('<h', data, pos)[0];      pos += 2
        refl       = struct.unpack_from('<i', data, pos)[0];      pos += 4
        evt_raw    = data[pos:pos + 8];                           pos += 8
        pos += 20  # end_prev, start_curr, end_curr, start_next, peak_curr
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
            'is_reflective': evt_type[:1] == '1',
            'is_end':        evt_type[1:2] == 'E',
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
                dec = zlib.decompress(chunk)
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

    # ── Parse EventTable entries ──
    exfo_events = []
    et_idx = stream.find(b'EventTable\x00')
    if et_idx >= 0:
        current = None
        is_section = False

        def _flush(current, is_section, exfo_events):
            if current and len(current) > 2:
                current['_is_section'] = is_section
                exfo_events.append(current)

        pos = et_idx
        search_end = min(len(stream) - 1, et_idx + 80000)

        while pos < search_end:
            end = stream.find(b'\x00', pos)
            if end < 0:
                break
            length = end - pos
            if length < 2 or length >= 80:
                pos = end + 1
                continue
            try:
                name = stream[pos:end].decode('ascii')
            except Exception:
                pos = end + 1
                continue
            if not (name.isprintable() and name[0].isalpha()):
                pos = end + 1
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

            if name == 'Position' and value is not None:
                _flush(current, is_section, exfo_events)
                current = {'Position': value}
                is_section = False
            elif current is not None:
                if name == 'Type' and value is not None:
                    current['Type'] = value
                elif name == 'Loss' and value is not None:
                    current['Loss'] = value
                    if 'Type' not in current:
                        is_section = True
                elif name in ('CurveLevel', 'Reflectance', 'PeakReflectionToRbs',
                               'LocalNoise', 'Length', 'Status',
                               'CursorAPosition', 'CursorBPosition',
                               'SubCursorAPosition', 'SubCursorBPosition') and value is not None:
                    current[name] = value

            pos = end + 1

        _flush(current, is_section, exfo_events)

    # Keep only events with plausible positions (0–500 km)
    exfo_events = [e for e in exfo_events
                   if isinstance(e.get('Position'), float) and -1 <= e['Position'] <= 500]

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


def _read_cstr(raw, offset):
    end = raw.find(b'\x00', offset)
    if end < 0:
        return '', offset
    try:
        s = raw[offset:end].decode('latin-1', errors='replace').strip()
    except Exception:
        s = ''
    return s, end + 1


def parse_sup_params(filepath):
    """Read the SOR SupParams block (supplier + OTDR mainframe/module IDs).

    Returns a dict with whatever could be read (empty strings otherwise):
    supplier_name, otdr_mainframe_id, otdr_mainframe_sn, otdr_module_id,
    otdr_module_sn, software_rev, otdr_other.
    """
    with open(filepath, 'rb') as f:
        raw = f.read()
    marker = b'SupParams\x00'
    i = raw.find(marker, 50)
    if i < 0:
        return {}
    p = i + len(marker)
    out = {}
    for key in ('supplier_name', 'otdr_mainframe_id', 'otdr_mainframe_sn',
                'otdr_module_id', 'otdr_module_sn', 'software_rev',
                'otdr_other'):
        s, p = _read_cstr(raw, p)
        out[key] = s
    return out


def _otdr_serial_from_sup(sup):
    """Pick the most useful OTDR serial. Prefers mainframe SN, falls back to module SN."""
    for key in ('otdr_mainframe_sn', 'otdr_module_sn'):
        v = (sup.get(key) or '').strip()
        if v:
            return v
    return ''


def parse_gen_params(filepath):
    """Pull route-level metadata from the SOR GenParams block.

    Returns a dict with whatever could be read (empty strings otherwise):
    cable_id, fiber_id, fiber_type_code, wavelength_code, location_a,
    location_b, cable_code, build_condition, operator, comment,
    serial_number (from SupParams).
    """
    with open(filepath, 'rb') as f:
        raw = f.read()
    marker = b'GenParams\x00'
    i = raw.find(marker, 50)
    if i < 0:
        return {}
    p = i + len(marker)
    # Telcordia SR-4731: language code (2 bytes) then strings in order.
    p += 2
    out = {}
    for key in ('cable_id', 'fiber_id', 'fiber_type_code',
                'wavelength_code', 'location_a', 'location_b',
                'cable_code', 'build_condition'):
        if key in ('fiber_type_code', 'wavelength_code'):
            if p + 2 <= len(raw):
                out[key] = struct.unpack_from('<H', raw, p)[0]
                p += 2
            continue
        s, p = _read_cstr(raw, p)
        out[key] = s
    # two 4-byte offsets (user offset + distance)
    p += 8
    for key in ('operator', 'comment'):
        s, p = _read_cstr(raw, p)
        out[key] = s
    out['serial_number'] = _otdr_serial_from_sup(parse_sup_params(filepath))
    return out


def direction_key_from_genparams(filepath):
    """Build a stable grouping key from a SOR file's internal metadata.

    The key is a compound of every present signal so that pairs that share
    a label end up in the same group regardless of OTDR identity, so two
    shots of the same fiber from different OTDR units can be compared
    against each other:

      location_a → location_b   (when both populated)

    OTDR serial number and operator are intentionally NOT in the key —
    different OTDRs / crews shooting the same direction should land in the
    same report and be paired. Forward vs reverse separation comes from
    the location pair; when those are empty the caller falls back to the
    filename prefix (which usually encodes direction).

    Returns '' when the file carries no location info — caller should
    fall back to the filename in that case.
    """
    gp = parse_gen_params(filepath)
    a = (gp.get('location_a') or '').strip()
    b = (gp.get('location_b') or '').strip()
    if a or b:
        return f'{a}_to_{b}'
    return ''


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
